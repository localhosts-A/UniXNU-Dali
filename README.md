# UniXNU-Dali

Device repository for REDMI K80 Ultra (dali / MT6991). This is the UEFI bring-up
stage; kernel, ADT, userdata and IPSW selections have not been configured.

Keep the repositories as siblings:

```text
UEFI-Dali/       Mu-Silicium firmware port
UniXNU-Dali/     This device repository
UniXNU/          Generic UniXNU upstream
device-backups/  Private local backups and test evidence, outside all repositories
```

Repositories: [firmware](https://github.com/localhosts-A/UEFI-Dali),
[device](https://github.com/localhosts-A/UniXNU-Dali),
[generic stack](https://github.com/localhosts-A/UniXNU),
[generic upstream](https://github.com/UniXNU/UniXNU).

`board.toml` describes the device firmware. `package/unixnu.toml` supplies a
UEFI-only test menu: load SampleDriver, run HelloPayload's driver/variable
checks, then return to the menu. It contains no XNU entry or shutdown action.

Build using a complete verified backup and initialized firmware checkout:

```sh
MU_PYTHON=/path/to/mu-venv/bin/python bash scripts/build-firmware.sh \
  /absolute/path/to/verified-dali-backup ../UEFI-Dali ../UniXNU
```

The device wrapper selects the boot-v4, legacy-LZ4 and FDT-preserving dali shim.
The generic upstream `unixnu bootimg` is not yet compatible with that packaging
path. Build commands never reboot or flash the phone. Firmware capabilities,
physical test evidence and rollback instructions belong to UEFI-Dali/DALI.md.
