import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch


DEVICE = Path(__file__).resolve().parents[1]
UNIXNU = DEVICE.parent / "UniXNU"
SCRIPT = DEVICE / "scripts/build-dali-verified-root.py"
MIB = 1 << 20

spec = importlib.util.spec_from_file_location("build_dali_verified_root", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def make_container(path):
    total = 3 * MIB
    windows = [("OS", MIB, b"O" * 4096), ("SystemOS", 2 * MIB, b"S" * 4096)]
    image = bytearray(total)
    entries = []
    for name, offset, content in windows:
        image[offset:offset + len(content)] = content
        entries.append((name, offset, len(content), hashlib.sha256(content).digest()))
    header = bytearray(4096)
    header[:8] = b"SHNGROOT"
    struct.pack_into("<IIQ", header, 8, 1, len(entries), total)
    for index, (name, offset, size, digest) in enumerate(entries):
        struct.pack_into("<32sQQII32s40s", header, 24 + index * 128,
                         name.encode(), offset, size, 512, 1, digest, bytes(40))
    header[4064:4096] = hashlib.sha256(header[:4064]).digest()
    image[:4096] = header
    path.write_bytes(image)
    report = {
        "image_bytes": total,
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "partition_name": "unixnu_data",
        "lun": 2,
        "partition_bytes": (15_403_518 - 7_023_103 + 1) * 4096,
        "userdata_bytes": (15_403_518 - 7_023_103 + 1) * 4096,
        "source": "local",
        "windows": [{"name": name, "offset": offset, "size": len(content),
                     "sha256": hashlib.sha256(content).hexdigest()}
                    for name, offset, content in windows],
    }
    return report, entries


class DaliVerifiedRootPolicyTests(unittest.TestCase):
    def test_compile_record_is_read_from_the_build_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree"
            source = Path(temporary) / "verified_root.c"
            output = Path(temporary) / "verified_root.o"
            with patch.object(builder, "SCRIPT_ROOT", tree):
                with self.assertRaisesRegex(SystemExit, str(tree / "build/xnu/ios-obj")):
                    builder.compile_xnu_object(UNIXNU, source, output)

    def test_verified_root_compile_matches_non_werror_xnu_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "canonical"
            canonical.mkdir()
            alias = root / "alias"
            alias.symlink_to(canonical, target_is_directory=True)
            tree = alias / "tree"
            source = alias / "verified_root.c"
            output = root / "verified_root.o"
            source.write_text("int verified_root_fixture;\n")
            record_path = tree / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/bsd/DEVELOPMENT/bsd_init.o.json"
            record_path.parent.mkdir(parents=True)
            record_path.write_text(json.dumps({
                "directory": str(alias),
                "file": str(source),
                "arguments": ["clang", "-c", str(source), "-o", "bsd_init.o"],
            }))
            captured = {}

            def compile_fixture(command, **kwargs):
                captured["command"] = command
                output.write_bytes(struct.pack("<4I", 0xfeedfacf, 0x100000c, 0, 1))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch.object(builder, "SCRIPT_ROOT", tree), \
                    patch.object(builder.subprocess, "run", side_effect=compile_fixture):
                builder.compile_xnu_object(UNIXNU, source, output)

            self.assertNotIn("-Werror", captured["command"])
            self.assertIn("-I" + str(UNIXNU / "kits/VerifiedRoot/include"), captured["command"])

    def test_policy_manifests_bind_both_verified_windows_and_share_one_owner_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image, report_path = root / "userdata-root.img", root / "userdata-root.json"
            report, _ = make_container(image)
            report_path.write_text(json.dumps(report))
            profile = builder.profile_constants()
            verify_built_image, make = builder.import_userdata(UNIXNU)
            verified, entries, image_digest = builder.load_verified_container(
                image, report_path, verify_built_image, profile)
            self.assertEqual(image_digest, report["image_sha256"])
            self.assertEqual([entry[0] for entry in entries], ["OS", "SystemOS"])
            self.assertEqual(verified["partition_name"], "unixnu_data")

            private_key, public_key = root / "owner.pem", root / "owner-public.pem"
            subprocess.run(["openssl", "genrsa", "-out", private_key, "3072"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "rsa", "-in", private_key, "-pubout", "-out", public_key], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            output = root / "policy-output"
            records, anchor = builder.build_policies(image, entries, private_key, public_key,
                                                     output, make, profile)
            self.assertEqual([record["name"] for record in records], ["OS", "SystemOS"])
            self.assertEqual([record["volume_uuid"] for record in records],
                             [profile["OS"].upper(), profile["SystemOS"].upper()])
            self.assertTrue(all(record["generation"] == 1 for record in records))
            self.assertTrue(all((output / "policies" / record["name"] / "manifest.json").is_file()
                                for record in records))
            self.assertEqual(records[0]["trusted_key_sha256"], anchor)
            self.assertEqual(records[1]["trusted_key_sha256"], anchor)
            self.assertFalse((output / "owner.pem").exists())

    def test_policy_builder_rejects_tampered_window_and_wrong_partition_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image, report_path = root / "userdata-root.img", root / "userdata-root.json"
            report, _ = make_container(image)
            profile = builder.profile_constants()
            verify_built_image, _ = builder.import_userdata(UNIXNU)

            changed_digest = json.loads(json.dumps(report))
            changed_digest["windows"][0]["sha256"] = "00" * 32
            report_path.write_text(json.dumps(changed_digest))
            with self.assertRaisesRegex(Exception, "does not match the built window|digest"):
                builder.load_verified_container(image, report_path, verify_built_image, profile)

            wrong_partition = json.loads(json.dumps(report))
            wrong_partition["partition_name"] = "userdata"
            report_path.write_text(json.dumps(wrong_partition))
            with self.assertRaisesRegex(Exception, "partition/LUN profile"):
                builder.load_verified_container(image, report_path, verify_built_image, profile)


if __name__ == "__main__":
    unittest.main()
