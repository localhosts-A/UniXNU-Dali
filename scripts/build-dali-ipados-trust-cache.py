#!/usr/bin/env python3
"""Assemble the exact 21H461 original trust-cache modules for a DarwinBoot handoff audit."""
import argparse
from pathlib import Path
import tomllib

from dali_ipados_trust_cache import build_bundle


DEVICE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-trust-cache", required=True, type=Path)
    parser.add_argument("--system-trust-cache", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    board = tomllib.loads((DEVICE / "board.toml").read_text())
    try:
        _, _, report = build_bundle(args.static_trust_cache, args.system_trust_cache, board, args.output_dir)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Dali iPadOS trust-cache build failed: {error}") from None
    print(report["bundle_sha256"])


if __name__ == "__main__":
    main()
