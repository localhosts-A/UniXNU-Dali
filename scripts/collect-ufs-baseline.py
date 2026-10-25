#!/usr/bin/env python3
"""Capture Dali UFS geometry/GPT from rooted Android using read-only operations.

Requires the existing complete original backup, Android B and all 18 original
partition hashes before and after capture. No reboot, flash, SG_IO, MMIO write,
sysfs write or controller initialization is performed. Output stays private.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex

import ufs_geometry as ufs

DEVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ufs_cycles", DEVICE / "scripts/run-bootb-cycles.py")
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


def collect(serial, backup, output):
    workspace = DEVICE.parent
    repositories = [path for path in workspace.iterdir() if (path / ".git").exists()]
    output = cycle.require_private_path(output, repositories, "UFS baseline")
    backup = cycle.require_private_path(backup, repositories, "backup")
    if output.exists() or output == backup or output in backup.parents or backup in output.parents:
        raise cycle.CycleError("output must be a new private directory separate from backup")
    before_android = cycle.current_android(serial)
    manifest, records = cycle.verify_backup(backup, serial)
    before_hashes = cycle.compare_partition_hashes(serial, records)
    output.mkdir(parents=True, mode=0o700)
    os.chmod(output, 0o700)

    def text(command):
        return cycle.adb_output(serial, command).decode("utf-8").strip()

    def integer(path):
        value = text("cat " + shlex.quote(path))
        if not re.fullmatch(r"[0-9]+", value):
            raise cycle.CycleError("invalid numeric sysfs read: " + path)
        return int(value)

    def save(name, data):
        path = output / name
        with path.open("xb") as target:
            os.chmod(path, 0o600)
            target.write(data)
        return {"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    node = text("readlink -f /dev/block/by-name/boot_b")
    match = re.fullmatch(r"/dev/block/(sd[a-z]+)([1-9][0-9]*)", node)
    if not match:
        raise cycle.CycleError("boot_b is not a direct SCSI disk partition")
    disk, number = match.group(1), int(match.group(2))
    part = disk + str(number)
    parent = "/sys/class/block/" + disk
    part_sys = "/sys/class/block/" + part
    scsi = text("readlink -f " + parent + "/device")
    address = re.fullmatch(r"(.*/([0-9]+):([0-9]+):([0-9]+):([0-9]+))", scsi)
    if not address or "/16810000.ufshci/" not in scsi:
        raise cycle.CycleError("boot_b parent is not the expected Dali UFS controller")
    logical = ufs.block_size(integer(parent + "/queue/logical_block_size"))
    capacity = integer(parent + "/size") * 512
    ufs.natural(capacity, "LUN capacity", positive=True)
    if capacity % logical:
        raise cycle.CycleError("parent capacity is not a multiple of logical blocks")

    def read_blocks(lba, blocks):
        if (type(lba) is not int or type(blocks) is not int or lba < 0 or blocks <= 0 or
                blocks * logical > ufs.MAX_ENTRY_BYTES + logical or lba + blocks > capacity // logical):
            raise cycle.CycleError("raw GPT read exceeds bounded LUN range")
        command = f"dd if=/dev/block/{disk} bs={logical} skip={lba} count={blocks} 2>/dev/null"
        data = cycle.adb_output(serial, command)
        if len(data) != blocks * logical:
            raise cycle.CycleError("truncated raw GPT/block read")
        return data

    captures = {}
    raw = []
    for label, lba in (("primary", 1), ("backup", capacity // logical - 1)):
        head = read_blocks(lba, 1)
        captures[label + "_header"] = save(label + "-header.bin", head)
        meta = ufs.header(head, logical, capacity, lba)
        table = read_blocks(meta["entries_lba"], (meta["entries_bytes"] + logical - 1) // logical)
        table = table[:meta["entries_bytes"]]
        captures[label + "_entries"] = save(label + "-entries.bin", table)
        raw.extend((head, table))
    gpt = ufs.validate_gpt(*raw, logical, capacity)
    events = dict(line.split("=", 1) for line in text("cat " + part_sys + "/uevent").splitlines() if "=" in line)
    ro = integer(part_sys + "/ro")
    if ro not in (0, 1):
        raise cycle.CycleError("unknown partition read-only state")
    geo = ufs.geometry(lun=int(address.group(5)), logical_bytes=logical, capacity_bytes=capacity,
                       start_sectors_512=integer(part_sys + "/start"),
                       size_sectors_512=integer(part_sys + "/size"), partition_number=number,
                       sysfs_read_only=bool(ro), gpt=gpt, partition_guid=events.get("PARTUUID"))
    descriptors = {key: text("cat " + shlex.quote(parent + "/device/unit_descriptor/" + key))
                   for key in ("lu_enable", "lun_write_protect", "logical_block_size", "logical_block_count")}
    geo = ufs.unit_descriptor(geo, descriptors)
    if geo["boot_b"]["bytes"] != records["boot_b"]["bytes"]:
        raise cycle.CycleError("GPT boot_b size differs from original backup")
    sample = read_blocks(geo["boot_b"]["first_lba"], 1)
    with (backup / "boot_b.img").open("rb") as source:
        expected = source.read(logical)
    if sample != expected:
        raise cycle.CycleError("parent-LUN boot_b sample differs from original backup")
    captures["boot_b_first_block"] = save("boot_b-first-block.bin", sample)
    fdt = cycle.adb_output(serial, "cat /sys/firmware/fdt")
    captures["live_fdt"] = save("runtime.dtb", fdt)
    controller = scsi.split("/host", 1)[0]
    metadata = {}
    for name, path in {
        "write_cache": parent + "/queue/write_cache",
        "physical_block_bytes": parent + "/queue/physical_block_size",
        "driver": controller + "/driver",
        "iommu_group": controller + "/iommu_group",
    }.items():
        if name in ("driver", "iommu_group"):
            command = "if [ -L " + shlex.quote(path) + " ]; then readlink -f " + shlex.quote(path) + "; fi"
        else:
            command = "cat " + shlex.quote(path)
        metadata[name] = {"path": path, "value": text(command) or None}
    after_android = cycle.current_android(serial)
    after_hashes = cycle.compare_partition_hashes(serial, records)
    if before_hashes != after_hashes or before_android != after_android:
        raise cycle.CycleError("Android identity or protected media changed during capture")
    report = {"schema": 1, "kind": "dali-ufs-android-read-only-baseline",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "serial": serial, "android": after_android, "backup": str(backup),
              "original_boot_b_sha256": records["boot_b"]["sha256"],
              "device_node": node, "parent_node": "/dev/block/" + disk,
              "scsi_path": scsi, "geometry": geo, "gpt": gpt,
              "captures": captures, "metadata": metadata, "unit_descriptor_raw": descriptors,
              "partition_hashes": after_hashes, "all_partition_hashes_match": True,
              "known_block_matches_backup": True, "device_writes_performed": False,
              "device_reboot_performed": False, "ufs_controller_qualified": False}
    cycle.write_json(output / "baseline.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = collect(args.serial, args.backup, args.output)
    except (cycle.CycleError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"UFS baseline refused: {error}\n")
    geo = report["geometry"]
    print(f"PASS: read-only GPT/sysfs/backup match; LUN={geo['lun']} block={geo['logical_block_bytes']} "
          f"boot_b={geo['boot_b']['first_lba']}+{geo['boot_b']['blocks']}; no controller/write qualification")


if __name__ == "__main__":
    main()
