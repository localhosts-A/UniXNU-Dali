"""Offline scratch grant generation binds live geometry and pinned XNU inputs."""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ufs_scratch_authorization as auth
import ufs_identity


def kernel_image():
    image = bytearray(8192)
    image[:8] = b"ANDROID!"
    struct.pack_into("<II", image, 8, 4096, 0)
    struct.pack_into("<I", image, 20, 1584)
    struct.pack_into("<I", image, 40, 4)
    image[4096:4100] = bytes.fromhex("02214c18")
    return bytes(image)


def source_manifest(kernel):
    return {"device_commit": "1" * 40, "unixnu_commit": "2" * 40,
            "uefi_commit": "3" * 40, "xnu_commit": "4" * 40,
            "kernel_sha256": hashlib.sha256(kernel).hexdigest()}


class ScratchAuthorizationTests(unittest.TestCase):
    def fixture(self, directory):
        partition_bytes = 64 * 1024 * 1024
        original = bytearray(partition_bytes)
        footer = memoryview(original)[-64:]
        footer[:4] = b"AVBf"
        struct.pack_into(">IIQQQ", footer, 4, 1, 0, 8 * 1024 * 1024, 61 * 1024 * 1024, 512 * 1024)
        path = Path(directory) / "boot_b.img"
        path.write_bytes(original)
        kernel = kernel_image()
        geo = {"lun": 2, "logical_block_bytes": 4096, "capacity_blocks": 64577536,
               "capacity_bytes": 64577536 * 4096,
               "geometry_verified": True, "write_protection_observed_android": True,
               "sysfs_read_only": False, "gpt_read_only": False, "unit_write_protected": False,
               "unit_descriptor": {"lu_enable": 1, "lun_write_protect": 0,
                                   "logical_block_size": 12, "logical_block_count": 64577536},
               "boot_b": {"name": "boot_b", "first_lba": 418560, "last_lba": 434943, "blocks": 16384,
                          "bytes": partition_bytes, "guid": "00000046-39c2-4488-9bb0-00cb43c9ccd4"}}
        baseline = {"kind": "dali-ufs-android-read-only-baseline", "serial": "DALI123",
                    "original_boot_b_sha256": hashlib.sha256(original).hexdigest(),
                    "all_partition_hashes_match": True, "known_block_matches_backup": True,
                    "device_writes_performed": False, "device_reboot_performed": False,
                    "gpt": {"partitions": [dict(geo["boot_b"])]}, "geometry": geo}
        controller = "/sys/devices/platform/soc/16810000.ufshci"
        identity, digest, raw = ufs_identity.read_identity(
            controller, lambda command: b"0x1234\n" if "manufacturer_id" in command else b"ABC123\n")
        baseline.update(ufs_device_binding_sha256=digest, ufs_device_identity=identity,
                        scsi_path=controller+"/host0/target0:0:0/0:0:0:2", captures={})
        for field, (_, filename, _) in ufs_identity.ATTRIBUTES.items():
            (Path(directory)/filename).write_bytes(raw[field])
            baseline["captures"]["ufs_"+field] = dict(file=filename, bytes=len(raw[field]),
                                                      sha256=hashlib.sha256(raw[field]).hexdigest())
        return path, bytes(original), baseline, kernel

    def test_grant_is_bound_to_actual_kernel_source_device_and_absolute_scratch(self):
        with tempfile.TemporaryDirectory() as temp:
            original_path, _, baseline, candidate = self.fixture(temp)
            kernel = b"verified source-only XNU kernel artifact"
            grant, wire, proposal = auth.build_xnu_scratch_grant(
                serial="DALI123", token="0123456789abcdef0123456789abcdef", sequence=1, baseline=baseline, baseline_dir=temp,
                source_manifest=source_manifest(kernel), kernel_bytes=kernel,
                candidate_image=candidate, original_boot_b=original_path)
        self.assertEqual(len(wire), 232)
        self.assertEqual((grant["lun"], grant["block_bytes"], grant["scratch_start_lba"],
                          grant["scratch_blocks"], grant["tail_guard_blocks"]), (2, 4096, 434432, 256, 256))
        self.assertEqual(grant["candidate_sha256"], hashlib.sha256(kernel).hexdigest())
        self.assertEqual(proposal["candidate_sha256"], hashlib.sha256(candidate).hexdigest())
        self.assertTrue(proposal["original_avb"]["scratch_disjoint"])

    def test_source_device_token_geometry_and_original_avb_mismatches_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            original_path, _, baseline, _ = self.fixture(temp)
            candidate = kernel_image()
            kernel = b"verified source-only XNU kernel artifact"
            args = dict(serial="DALI123", token="0123456789abcdef0123456789abcdef", baseline=baseline,
                        sequence=1, baseline_dir=temp, source_manifest=source_manifest(kernel), kernel_bytes=kernel,
                        candidate_image=candidate, original_boot_b=original_path)
            bad = [dict(args, serial="OTHER"), dict(args, token="bad"),
                   dict(args, baseline=dict(baseline, ufs_device_identity=None)),
                   dict(args, baseline_dir=None),
                   dict(args, baseline=dict(baseline, ufs_device_binding_sha256="00" * 32)),
                   dict(args, baseline=dict(baseline, geometry=dict(baseline["geometry"], capacity_blocks=1))),
                   dict(args, baseline=dict(baseline, geometry=dict(baseline["geometry"], unit_write_protected=True))),
                   dict(args, kernel_bytes=kernel + b"changed"),
                   dict(args, source_manifest=dict(source_manifest(kernel), device_commit="bad")),
                   dict(args, original_boot_b=Path(temp) / "missing.img")]
            for value in bad:
                with self.subTest(value=value.keys()), self.assertRaises((OSError, ValueError)):
                    auth.build_xnu_scratch_grant(**value)


if __name__ == "__main__":
    unittest.main()
