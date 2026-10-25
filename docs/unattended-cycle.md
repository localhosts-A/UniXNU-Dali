# Dali unattended diagnostic cycle

The active first implementation retests PSCI cold/warm resets with retained
ramoops and original Android recovery. If both fail, investigate the verified
AEE reset type `0x80000201` using source and device evidence. Hardware UFS,
scratch writing/sync and persistence now belong to the independent
[UFS milestone](ufs.md); they no longer wait for this plan's reset results.
The Linux USB reader below remains deferred consumer work. RAM-root/PID1 and full
native USB/OTG remain paused. No complete unattended cycle has passed on Dali.

The reference is the [sheng unattended cycle](https://unixnu.com/devices/sheng-unattended-test).
Its userdata reservation, Qualcomm hardware, PMIC reset and `fastboot boot`
commands do not apply to this device. Dali requires the boot-v4, legacy-LZ4
`boot_b` path and its own storage and reset qualification.

## Inputs and ownership

[mt6991-linux.toml](../references/mt6991-linux.toml) pins all three official
MiCode `bsp-dali-v-oss` repositories. The Linux base is 6.6.56. The MTK device
modules contain Dali/MT6991 device trees, UFS/PHY, MTU3, MT6379 TCPC, clocks,
power domains and build configuration. The companion vendor-module repository
is a separate input; its `udc_lib` is ECCCI compression, not a USB controller.
These releases must be checked against the private live FDT and actual device
state; they do not identify the running 6.6.118 kernel or its module ABI.

Follow the [porting overview](https://unixnu.com/porting/),
[UFS guide](https://unixnu.com/porting/ufs) and
[evidence levels](https://unixnu.com/porting/records). The current format-1
checkout does not contain the latest upstream porting-record checker. Do not
claim its record check has passed or silently migrate all board/profile pins.

UFS resources and write policy belong to the independent UFS milestone.
The Linux reader and automatic-cycle host runner belong here.
UEFI integration and boot-image packing belong in UEFI-Dali. Reusable transport
implementations belong in the generic stack or a separately pinned component;
ShengUfs constants and persistence policy must not be imported as Dali defaults.

## Current reset/ramoops qualification

`scripts/run-reset-qualification.py` runs one explicitly selected `psci-cold`,
`psci-warm` or `aee` experiment. `off` is a manual-recovery baseline: it neither
sets the fastboot marker nor requests a reset and cannot pass an automatic cycle.
The runner generates a unique random 128-bit token for every candidate. The
packager checks token syntax; token freshness belongs to this runner.

BootShim and SEC retain valid console-ring headers and data. Qualification sets
and reads back the known FDT-derived fastboot marker without arming RGU WDT,
disables the UEFI watchdog and suppresses application reset deadlines. After a
saved real XNU CPU/platform/GIC/timer checkpoint, XNU waits at least 65 million
physical timer ticks at the verified 13 MHz, then saves the selected method,
arguments and RGU state before its single secure call. Cold uses SYSTEM_RESET;
warm queries RESET2 support and uses architectural type 0/cookie 0. Unsupported
or returned calls halt without falling back. AEE retains the stock sign-extended
vendor type and zero cookie; this does not qualify its dump/fastboot behavior.

First package with `--dry-run`, providing `--backup`, `--uefi`, `--unixnu`,
`--artifact-dir`, `--evidence-root`, `--serial` and `--reset-method`. A real run
checks rooted Android B, live FDT, exact source/artifact pins and all 18 protected
hashes, then flashes only boot_b. It observes fresh fastboot for at most 120
seconds. Restoration always flashes the complete original boot_b and uses
`fastboot reboot`; `fastboot continue` is rejected by this device's security
control. Pstore and boot-reason properties are collected immediately after
Android returns, followed by B/root/18-hash verification. Empty pstore means
retention failed across this entire chain; it does not identify which reset
cleared RAM.

On timeout, the identity-bound `restore-state.json` retains an executable
`--restore-run` command. Enter fastboot manually and run it before any further
experiment. That mode only restores the original image and collects evidence.
A saved SMC record proves a pre-call request, not the executed reset cause.
Host enumeration plus logs cannot prove no keys were pressed: the owner's
separate observation remains mandatory. First full success is followed by three
additional consecutive complete no-key cycles. Manual menu entry or recovery,
stale logs and a competing watchdog cannot satisfy acceptance.

Historical correction, 2026-10-25: the owner reports a short Power press to
confirm the menu in the older PSCI cold/warm rounds. Those original observations
remain recorded, but neither round counts as completely unattended. A short
menu confirmation is not evidence of a held Power forced reset.

## Reader cycle after UFS qualification

1. Verify the device, slot B, original backups, live FDT and all 18 protected
   partition hashes. Produce one candidate and one identity-bound recovery record.
2. Boot the candidate and obtain the actual single-core XNU checkpoint. Capture
   available diagnostic and kernel-message data with the current host token.
3. Use the independently qualified UFS API to commit that round's bounded log to
   its authorized scratch area. Require its medium-commit result before requesting
   the selected MT6991/PSCI fastboot reset; do not implement another raw writer.
4. Start the qualified Linux reader without losing the scratch data. Copy the
   log to RAM, export over USB CDC ACM, and validate token, source/image identity,
   sequence, length and digest on the host. Mount no storage filesystems.
5. Return to fastboot, restore the complete original boot_b, boot Android and
   verify slot B, root and all 18 original hashes. Repeat for three consecutive
   cycles without physical keys.

The known early-XNU CPU/platform/GIC/timer checkpoint is sufficient for the
first payload. PID1 execution, Apple userspace and a UFS root filesystem are
separate milestones. USB enumeration or a reset-request log alone cannot pass
the cycle. A timeout followed by manual recovery is a failed unattended cycle.

## Deferred reader-switching gate

The independent UFS milestone owns actual partition geometry, scratch allocation,
boot-v4/AVB bounds and runtime write authorization. Its 1 MiB proposal and 1 MiB
tail guard are not automatically qualified by this document. This reader must
consume that exact authorized range and may not widen it or add media writers.

Fastboot's erase range when flashing a shorter boot_b image is currently
unknown. A full original-image restore necessarily replaces the scratch data.
Before enabling a persistent XNU writer, verify a candidate-owned canary across
the proposed reader transition and export it before restoration. If switching
images destroys it, stop that route and investigate a reader in the same boot
image; do not silently switch to userdata, expdb or blackbox.

The preferred reader research path is a matching source Linux and minimal
built-in initramfs with a distinct reader entry. Qualify its boot-v4/rdinit
behavior with the retained init_boot/vendor_boot, including that Android init
does not run and filesystems remain unmounted. Required platform modules or
built-ins must be compiled together. CDC ACM in the generic kernel configuration
does not establish MTU3/PHY/TCPC initialization or real USB enumeration.

## Limits and defaults

All new writer/reset modes default off and require a fresh host token. Driver
polling and host waits are bounded. Loss of USB leaves a pending recovery record
and prevents another flash; it cannot be advertised as recovery from any hang.
Normal timed reset and watchdog recovery from a deliberate freeze need separate
evidence. Ramoops remains useful alongside storage but is not assumed to survive
the reset path.

Full UEFI/XNU native USB, OTG HID/storage and automatic Type-C role switching
stay in the paused plan. The reader needs only Linux USB 2.0 device-side CDC ACM.
No raw storage logger or functioning driver is implemented by this document or
the source-reference file. Hardware images, FDT, logs and device identifiers
remain private; source inventory and host checks are separate from device proof.
