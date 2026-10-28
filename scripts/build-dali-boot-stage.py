#!/usr/bin/env python3
"""Compose a verified Dali root image with a separately pinned FAT boot region.

The SHNGROOT image remains unchanged and contains only the signed OS and
SystemOS windows. The read-only Boot FAT image is placed after its declared
image_bytes, aligned to 1 MiB, in the same unixnu_data partition. The emitted
144-byte descriptor is intended to be embedded in the matching firmware FV.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
MIB = 1 << 20
CHUNK = 8 * MIB
MAX_BOOT_STAGE_BYTES = 512 * MIB
DESCRIPTOR_MAGIC = b"DALIBOOT"
DESCRIPTOR_VERSION = 1
DESCRIPTOR_PREFIX = struct.Struct("<8sIIQQQQQQII32s32s")
DESCRIPTOR_BYTES = DESCRIPTOR_PREFIX.size + 32


def root_builder():
    path = SCRIPT_ROOT / "scripts/build-dali-verified-root.py"
    spec = importlib.util.spec_from_file_location("dali_verified_root_for_boot_stage", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load Dali VerifiedRoot image verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    unixnu = module.unixnu_root()
    verify_built_image, _ = module.import_userdata(unixnu)
    return module, verify_built_image, module.profile_constants()


def file_digest(path):
    digest = hashlib.sha256()
    total = 0
    with path.open("rb", buffering=0) as stream:
        while True:
            data = stream.read(CHUNK)
            if not data:
                break
            digest.update(data)
            total += len(data)
    return total, digest.digest()


def validate_fat_image(path):
    size = path.stat().st_size
    if size < 512 or size > MAX_BOOT_STAGE_BYTES or size % 512:
        raise ValueError("Boot FAT image must be 512-byte aligned and at most 512 MiB")
    with path.open("rb", buffering=0) as stream:
        bpb = stream.read(512)
    if len(bpb) != 512 or bpb[510:512] != b"\x55\xaa":
        raise ValueError("Boot stage has no complete FAT boot sector")
    sector_size = struct.unpack_from("<H", bpb, 11)[0]
    total_sectors = struct.unpack_from("<H", bpb, 19)[0]
    if total_sectors == 0:
        total_sectors = struct.unpack_from("<I", bpb, 32)[0]
    if sector_size != 512 or total_sectors * sector_size != size:
        raise ValueError("Boot FAT BPB must describe the complete 512-byte-sector image")
    return size


def descriptor_bytes(root_bytes, boot_offset, boot_bytes, capacity, first_lba, last_lba,
                     lun, block_size, root_manifest_sha, boot_sha):
    if len(root_manifest_sha) != 32 or len(boot_sha) != 32:
        raise ValueError("Boot descriptor hashes must be SHA-256 digests")
    prefix = DESCRIPTOR_PREFIX.pack(
        DESCRIPTOR_MAGIC,
        DESCRIPTOR_VERSION,
        DESCRIPTOR_BYTES,
        root_bytes,
        boot_offset,
        boot_bytes,
        capacity,
        first_lba,
        last_lba,
        lun,
        block_size,
        root_manifest_sha,
        boot_sha,
    )
    result = prefix + hashlib.sha256(prefix).digest()
    if len(result) != DESCRIPTOR_BYTES:
        raise AssertionError("Boot descriptor layout size changed unexpectedly")
    return result


def copy_exact(source, destination, size):
    copied = 0
    while copied < size:
        amount = min(CHUNK, size - copied)
        data = source.read(amount)
        if len(data) != amount:
            raise ValueError(f"short input while copying at byte {copied}")
        if destination.write(data) != amount:
            raise ValueError(f"short output while copying at byte {copied}")
        copied += amount


def validate_boot_report(boot_report, boot_bytes, boot_sha):
    try:
        record = json.loads(Path(boot_report).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("cannot read Boot FAT build report") from error
    if (not isinstance(record, dict) or record.get("bytes") != boot_bytes or
            record.get("sha256") != boot_sha.hex() or record.get("readonly") is not True or
            record.get("physical_storage") is not False):
        raise ValueError("Boot FAT report does not bind the exact read-only image")
    files = record.get("files")
    if not isinstance(files, dict):
        raise ValueError("Boot FAT report has no staged-file inventory")
    for name in ("EFI/UniXNU/unixnu.toml", "EFI/UniXNU/dali-boot.conf"):
        item = files.get(name)
        if not isinstance(item, dict) or not isinstance(item.get("bytes"), int) or item["bytes"] <= 0:
            raise ValueError("Boot FAT report lacks the firmware boot configuration members")
    recovery = record.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("auto_fastboot") is not False:
        raise ValueError("Boot FAT must disable automatic fastboot recovery")
    for name, field in (("EFI/UniXNU/Payloads/kernel.macho", "xnu_kernel_sha256"),
                        ("EFI/UniXNU/Payloads/kernel.macho.adt", "xnu_adt_sha256")):
        item = files.get(name)
        if (not isinstance(item, dict) or not isinstance(item.get("bytes"), int) or
                item["bytes"] <= 0 or not isinstance(item.get("sha256"), str) or
                len(item["sha256"]) != 64 or record.get(field) != item["sha256"]):
            raise ValueError("Boot FAT report does not bind its kernel and Apple DT members")
    if record.get("xnu_trust_cache_sha256") is not None:
        item = files.get("EFI/UniXNU/Payloads/kernel.macho.trustcache")
        if not isinstance(item, dict) or item.get("sha256") != record["xnu_trust_cache_sha256"]:
            raise ValueError("Boot FAT trust-cache hash differs from its staged member")
    if record.get("xnu_ramdisk_sha256") is not None:
        item = files.get("EFI/UniXNU/Payloads/root.img")
        if not isinstance(item, dict) or item.get("sha256") != record["xnu_ramdisk_sha256"]:
            raise ValueError("Boot FAT ramdisk hash differs from its staged member")
    return hashlib.sha256(Path(boot_report).read_bytes()).hexdigest()


def compose(root_image, root_report, boot_image, boot_report, output, descriptor, report):
    root_image = Path(root_image)
    root_report = Path(root_report)
    boot_image = Path(boot_image)
    output = Path(output)
    descriptor = Path(descriptor)
    report = Path(report)
    boot_report = Path(boot_report)
    if not all(path.is_file() for path in (root_image, root_report, boot_image, boot_report)):
        raise ValueError("root image/report and Boot FAT image/report must all exist")
    inputs = {root_image.resolve(), root_report.resolve(), boot_image.resolve(), boot_report.resolve()}
    if output.resolve() in inputs or descriptor.resolve() in inputs:
        raise ValueError("generated outputs must not overwrite their inputs")
    if output.resolve() == descriptor.resolve() or output.resolve() == report.resolve() or descriptor.resolve() == report.resolve():
        raise ValueError("partition image, descriptor, and report must use distinct paths")

    module, verify_built_image, profile = root_builder()
    root_info, entries, root_digest = module.load_verified_container(
        root_image, root_report, verify_built_image, profile)
    root_bytes = root_image.stat().st_size
    capacity = (profile["last_lba"] - profile["first_lba"] + 1) * 4096
    boot_bytes = validate_fat_image(boot_image)
    boot_offset = (root_bytes + MIB - 1) & ~(MIB - 1)
    if boot_offset < root_bytes or boot_bytes > capacity - boot_offset:
        raise ValueError("Boot FAT region exceeds the measured unixnu_data partition")
    if boot_offset % 4096 or boot_bytes % 4096:
        raise ValueError("Boot stage bounds must align to the measured 4096-byte UFS blocks")
    with root_image.open("rb", buffering=0) as stream:
        root_manifest = stream.read(4096)
    if len(root_manifest) != 4096:
        raise ValueError("SHNGROOT manifest block is truncated")
    root_manifest_sha = hashlib.sha256(root_manifest).digest()
    boot_file_bytes, boot_sha = file_digest(boot_image)
    if boot_file_bytes != boot_bytes:
        raise ValueError("Boot FAT image changed during validation")
    boot_report_sha = validate_boot_report(boot_report, boot_bytes, boot_sha)

    descriptor_data = descriptor_bytes(
        root_bytes, boot_offset, boot_bytes, capacity,
        profile["first_lba"], profile["last_lba"], profile["lun"], 4096,
        root_manifest_sha, boot_sha)
    image_bytes = boot_offset + boot_bytes
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb", buffering=0) as target:
        target.truncate(image_bytes)
        target.seek(0)
        with root_image.open("rb", buffering=0) as source:
            copy_exact(source, target, root_bytes)
            if source.read(1):
                raise ValueError("root image grew while composing the partition image")
        target.seek(boot_offset)
        with boot_image.open("rb", buffering=0) as source:
            copy_exact(source, target, boot_bytes)
            if source.read(1):
                raise ValueError("Boot FAT image grew while composing the partition image")
        target.flush()

    actual_bytes, image_sha = file_digest(output)
    if actual_bytes != image_bytes:
        raise ValueError("composite partition image has an unexpected length")
    with output.open("rb", buffering=0) as stream:
        root_sha = hashlib.sha256()
        remaining = root_bytes
        while remaining:
            data = stream.read(min(CHUNK, remaining))
            if not data:
                raise ValueError("composite SHNGROOT image is truncated")
            root_sha.update(data)
            remaining -= len(data)
        if root_sha.hexdigest() != root_digest:
            raise ValueError("composite SHNGROOT bytes differ from the verified root image")
        gap = boot_offset - root_bytes
        while gap:
            data = stream.read(min(CHUNK, gap))
            if not data or any(data):
                raise ValueError("composite alignment gap is short or nonzero")
            gap -= len(data)
        stream.seek(boot_offset)
        actual_boot_sha = hashlib.sha256()
        remaining = boot_bytes
        while remaining:
            data = stream.read(min(CHUNK, remaining))
            if not data:
                raise ValueError("composite Boot FAT region is truncated")
            actual_boot_sha.update(data)
            remaining -= len(data)
    if actual_boot_sha.digest() != boot_sha:
        raise ValueError("composite Boot FAT bytes differ from the pinned source image")

    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_bytes(descriptor_data)
    report.parent.mkdir(parents=True, exist_ok=True)
    report_data = {
        "partition_name": root_info["partition_name"],
        "lun": root_info["lun"],
        "first_lba": profile["first_lba"],
        "last_lba": profile["last_lba"],
        "block_size": 4096,
        "partition_bytes": capacity,
        "root_image_bytes": root_bytes,
        "root_image_sha256": root_digest,
        "root_manifest_sha256": root_manifest_sha.hex(),
        "root_windows": [entry[0] for entry in entries],
        "boot_stage_offset_bytes": boot_offset,
        "boot_stage_bytes": boot_bytes,
        "boot_stage_sha256": boot_sha.hex(),
        "boot_package_report_sha256": boot_report_sha,
        "boot_sector_bytes": 512,
        "firmware_descriptor_bytes": len(descriptor_data),
        "firmware_descriptor_sha256": hashlib.sha256(descriptor_data).hexdigest(),
        "partition_image_bytes": image_bytes,
        "partition_image_sha256": image_sha.hex(),
        "physical_ufs_qualified": False,
        "hardware_tested": False,
    }
    report.write_text(json.dumps(report_data, indent=2) + "\n")
    return report_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-image", type=Path, required=True)
    parser.add_argument("--root-report", type=Path, required=True)
    parser.add_argument("--boot-fat", type=Path, required=True)
    parser.add_argument("--boot-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compose(args.root_image, args.root_report, args.boot_fat, args.boot_report,
                         args.output, args.descriptor, args.report)
    except Exception as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
