#!/usr/bin/env python3
"""Validate and stage the short-lived source-only Dali XNU artifact."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tomllib


def git_head(path):
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def git_clean(path):
    status = subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"], text=True)
    if status.strip():
        raise SystemExit(f"repository must be clean before producing a public artifact: {path}\n{status}")


def validate_kernel(path):
    data = path.read_bytes()
    if len(data) < 32:
        raise SystemExit("source XNU Mach-O is truncated")
    magic, cpu, _, filetype, count, command_bytes, _, _ = struct.unpack_from("<8I", data)
    if (magic, cpu, filetype) != (0xFEEDFACF, 0x0100000C, 2) or count == 0 or 32 + command_bytes > len(data):
        raise SystemExit("source XNU must be a complete arm64 MH_EXECUTE Mach-O")
    offset = 32
    end = 32 + command_bytes
    for _ in range(count):
        if offset + 8 > end:
            raise SystemExit("Mach-O load-command table is truncated")
        _, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > end:
            raise SystemExit("Mach-O has an invalid load-command size")
        offset += size
    if offset != end:
        raise SystemExit("Mach-O load-command table length does not match the header")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--unixnu", required=True, type=Path)
    parser.add_argument("--device-repo", required=True, type=Path)
    parser.add_argument("--auto-fastboot", choices=("true", "false"), default="true")
    parser.add_argument("--fastboot-timeout-seconds", type=int, default=120)
    parser.add_argument("--watchdog-timeout-seconds", type=int, default=31)
    args = parser.parse_args()
    if not 2 <= args.watchdog_timeout_seconds <= 31:
        parser.error("MT6991 watchdog timeout must be between 2 and 31 seconds")
    if not args.watchdog_timeout_seconds + 30 <= args.fastboot_timeout_seconds <= 600:
        parser.error("host fastboot timeout must exceed the device watchdog by at least 30 seconds")
    args.kernel = args.kernel.resolve()
    args.unixnu = args.unixnu.resolve()
    args.device_repo = args.device_repo.resolve()
    tree = args.unixnu / "out/dali/tree"
    xnu_work = tree / ".cache/xnu-work"
    if not (xnu_work / ".git").is_dir():
        raise SystemExit("XNU source checkout is missing from the completed build tree")
    for path in (args.unixnu, args.device_repo):
        git_clean(path)
    board = tomllib.loads((args.device_repo / "board.toml").read_text())
    expected_unixnu = board.get("firmware", {}).get("unixnu_commit")
    if not expected_unixnu or git_head(args.unixnu) != expected_unixnu:
        raise SystemExit("checked out UniXNU revision differs from board.toml firmware.unixnu_commit")
    expected_uefi = board.get("firmware", {}).get("uefi_commit")
    if not expected_uefi or len(expected_uefi) != 40 or any(ch not in "0123456789abcdef" for ch in expected_uefi):
        raise SystemExit("board.toml must pin a full UEFI-Dali firmware commit")
    feeds = tomllib.loads((args.unixnu / "feeds.toml").read_text())
    xnu_source = next((entry for entry in feeds.get("source", []) if entry.get("name") == "xnu"), None)
    if xnu_source is None or not xnu_source.get("commit"):
        raise SystemExit("feeds.toml must pin the source XNU commit")
    if git_head(xnu_work) != xnu_source["commit"]:
        raise SystemExit("built XNU source revision differs from feeds.toml")
    config = tomllib.loads((args.device_repo / "package/unixnu.toml").read_text())
    xnu_entry = next((entry for entry in config.get("entry", []) if entry.get("id") == "xnu"), None)
    if not xnu_entry or "dali-earlyboot=1" not in xnu_entry.get("args", ""):
        raise SystemExit("Dali XNU entry must pass the boot-argument checkpoint marker")
    data = validate_kernel(args.kernel)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "kernel.macho"
    shutil.copyfile(args.kernel, output)
    digest = hashlib.sha256(data).hexdigest()
    record = {
        "schema": 1,
        "artifact_name": "dali-xnu-source",
        "target": "dali",
        "source_only": True,
        "kernel_file": output.name,
        "kernel_bytes": len(data),
        "kernel_sha256": digest,
        "device_commit": git_head(args.device_repo),
        "unixnu_commit": git_head(args.unixnu),
        "uefi_commit": expected_uefi,
        "xnu_commit": git_head(xnu_work),
        "xnu_darwin": xnu_source.get("darwin"),
        "xnu_build": xnu_source.get("version"),
        "boot_args": xnu_entry.get("args", ""),
        "retention_days": 1,
        "recovery": {
            "auto_fastboot": args.auto_fastboot == "true",
            "fastboot_timeout_seconds": args.fastboot_timeout_seconds,
            "device_watchdog_timeout_seconds": args.watchdog_timeout_seconds,
        },
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
