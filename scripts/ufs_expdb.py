"""Offline expdb test-window validation; no device operations or live grants.

Zero bytes are a required precondition for the diagnostic's at-most-once record
format, not evidence of vendor-reserved free space. Every original byte remains
covered by the full backup hash and the restore record.
"""
import hashlib
import os
from pathlib import Path
import stat

import ufs_geometry as ufs

MIB = 1024 * 1024
MAX_PARTITION_BYTES = 512 * MIB


def _read(root, filename, size):
    if type(size) is not int or not 0 < size <= MAX_PARTITION_BYTES:
        raise ValueError("invalid expdb evidence length")
    with os.fdopen(os.open(root / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError("expdb evidence must be a complete regular file")
        data = source.read(size + 1)
    if len(data) != size:
        raise ValueError("expdb evidence changed length while reading")
    return data


def validate_expdb_backup(baseline, directory, *, serial, geometry, reference_gpt, original_boot_sha):
    """Re-read full backup and reciprocal GPT files before shaping an expdb grant.

    geometry/identity come from the independently captured Android UFS baseline.
    Runtime firmware/XNU must repeat device identity, geometry, WP and GPT checks.
    These offline inputs cannot establish ownership, freshness or export support.
    """
    if (not isinstance(baseline, dict) or baseline.get("kind") != "dali-expdb-readonly-baseline" or
            baseline.get("serial") != serial or baseline.get("media_writes_performed") is not False or
            baseline.get("device_reboot_performed") is not False):
        raise ValueError("matching read-only expdb baseline is required")
    android = baseline.get("android", {})
    if android.get("product") != "dali" or android.get("slot_suffix") != "_b" or not android.get("root", "").startswith("uid=0("):
        raise ValueError("expdb baseline must come from original rooted Android B")
    before, after = baseline.get("all18_before"), baseline.get("all18_after")
    if (not isinstance(before, dict) or len(before) != 18 or before != after or
            before.get("boot_b") != original_boot_sha):
        raise ValueError("expdb baseline lacks matching original partition hashes")
    block = ufs.block_size(baseline.get("logical_block_bytes"))
    capacity = ufs.natural(baseline.get("capacity_bytes"), "expdb LU capacity", positive=True)
    if (baseline.get("lun") != geometry.get("lun") or block != geometry.get("logical_block_bytes") or
            capacity != geometry.get("capacity_bytes") or type(baseline.get("blockdev_read_only")) is not int or
            baseline["blockdev_read_only"] != 0):
        raise ValueError("expdb baseline differs from UFS geometry or is read-only")
    if not isinstance(directory, (str, os.PathLike)):
        raise ValueError("expdb backup directory is required")
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid expdb backup directory")
    captures = baseline.get("captures", {})
    raw = {}
    for name in ("primary-header.bin", "primary-entries.bin", "backup-header.bin", "backup-entries.bin"):
        record = captures.get(name, {})
        size = record.get("bytes")
        if type(size) is not int or size > ufs.MAX_ENTRY_BYTES or ("header" in name and size != block):
            raise ValueError("invalid expdb GPT capture size")
        raw[name] = _read(root, name, size)
        if hashlib.sha256(raw[name]).hexdigest() != record.get("sha256"):
            raise ValueError("expdb GPT capture hash mismatch")
    gpt = ufs.validate_gpt(raw["primary-header.bin"], raw["primary-entries.bin"],
                           raw["backup-header.bin"], raw["backup-entries.bin"], block, capacity)
    if gpt != baseline.get("gpt") or gpt != reference_gpt:
        raise ValueError("expdb raw GPT differs from either baseline")
    matches = [part for part in gpt["partitions"] if part["name"] == "expdb"]
    if len(matches) != 1 or matches[0] != baseline.get("partition") or matches[0]["gpt_read_only"]:
        raise ValueError("expdb partition identity or GPT write protection mismatch")
    part = matches[0]
    size = ufs.natural(baseline.get("bytes"), "expdb backup length", positive=True)
    if size != part["blocks"] * block or size < 6 * MIB or size > MAX_PARTITION_BYTES:
        raise ValueError("expdb backup size differs from partition or lacks guards")
    data = _read(root, "expdb.img", size)
    digest = hashlib.sha256(data).hexdigest()
    if any(baseline.get(key) != digest for key in ("sha256", "device_sha256_before", "device_sha256_after")):
        raise ValueError("expdb backup does not match both device observations")
    scratch = data[MIB:2 * MIB]
    if any(scratch):
        raise ValueError("expdb test window is not zero in original backup")
    return {
        "partition": dict(part, bytes=size), "partition_bytes": size,
        "scratch": {"offset_bytes": MIB, "bytes": MIB, "alignment_bytes": block,
                    "tail_guard_bytes": size - 2 * MIB},
        "original_partition_sha256": digest,
        "original_scratch_sha256": hashlib.sha256(scratch).hexdigest(),
        "original_head_sha256": hashlib.sha256(data[:MIB]).hexdigest(),
        "original_after_sha256": hashlib.sha256(data[2 * MIB:]).hexdigest(),
        "free_space_proven": False,
    }
