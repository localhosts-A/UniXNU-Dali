#!/usr/bin/env bash
set -euo pipefail
DEVICE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ $# == 3 || $# == 5 || $# == 7 ]] || {
  echo "usage: bash scripts/build-firmware.sh VERIFIED_BACKUP UEFI_DALI UNIXNU [SOURCE_XNU_MACHO APPLE_ADT [OWN_PID1_MACHO OWN_TRUST_CACHE]]" >&2
  exit 2
}
DEVICE_BACKUP=$(realpath "$1")
DEVICE_UEFI=$(realpath "$2")
DEVICE_UNIXNU=$(realpath "$3")
if [[ $# -ge 5 ]]; then
  DEVICE_KERNEL=$(realpath "$4")
  DEVICE_ADT=$(realpath "$5")
  DEVICE_INPUTS=("$DEVICE_KERNEL" "$DEVICE_ADT")
  if [[ $# == 7 ]]; then
    DEVICE_INPUTS+=("$(realpath "$6")" "$(realpath "$7")")
  fi
  bash "$DEVICE_UEFI/build-dali.sh" "$DEVICE_BACKUP" "$DEVICE_UNIXNU" "$DEVICE_ROOT" "${DEVICE_INPUTS[@]}"
else
  bash "$DEVICE_UEFI/build-dali.sh" "$DEVICE_BACKUP" "$DEVICE_UNIXNU" "$DEVICE_ROOT"
fi
