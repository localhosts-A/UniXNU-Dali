# UniXNU-Dali

Device repository for REDMI K80 Ultra (dali / MT6991). The current target is a
single-core XNU early-boot checkpoint through UniXNU and DarwinBoot. Storage,
root and iOS userland are outside this stage.

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

`board.toml` records the Dali source-kernel profile, platform archives, and the
ADT generator. The source-only kernel build needs macOS/Xcode and is run by the
`dali-xnu.yml` GitHub Action. The short-lived public artifact contains only the
unprelinked arm64 kernel Mach-O and its SHA-256 manifest; no IPSW, kernelcache,
device FDT, boot image, or ramoops logs are uploaded. Auto-fastboot defaults to
on. Each hardware cycle builds a private payload with a unique XNU log token.
The MT6991 watchdog interval is at most 31 seconds; the 120-second default is
the host's fastboot wait deadline.

`package/unixnu.toml` selects the source-built kernel, with HelloPayload and a
firmware-return action retained for diagnostics. `package/unixnu-uefi-test.toml`
keeps the prior UEFI-only test menu for firmware checks without an XNU artifact.

Build using a complete verified backup and initialized firmware checkout:

```sh
MU_PYTHON=/path/to/mu-venv/bin/python bash scripts/build-firmware.sh \
  /absolute/path/to/verified-dali-backup ../UEFI-Dali ../UniXNU
```

The device wrapper selects the boot-v4, legacy-LZ4 and FDT-preserving Dali shim.
Provide the Action kernel and an Apple ADT generated from the private live FDT to
produce the early-boot payload. The generic upstream `unixnu bootimg` is not yet
compatible with this packaging path. Build commands never reboot or flash the
phone. For end-to-end testing, `scripts/run-bootb-cycles.py` creates or verifies
the private backup, packages the Action artifact with the live FDT, binds ADB and
fastboot to the same device, flashes only `boot_b`, waits for a fresh fastboot
enumeration, restores and verifies the original image, and validates per-cycle
XNU evidence, Android/root and all backed partition hashes. Firmware capabilities
and hardware recovery evidence belong to UEFI-Dali/DALI.md.
