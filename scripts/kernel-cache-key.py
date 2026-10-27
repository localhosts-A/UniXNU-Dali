#!/usr/bin/env python3
"""Fingerprint kernel inputs and the exact source pins compiled into Dali UFS."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib


EFI_ONLY = frozenset({
    "README.md", "drivers/DarwinBoot", "scripts/build-darwinboot.sh",
    "scripts/test-darwinboot.py", "scripts/test-darwinboot-entry.py",
})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def board_fingerprint(path: Path) -> str:
    with path.open("rb") as source:
        board = tomllib.load(source)
    # These revisions are now actual compiled source-identity inputs.
    return digest(board)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def source_records(root, excluded=frozenset(), *, board=False):
    """Include unknown tracked inputs and recurse into every other gitlink.

    Refuse dirty, missing or mismatched checkouts rather than silently hashing
    a gitlink while compiling different files. CI runs this before generating
    output files. Only the explicitly audited EFI paths are excluded.
    """
    if git(root, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError(f"cache fingerprint requires a clean checkout: {root}")
    result = []
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        name = raw_path.decode()
        if name in excluded:
            continue
        if stage != "0":
            raise ValueError(f"unmerged cache input: {root / name}")
        path = root / name
        if mode == "160000":
            if git(path, "rev-parse", "HEAD").strip().decode() != oid:
                raise ValueError(f"cache input gitlink differs from checkout: {path}")
            result.append((name, mode, oid, source_records(path)))
        elif board and name == "board.toml":
            result.append((name, mode, board_fingerprint(path)))
        else:
            data = path.readlink().as_posix().encode() if path.is_symlink() else path.read_bytes()
            result.append((name, mode, hashlib.sha256(data).hexdigest()))
    return result


def fingerprints(unixnu, device, toolchain, profile="default"):
    generic = source_records(unixnu, EFI_ONLY)
    target = source_records(device, frozenset({"README.md"}), board=True)
    # Feeds contain pinned sources and compiled EDK BaseTools, so their separate
    # cache also includes the toolchain, but does not depend on kernel patches.
    feed_inputs = [(str(path.relative_to(unixnu)), path.read_bytes().hex()) for path in
                   (unixnu / "feeds.toml", unixnu / "scripts/unixnu/feeds.py")]
    if (device / "feeds.toml").exists():
        feed_inputs.append(("device/feeds.toml", (device / "feeds.toml").read_bytes().hex()))
    revisions = [git(root, "rev-parse", "HEAD").decode().strip() for root in (unixnu, device)]
    return {"kernel": digest([generic, target, revisions, toolchain, profile]),
            "feeds": digest([feed_inputs, toolchain]),
            "edk": digest([git(unixnu, "rev-parse", "HEAD").decode().strip(), target, toolchain])}


def toolchain_identity():
    commands = [
        ["sw_vers"], ["uname", "-m"], ["xcodebuild", "-version"],
        ["xcrun", "--sdk", "iphoneos", "--show-sdk-version"],
        ["xcrun", "--sdk", "iphoneos", "--show-sdk-build-version"],
        ["xcrun", "--sdk", "iphoneos", "clang", "--version"],
        ["clang", "--version"], ["ld.lld", "--version"], ["llvm-ar", "--version"],
        ["make", "--version"], ["cmake", "--version"], ["ninja", "--version"],
        ["unifdef", "-V"], ["python3", "--version"],
    ]
    return [(command, subprocess.check_output(command, stderr=subprocess.STDOUT, text=True).strip())
            for command in commands]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unixnu", required=True, type=Path)
    parser.add_argument("--device", required=True, type=Path)
    parser.add_argument("--profile", default="default", help="selected workflow validation profile/defconfig")
    args = parser.parse_args()
    for name, value in fingerprints(args.unixnu, args.device, toolchain_identity(), args.profile).items():
        print(f"{name}={value}")
