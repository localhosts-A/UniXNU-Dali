#!/usr/bin/env python3
"""Validate raw GPT copies and convert Linux's 512-byte sectors to UFS LBAs.

No device I/O lives here. A valid layout is evidence of geometry, never a grant
to program a controller or write media. All returned LBAs use the observed UFS
logical block size, not sysfs sector units or the Android boot header page size.
"""
import hashlib
import struct
import uuid
import zlib


UINT64_MAX = (1 << 64) - 1
MAX_ENTRY_BYTES = 4 * 1024 * 1024
GPT_READ_ONLY = 1 << 60


def natural(value, label, *, positive=False):
    if type(value) is not int or value < int(positive) or value > UINT64_MAX:
        raise ValueError(f"invalid {label}")
    return value


def block_size(value):
    natural(value, "logical block size", positive=True)
    if value < 512 or value > 65536 or value & (value - 1):
        raise ValueError("unsupported logical block size")
    return value


def header(block, logical_bytes, capacity_bytes, expected_lba):
    """Validate one GPT header before trusting its entry-array address/length."""
    block_size(logical_bytes)
    natural(capacity_bytes, "capacity", positive=True)
    if capacity_bytes % logical_bytes or len(block) != logical_bytes:
        raise ValueError("unaligned capacity or truncated GPT header")
    total = capacity_bytes // logical_bytes
    if total < 6 or expected_lba not in (1, total - 1):
        raise ValueError("invalid GPT header location")
    fields = struct.unpack_from("<8sIIIIQQQQ16sQIII", block)
    (signature, revision, size, crc, reserved, current, other, first, last,
     disk_guid, entries_lba, count, entry_bytes, entries_crc) = fields
    if signature != b"EFI PART" or revision != 0x10000 or not 92 <= size <= logical_bytes or reserved:
        raise ValueError("unsupported GPT header")
    checked = bytearray(block[:size])
    checked[16:20] = bytes(4)
    if zlib.crc32(checked) & 0xffffffff != crc:
        raise ValueError("GPT header CRC mismatch")
    if current != expected_lba or other != (total - 1 if current == 1 else 1):
        raise ValueError("GPT reciprocal header location mismatch")
    if not 2 <= first <= last < total - 1:
        raise ValueError("invalid GPT usable range")
    if not 1 <= count <= 32768 or not 128 <= entry_bytes <= 4096 or entry_bytes % 128:
        raise ValueError("unsupported GPT entry shape")
    length = count * entry_bytes
    if length > MAX_ENTRY_BYTES:
        raise ValueError("GPT entry array exceeds bounded read")
    blocks = (length + logical_bytes - 1) // logical_bytes
    if current == 1:
        valid = 2 <= entries_lba and entries_lba + blocks <= first
    else:
        valid = last < entries_lba and entries_lba + blocks <= current
    if not valid:
        raise ValueError("GPT entry array overlaps usable blocks or header")
    return {"current_lba": current, "other_lba": other, "first_usable_lba": first,
            "last_usable_lba": last, "disk_guid": str(uuid.UUID(bytes_le=disk_guid)),
            # Dali's original primary AND backup use a zero disk GUID. Keep it
            # observable, but never use it instead of device/partition identity.
            "disk_guid_usable_as_identity": bool(any(disk_guid)),
            "entries_lba": entries_lba, "entry_count": count, "entry_bytes": entry_bytes,
            "entries_bytes": length, "entries_crc32": entries_crc,
            "header_sha256": hashlib.sha256(block).hexdigest()}


def entries(data, meta):
    if len(data) != meta["entries_bytes"] or zlib.crc32(data) & 0xffffffff != meta["entries_crc32"]:
        raise ValueError("GPT entry array length/CRC mismatch")
    data = bytes(data)
    used = []
    names, guids = set(), set()
    for number in range(meta["entry_count"]):
        raw = data[number * meta["entry_bytes"]:(number + 1) * meta["entry_bytes"]]
        if raw[:16] == bytes(16):
            continue
        type_guid = str(uuid.UUID(bytes_le=raw[:16]))
        guid = str(uuid.UUID(bytes_le=raw[16:32]))
        first, last, attributes = struct.unpack_from("<QQQ", raw, 32)
        if not meta["first_usable_lba"] <= first <= last <= meta["last_usable_lba"]:
            raise ValueError("GPT partition outside usable range")
        if raw[16:32] == bytes(16) or guid in guids:
            raise ValueError("missing or duplicate GPT partition GUID")
        try:
            name = raw[56:128].decode("utf-16-le").split("\0", 1)[0]
        except UnicodeError:
            raise ValueError("invalid GPT UTF-16 partition name") from None
        if not name or name in names:
            raise ValueError("missing or duplicate GPT partition name")
        names.add(name)
        guids.add(guid)
        used.append({"number": number + 1, "name": name, "type_guid": type_guid,
                     "guid": guid, "first_lba": first, "last_lba": last,
                     "blocks": last - first + 1, "attributes": attributes,
                     "gpt_read_only": bool(attributes & GPT_READ_ONLY)})
    ordered = sorted(used, key=lambda row: row["first_lba"])
    if any(left["last_lba"] >= right["first_lba"] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("overlapping GPT partitions")
    return used


def validate_gpt(primary_header, primary_entries, backup_header, backup_entries,
                 logical_bytes, capacity_bytes):
    """Require reciprocal headers and identical CRC-checked partition arrays."""
    first = header(primary_header, logical_bytes, capacity_bytes, 1)
    second = header(backup_header, logical_bytes, capacity_bytes, capacity_bytes // logical_bytes - 1)
    for key in ("first_usable_lba", "last_usable_lba", "disk_guid", "entry_count", "entry_bytes", "entries_crc32"):
        if first[key] != second[key]:
            raise ValueError("primary/backup GPT metadata mismatch")
    left, right = entries(primary_entries, first), entries(backup_entries, second)
    if primary_entries != backup_entries or left != right:
        raise ValueError("primary/backup GPT entries differ")
    return {"primary": first, "backup": second, "partitions": left,
            "entries_sha256": hashlib.sha256(primary_entries).hexdigest()}


def geometry(*, lun, logical_bytes, capacity_bytes, start_sectors_512, size_sectors_512,
             partition_number, sysfs_read_only, gpt, partition_guid=None):
    block_size(logical_bytes)
    natural(lun, "LUN")
    natural(partition_number, "partition number", positive=True)
    if lun >= 0x80:
        raise ValueError("well-known/unsupported LUN cannot back boot_b")
    natural(capacity_bytes, "capacity", positive=True)
    for value in (start_sectors_512, size_sectors_512):
        natural(value, "sysfs sector range", positive=True)
        if value > UINT64_MAX // 512:
            raise ValueError("sysfs sector conversion overflow")
    start, size = start_sectors_512 * 512, size_sectors_512 * 512
    if capacity_bytes % logical_bytes or start % logical_bytes or size % logical_bytes:
        raise ValueError("sysfs range is not aligned to actual UFS logical blocks")
    if start >= capacity_bytes or size > capacity_bytes - start:
        raise ValueError("boot_b exceeds LUN capacity")
    matches = [row for row in gpt["partitions"] if row["name"] == "boot_b"]
    if len(matches) != 1:
        raise ValueError("GPT must identify exactly one boot_b")
    part = matches[0]
    if (part["number"] != partition_number or part["first_lba"] != start // logical_bytes or
            part["blocks"] != size // logical_bytes):
        raise ValueError("sysfs/GPT boot_b range or partition number mismatch")
    if partition_guid is not None and part["guid"].lower() != partition_guid.lower():
        raise ValueError("sysfs/GPT boot_b GUID mismatch")
    if type(sysfs_read_only) is not bool:
        raise ValueError("missing sysfs write-protection observation")
    return {"lun": lun, "logical_block_bytes": logical_bytes, "capacity_bytes": capacity_bytes,
            "capacity_blocks": capacity_bytes // logical_bytes, "boot_b": dict(part, bytes=size),
            "sysfs_read_only": sysfs_read_only, "gpt_read_only": part["gpt_read_only"],
            "geometry_verified": True, "write_protection_verified": False,
            "dma_ownership_verified": False, "media_writes_qualified": False}


def scratch_window(candidate, verified_geometry):
    """Return a bounded proposal using actual LU geometry; never enable writes."""
    from unattended_layout import plan_scratch
    geo = verified_geometry
    if geo.get("geometry_verified") is not True:
        raise ValueError("verified geometry is required")
    logical = block_size(geo["logical_block_bytes"])
    part = geo["boot_b"]
    if (geo.get("sysfs_read_only") is not False or geo.get("gpt_read_only") is not False or
            geo.get("unit_write_protected", False)):
        raise ValueError("boot_b is write-protected")
    proposal = plan_scratch(candidate, part["bytes"])
    offset, length = proposal["scratch"]["offset_bytes"], proposal["scratch"]["bytes"]
    if offset % logical or length % logical:
        raise ValueError("scratch is not aligned to UFS logical blocks")
    absolute = part["first_lba"] + offset // logical
    blocks = length // logical
    if absolute < part["first_lba"] or absolute + blocks - 1 > part["last_lba"]:
        raise ValueError("scratch exceeds GPT boot_b")
    return dict(proposal, lun=geo["lun"], logical_block_bytes=logical,
                partition_guid=part["guid"], scratch_first_lba=absolute,
                scratch_blocks=blocks, write_authorized=False)


def unit_descriptor(geo, values):
    """Cross-check READ-only unit descriptor values against Android/GPT units.

    This observes Android's current state. Firmware/XNU must repeat the device
    queries after taking ownership; the observation grants no write permission.
    """
    required = ("lu_enable", "lun_write_protect", "logical_block_size", "logical_block_count")
    numbers = {}
    for key in required:
        value = values.get(key)
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ValueError("missing or invalid unit descriptor: " + key)
        try:
            numbers[key] = int(value, 16)
        except ValueError:
            raise ValueError("invalid unit descriptor: " + key) from None
        natural(numbers[key], key)
    exponent = numbers["logical_block_size"]
    if not 9 <= exponent <= 16 or 1 << exponent != geo["logical_block_bytes"]:
        raise ValueError("unit descriptor/sysfs block size mismatch")
    if numbers["lu_enable"] != 1 or numbers["logical_block_count"] != geo["capacity_blocks"]:
        raise ValueError("unit descriptor LU is disabled or capacity differs")
    if numbers["lun_write_protect"] not in (0, 1, 2):
        raise ValueError("unknown unit write-protection type")
    return dict(geo, unit_descriptor=numbers, write_protection_observed_android=True,
                unit_write_protected=numbers["lun_write_protect"] != 0)
