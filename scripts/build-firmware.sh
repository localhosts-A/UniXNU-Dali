#!/usr/bin/env bash
set -euo pipefail
DEVICE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ $# == 3 ]] || { echo "usage: bash scripts/build-firmware.sh VERIFIED_BACKUP UEFI_DALI UNIXNU" >&2; exit 2; }
DEVICE_BACKUP=$(realpath "$1")
DEVICE_UEFI=$(realpath "$2")
DEVICE_UNIXNU=$(realpath "$3")
bash "$DEVICE_UEFI/build-dali.sh" "$DEVICE_BACKUP" "$DEVICE_UNIXNU" "$DEVICE_ROOT"
