import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


DEVICE = Path(__file__).resolve().parents[1]
SCRIPT = DEVICE / "scripts/build-dali-boot-stage.py"
MIB = 1 << 20

spec = importlib.util.spec_from_file_location("build_dali_boot_stage", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def make_root(path, root_bytes=3 * MIB + 4096):
    windows = [("OS", MIB, b"O" * 4096), ("SystemOS", 2 * MIB, b"S" * 4096)]
    image = bytearray(root_bytes)
    entries = []
    for name, offset, content in windows:
        image[offset:offset + len(content)] = content
        entries.append((name, offset, len(content), hashlib.sha256(content).digest()))
    header = bytearray(4096)
    header[:8] = b"SHNGROOT"
    struct.pack_into("<IIQ", header, 8, 1, len(entries), root_bytes)
    for index, (name, offset, size, digest) in enumerate(entries):
        struct.pack_into("<32sQQII32s40s", header, 24 + index * 128,
                         name.encode(), offset, size, 512, 1, digest, bytes(40))
    header[4064:4096] = hashlib.sha256(header[:4064]).digest()
    image[:4096] = header
    path.write_bytes(image)
    capacity = (15_403_518 - 7_023_103 + 1) * 4096
    report = {
        "image_bytes": root_bytes,
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "partition_name": "unixnu_data",
        "lun": 2,
        "partition_bytes": capacity,
        "userdata_bytes": capacity,
        "source": "local",
        "windows": [{"name": name, "offset": offset, "size": size,
                     "sha256": digest.hex()} for name, offset, size, digest in entries],
    }
    return report


def make_fat(path, sectors=2048, sector_size=512):
    image = bytearray(sectors * sector_size)
    image[0:3] = b"\xeb\x3c\x90"
    struct.pack_into("<H", image, 11, sector_size)
    if sectors <= 0xFFFF:
        struct.pack_into("<H", image, 19, sectors)
    else:
        struct.pack_into("<I", image, 32, sectors)
    image[510:512] = b"\x55\xaa"
    path.write_bytes(image)


class BootStageImageTests(unittest.TestCase):
    def test_composite_keeps_two_window_root_and_pins_trailing_boot_fat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_image, root_report = root / "root.img", root / "root.json"
            boot_fat = root / "boot-fat.img"
            boot_report = root / "boot-fat.json"
            output, descriptor, report = root / "partition.img", root / "boot.desc", root / "composite.json"
            root_data = make_root(root_image)
            root_report.write_text(json.dumps(root_data))
            make_fat(boot_fat)
            boot_digest = hashlib.sha256(boot_fat.read_bytes()).hexdigest()
            boot_report.write_text(json.dumps({
                "bytes": boot_fat.stat().st_size, "sha256": boot_digest,
                "readonly": True, "physical_storage": False,
                "files": {
                    "EFI/UniXNU/unixnu.toml": {"bytes": 20, "sha256": "33" * 32},
                    "EFI/UniXNU/dali-boot.conf": {"bytes": 20, "sha256": "44" * 32},
                    "EFI/UniXNU/Payloads/kernel.macho": {"bytes": 100, "sha256": "11" * 32},
                    "EFI/UniXNU/Payloads/kernel.macho.adt": {"bytes": 100, "sha256": "22" * 32},
                },
                "xnu_kernel_sha256": "11" * 32, "xnu_adt_sha256": "22" * 32,
                "xnu_trust_cache_sha256": None, "xnu_ramdisk_sha256": None,
                "recovery": {"auto_fastboot": False},
            }))

            result = builder.compose(root_image, root_report, boot_fat, boot_report,
                                     output, descriptor, report)

            expected_offset = 4 * MIB
            self.assertEqual(result["root_windows"], ["OS", "SystemOS"])
            self.assertEqual(result["root_image_bytes"], 3 * MIB + 4096)
            self.assertEqual(result["boot_stage_offset_bytes"], expected_offset)
            self.assertEqual(result["boot_stage_sha256"], hashlib.sha256(boot_fat.read_bytes()).hexdigest())
            self.assertEqual(output.stat().st_size, expected_offset + boot_fat.stat().st_size)
            with output.open("rb") as stream:
                stream.seek(result["root_image_bytes"])
                self.assertEqual(stream.read(expected_offset - result["root_image_bytes"]),
                                 bytes(expected_offset - result["root_image_bytes"]))
                stream.seek(expected_offset)
                self.assertEqual(stream.read(), boot_fat.read_bytes())
            encoded = descriptor.read_bytes()
            self.assertEqual(len(encoded), builder.DESCRIPTOR_BYTES)
            prefix, digest = encoded[:-32], encoded[-32:]
            self.assertEqual(hashlib.sha256(prefix).digest(), digest)
            fields = builder.DESCRIPTOR_PREFIX.unpack(prefix)
            self.assertEqual(fields[:11], (b"DALIBOOT", 1, 168, result["root_image_bytes"],
                                          expected_offset, boot_fat.stat().st_size,
                                          result["partition_bytes"], 7_023_103, 15_403_518,
                                          2, 4096))
            self.assertEqual(fields[11].hex(), result["root_manifest_sha256"])
            self.assertEqual(fields[12].hex(), result["boot_stage_sha256"])
            self.assertEqual(json.loads(report.read_text()), result)

    def test_boot_fat_rejects_bad_sector_geometry_and_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_sector = root / "bad-sector.img"
            make_fat(bad_sector, sector_size=4096)
            with self.assertRaisesRegex(ValueError, "512-byte-sector"):
                builder.validate_fat_image(bad_sector)
            bad_signature = root / "bad-signature.img"
            make_fat(bad_signature)
            data = bytearray(bad_signature.read_bytes())
            data[510:512] = bytes(2)
            bad_signature.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "boot sector"):
                builder.validate_fat_image(bad_signature)

    def test_composite_refuses_tampered_root_or_boot_fat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_image, root_report = root / "root.img", root / "root.json"
            boot_fat, boot_report = root / "boot-fat.img", root / "boot-fat.json"
            root_data = make_root(root_image)
            root_report.write_text(json.dumps(root_data))
            make_fat(boot_fat)
            def write_boot_report():
                boot_digest = hashlib.sha256(boot_fat.read_bytes()).hexdigest()
                boot_report.write_text(json.dumps({
                    "bytes": boot_fat.stat().st_size, "sha256": boot_digest,
                    "readonly": True, "physical_storage": False,
                    "files": {
                        "EFI/UniXNU/unixnu.toml": {"bytes": 20, "sha256": "33" * 32},
                        "EFI/UniXNU/dali-boot.conf": {"bytes": 20, "sha256": "44" * 32},
                        "EFI/UniXNU/Payloads/kernel.macho": {"bytes": 100, "sha256": "11" * 32},
                        "EFI/UniXNU/Payloads/kernel.macho.adt": {"bytes": 100, "sha256": "22" * 32},
                    },
                    "xnu_kernel_sha256": "11" * 32, "xnu_adt_sha256": "22" * 32,
                    "xnu_trust_cache_sha256": None, "xnu_ramdisk_sha256": None,
                    "recovery": {"auto_fastboot": False},
                }))
            write_boot_report()
            output, descriptor, report = root / "partition.img", root / "boot.desc", root / "composite.json"

            root_report.write_text(json.dumps({**root_data, "windows": root_data["windows"] + [
                {"name": "Boot", "offset": 3 * MIB, "size": 1, "sha256": "00" * 32}]}))
            with self.assertRaisesRegex(Exception, "exactly OS then SystemOS"):
                builder.compose(root_image, root_report, boot_fat, boot_report,
                                output, descriptor, report)

            root_report.write_text(json.dumps(root_data))
            original_digest = builder.file_digest

            def mutate_after_hash(path):
                result = original_digest(path)
                if Path(path) == boot_fat:
                    changed = bytearray(boot_fat.read_bytes())
                    changed[1000] = 1
                    boot_fat.write_bytes(changed)
                return result

            with patch.object(builder, "file_digest", side_effect=mutate_after_hash), \
                    self.assertRaisesRegex(ValueError, "differ from the pinned source image"):
                builder.compose(root_image, root_report, boot_fat, boot_report,
                                output, descriptor, report)


if __name__ == "__main__":
    unittest.main()
