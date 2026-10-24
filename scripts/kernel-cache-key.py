#!/usr/bin/env python3
"""Hash every board setting except the firmware-only UEFI source revision.

The workflow still keys on the full UniXNU revision, toolchain and source,
config, driver and patch hashes. Artifact manifests retain both exact pins.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib


def board_fingerprint(path: Path) -> str:
    with path.open("rb") as source:
        board = tomllib.load(source)
    # UEFI is built on the host, not by the source-only XNU build. No other
    # board field is excluded, including future fields and the UniXNU pin.
    board.get("firmware", {}).pop("uefi_commit", None)
    encoded = json.dumps(board, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    print(board_fingerprint(parser.parse_args().board))
