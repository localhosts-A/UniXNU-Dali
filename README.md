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
the host's fastboot wait deadline. The cache fingerprint covers all board settings
except the host-built UEFI revision, alongside the full UniXNU pin, toolchain,
source, config, driver and patch inputs. A firmware-only pin update can reuse
kernel objects; artifact manifests still require the exact current source pins.

The pinned firmware adds an FDT-derived conventional-memory region for
DarwinBoot's 256 MiB contiguous window. Static reservations, protected regions,
loaded firmware and runtime boot data are excluded; size-only pools receive
excluded placements respecting their allocation constraints. The initial
diagnostic heap remains 128 MiB. A separate 60-second UEFI application timer
provides a warm-reset fallback while Boot Services remain active. Recovery from
an XNU freeze after ExitBootServices still depends on the hardware watchdog and
requires hardware qualification. One cycle reached the loader and returned Out
of Resources before the memory extension. Trace v2 located the subsequent stop
inside the static reserved-memory walk. The pinned firmware removes repeated
parent searches there and records node progress/timer ticks. Hardware then
completed memory selection and stopped during MMIO-node validation. The current
pin validates/stops the inherited LK watchdog before memory scans and timestamps
each probe; expiry as the cause remains unproven. The owner entered fastboot
manually in that cycle. The next hardware cycle reached menu timeout and
DarwinBoot window allocation, but only printed diagnostics before calling EBS.
The following hardware cycle proved EBS returned successfully and the loader
trampoline was called, with no XNU checkpoint. The current pins trace trampoline
stages through cache maintenance and MMU disable; a guarded, stack-free Dali
kernel breadcrumb records cold-entry execution before its first register access.
This early marker does not satisfy the platform/GIC/timer acceptance gate.
No Dali XNU pass has been claimed.

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
