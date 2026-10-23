import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run-bootb-cycles.py"
SPEC = importlib.util.spec_from_file_location("run_bootb_cycles", SCRIPT)
cycles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cycles)


class BackupValidationTests(unittest.TestCase):
    def manifest(self):
        files = {}
        for name in cycles.PARTITIONS:
            files[name + ".img"] = {
                "bytes": 1,
                "sha256": cycles.sha256(b"x"),
                "source": "/dev/block/by-name/" + name,
            }
        files["runtime.dtb"] = {
            "bytes": 1,
            "sha256": cycles.sha256(b"d"),
            "source": "/sys/firmware/fdt",
        }
        return {"complete": True, "product": "dali", "slot_suffix": "_b",
                "serial": "test-device", "files": files}

    def test_complete_backup_and_partition_set_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest()
            for name in cycles.PARTITIONS:
                (root / (name + ".img")).write_bytes(b"x")
            (root / "runtime.dtb").write_bytes(b"d")
            (root / "manifest.json").write_text(json.dumps(manifest))
            result, records = cycles.verify_backup(root, "test-device")
            self.assertTrue(result["complete"])
            self.assertEqual(set(records), set(cycles.PARTITIONS))

    def test_missing_or_foreign_partition_fails_closed(self):
        manifest = self.manifest()
        del manifest["files"]["boot_b.img"]
        with self.assertRaisesRegex(cycles.CycleError, "missing partition"):
            cycles.partition_records(manifest)

        manifest = self.manifest()
        manifest["files"]["boot_b.img"]["source"] = "/dev/block/by-name/misc"
        with self.assertRaisesRegex(cycles.CycleError, "unexpected backed partition"):
            cycles.partition_records(manifest)

    def test_corrupt_original_boot_b_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.manifest()
            for name in cycles.PARTITIONS:
                (root / (name + ".img")).write_bytes(b"bad" if name == "boot_b" else b"x")
            (root / "runtime.dtb").write_bytes(b"d")
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(cycles.CycleError, "boot_b.img"):
                cycles.verify_backup(root, "test-device")

    def test_adb_duplicate_and_offline_serials_are_not_treated_as_unique(self):
        output = "List of devices attached\ntest-device\tdevice\ntest-device\toffline\n"
        with mock.patch.object(cycles.subprocess, "check_output", return_value=output):
            self.assertEqual(cycles.adb_states("test-device"), ["device", "offline"])
            self.assertEqual(cycles.adb_serial_count("test-device"), 2)
            self.assertTrue(cycles.adb_online("test-device"))

    def test_preexisting_fastboot_target_blocks_android_transition(self):
        with mock.patch.object(cycles, "fastboot_serial_count", return_value=1), \
             mock.patch.object(cycles, "run_capture") as run:
            with self.assertRaisesRegex(cycles.CycleError, "already online"):
                cycles.request_fastboot_from_android("android", "fastboot", 120)
            run.assert_not_called()

    def test_original_boot_b_is_rehashed_at_restore_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "boot_b.img"
            image.write_bytes(b"original")
            record = {"bytes": 8, "sha256": cycles.sha256(b"original")}
            cycles.verify_original_boot_b(image, record)
            image.write_bytes(b"tampered")
            with self.assertRaisesRegex(cycles.CycleError, "backup changed"):
                cycles.verify_original_boot_b(image, record)


if __name__ == "__main__":
    unittest.main()
