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

The extended version-2 run used firmware `dc401de`, generic `5899e85` and
device `eca4fec`. Its 25-read fresh-token sequence passed, as did full original
`boot_b` recovery, Android B/root/all 18 hashes and independent raw-evidence
rescoring. Standard registers agreed with the version-1 snapshot.

| Register | Observed raw value |
| --- | --- |
| CAP / version | `55c7051f` / `00000400` (UFSHCI 4.0) |
| HCE / HCS | `0` / `0` |
| IS / IE / AHIT | `0` / `0` / `0` |
| UTRL / UTMRL bases, doorbells and run bits | all `0` |
| MCQCAP / MEMCFG / MCQCFG | `080b0207` / `0` / `00001f00` |
| Both inspected AO clock-gate status registers | `0` |
| MTK IP / reference-clock request and acknowledge | `10470000` / `00000003` |
| XOUFS / OPT / OTSD | `00000102` / `10553070` / `00000410` |

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
additional fields. Version 2 has passed its own hardware register snapshot;
it provides no DMA, command or media-I/O qualification.

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

The firmware platform library now contains a control-plane core
(`Mt6991UfsController.h` / `UfsController.c`) for the measured MT6991 B0 state.
It validates the reset binding and DMA range, refuses inherited active queues,
and implements the fixed SiP/reset/HCE/UIC link sequence with bounded polls,
first-failure diagnostics, poisoned failure state and idempotent controller
stop. TI reset writes use the source driver's masked read/modify/write rather
than assume W1S semantics. Twenty-two tests execute the production C with simulated
MMIO/SiP, including stale completion, timeout and failed-stop cases.
The default-disabled physical-host diagnostic now calls this core after BDS
prepares the live FDT. The register-only probe remains separate. This integration
has no physical-media or DMA qualification yet.

`Mt6991UfsDmaLib` supplies the private DMA ownership component: Normal Non-cacheable
pages, owned common-buffer mappings, separate data bounce pages, checked full
spans and mapping IDs, serialized submission/completion, and quarantine after
an uncertain request. Every release entry refuses poisoned memory until HCE=0
has been confirmed. Attribute-restoration and page-free failures retain their
records for safe retry. The UEFI backend uses GCD/Cpu protocols and restores the
complete original attributes; it does not program an IOMMU or use the platform's
default coherent DmaLib. The initial DMA address window is below 4 GiB within
the declared 64 GiB bus window; its 4096-byte allocation pages do not define
the device's logical block size. Fourteen native fault-injection tests execute
the actual core and backend with mocked UEFI services. This pinned AArch64
CpuDxe maps EFI_MEMORY_WC to Normal Non-cacheable and UC to Device memory;
the allocator requires WC, with no UC fallback for packed generic descriptors.

The new `UfsHostDxe` connects the controller/DMA components to UfsPassThru,
ScsiBus, ScsiDisk and PartitionDxe. Its opt-in v4 platform policy rejects
async submissions, zero-timeout/write/unknown commands and independently checks descriptors,
PRDT, ownership, learned geometry and responses at the hardware doorbell.
Timeouts close submissions and confirm HCE=0 before cleanup; stop failure
retains buffers. BDS stops, disconnects and releases this diagnostic host before
the RAM menu. The generic DarwinBoot/XNU handoff is still pending.

ScsiDisk now obtains forced read-only policy from its actual ExtSCSI parent
before publishing media. For this v4 parent, both flush interfaces send one
bounded blocking SYNCHRONIZE CACHE(10) and propagate completion errors. Positive
ScsiDisk timeouts are capped at three seconds rather than rejecting its normal
30-second default. Direct ExtSCSI calls ignore Event when NONBLOCKIO is absent.

The read-only diagnostic logs actual root geometry, partition paths and SHA-256
samples of blocks 0, 1 and LastBlock. Only the observed baseline LUN-2 geometry
selects the extra boot_b sample, which must match the current candidate's first
block. Read and flush operations recheck media identity. Flush success also
requires exactly one new validated device-SYNC completion, excluding a no-op.

158 firmware host tests pass, including 22 controller, 9 wire-policy, 12 production PassThru,
8 ScsiDisk media/sync, 8 BlockIo diagnostic, 4 host sync-completion and 5
physical-profile packaging tests. The device suite has 88 passing tests,
including 9 new physical-runner lifecycle/metadata regressions.
An enabled AArch64 build compiles these changes; existing off/register-probe
packaging still refuses enabled physical hosts. The separate explicit physical
profile/runner is implemented below; final candidate and hardware read/sync/DMA
qualification remain pending. Scratch writes remain disabled.

`scripts/ufs_readonly_evidence.py` now provides the offline evidence checker for
that future runner. It revalidates raw baseline capture hashes, reciprocal GPT
CRCs and all partition records, bound to the original boot_b image and expected
serial. It requires one ordered current-token diagnostic trace, the complete
baseline LUN partition set, known GPT-header and current-candidate block hashes,
actual SYNC completions with monotonic per-LUN counts, and successful HCE=0 stop.
Eleven host tests cover corrupt and mixed evidence, both logical block sizes,
sync variants and prior connection syncs. The saved Android baseline's 96
partitions also pass raw revalidation. The checker does not identify a connected
phone from console text; the dedicated runner must bind the device and candidate
to the fresh token. The new runner supplies that binding; its final sealed
candidate and hardware trial are still pending.

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
CI run `40168776759` passed the actual Apple-toolchain compile, archive, final
kernel-link checks, one-core baseline and both storage runtime cases. It uses
generic `2ab7dfd` and device `47838f0`; the kernel SHA-256 is
`209e0d059f07d5003d3f8c109a948990636bd411d7250a774a083fe8076e8e58`.
The earlier run `40167739634` stopped at `IOCatalogue::initialize` because the
new GPT content-mask string contained an unquoted underscore. Quoting it fixed
that parser error. The original crypto readiness checks passed without change.
Build and link reports are separate from runtime and MT6991 qualification.

`scripts/test-source-storage-qemu.py` boots two explicit software-device cases
through the real source `IOBlockStorageDriver` and `IOMedia`: writable 4096-byte
RAM blocks and readonly 512-byte RAM blocks. It checks byte counts and content,
out-of-range rejection, readonly rejection before transport, synchronization
dispatch/failure, and partial-transfer error propagation. The fixture requires
an explicit QEMU test nub, QEMU model and boot argument; the normal device tree
does not publish it. Its checkpoint is inspected after the actual kernel
function returns, bound to the kernel hash and case arguments. These tests are
qualified in QEMU. Independent inspection of the raw GDB reports matched the
kernel hash, checkpoint symbol, actual call instruction, matching return stack
and complete check masks: `0x7f` for writable 4096-byte blocks, `0x0f` for
readonly 512-byte blocks. The private evidence directory is
`dali-source-storage-ci-40168776759`. This volatile software transport does not
qualify MT6991 DMA, physical UFS, scratch writes or persistence.

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

## Physical read-only diagnostic runner

The separate `DALI_UFS_PROFILE=read-only-blockio` build requires the three
UEFI-only arguments, `DALI_UFS_READ_ONLY=true`, `DALI_UFS_PROBE=false`, a fresh
32-hex `DALI_RUN_TOKEN`, `DALI_AUTO_FASTBOOT=false`, and no reset method.
Packaging validates BDS and host enable/probe/token PCDs and all six ARM64
BDS/Host/PassThru/ScsiBus/ScsiDisk/Partition modules in the actual FD. Old
off/register packaging retains its rejection of enabled physical hosts.

```sh
python3 scripts/run-ufs-probe.py --mode read-only-blockio --serial SERIAL \
  --backup /private/original-backup --uefi /src/UEFI-Dali --unixnu /src/UniXNU \
  --candidate-dir /private/sealed-read-only --baseline-dir /private/android-ufs-baseline \
  --evidence-root /private/read-only-runs --dry-run
```

Dry-run validates the actual image, FD, compiled PCDs/modules, all source
declarations and rebuilt entry shim, then copies only hash-checked raw baseline
captures. It contacts no device and consumes no token. A real trial first
collects a fresh read-only Android baseline and compares its LUN, geometry,
reciprocal GPT, partition set and header hashes with the supplied baseline.
All source/candidate/device checks complete before the durable pending record
and the single literal `boot_b` flash. The manual menu/Volume Down recovery
sequence remains the same as the register-only runner.

Read-only scoring binds the private candidate, current token and fresh live
baseline to that run's verified device. After original Android restoration it
recomputes every conclusion from the saved raw console, verifies Android B/root
and all 18 original partition hashes, and only then clears pending recovery.
Its read/sync/diagnostic-stop results remain separate from full physical UFS,
XNU handoff, scratch writes and reboot persistence, which it cannot qualify.

The saved original-only recovery command includes `--mode read-only-blockio`
but no source, candidate or baseline paths. Missing/corrupt diagnostic evidence
produces an unqualified result; collection-directory and result-write failures
cannot prevent the original restore. A failed original recovery still keeps
the identity-bound pending record and stops new trials.

The first physical control diagnostic, `run-20261025T095537Z-e8b7064d`, used
firmware `3dc6d5a`, device `57e34be` and generic `2ab7dfd`. Both candidate modes
had passed full builds, final-FD module checks and the real CLI dry-run. On the
phone it stopped during LINK_STARTUP: `link=4 stage=6 fault=16810020/4`.
The raw console then recorded successful quiesce and HCE=0. Initialization
returned before private DMA allocation or physical media commands, so this is
failed control-plane qualification, not one of the three persistence rounds.
The operator entered fastboot and the runner restored complete original boot_b;
Android B/root/all 18 original hashes and the cleared ledger were independently
rechecked against raw evidence and the backup. The token remains consumed.

The fixed Linux source decodes UIC interrupt bit 2 before deciding whether it
is fatal; LINK_STARTUP may also produce a PA generic/LINERESET indication. This
run did not capture the layer-error registers and cannot establish that cause.
The source comparison also identified the missing initial device-reset sequence:
the MTK callback asserts the SiP device-reset line, stops HBA, waits 10–15 us,
releases reset, then waits 10–15 ms before the initial HCI enable.

The controller now implements that pulse using SiP selector 2, a bounded HCE
stop during reset low, 10 us low and 15 ms settling after release. Assertion or
HCE-stop failure attempts one cleanup release while preserving the first fault;
a failed release is never replayed. The first initialized startup UIC failure
also saves IS/HCS/HCE, three UIC arguments and all five layer-error registers
before any stop write. Read-clear registers are sampled once; early power/HCE
and standalone stop failures do not sample them. Separate `DALI-UFS-CONTROL`
logs preserve the strict existing qualification trace. Native tests execute
the actual pulse, failure/cleanup and capture-order paths. The error mask is
unchanged. Neither this correction nor the cause of the previous IS=4 has been
hardware-qualified.

The next hardware run, `run-20261025T103415Z-a61e87bc`, used firmware `923acb7`,
device `5b05f20` and generic `2ab7dfd`. It confirmed successful reset assertion
and release. The saved first LINK_STARTUP failure was IS=4/HCS=8/HCE=1,
arguments zero and layer errors `80000010/0/0/0/0`. Raw console hash, fresh
baseline, candidate binding, quiesce/HCE=0 and original Android B/root/all 18
partition hashes were independently rechecked; pending restoration is cleared
and the token remains consumed. It still returned before DMA/media I/O.

The pinned Linux source identifies PA `0x80000010` as generic/LINERESET. During
link startup it enables UCCS separately from UIC_ERROR, waits for the UIC result
and device presence, then reads UECPA before making the HBA operational.
The controller now has a stricter bounded equivalent for the observed event:
only one exact isolated indication after this LINK_STARTUP submission may be
saved and acknowledged. W1C clears only IS bit 2, preserving simultaneous UCCS;
the unchanged 500 ms deadline still requires a real UCCS, result zero and then
device presence. Other layers/lanes/fatal/reserved bits, repeated/sticky events,
preexisting/non-link events, failed ACK, absent completion, nonzero result or
missing device presence all fail closed, with no command resubmission. Native
tests include delayed completion, exact-deadline success and completion 10 us
too late. This event handling has not been hardware-qualified.

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
