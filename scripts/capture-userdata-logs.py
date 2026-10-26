#!/usr/bin/env python3
"""Read and seal the exact 32 MiB userdata log partition from rooted Android."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
DEVICE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
from ufs_expdb_restore import load_restore_baseline
from ufs_userdata_android import AndroidUserdataLogs
from ufs_userdata_logs import decode_partition, validate_baseline_captures

spec = importlib.util.spec_from_file_location("userdata_capture_probe", SCRIPTS / "run-ufs-probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
cycle = probe.cycle


def capture(serial, backup, baseline_dir, output):
    repos = (DEVICE, DEVICE.parent / "UEFI-Dali", DEVICE.parent / "UniXNU")
    backup = cycle.require_private_path(Path(backup), repos, "backup")
    baseline_dir = cycle.require_private_path(Path(baseline_dir), repos, "baseline")
    output = cycle.require_private_path(Path(output), repos, "output")
    if output.exists() or any(probe.paths_overlap(output, path) for path in (backup, baseline_dir)):
        raise cycle.CycleError("output must be a new directory separate from backup and baseline")
    _, records = cycle.verify_backup(backup, serial)
    baseline = load_restore_baseline(baseline_dir, serial=serial, original_boot_b=backup / "boot_b.img")
    validate_baseline_captures(baseline, baseline_dir, backup / "boot_b.img")
    io = AndroidUserdataLogs(cycle, serial, records, baseline)
    before_layout = io.observe()
    before = io.read_partition()
    after_layout = io.observe()
    after = io.read_partition()
    if before != after or before_layout != after_layout:
        raise cycle.CycleError("Android UFS identity/GPT/log partition changed during the read-only capture")
    decoded = decode_partition(after)
    output.mkdir(parents=True, mode=0o700)
    image_path = output / "unixnu_logs.img"
    with image_path.open("xb") as target:
        os.chmod(image_path, 0o400)
        target.write(after); target.flush(); os.fsync(target.fileno())
    report = dict(schema=1, kind="dali-userdata-logs-read-only-capture", serial=serial,
                  baseline_sha256=baseline["baseline_sha256"], layout=after_layout,
                  first_sha256=hashlib.sha256(before).hexdigest(),
                  second_sha256=hashlib.sha256(after).hexdigest(), bytes=len(after),
                  decoded_entries=decoded["entries"], device_writes_performed=False,
                  device_reboot_performed=False, media_writes_qualified=False,
                  persistence_qualified=False)
    report_path = output / "capture.json"
    with report_path.open("x") as target:
        os.chmod(report_path, 0o400); json.dump(report, target, indent=2, sort_keys=True)
        target.write("\n"); target.flush(); os.fsync(target.fileno())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = capture(args.serial, args.backup, args.baseline_dir, args.output)
    except (cycle.CycleError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, "userdata log capture refused: " + str(error) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
