import hashlib
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ufs_expdb_log import MAGIC_V2
from ufs_userdata_logs import (
    PARTITION_BYTES,
    SLOT_BLOCKS,
    SLOT_BYTES,
    XNU_CONSOLE_CAPACITY,
    decode_partition,
    decode_slot,
)

BLOCK_BYTES = 4096


def make_v2_slot(*, sequence, generation, owner=1, token="a" * 32, body=b"console"):
    slot = bytearray(SLOT_BYTES)
    slot[2 * BLOCK_BYTES:2 * BLOCK_BYTES + len(body)] = body
    body_digest = hashlib.sha256(body).digest()
    device_binding = bytes.fromhex("01" * 32)
    candidate = bytes.fromhex("02" * 32)
    source = bytes.fromhex("03" * 32)
    capture_stage, body_format = ((1, 1) if owner == 1 else (2, 2))
    for offset, kind in ((0, 1), (BLOCK_BYTES, 2), (SLOT_BYTES - BLOCK_BYTES, 3)):
        record = bytearray(BLOCK_BYTES)
        record[:16] = MAGIC_V2
        struct.pack_into("<II", record, 16, kind, 2)
        record[24:56] = token.encode("ascii")
        struct.pack_into("<IQIIQ", record, 56, owner, sequence, len(body), BLOCK_BYTES, SLOT_BLOCKS)
        record[84:180] = device_binding + candidate + source
        record[180:212] = body_digest
        struct.pack_into("<QIII", record, 244, generation, 0, capture_stage, body_format)
        record[212:244] = hashlib.sha256(record).digest()
        slot[offset:offset + BLOCK_BYTES] = record
    return bytes(slot)


def make_xnu_body():
    diagnostic = b"d"
    msgbuf = bytes(16)
    total = 64 + len(diagnostic) + len(msgbuf)
    body = bytearray(total)
    body[:14] = b"DALI-XNU-BODY1"
    struct.pack_into("<12I", body, 16, 1, 64, len(diagnostic), len(msgbuf), len(msgbuf),
                      0, 0, 0, XNU_CONSOLE_CAPACITY, 0, 0, total)
    body[64:65] = diagnostic
    body[65:] = msgbuf
    return bytes(body)


def put_slot(partition, *, sequence, owner, slot):
    index = (sequence - 1) * 2 + owner - 1
    start = index * SLOT_BYTES
    partition[start:start + SLOT_BYTES] = slot


class UserdataLogV2Tests(unittest.TestCase):
    def test_slot_decoder_returns_v2_generation_and_flags(self):
        slot = make_v2_slot(sequence=2, generation=23)
        decoded = decode_slot(slot, expected_owner=1, expected_sequence=2)
        self.assertEqual(decoded["record"]["schema"], 2)
        self.assertEqual(decoded["record"]["generation"], 23)
        self.assertEqual(decoded["record"]["body_format"], 1)

    def test_partition_orders_v2_by_generation_and_keeps_v1_order_unknown(self):
        partition = bytearray(PARTITION_BYTES)
        put_slot(partition, sequence=1, owner=1,
                 slot=make_v2_slot(sequence=1, generation=2))
        put_slot(partition, sequence=3, owner=1,
                 slot=make_v2_slot(sequence=3, generation=1))
        decoded = decode_partition(bytes(partition))
        self.assertEqual([entry["sequence"] for entry in decoded["v2_generation_order"]], [3, 1])
        self.assertEqual(decoded["v1_order"], "unknown")

    def test_xnu_v2_requires_matching_uefi_pair(self):
        partition = bytearray(PARTITION_BYTES)
        put_slot(partition, sequence=2, owner=2,
                 slot=make_v2_slot(sequence=2, generation=7, owner=2, body=make_xnu_body()))
        with self.assertRaisesRegex(ValueError, "no matching v2 UEFI slot"):
            decode_partition(bytes(partition))

    def test_mismatched_v2_pair_token_is_rejected(self):
        partition = bytearray(PARTITION_BYTES)
        put_slot(partition, sequence=2, owner=1,
                 slot=make_v2_slot(sequence=2, generation=7, owner=1, token="a" * 32))
        put_slot(partition, sequence=2, owner=2,
                 slot=make_v2_slot(sequence=2, generation=7, owner=2, token="b" * 32,
                                   body=make_xnu_body()))
        with self.assertRaisesRegex(ValueError, "pair metadata differs"):
            decode_partition(bytes(partition))


if __name__ == "__main__":
    unittest.main()
