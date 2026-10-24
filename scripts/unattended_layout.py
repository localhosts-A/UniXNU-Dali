#!/usr/bin/env python3
"""Propose a boot_b scratch range offline; never contact or write a device.

This checks the currently supported unsigned boot-v4/legacy-LZ4 container.
It neither establishes DMA reachability nor fastboot's erase/preservation policy.
The returned proposal is deliberately not a device-write qualification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


PAGE = 4096
MIB = 1024 * 1024
SCRATCH_BYTES = MIB
TAIL_GUARD_BYTES = MIB
LEGACY_LZ4 = bytes.fromhex("02214c18")


def aligned(value):
    return (value + PAGE - 1) // PAGE * PAGE


def plan_scratch(image, partition_bytes):
    """Fail closed on unsupported metadata and return byte ranges, not LBAs."""
    if type(partition_bytes) is not int or partition_bytes <= 0 or partition_bytes % PAGE:
        raise ValueError("partition size must be a positive 4096-byte multiple")
    if len(image) < PAGE or len(image) > partition_bytes or len(image) % PAGE:
        raise ValueError("candidate is truncated or exceeds boot_b")
    if image[:8] != b"ANDROID!" or struct.unpack_from("<I", image, 40)[0] != 4:
        raise ValueError("only a raw Android boot-v4 image is supported")
    if struct.unpack_from("<I", image, 20)[0] != 1584:
        raise ValueError("unexpected boot-v4 header size")
    kernel_size, ramdisk_size = struct.unpack_from("<II", image, 8)
    if ramdisk_size or struct.unpack_from("<I", image, 1580)[0]:
        raise ValueError("external boot ramdisk or GKI signature needs separate range qualification")
    if any(image[1584:PAGE]):
        raise ValueError("unrecognized nonzero header-page padding")
    payload_end = PAGE + aligned(kernel_size)
    if kernel_size < 4 or payload_end > len(image):
        raise ValueError("declared kernel payload is truncated")
    if image[PAGE:PAGE + 4] != LEGACY_LZ4:
        raise ValueError("candidate does not use the observed legacy-LZ4 kernel wrapper")
    if image[-64:-60] == b"AVBf":
        raise ValueError("AVB footer/hash coverage has not been qualified for scratch writes")
    # The diagnostic packer currently emits only the header and padded kernel.
    # Reject any other metadata instead of treating an apparently empty tail as free.
    if any(image[PAGE + kernel_size:]):
        raise ValueError("unrecognized data outside the declared kernel payload")
    scratch_start = partition_bytes - TAIL_GUARD_BYTES - SCRATCH_BYTES
    if scratch_start < payload_end:
        raise ValueError("boot payload overlaps the proposed scratch reservation")
    return {
        "schema": 1,
        "kind": "offline-scratch-proposal",
        "partition": "boot_b",
        "partition_bytes": partition_bytes,
        "candidate_bytes": len(image),
        "candidate_sha256": hashlib.sha256(image).hexdigest(),
        "boot_payload_end_bytes": payload_end,
        "scratch": {"offset_bytes": scratch_start, "bytes": SCRATCH_BYTES,
                    "alignment_bytes": PAGE, "tail_guard_bytes": TAIL_GUARD_BYTES},
        "logical_block_bytes": None,
        "reader_switch_qualified": False,
        "media_writes_qualified": False,
        "device_access_performed": False,
    }


def verified_partition_size(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    if (manifest.get("complete") is not True or manifest.get("product") != "dali"
            or manifest.get("slot_suffix") != "_b"):
        raise ValueError("a complete Dali B-slot backup is required")
    entry = manifest["files"]["boot_b.img"]
    original = backup / "boot_b.img"
    if original.stat().st_size != entry["bytes"]:
        raise ValueError("original boot_b backup size mismatch")
    with original.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != entry["sha256"]:
        raise ValueError("original boot_b backup SHA-256 mismatch")
    return entry["bytes"], digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        partition_bytes, original_sha = verified_partition_size(args.backup)
        report = plan_scratch(args.candidate.read_bytes(), partition_bytes)
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))
    report["original_boot_b_sha256"] = original_sha
    output = json.dumps(report, indent=2) + "\n"
    if args.output:
        # Do not allow the report path to overwrite any input or existing evidence.
        if args.output.resolve() in {args.candidate.resolve(),
                                    (args.backup / "boot_b.img").resolve(),
                                    (args.backup / "manifest.json").resolve()}:
            parser.error("output cannot overwrite an input")
        with args.output.open("x") as target:
            target.write(output)
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
