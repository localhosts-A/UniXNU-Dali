#!/usr/bin/env python3
"""Fetch/verify fixed USB reference sources in an independent directory; no device I/O."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tomllib

DEFAULT = Path(__file__).resolve().parents[1] / "references/usb-sources.toml"


def git(path, *args, input=None):
    result = subprocess.run(["git", "-C", str(path), *args], input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    if result.returncode:
        raise ValueError(f"git {args[0]} in {path.name}: {result.stderr.strip()}")
    return result.stdout.strip()


def sources(path):
    document = tomllib.loads(path.read_text())
    if document.get("schema") != 1:
        raise ValueError("unsupported USB source manifest schema")
    names = set()
    for source in document["source"]:
        name = source["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in names:
            raise ValueError("invalid/duplicate source name")
        names.add(name)
        if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
            raise ValueError("source must have a complete commit pin")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git", source["git"]):
            raise ValueError("source must use a public HTTPS GitHub repository")
        for name in ("paths", "required"):
            if not source[name]:
                raise ValueError(f"empty {name}")
            for value in source[name]:
                if value == "/" and name == "paths":
                    continue
                if (not value or value.startswith("/") or ".." in PurePosixPath(value).parts
                        or any(c in value for c in "\n\r\\*?[]!")):
                    raise ValueError("unsafe source path")
    return document["source"]


def verify(source, path):
    if path.is_symlink() or not (path / ".git").is_dir():
        raise ValueError(f"not an independent source repository: {path}")
    if git(path, "rev-parse", "HEAD") != source["commit"]:
        raise ValueError(f"source revision differs: {source['name']}")
    if git(path, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"source is modified: {source['name']}")
    hashes = {}
    for relative in source["required"]:
        file = path / relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(path.resolve()):
            raise ValueError(f"missing required source: {source['name']}/{relative}")
        hashes[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
    return {"name": source["name"], "repository": source["git"], "commit": source["commit"],
            "required_sha256": hashes}


def prepare(source, root, verify_only):
    target = root / source["name"]
    if not target.exists():
        if verify_only:
            raise ValueError(f"missing source {source['name']}")
        target.mkdir()
        git(target, "init", "--quiet")
        git(target, "remote", "add", "origin", source["git"])
    # A failed interrupted fetch is resumable, but an arbitrary existing directory isn't.
    if target.is_symlink() or not (target / ".git").is_dir():
        raise ValueError(f"refusing unmanaged source directory {target}")
    if git(target, "remote", "get-url", "origin") != source["git"]:
        raise ValueError(f"repository URL differs: {source['name']}")
    head = subprocess.run(["git", "-C", str(target), "rev-parse", "--verify", "HEAD"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
    if not head:
        if verify_only:
            raise ValueError(f"incomplete source {source['name']}")
        git(target, "fetch", "--quiet", "--depth=1", "--filter=blob:none", "origin", source["commit"])
        if source["paths"] != ["/"]:
            git(target, "sparse-checkout", "set", "--no-cone", "--stdin",
                input="\n".join("/" + p for p in source["paths"]) + "\n")
        git(target, "checkout", "--quiet", "--detach", source["commit"])
    return verify(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--jobs", type=int, default=3, choices=range(1, 6))
    args = parser.parse_args()
    try:
        entries = sources(args.manifest)
        root = args.output.resolve()
        if args.verify_only and not root.is_dir():
            raise ValueError("source output directory is absent")
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".usb-source.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                reports = list(pool.map(lambda s: prepare(s, root, args.verify_only), entries))
            report = {"schema": 1, "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                      "sources": reports, "device_access_performed": False, "hardware_qualified": False}
            if not args.verify_only:
                temporary = root / "source-manifest.json.partial"
                with temporary.open("w") as output:
                    output.write(json.dumps(report, indent=2) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(root / "source-manifest.json")
            else:
                saved = json.loads((root / "source-manifest.json").read_text())
                if saved != report:
                    raise ValueError("saved source manifest differs")
            print(json.dumps(report, indent=2))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"USB source verification failed: {error}") from None


if __name__ == "__main__":
    main()
