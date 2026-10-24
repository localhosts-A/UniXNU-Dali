#!/usr/bin/env python3
"""Record/verify an exact-input source-built kernel; all QEMU gates still run."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess


spec = importlib.util.spec_from_file_location("artifact", Path(__file__).with_name("make-kernel-artifact.py"))
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)
KERNEL = "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837.unstripped.noctf"


def snapshot(tree, key):
    data = artifact.validate_kernel(tree / KERNEL)
    revision = subprocess.check_output(["git", "-C", str(tree / ".cache/xnu-work"),
                                        "rev-parse", "HEAD"], text=True).strip()
    return {"schema": 1, "input_key": key, "xnu_commit": revision,
            "kernel_sha256": hashlib.sha256(data).hexdigest(), "kernel_bytes": len(data)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "verify"))
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    if len(args.key) != 64 or any(ch not in "0123456789abcdef" for ch in args.key):
        parser.error("input key must be a SHA-256 fingerprint")
    stamp = args.tree / ".cache/source-kernel-cache.json"
    current = snapshot(args.tree, args.key)
    if args.mode == "record":
        stamp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    elif json.loads(stamp.read_text()) != current:
        raise SystemExit("cached source kernel does not match its exact-input stamp")
    print(f"source kernel cache {args.mode}: {current['kernel_sha256']}")


if __name__ == "__main__":
    main()
