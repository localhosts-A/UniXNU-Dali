#!/usr/bin/env python3
"""Export verified Dali unixnu_logs records from an offline partition capture."""
import argparse
import json
from pathlib import Path

from ufs_userdata_logs import PARTITION_BYTES, _read_regular, decode_partition, write_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="exact 32 MiB raw partition image")
    parser.add_argument("output", type=Path, help="new output directory")
    args = parser.parse_args()
    try:
        raw = _read_regular(args.capture, PARTITION_BYTES)
        decoded = decode_partition(raw)
        manifest = write_export(args.output, raw, decoded)
    except (OSError, ValueError) as error:
        parser.exit(1, f"log export refused: {error}\n")
    print(json.dumps({"partition_sha256": manifest["partition_sha256"],
                      "records": len(manifest["entries"]),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
