"""Evidence-binding tests for the source-XNU QEMU xHCI runtime gate."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "source_xhci_runtime_qemu", ROOT / "scripts/test-source-xhci-runtime-qemu.py")
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class SourceXhciRuntimeQemuTests(unittest.TestCase):
    arguments = "rd=md0 serial=3 debug=0x14e source-storage-test=2 source-msdosfs-test=3"

    def report(self):
        return {
            "kernel_sha256": "a" * 64,
            "kernel_arguments": self.arguments,
            "bounded_timeout_interrupted": False,
            "native_4k_mmu_observed": True,
            "crypto14_initialization_observed": True,
            "stop_symbol": "_source_msdosfs_test_checkpoint",
            "function_return_target": "0xfffffff007123456",
            "pc": "0xfffffff007123456",
            "x0": hex(RUNTIME.CHECKPOINT),
            "qemu_xhci_attached": True,
            "usb_storage_image_sha256": "b" * 64,
        }

    def test_builds_valid_fat32_superfloppy_image_with_expected_readme(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "disposable-usb-fat32.raw"
            layout = RUNTIME.build_usb_fat32_image(image)
            self.assertEqual(image.stat().st_size,
                             RUNTIME.USB_FAT32_TOTAL_SECTORS * RUNTIME.USB_FAT32_SECTOR_BYTES)
            self.assertEqual(layout["filesystem"], "FAT32")
            self.assertEqual(layout["cluster_count"], 65526)
            self.assertEqual(layout["readme_contents"], "Dali USB BOT FAT32 QEMU read-only\n")
            with image.open("r+b") as disk:
                disk.seek(510)
                self.assertEqual(disk.read(2), b"\x55\xaa")
                disk.seek(RUNTIME.USB_FAT32_DATA_START * RUNTIME.USB_FAT32_SECTOR_BYTES)
                self.assertEqual(disk.read(11), b"README  TXT")
                entry = bytearray(32)
                entry[:11] = RUNTIME.USB_FAT32_PERSISTED_NAME
                entry[11] = 0x20
                struct.pack_into("<H", entry, 26, 4)
                struct.pack_into("<I", entry, 28, len(RUNTIME.USB_FAT32_PERSISTED_CONTENTS))
                disk.seek((RUNTIME.USB_FAT32_DATA_START * RUNTIME.USB_FAT32_SECTOR_BYTES) + 32)
                disk.write(entry)
                for fat_index in range(RUNTIME.USB_FAT32_FAT_COUNT):
                    fat_lba = RUNTIME.USB_FAT32_RESERVED_SECTORS + fat_index * RUNTIME.USB_FAT32_SECTORS_PER_FAT
                    disk.seek(fat_lba * RUNTIME.USB_FAT32_SECTOR_BYTES + 4 * 4)
                    disk.write(struct.pack("<I", 0x0fffffff))
                file_lba = RUNTIME.USB_FAT32_DATA_START + 2
                disk.seek(file_lba * RUNTIME.USB_FAT32_SECTOR_BYTES)
                disk.write(RUNTIME.USB_FAT32_PERSISTED_CONTENTS)
            self.assertEqual(RUNTIME.read_usb_fat32_root_file(image,
                RUNTIME.USB_FAT32_PERSISTED_NAME), RUNTIME.USB_FAT32_PERSISTED_CONTENTS)
            with image.open("r+b") as disk:
                disk.seek(510)
                disk.write(b"\x00\x00")
            with self.assertRaisesRegex(ValueError, "boot sector"):
                RUNTIME.validate_usb_fat32_image(image)

    def test_accepts_bound_readonly_fat32_checkpoint_and_connected_usb2_port(self):
        uart = (RUNTIME.RUNTIME_MARKER + "\n" +
                "DaliUsbQemuXhciRuntime: USB2 port 2 connected=1 powered=1 "
                "overcurrent=0 speed=3 changes=0x1\n" +
                "DaliUsbQemuXhciRuntime: enumerated USB device at root port 2 "
                "depth 0 address 1 speed 2\n" +
                "DaliUsbQemuXhciRuntime: published USB BOT storage at address 1 "
                "(66584 blocks, 512-byte sectors)\n")
        result = RUNTIME.validate(self.report(), "a" * 64,
                                  self.arguments,
                                  uart, "b" * 64, "c" * 64,
                                  RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())
        self.assertEqual(result["ports"], [{"port": 2, "powered": True,
                                             "overcurrent": False,
                                             "speed_id": 3, "changes": 1}])
        self.assertEqual(result["devices"], [{"root_port": 2, "depth": 0,
                                               "address": 1, "speed_id": 2}])
        self.assertTrue(result["storage_child_published"])
        self.assertEqual(result["storage_children"],
                         [{"address": 1, "blocks": 66584, "sector_bytes": 512}])
        self.assertTrue(result["fat32_readonly_mount_pass"])
        self.assertFalse(result["hid_child_published"])

    def test_rejects_root_port_only_runtime_without_device_and_storage_child(self):
        uart = (RUNTIME.RUNTIME_MARKER + "\n" +
                "DaliUsbQemuXhciRuntime: USB2 port 2 connected=1 powered=1 "
                "overcurrent=0 speed=3 changes=0x1\n")
        with self.assertRaisesRegex(ValueError, "did not enumerate"):
            RUNTIME.validate(self.report(), "a" * 64,
                             self.arguments,
                             uart, "b" * 64, "c" * 64,
                             RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())

    def test_rejects_missing_runtime_or_root_port_evidence(self):
        with self.assertRaisesRegex(ValueError, "did not start"):
            RUNTIME.validate(self.report(), "a" * 64,
                             self.arguments,
                             "", "b" * 64, "c" * 64,
                             RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())
        with self.assertRaisesRegex(ValueError, "connected USB2 root port"):
            RUNTIME.validate(self.report(), "a" * 64,
                             self.arguments,
                             RUNTIME.RUNTIME_MARKER, "b" * 64, "c" * 64,
                             RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())

    def test_rejects_wrong_kernel_checkpoint_and_changed_virtual_disk(self):
        uart = (RUNTIME.RUNTIME_MARKER + "\n" +
                "DaliUsbQemuXhciRuntime: USB2 port 2 connected=1 powered=1 "
                "overcurrent=0 speed=3 changes=0x1\n" +
                "DaliUsbQemuXhciRuntime: enumerated USB device at root port 2 "
                "depth 0 address 1 speed 2\n" +
                "DaliUsbQemuXhciRuntime: published USB BOT storage at address 1 "
                "(66584 blocks, 512-byte sectors)\n")
        report = self.report()
        report["kernel_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "not bound"):
            RUNTIME.validate(report, "a" * 64,
                             self.arguments,
                             uart, "b" * 64, "c" * 64,
                             RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())
        with self.assertRaisesRegex(ValueError, "did not change"):
            RUNTIME.validate(self.report(), "a" * 64,
                             self.arguments,
                             uart, "b" * 64, "b" * 64,
                             RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())

    def test_rejects_wrong_stop_site_missing_boot_evidence_or_checkpoint(self):
        uart = (RUNTIME.RUNTIME_MARKER + "\n" +
                "DaliUsbQemuXhciRuntime: USB2 port 2 connected=1 powered=1 "
                "overcurrent=0 speed=3 changes=0x1\n" +
                "DaliUsbQemuXhciRuntime: enumerated USB device at root port 2 "
                "depth 0 address 1 speed 2\n" +
                "DaliUsbQemuXhciRuntime: published USB BOT storage at address 1 "
                "(66584 blocks, 512-byte sectors)\n")
        for change in ({"stop_symbol": "_source_storage_test_checkpoint"},
                       {"native_4k_mmu_observed": False},
                       {"crypto14_initialization_observed": False},
                       {"x0": "0x0"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "not bound"):
                RUNTIME.validate(dict(self.report(), **change), "a" * 64,
                                 self.arguments, uart, "b" * 64, "c" * 64,
                                 RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())

    def test_rejects_changed_image_without_persisted_contents(self):
        with self.assertRaisesRegex(ValueError, "did not persist"):
            RUNTIME.validate(self.report(), "a" * 64, self.arguments,
                             RUNTIME.RUNTIME_MARKER + "\n" +
                             "DaliUsbQemuXhciRuntime: USB2 port 2 connected=1 powered=1 "
                             "overcurrent=0 speed=3 changes=0x1\n" +
                             "DaliUsbQemuXhciRuntime: enumerated USB device at root port 2 "
                             "depth 0 address 1 speed 2\n" +
                             "DaliUsbQemuXhciRuntime: published USB BOT storage at address 1 "
                             "(66584 blocks, 512-byte sectors)\n",
                             "b" * 64, "c" * 64, "wrong contents\n")

    def test_workflow_runs_and_discovers_the_usb_fat32_qemu_gate(self):
        workflow = (ROOT / ".github/workflows/dali-usb-xnu.yml").read_text()
        self.assertIn("scripts/test-source-xhci-runtime-qemu.py", workflow)
        self.assertIn("tests/test_source_xhci_runtime_qemu.py", workflow)
        self.assertIn("test_source_xhci_runtime_qemu.py' -v", workflow)
        self.assertIn("USB BOT FAT32 write, sync and remount", workflow)

    def test_rejects_a_bot_capacity_that_does_not_match_the_fat32_image(self):
        uart = (RUNTIME.RUNTIME_MARKER + "\n" +
                "DaliUsbQemuXhciRuntime: USB2 port 2 connected=1 powered=1 "
                "overcurrent=0 speed=3 changes=0x1\n" +
                "DaliUsbQemuXhciRuntime: enumerated USB device at root port 2 "
                "depth 0 address 1 speed 2\n" +
                "DaliUsbQemuXhciRuntime: published USB BOT storage at address 1 "
                "(32768 blocks, 512-byte sectors)\n")
        with self.assertRaisesRegex(ValueError, "geometry"):
            RUNTIME.validate(self.report(), "a" * 64, self.arguments,
                             uart, "b" * 64, "c" * 64,
                             RUNTIME.USB_FAT32_PERSISTED_CONTENTS.decode())


if __name__ == "__main__":
    unittest.main()
