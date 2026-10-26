import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ufs_userdata_logs as logs
import ufs_userdata_android as android_logs
import ufs_scratch_authorization as authorization

TOKEN = "1234567890abcdef" * 2
DEVICE = "11" * 32
CANDIDATE = "22" * 32
SOURCE = "33" * 32


def baseline():
    part = dict(number=98, name="unixnu_logs", guid=logs.PARTITION_GUID,
                first_lba=64565240, last_lba=64573431, blocks=8192,
                attributes=0, gpt_read_only=False,
                type_guid=str(uuid.uuid5(uuid.NAMESPACE_URL, "https://unixnu.com/partitions/diagnostic-log-v1")))
    return {
        "kind": "dali-ufs-android-read-only-baseline", "serial": "N7MJ65H6MRGESC9X",
        "android": {"product": "dali", "slot_suffix": "_b", "root": "uid=0(root)"},
        "all_partition_hashes_match": True, "known_block_matches_backup": True,
        "device_writes_performed": False, "device_reboot_performed": False,
        "geometry": {"geometry_verified": True, "lun": 2, "logical_block_bytes": 4096,
            "capacity_blocks": 64577536, "capacity_bytes": 64577536 * 4096,
            "sysfs_read_only": False, "gpt_read_only": False,
            "write_protection_observed_android": True, "unit_write_protected": False,
            "unit_descriptor": {"lu_enable": 1, "lun_write_protect": 0,
                "logical_block_size": 12, "logical_block_count": 64577536}},
        "gpt": {"partitions": [part]},
    }


def xnu_body():
    diagnostic = b"DIAG"
    msgbuf = b"kernel!\0"
    console = b"DALI-XNU-UFS: message\n"
    total = logs.XNU_BODY_HEADER + len(diagnostic) + len(msgbuf) + len(console)
    header = bytearray(logs.XNU_BODY_HEADER)
    header[:len(logs.XNU_BODY_MAGIC)] = logs.XNU_BODY_MAGIC
    struct.pack_into("<12I", header, 16, 1, logs.XNU_BODY_HEADER, len(diagnostic), len(msgbuf),
                     len(msgbuf), 3, 1, len(console), logs.XNU_CONSOLE_CAPACITY, 1, 7, total)
    return bytes(header) + diagnostic + msgbuf + console


def make_slot(body, owner, sequence=1, *, token=TOKEN, device=DEVICE, candidate=CANDIDATE, source=SOURCE):
    slot = bytearray(logs.SLOT_BYTES)
    block = 4096
    digest = hashlib.sha256(body).digest()
    for kind, offset in ((1, 0), (2, block), (3, logs.SLOT_BYTES - block)):
        record = bytearray(block)
        record[:11] = b"DALIUFSLOG1"
        struct.pack_into("<II", record, 16, kind, 1)
        record[24:56] = token.encode()
        struct.pack_into("<IQIIQ", record, 56, owner, sequence, len(body), block, logs.SLOT_BLOCKS)
        record[84:116] = bytes.fromhex(device)
        record[116:148] = bytes.fromhex(candidate)
        record[148:180] = bytes.fromhex(source)
        record[180:212] = digest
        record[212:244] = hashlib.sha256(record).digest()
        slot[offset:offset + block] = record
    slot[2 * block:2 * block + len(body)] = body
    return bytes(slot)


class UserdataLogTests(unittest.TestCase):
    def test_partition_geometry_and_exact_zero_slot_precondition(self):
        base = baseline()
        part = logs.userdata_partition(base)
        self.assertEqual((part["number"], part["first_lba"], part["blocks"], part["bytes"]),
                         (98, 64565240, 8192, 32 * logs.MIB))
        raw = bytes(logs.PARTITION_BYTES)
        report = logs.validate_partition_snapshot(raw, base, 1)
        self.assertTrue(report["sequence_pair_is_zero"])
        occupied_elsewhere = bytearray(raw)
        occupied_elsewhere[logs.PAIR_BYTES + 16] = 1
        self.assertTrue(logs.validate_partition_snapshot(bytes(occupied_elsewhere), base, 1)["sequence_pair_is_zero"])
        with self.assertRaisesRegex(ValueError, "not both pristine"):
            logs.validate_partition_snapshot(bytes(occupied_elsewhere), base, 2)

    def test_layout_identity_and_write_protection_fail_closed(self):
        base = baseline()
        for change in (
            {"guid": "cde9ab32-18d8-4777-8ced-691e7cd5c635"},
            {"first_lba": 64565239}, {"blocks": 8191}, {"attributes": 1},
            {"type_guid": str(uuid.UUID(int=1))},
        ):
            bad = baseline(); bad["gpt"]["partitions"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                logs.userdata_partition(bad)
        base["geometry"]["unit_write_protected"] = True
        with self.assertRaises(ValueError):
            logs.userdata_partition(base)

    def test_decode_both_owner_slots_and_real_msgbuf_body(self):
        raw = bytearray(logs.PARTITION_BYTES)
        uefi = b"UEFI diagnostics snapshot\r\n"
        xnu = xnu_body()
        raw[:logs.SLOT_BYTES] = make_slot(uefi, 1)
        raw[logs.SLOT_BYTES:2 * logs.SLOT_BYTES] = make_slot(xnu, 2)
        expected = dict(sequence=1, token=TOKEN, device_binding=DEVICE,
                        candidate_sha256=CANDIDATE, source_sha256=SOURCE,
                        required_owners=(1, 2))
        result = logs.decode_partition(bytes(raw), expected=expected)
        self.assertTrue(result["required_owner_slots_verified"])
        self.assertEqual([e["owner"] for e in result["entries"]], [1, 2])
        xnu_entry = result["entries"][1]["xnu_body"]
        self.assertEqual(xnu_entry["msgbuf_ring"], b"kernel!\0")
        self.assertEqual(xnu_entry["diagnostic"], b"DIAG")

    def test_owner_sequence_binding_padding_and_body_metadata_are_verified(self):
        slot = make_slot(b"body", 1)
        self.assertIsNotNone(logs.decode_slot(slot, expected_owner=1, expected_sequence=1))
        with self.assertRaisesRegex(ValueError, "owner/sequence"):
            logs.decode_slot(slot, expected_owner=2, expected_sequence=1)
        damaged = bytearray(slot); damaged[4096 + 84] ^= 1
        with self.assertRaises(ValueError):
            logs.decode_slot(bytes(damaged), expected_owner=1, expected_sequence=1)
        padded = bytearray(slot); padded[8192 + len(b"body")] = 1
        with self.assertRaisesRegex(ValueError, "padding"):
            logs.decode_slot(bytes(padded), expected_owner=1, expected_sequence=1)
        with self.assertRaisesRegex(ValueError, "metadata"):
            logs.decode_xnu_body(xnu_body()[:-1])

    def test_android_export_keeps_raw_partition_and_writes_binary_and_text_views(self):
        raw = bytearray(logs.PARTITION_BYTES)
        raw[:logs.SLOT_BYTES] = make_slot(b"UEFI\r\n", 1)
        raw[logs.SLOT_BYTES:2 * logs.SLOT_BYTES] = make_slot(xnu_body(), 2)
        decoded = logs.decode_partition(bytes(raw), expected=dict(sequence=1, token=TOKEN,
            device_binding=DEVICE, candidate_sha256=CANDIDATE, source_sha256=SOURCE,
            required_owners=(1, 2)))
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "export"
            manifest = logs.write_export(out, bytes(raw), decoded)
            self.assertEqual((out / "unixnu_logs.img").stat().st_size, logs.PARTITION_BYTES)
            msgbuf = next(out.glob("seq-1-xnu-msgbuf-ring.bin"))
            self.assertEqual(msgbuf.read_bytes(), b"kernel!\0")
            self.assertTrue((out / "seq-1-xnu-msgbuf-ring.txt").is_file())
            self.assertEqual(manifest["partition_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(logs.validate_export(out, bytes(raw), decoded), manifest)
            inventory = android_logs._export_inventory(out, manifest, bytes(raw), manifest)
            self.assertEqual(inventory["unixnu_logs.img"], bytes(raw))
            self.assertIn("seq-1-xnu-msgbuf-ring.txt", inventory)
            msgbuf.chmod(0o600); msgbuf.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "export file differs"):
                logs.validate_export(out, bytes(raw), decoded)
            with self.assertRaisesRegex(ValueError, "sealed manifest"):
                android_logs._export_inventory(out, manifest, bytes(raw), manifest)
            with self.assertRaises(FileExistsError):
                logs.write_export(out, bytes(raw), decoded)

    def test_userdata_grant_binds_fresh_token_kernel_source_and_one_pristine_pair(self):
        base = baseline(); kernel = b"source XNU kernel bytes"
        manifest = {"device_commit": "a" * 40, "unixnu_commit": "b" * 40,
                    "uefi_commit": "c" * 40, "xnu_commit": "d" * 40,
                    "kernel_sha256": hashlib.sha256(kernel).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            boot = Path(temp) / "boot_b.img"; boot.write_bytes(b"B" * (4096 * 4))
            base["original_boot_b_sha256"] = hashlib.sha256(boot.read_bytes()).hexdigest()
            raw = bytes(logs.PARTITION_BYTES)
            with patch.object(authorization.userdata_logs, "validate_baseline_captures",
                              return_value={"device_binding_sha256": DEVICE}):
                grant, wire, proposal = authorization.build_userdata_logs_grant(
                    serial="N7MJ65H6MRGESC9X", token=TOKEN, sequence=1, baseline=base,
                    baseline_dir=temp, source_manifest=manifest, kernel_bytes=kernel,
                    candidate_image=b"C" * 4096, original_boot_b=boot, log_partition_bytes=raw)
                self.assertEqual(len(wire), 240)
                self.assertEqual((grant["profile"], grant["target_partition"], grant["scratch_start_lba"],
                                  grant["scratch_blocks"], grant["sequence"]),
                                 ("userdata-logs-diagnostic-v1", "unixnu_logs", 64565240, 1024, 1))
                self.assertTrue(proposal["sequence_pair_is_zero"])
                for changes in ({"sequence": 0}, {"serial": "wrong"},
                                {"source_manifest": dict(manifest, kernel_sha256="f" * 64)}):
                    kwargs = dict(serial="N7MJ65H6MRGESC9X", token=TOKEN, sequence=1, baseline=base,
                        baseline_dir=temp, source_manifest=manifest, kernel_bytes=kernel,
                        candidate_image=b"C" * 4096, original_boot_b=boot, log_partition_bytes=raw)
                    kwargs.update(changes)
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        authorization.build_userdata_logs_grant(**kwargs)

    def test_userdata_grant_refuses_nonzero_selected_pair_but_preserves_other_sequences(self):
        base = baseline(); kernel = b"kernel"; manifest = {"device_commit":"a"*40,
            "unixnu_commit":"b"*40,"uefi_commit":"c"*40,"xnu_commit":"d"*40,
            "kernel_sha256":hashlib.sha256(kernel).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            boot=Path(temp)/"boot_b.img";boot.write_bytes(b"B"*4096)
            base["original_boot_b_sha256"] = hashlib.sha256(boot.read_bytes()).hexdigest()
            occupied=bytearray(logs.PARTITION_BYTES);occupied[logs.PAIR_BYTES+32]=1
            with patch.object(authorization.userdata_logs,"validate_baseline_captures",
                              return_value={"device_binding_sha256":DEVICE}):
                common=dict(serial="N7MJ65H6MRGESC9X",token=TOKEN,baseline=base,baseline_dir=temp,
                    source_manifest=manifest,kernel_bytes=kernel,candidate_image=b"C"*4096,
                    original_boot_b=boot)
                authorization.build_userdata_logs_grant(**common,sequence=1,log_partition_bytes=bytes(occupied))
                with self.assertRaisesRegex(ValueError,"not both pristine"):
                    authorization.build_userdata_logs_grant(**common,sequence=2,log_partition_bytes=bytes(occupied))


if __name__ == "__main__":
    unittest.main()
