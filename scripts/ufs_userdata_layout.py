"""Pure GPT planner for the explicitly authorized destructive Dali data split.

No device I/O. Preserve every existing partition except userdata's final LBA.
The 32 GiB carved from its tail contains exFAT data and a separate 32 MiB raw
log partition. A complete old/new GPT transaction must be sealed before use.
"""
import hashlib
import struct
import uuid
import zlib

import ufs_geometry as ufs

GIB = 1024**3
MIB = 1024**2
RESERVE_BYTES = 32 * GIB
LOG_BYTES = 32 * MIB
DATA_NAME = "unixnu_data"
LOG_NAME = "unixnu_logs"
BASIC_DATA_TYPE = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")
LOG_TYPE = uuid.uuid5(uuid.NAMESPACE_URL, "https://unixnu.com/partitions/diagnostic-log-v1")


def apply_gpt_transaction(patches, originals, *, read, write, sync, journal):
    """Apply the planner's four patches once, preserving a complete valid copy.

    journal is durable and called before each write. Any read/write/flush error
    stops the transaction; there is no automatic write retry or rollback.
    """
    names = [name for name, _, _ in patches]
    if names != ['backup-entries', 'backup-header', 'primary-entries', 'primary-header']:
        raise ValueError('invalid GPT transaction order')
    for name, lba, data in patches:
        if read(lba, len(data)) != originals[name]:
            raise ValueError('GPT preimage changed: ' + name)
        journal(name, 'attempted')
        write(name, lba, data)
        sync()
        if read(lba, len(data)) != data:
            raise ValueError('GPT readback differs: ' + name)
        journal(name, 'verified')


def _guid(value):
    parsed = uuid.UUID(str(value))
    if parsed.int == 0:
        raise ValueError("new partition GUID cannot be zero")
    return parsed


def _entry(name, guid, kind, first, last):
    row = bytearray(128)
    row[:16], row[16:32] = kind.bytes_le, guid.bytes_le
    struct.pack_into("<QQQ", row, 32, first, last, 0)
    encoded = name.encode("utf-16-le")
    if len(encoded) > 70:
        raise ValueError("partition name too long")
    row[56:56 + len(encoded)] = encoded
    return row


def _header(old, count, entries):
    block = bytearray(old)
    struct.pack_into("<II", block, 80, count, 128)
    struct.pack_into("<I", block, 88, zlib.crc32(entries) & 0xffffffff)
    struct.pack_into("<I", block, 16, 0)
    size = struct.unpack_from("<I", block, 12)[0]
    struct.pack_into("<I", block, 16, zlib.crc32(block[:size]) & 0xffffffff)
    return bytes(block)


def plan_split(primary, entries, backup, backup_entries, *, block_bytes,
               capacity_bytes, data_guid, log_guid):
    """Return exact new GPT bytes plus bounded ranges; never authorize writes.

    Dali's stock 96-entry table is full. Extend to 128 only inside the existing
    metadata gaps; retain both entry-array addresses and usable LBA boundaries.
    This avoids moving flashinfo or any other original partition.
    """
    original = ufs.validate_gpt(primary, entries, backup, backup_entries,
                                block_bytes, capacity_bytes)
    head, tail = original["primary"], original["backup"]
    if head["entry_bytes"] != 128 or head["entry_count"] > 128:
        raise ValueError("unsupported GPT table expansion")
    if any(p["name"] in (DATA_NAME, LOG_NAME) for p in original["partitions"]):
        raise ValueError("UniXNU partitions already exist; no repeat split")
    found = [p for p in original["partitions"] if p["name"] == "userdata"]
    if len(found) != 1 or found[0]["gpt_read_only"]:
        raise ValueError("one writable userdata partition is required")
    part = found[0]
    if RESERVE_BYTES % block_bytes or LOG_BYTES % block_bytes:
        raise ValueError("reserved regions are not aligned to actual logical blocks")
    reserve, logs = RESERVE_BYTES // block_bytes, LOG_BYTES // block_bytes
    if part["blocks"] <= reserve + GIB // block_bytes:
        raise ValueError("insufficient userdata left for Android")
    first = part["last_lba"] + 1 - reserve
    log_first = part["last_lba"] + 1 - logs
    data_id, log_id = _guid(data_guid), _guid(log_guid)
    existing = {p["guid"] for p in original["partitions"]}
    if data_id == log_id or str(data_id) in existing or str(log_id) in existing:
        raise ValueError("duplicate new partition identity")
    table = bytearray(entries)
    table.extend(bytes(128 * 128 - len(table)))
    unused = [i for i in range(128) if not any(table[i*128:(i+1)*128])]
    if len(unused) < 2:
        raise ValueError("insufficient empty GPT entries")
    table_blocks = (len(table) + block_bytes - 1) // block_bytes
    if (head["entries_lba"] + table_blocks > head["first_usable_lba"] or
            tail["entries_lba"] + table_blocks > tail["current_lba"]):
        raise ValueError("expanded GPT would overlap usable data or a header")
    struct.pack_into("<Q", table, (part["number"] - 1)*128 + 40, first - 1)
    for index, row in ((unused[0], _entry(DATA_NAME, data_id, BASIC_DATA_TYPE, first, log_first - 1)),
                       (unused[1], _entry(LOG_NAME, log_id, LOG_TYPE, log_first, part["last_lba"]))):
        table[index*128:(index+1)*128] = row
    table = bytes(table)
    left, right = _header(primary, 128, table), _header(backup, 128, table)
    changed = ufs.validate_gpt(left, table, right, table, block_bytes, capacity_bytes)
    old_by_number = {p["number"]: p for p in original["partitions"]}
    new_by_number = {p["number"]: p for p in changed["partitions"]}
    for number, old in old_by_number.items():
        expected = dict(old)
        if number == part["number"]:
            expected.update(last_lba=first - 1, blocks=first - part["first_lba"])
        if new_by_number[number] != expected:
            raise ValueError("unapproved original partition change")
    padding = bytes(table_blocks * block_bytes - len(table))
    patches = (
        ("backup-entries", tail["entries_lba"], table + padding),
        ("backup-header", tail["current_lba"], right),
        ("primary-entries", head["entries_lba"], table + padding),
        ("primary-header", head["current_lba"], left),
    )
    return dict(schema=1, kind="dali-destructive-userdata-32gib-split-v1",
        block_bytes=block_bytes, capacity_bytes=capacity_bytes,
        carved_bytes=RESERVE_BYTES, data_bytes=RESERVE_BYTES - LOG_BYTES,
        log_bytes=LOG_BYTES, original_gpt=original, proposed_gpt=changed,
        original_userdata=part, userdata=new_by_number[part["number"]],
        data=new_by_number[unused[0] + 1], logs=new_by_number[unused[1] + 1],
        patches=[dict(name=name, lba=lba, bytes=len(raw),
                      sha256=hashlib.sha256(raw).hexdigest()) for name, lba, raw in patches],
        media_writes_performed=False, write_authorized=False), patches
