# Dali UFS read/write

The independent UFS milestone owns the MT6991 controller inventory, DMA and
request-list ownership, physical UEFI BlockIo, source-only XNU block devices,
and bounded `boot_b` scratch writes, synchronization and reset persistence.
The unattended cycle consumes these interfaces; reset policy and its Linux USB
reader remain separate work. RAM-root/PID1, root migration and native USB/OTG
remain paused.

## Current qualification

The Android read-only baseline collector and opt-in firmware register snapshot
are implemented and host-tested, including a complete firmware build.
No Dali physical UEFI/XNU UFS driver, DMA takeover, scratch writer or cross-reset
persistence has been qualified. The existing firmware payload remains a RAM disk.

The 2026-10-25 register-only hardware probe captured one complete fresh-token
read sequence in the raw console. The source-bound candidate used firmware
`f057924`, generic `e3033a0`, and device `33228ca`. The first attempt retained no
pstore file and is a failed evidence capture; the retry passed snapshot scoring.
The original complete `boot_b`, rooted Android B, all 18 original partition
hashes, and the cleared pending-restore ledger were verified after both attempts.

| Register | Observed raw value |
| --- | --- |
| CAP / version | `55c7051f` / `00000400` (UFSHCI 4.0) |
| HCE / HCS | `0` / `0` |
| IS / IE / AHIT | `0` / `0` / `0` |
| UTRL / UTMRL bases, doorbells and run bits | all `0` |
| MCQCAP / MEMCFG / MCQCFG | `080b0207` / `0` / `00001f00` |
| Both inspected AO clock-gate status registers | `0` |

This snapshot shows a disabled HCI and cleared legacy queues at this handoff.
It does not establish a working link, PHY state, MCQ queue ownership, DMA
translation or cache policy. The next controller stage must verify initialization
from the pinned MT6991 implementation rather than assume an active LK link.
The console records the real exit action and a warm-reset request; the operator
confirmed fastboot. This is manual recovery evidence, without an automatic-reset
or automatic-fastboot qualification claim.

`scripts/collect-ufs-baseline.py` requires the complete original private backup,
rooted Android B, and all 18 protected partition hashes before and after capture.
It reads both GPT headers and partition arrays from the parent SCSI LUN, checks
CRCs and reciprocal locations, cross-checks sysfs and unit-descriptor geometry,
and compares the raw-parent `boot_b` first block with the verified original image.
It performs no phone writes, controller initialization or reboots.

```sh
python3 scripts/collect-ufs-baseline.py --serial SERIAL \
  --backup /private/verified-dali-backup --output /private/new-ufs-baseline
python3 -m unittest discover -s tests -p 'test_ufs_geometry.py' -v
```

Output must be a new directory outside Git repositories and the immutable backup.
Partial captures have no successful `baseline.json`; they cannot qualify a run.
The report is private and includes device identity and GPT metadata.

The inspected Dali reports `boot_b` on LUN 2 with 4096-byte logical blocks.
These are observations, not defaults for another device or a changed layout.
Linux sysfs partition start/size use 512-byte sectors even on a 4096-byte device.
Both original GPT disk GUIDs are zero; they are recorded but must not serve as a
device identity. Nonzero unique partition GUIDs, reciprocal CRC-checked tables,
the bound device identity and original backup remain independently checked.

`ufs_geometry.scratch_window` converts the existing byte-only 1 MiB proposal
and 1 MiB tail guard into actual LUN LBAs. Its result always has
`write_authorized=false`. Android unit write-protection observations do not
qualify the later firmware/XNU state. No runtime write authorization is produced
by this collector or planner.

## Source and ownership gates

The [MT6991 resource baseline](ufs-resources.md) ties FDT resources, measured
handoff state, and initialization dependencies to the fixed MiCode sources.

The pinned firmware offers `DALI_UFS_PROBE=true` only for a three-argument
UEFI-only build, with `DALI_AUTO_FASTBOOT=false`, no reset method and a fresh
32-lowercase-hex `DALI_RUN_TOKEN`. The option defaults off. It checks the exact
live HCI/PHY/clock bindings and all 14 clock references, reads existing AO gate
status, then snapshots standard HCI and queue registers only when the required
clocks are already enabled. MCQ registers additionally require the capability
and version. It does not enable clocks, reset hardware, allocate DMA or submit
storage commands. Each bounded log line carries the build's unique token.

Probe log version 2 additionally requires `mphy-ver=1` and reads the pinned MTK
IP-version, reference-clock, XOUFS, option and outstanding-state registers. It
does not issue UIC or secure-monitor calls. The runner validates each schema's
exact read list; old version-1 captures remain usable without claiming these
additional fields. Version 2 is host-tested and awaiting its own hardware run.

Packaging checks the generated enable/token PCDs and the actual compiled BDS
module. It also extracts BDS through the final FD's FV/FFS/LZMA encapsulation
and requires identical module bytes. This prevents a stale or mixed FD from
being described by unrelated build metadata. Host tests and a successful build
do not establish a real-device register snapshot or safe controller takeover.

[mt6991-linux.toml](../references/mt6991-linux.toml) pins the three official
MiCode inputs. The Linux DMA API and absent `iommus`/`iommu_group` do not by
themselves establish direct DMA or a safe IOMMU domain. The standard Linux MTK
initialization resets the device/controller and replaces DMA request lists; it
is not a verified protocol for adopting LK's active state.

Before submitting commands, inspect LK's HCE/HCS, link state, legacy/MCQ mode,
request-list bases, doorbells and DMA mappings. Require exclusive ownership,
known address widths and cache maintenance, with bounded timeouts and no write
replay after an uncertain completion. Platform initialization must be derived
from the pinned MT6991 sources and verified device state.

UEFI will use its UFS host-controller protocol with UfsPassThru, ScsiBus and
ScsiDisk. Ordinary media stays read-only; a separate scratch child and the
underlying SCSI submission guard enforce the same permitted range. DarwinBoot
must quiesce storage before its final memory map. XNU must establish its own
queues and DMA allocations rather than using freed firmware pointers.

The current NativeUfs implementation is sheng/SM8550-specific, including LUN 0,
4 KiB assumptions and board persistence windows. Its command validation and
tests are references; copying its hardware constants or windows is not a Dali
port. Source-only XNU additionally needs a real public IOStorageFamily build and
builtin block-driver registration. Header preparation alone does not provide it.

The source build now has an explicit `CONFIG_XNU_SOURCE_STORAGE_FAMILY` path.
Its builder compiles the pinned public embedded target's 13 C++ files and the
real IIG-generated DriverKit bridge using XNU's recorded compiler flags. It
force-loads the archive, registers the block driver, GPT scheme and BSD media
client, and checks their definitions and personalities in the final kernel.
This integration is undergoing macOS CI validation; host command/ABI tests alone
do not prove a successful Apple-toolchain build or a working block device.
Build and link reports are separate from runtime and MT6991 qualification.

`scripts/test-source-storage-qemu.py` boots two explicit software-device cases
through the real source `IOBlockStorageDriver` and `IOMedia`: writable 4096-byte
RAM blocks and readonly 512-byte RAM blocks. It checks byte counts and content,
out-of-range rejection, readonly rejection before transport, synchronization
dispatch/failure, and partial-transfer error propagation. The fixture requires
an explicit QEMU test nub, QEMU model and boot argument; the normal device tree
does not publish it. Its checkpoint is inspected after the actual kernel
function returns, bound to the kernel hash and case arguments. These tests are
implemented but still awaiting actual compilation and QEMU validation.

## Register-probe runner

`scripts/run-ufs-probe.py` runs one register-only candidate with a 120-second
manual fastboot deadline. The sealed candidate directory contains the boot
image, `manifest.json`, and `SILICIUM_UEFI.fd`. It must match the clean pinned
source revisions and the current local DEBUG build, including generated probe
PCDs and compiled BDS. The runner rebuilds the entry shim and compares its bytes
and the bound original Android header with the actual decompressed candidate.
Rebuild the candidate after committing any device-source changes.

```sh
python3 scripts/run-ufs-probe.py --serial SERIAL \
  --backup /private/original-backup --uefi /src/UEFI-Dali --unixnu /src/UniXNU \
  --candidate-dir /private/sealed-probe --evidence-root /private/probe-runs \
  --dry-run
```

Dry-run uses the saved verified FDT without contacting the device. Remove
`--dry-run` only when the operator is present: select `Return to firmware diagnostics` after the menu
appears, confirm with Power, then hold only Volume Down during the firmware's 10-second recovery window.
This runner requires the same verified Android/fastboot serial. All output
stays outside the source repositories, immutable backup and sealed candidate.

Before flashing, the runner persists an identity-bound original-only recovery
command, consumes the unique token and records a pending restore. A per-device
advisory lock excludes a second UFS runner; pending restoration rejects another
trial across process restarts. The only partition target is literal `boot_b`.
After original Android returns, it reads token-bound ramoops evidence and checks
Android B, root and all 18 original partition hashes before clearing pending.
The register probe has no scratch data to export before this original restore;
it does not satisfy the later storage-write cycle's export-before-restore gate.

If recovery is interrupted, use the exact saved `--restore-run` command. It
accepts no candidate or source-build paths and verifies only the bound original
backup and target. Keep the state directory and run evidence until restoration
has been verified. Preserve the token ledger to prevent token reuse.

Each collection attempt keeps its own raw files. Cached success is recomputed
from hash-checked raw text; a partial or corrupt capture can be retried. Scoring
requires one complete current-token sequence and the exact gated register-read
list. Snapshot success always leaves DMA, physical media I/O, writes and reset
persistence unqualified.

## Completion

UEFI and XNU must each demonstrate known-block reads, bounded scratch writes,
real device synchronization, and readback. A second boot of the same candidate
uses an explicit readback-only entry without reflashing or restoring `boot_b`.
Three independent-token cycles are required. Physical menu/recovery keys are
allowed; automatic fastboot is not a UFS acceptance gate.

Export evidence before the complete original `boot_b` restore, which overwrites
the scratch by design. Every cycle ends with Android B, root and all 18 original
hashes verified. A pending restore prevents another candidate. No GPT, RPMB,
userdata, expdb, blackbox, LK, preloader, AVB or misc writes are permitted.
