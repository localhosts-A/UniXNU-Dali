"""Corrupt media and unit mismatches must not create a scratch write grant."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import unittest
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ufs_geometry as ufs
import ufs_identity
import unattended_layout as layout
collector_spec = importlib.util.spec_from_file_location(
    "ufs_baseline_collector", Path(__file__).resolve().parents[1] / "scripts/collect-ufs-baseline.py")
collector = importlib.util.module_from_spec(collector_spec)
collector_spec.loader.exec_module(collector)


def fixture(logical=4096):
    total = 262144
    table = bytearray(128 * 128)
    row = memoryview(table)[70 * 128:71 * 128]
    row[:16] = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le
    row[16:32] = uuid.UUID("5fb6f86d-11ab-4296-8867-1199ec68e09a").bytes_le
    blocks = 64 * layout.MIB // logical
    struct.pack_into("<QQQ", row, 32, 512, 512 + blocks - 1, 0)
    row[56:68] = "boot_b".encode("utf-16-le")
    count = len(table) // logical
    first, last = 2 + count, total - 2 - count

    def head(current, other, at):
        data = bytearray(logical)
        struct.pack_into("<8sIIIIQQQQ16sQIII", data, 0, b"EFI PART", 0x10000, 92, 0, 0,
                         current, other, first, last, uuid.UUID(int=17).bytes_le,
                         at, 128, 128, zlib.crc32(table))
        struct.pack_into("<I", data, 16, zlib.crc32(data[:92]))
        return data
    return [head(1, total - 1, 2), table, head(total - 1, 1, total - 1 - count), bytearray(table)]


def fix_header_crc(data):
    data[16:20] = bytes(4)
    struct.pack_into("<I", data, 16, zlib.crc32(data[:92]))


def fix_tables(raw):
    for head, table in ((raw[0], raw[1]), (raw[2], raw[3])):
        struct.pack_into("<I", head, 88, zlib.crc32(table))
        fix_header_crc(head)


def verified(raw=None, logical=4096, **overrides):
    gpt = ufs.validate_gpt(*(raw or fixture(logical)), logical, 262144 * logical)
    values = {"lun": 2, "logical_bytes": logical, "capacity_bytes": 262144 * logical,
              "start_sectors_512": 512 * logical // 512,
              "size_sectors_512": 64 * layout.MIB // 512,
              "partition_number": 71, "sysfs_read_only": False, "gpt": gpt}
    values.update(overrides)
    return ufs.geometry(**values)


def candidate():
    image = bytearray(8192)
    image[:8] = b"ANDROID!"
    struct.pack_into("<II", image, 8, 4096, 0)
    struct.pack_into("<I", image, 20, 1584)
    struct.pack_into("<I", image, 40, 4)
    image[4096:4100] = layout.LEGACY_LZ4
    return image


class UfsGeometryTests(unittest.TestCase):
    def test_linux_ufs_identity_reconstructs_the_xnu_raw_descriptor_binding(self):
        raw = bytes.fromhex("1234") + bytes((14, 5)) + "ABC123".encode("utf-16-be")
        self.assertEqual(ufs.device_identity_binding("0x1234", "ABC123"), hashlib.sha256(raw).hexdigest())
        for manufacturer, serial in (("1234", "ABC123"), ("0x1234", ""),
                                     ("0x1234", "ABC\n123"), ("0x1234", "é" * 127)):
            with self.subTest(manufacturer=manufacturer, serial=serial), self.assertRaises(ValueError):
                ufs.device_identity_binding(manufacturer, serial)

    def test_android_identity_reader_cat_reads_both_descriptors(self):
        commands = []

        def read(command):
            commands.append(command)
            return b"0x1234\n" if "manufacturer_id" in command else b"ABC123\n"

        record, digest, captured = ufs_identity.read_identity("/sys/ufs", read)
        raw = bytes.fromhex("1234") + bytes((14, 5)) + "ABC123".encode("utf-16-be")
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["serial_number"], "ABC123")
        self.assertEqual(captured["serial_number"], b"ABC123\n")
        self.assertEqual(commands, [
            "cat /sys/ufs/device_descriptor/manufacturer_id",
            "cat /sys/ufs/string_descriptors/serial_number",
        ])

    def test_stock_zero_disk_guid_is_recorded_but_not_an_identity(self):
        raw = fixture()
        for index in (0, 2):
            raw[index][56:72] = bytes(16)
            fix_header_crc(raw[index])
        gpt = ufs.validate_gpt(*raw, 4096, 262144 * 4096)
        self.assertFalse(gpt["primary"]["disk_guid_usable_as_identity"])
        self.assertFalse(gpt["backup"]["disk_guid_usable_as_identity"])
        self.assertTrue(verified(raw)["geometry_verified"])

    def test_lun2_and_actual_block_units_are_preserved(self):
        for logical in (512, 4096):
            with self.subTest(logical=logical):
                geo = verified(logical=logical)
                self.assertEqual(geo["lun"], 2)
                self.assertEqual(geo["boot_b"]["first_lba"], 512)
                self.assertEqual(geo["boot_b"]["bytes"], 64 * layout.MIB)
                self.assertFalse(geo["dma_ownership_verified"])
                self.assertFalse(geo["write_protection_verified"])
                self.assertFalse(geo["media_writes_qualified"])

    def test_header_and_both_entry_arrays_are_crc_checked(self):
        for index in range(4):
            raw = fixture()
            raw[index][32 if index % 2 == 0 else 1024] ^= 1
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "CRC"):
                verified(raw)

    def test_backup_is_required_and_reciprocal(self):
        raw = fixture()
        raw[2] = raw[2][:-1]
        with self.assertRaisesRegex(ValueError, "truncated"):
            verified(raw)
        raw = fixture()
        struct.pack_into("<Q", raw[2], 32, 2)
        fix_header_crc(raw[2])
        with self.assertRaisesRegex(ValueError, "reciprocal"):
            verified(raw)

    def test_crc_correct_but_different_gpt_copies_fail(self):
        raw = fixture()
        raw[3][70 * 128 + 56:70 * 128 + 68] = "other_".encode("utf-16-le")
        fix_tables(raw)
        with self.assertRaisesRegex(ValueError, "mismatch|differ"):
            verified(raw)

    def test_entry_array_must_not_overlap_partition_or_header(self):
        for index, at in ((0, 512), (2, 262143)):
            raw = fixture()
            struct.pack_into("<Q", raw[index], 72, at)
            fix_header_crc(raw[index])
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "overlaps"):
                verified(raw)

    def test_duplicate_and_overlapping_partitions_fail(self):
        for duplicate in (True, False):
            raw = fixture()
            for table in (raw[1], raw[3]):
                table[:128] = table[70 * 128:71 * 128]
                if not duplicate:
                    table[16:32] = uuid.UUID(int=88).bytes_le
                    table[56:128] = bytes(72)
                    table[56:66] = "other".encode("utf-16-le")
            fix_tables(raw)
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, "duplicate|overlapping"):
                verified(raw)

    def test_sysfs_geometry_guid_and_units_must_match_gpt(self):
        for changes in ({"start_sectors_512": 512}, {"size_sectors_512": 4096},
                        {"partition_number": 1}, {"partition_guid": str(uuid.UUID(int=10))},
                        {"lun": 128}, {"lun": True}, {"sysfs_read_only": 0},
                        {"start_sectors_512": 1}, {"size_sectors_512": 1 << 63}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verified(**changes)

    def test_invalid_block_sizes_and_unbounded_tables_fail(self):
        for logical in (True, 0, 256, 768, 131072):
            with self.subTest(logical=logical), self.assertRaises(ValueError):
                ufs.block_size(logical)
        raw = fixture()
        struct.pack_into("<I", raw[0], 80, 32769)
        fix_header_crc(raw[0])
        with self.assertRaises(ValueError):
            verified(raw)

    def test_scratch_uses_actual_lun_and_absolute_gpt_lba_without_authorizing(self):
        for logical in (512, 4096):
            geo = verified(logical=logical)
            result = ufs.scratch_window(candidate(), geo)
            self.assertEqual(result["lun"], 2)
            self.assertEqual(result["scratch_first_lba"], 512 + 62 * layout.MIB // logical)
            self.assertEqual(result["scratch_blocks"], layout.MIB // logical)
            self.assertFalse(result["write_authorized"])
            self.assertFalse(result["media_writes_qualified"])

    def test_write_protection_and_unverified_geometry_refuse_scratch(self):
        geo = verified()
        for key, value in (("sysfs_read_only", True), ("gpt_read_only", True),
                           ("geometry_verified", False), ("sysfs_read_only", None)):
            bad = copy.deepcopy(geo)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ufs.scratch_window(candidate(), bad)

    def test_unit_descriptor_is_cross_checked_without_qualifying_runtime_dma_or_writes(self):
        geo = verified()
        values = {"lu_enable": "0x01", "lun_write_protect": "0x00", "logical_block_size": "0x0c",
                  "logical_block_count": hex(geo["capacity_blocks"])}
        observed = ufs.unit_descriptor(geo, values)
        self.assertTrue(observed["write_protection_observed_android"])
        self.assertFalse(observed["unit_write_protected"])
        self.assertFalse(observed["write_protection_verified"])
        self.assertFalse(observed["media_writes_qualified"])
        for key, value in (("lu_enable", "0x00"), ("lun_write_protect", "0x03"),
                           ("logical_block_size", "0x09"), ("logical_block_count", "0x01"),
                           ("logical_block_count", "cat: missing")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ufs.unit_descriptor(geo, dict(values, **{key: value}))
        for value in ("0x01", "0x02"):
            protected = ufs.unit_descriptor(geo, dict(values, lun_write_protect=value))
            with self.assertRaisesRegex(ValueError, "protected"):
                ufs.scratch_window(candidate(), protected)


if __name__ == "__main__":
    unittest.main()
