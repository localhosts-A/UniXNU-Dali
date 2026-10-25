# Dali UFS read/write

The independent UFS milestone owns the MT6991 controller inventory, DMA and
request-list ownership, physical UEFI BlockIo, source-only XNU block devices,
and bounded `boot_b` scratch writes, synchronization and reset persistence.
The unattended cycle consumes these interfaces; reset policy and its Linux USB
reader remain separate work. RAM-root/PID1, root migration and native USB/OTG
remain paused.

## Current qualification

UEFI physical named-LUN read-only, real device synchronization and diagnostic
stop passed the `ea82d126` hardware run described below. The source-only storage
family and DarwinBoot handoff consumer have separate QEMU qualification.
The Dali private drain/sync provider passed a subsequent BDS-invoked hardware
trial, described below. Physical XNU ownership, scratch writes and
cross-reset persistence remain unqualified. The boot payload remains a RAM disk.

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
Firmware 67118fc used a stricter bounded policy for the observed event:
only one exact isolated indication after this LINK_STARTUP submission may be
saved and acknowledged. W1C clears only IS bit 2, preserving simultaneous UCCS;
the unchanged 500 ms deadline still requires a real UCCS, result zero and then
device presence. Other layers/lanes/fatal/reserved bits, repeated/sticky events,
preexisting/non-link events, failed ACK, absent completion, nonzero result or
missing device presence all fail closed, with no command resubmission. Native
tests included delayed completion and exact-deadline success. A later audit
found its nominal late-completion case never actually injected completion;
the deferred revision below fixes that coverage gap.

The following trial, `run-20261025T111012Z-b67c24a6`, used firmware `67118fc`,
device `68d3fcc` and generic `2ab7dfd`. It accepted and acknowledged the first
isolated LINERESET at IS=4/HCS=8. The later first-failure snapshot showed
IS=`0x404`, HCS=`0xf`, HCE=1, zero UIC arguments and PA=`0x80000010` with the
other layers zero. UCCS and DEVICE_PRESENT were visible, but the repeated
indication failed the strict policy; link qualification and DMA/media I/O were
not reached. The raw console, baseline, candidate and restored Android B/root/
all 18 original hashes were independently audited. Pending recovery is cleared
and this token is consumed. The next control change must check the pinned
source's timing for deferred LINERESET cleanup around command completion;
these observations do not justify ignoring all UIC errors or replaying writes.

The next firmware revision retains startup IS=4 without reading clear-on-read
layer registers or acknowledging the event until real UCCS arrives. A latched
bit over multiple polls does not extend the original 500 ms deadline. Completed
LINK_STARTUP must show exact IS=404, HCE=1, HCS without error bits, exact PA
80000010, four other layers zero, result zero and valid ARG3; then one combined
404 acknowledgement must leave IS=0. The plain completion path also requires
clean IS, closing an unrelated-status race after the last poll. Device presence
is required before post-link commands. Fatal/unrelated bits, lost pending IS,
bad layers/state/results, failed/sticky ACK and timeouts stop without replay.

The fixed-source basis is Linux f089d58 ufshcd.c:4788-4789 (UCCS enabled without
UIC_ERROR), :6948-6955 (raw IS acknowledged, enabled bits handled) and :4957-4958
(UECPA read-clear after POST_CHANGE). The adapter deliberately classifies all
layers earlier, at completed LINK_STARTUP before post-link commands; this is
not a claim that Linux explicitly waits for UCCS before every raw-IS ACK.
Twenty-six production-controller tests cover b67's latched status, no early
read-clear/ACK, unchanged deadline, completion exactly at deadline, completion
actually injected 10 us late during stop, and status arriving before ACK.
162 firmware tests pass. This revision still has no hardware qualification.

The e2e5c78/2bc4334/2ab7dfd trial, run-20261025T120056Z-49a0b99b,
showed firmware text/color bars and never reached the menu within 120 seconds.
The main runner exited with original restoration pending. After the operator
forced fastboot, the original-only recovery entry completed successfully:
full original boot_b, Android B/root and all 18 hashes match. An independent
audit rehashed all 18 backup images and checked the recovery, fresh-baseline/
candidate bindings and cleared pending ledger; the token remains consumed.
No raw console survived forced recovery. Controller/DMA/media progress is
unknown, and no read/sync/diagnostic-stop qualification is claimed.

The following candidate adds visible progress before existing control-plane
SiP/HCE/UIC requests and at DMA initialization, host installation/connection,
BlockIo diagnostics and stop boundaries. It adds no hardware reads/commands,
poll-loop output or media-write permission. If the RAM menu does not appear,
the last visible line can narrow the failure; it cannot prove completed I/O.

Trial run-20261025T121447Z-db963732 (89524c9/b62596d/2ab7dfd) supplied
a screen photo with controller result=7/stage=7/fault=0, DMA result=Success,
connect result=Success, then the BlockIo diagnostic entry without return.
The photo lacks token/device identity and does not prove a completed read or
sync. The 120-second observation expired; forced fastboot and original-only
recovery restored complete original boot_b, Android B/root/all 18 hashes.
An independent audit rehashed all 18 backup images, copied-photo SHA256,
candidate/fresh-baseline bindings and ledger, and checked live B/root. Pending
is cleared and db963732 remains consumed. No raw console survived recovery.

The actual host static link list exposed a definite defect: BaseCryptLibNull's
Sha256HashAll ASSERTs, and the DEBUG policy deadloops on ASSERT. If the first
read completed, its following digest would hang there; the photo alone cannot
establish that exact cause. The correction scopes real BaseCryptLib/OpenSSL
and dependencies to this host, checks SHA-256(abc) before control writes and
rejects null/missing/mixed crypto link inputs during physical packaging.
Runtime/build qualification of the correction is pending; media writes remain
disabled and physical read/sync/persistence are not qualified.

## First qualified UEFI physical read-only diagnostic

Run `run-20261025T123844Z-ea82d126` used firmware `1746ed7`, device
`9067ecc` and generic `2ab7dfd`. The actual final-FD SHA function had passed
five direct ARM64 QEMU cases before this trial. The phone returned to the RAM
menu; the operator selected the firmware return and entered fastboot.
The 49,836-byte raw console SHA256 is
`d98c337f798fb81cd3534369f837d2aed58769b7e065552bd26884cf33635b56`.

The trace reports successful controller initialization, 3 read-only roots,
96 GPT partitions, 10 samples and actual SYNC(10) completions for LUNs 0/1/2,
then successful diagnostic stop with HCE=0. LUN 2 has 4096-byte blocks and
64,577,536 blocks; its complete partition geometry/GUID set and both GPT header
hashes match the fresh Android baseline. Its boot_b sample matches the actual
candidate rather than original Android. Contents of LUNs 0/1 have no Android
baseline qualification; their geometry/read/sync observations remain distinct.

The original runner exited 2 because its scorer incorrectly required UPIU task
tag zero. Actual PassThru increments a UINT8 tag for each command, independently
of transfer slot zero; the observed sync tags were 92/96/101. The corrected
offline parser accepts only tags 0..255 while retaining exact completion-count,
LUN/opcode/order/GPT/hash/stop checks. A new regression covers nonzero and
out-of-range tags. Rescoring the hash-verified original raw capture passed;
the original failure JSON remains preserved, with the correction in separate
offline-rescore and independent-audit records. No reflash was needed.

Independent verification also rehashed all 18 backup images, compared restored
partition hashes, checked candidate/fresh-baseline bindings, live Android B/root
and cleared pending/consumed-token state. UEFI named-LUN read-only, true sync
and diagnostic stop are qualified. Physical XNU, writable scratch, firmware/XNU
ownership handoff and all three reboot-persistence rounds remain pending.

## Storage handoff consumer

The handoff consumer was integrated at generic `b54db17` and DarwinBoot
`fb0e77f`. Historical candidates retain their original pins; the current board
also includes the portable transport foundation described below.
UniXNUPkg defines an optional versioned storage-quiesce protocol. DarwinBoot
calls all providers after closing boot files/volume and before its final memory
map. Only exact EFI_SUCCESS permits entry; warnings, malformed providers and
errors refuse it. EBS retries refresh the map without reopening storage or
repeating callbacks. Providers must finish requests, synchronize writes, stop
the controller and revoke DMA, and XNU must establish its own mappings.

Twenty-five generic host tests pass, including seven production-consumer tests.
The rebuilt AArch64 QEMU fixture verifies a real loader refusal on quiesce
timeout, then a sealed provider, deliberate first-EBS INVALID_PARAMETER, real
EBS success and synthetic Mach-O entry. Source/EFI/raw-console hashes and order
were independently verified. The test provider is excluded from Dali DSC/FDF
and the previously qualified actual FD. This qualifies the consumer flow only;
XNU physical ownership remains pending.

Firmware `cffefda` adds the Dali provider. It closes public submissions, drains
and validates a pending command, then uses its own common DMA page for ordered
no-data SYNC(10) on each learned valid LUN. Old common pages remain retained.
Uncertain/failed requests are not replayed. Cleanup still attempts controller
stop on failure, but releases pages only after HCE=0, PassThru disconnect and
confirmed protocol absence. The first error remains sticky; successful repeated
calls do no work and cleanup retries never repeat the sync attempt.

The interface supports BSP callers at TPL_APPLICATION. Admission, drain/stop
and public submission/DMA callbacks share TPL_NOTIFY; disconnect runs back at
application TPL so generic Stop can retire its buffers. Reentry and unsupported
TPLs are refused. The provider is installed whenever physical read-only mode is
enabled, even before initialization, so a partial failure cannot silently remove
the handoff gate. The default-off RAM path is preserved.

The 187 firmware host tests include 14 production shutdown/policy/DMA tests and
10 production-envelope tests. The former simulate hardware with independent
CPU/device addresses, complete CDB checks, validated read copyback, partial sync,
timeouts, uncertain doorbells and no replay. The latter stub the shutdown engine
and UEFI services while executing the real close/TPL/stop/disconnect/release
functions. They cover HCE still enabled, errors/warnings, reentry and attempts
to submit after closure. These separate software checks do not qualify the new
hardware shutdown path or physical XNU handoff by themselves. Media writes remain disabled.

## Qualified Dali private sync and stop

Run `run-20261025T134202Z-4d77705d` used the sealed firmware `cffefda`, device
`38ab920` and generic `b54db17` / DarwinBoot `fb0e77f` candidate. The runner
exited 0. Its 49,877-byte raw console SHA256 is
`f82f14dd814205d8990cfe49c519f1e1315f9c2efbf3caeb5ac822dc1f75fca2`.
The new provider reports `closed=1 drained-synced=1 mask=7 commands=3
result=Success`, then quiesce succeeds with HCE=0. The phone reaches the RAM
menu and returns to firmware. This exercises the actual private owned-page
SYNC(10) and stop/disconnect/DMA-release path through BDS.

Independent inspection recomputed reciprocal GPT CRCs, all 96 partition
geometry/GUID records, LUN-2 header/current-candidate samples and sync/stop
ordering. The 96 partition teardown flushes are expected refusals after public
admission closes; they must not be counted as device sync successes. Private
sync completes before stop/disconnect. The complete original boot_b was
restored, Android B/root/all 18 hashes passed, and all 18 backup image bytes
were independently rehashed. Pending restore is cleared; this token is consumed.

The current board's firmware pin advances to an evidence-only commit after
`cffefda`; it does not change the code exercised by this run. Its candidate
retains the original three pins. Qualification is limited to the BDS-invoked
provider: the complete DarwinBoot-to-XNU physical ownership transfer, XNU
block devices, scratch writes and reboot persistence remain pending.

## Portable XNU transport foundation

Generic `7cabc66` added `drivers/UfsTransport` to the
source-only IOStorageFamily archive. Its CPU and device addresses are separate,
with checked full-span limits, caller-proven exclusive legacy ownership and
real platform cache callbacks. The core builds its own UTRD/UPIU/PRDT commands,
checks UnitDescriptor and SCSI capacity together, supports actual 512/4096-byte
geometry and READ/WRITE(10)/(16), and sends an ordered device SYNC(10).

Writing defaults to denial. An external platform grant must bind device,
candidate, source, token, LUN, geometry and scratch; it is checked before copying
and again before submission. Dali does not yet implement that grant. Partial
operations, malformed completion, cache failure and uncertain requests poison
the transport without automatic replay. Only a completed reset UNIT ATTENTION
permits bounded TEST UNIT READY retries. Exposed DMA remains quarantined until
close and an observed HCE=0.

All 41 generic host tests pass, including 15 tests compiling the production
transport. The QEMU UFS model verifies LUN 2, 4096-byte capacity, 4 KiB and
128 KiB reads, rejected writes, one permitted block write, real synchronization
and readback. The entire synthetic disk changes only at the permitted block.
Evidence includes frozen compiled sources, before/after source hashes, library
hash, submitted UPIUs and completed response bytes. Qtest copies host memory
to guest DMA; these results do not qualify ARM cache maintenance or XNU DMA.

The source builder requires the transport ABI symbol in both the actual archive
and final kernel, and rejects source/header changes after compilation. CI
[`40204508502`](https://github.com/localhosts-A/UniXNU-Dali/actions/runs/40204508502)
passed with generic `7cabc66` and device `2b949aa`: actual Apple-toolchain
archive/final link, one-core boot, RAM IOStorageFamily 4096-byte read/write and
512-byte read-only interfaces, plus the separate QEMU UFS model test. The first
run stopped before kernel compilation because two test fixtures did not resolve
macOS temporary-path symlinks; the fix matches production CLI normalization
without changing recovery identity checks.

The final kernel SHA256 is
`20983b97b0e3ceab9b14dc118a9a3f9ec2c5ad7c6ad4d99d0a9fa4c9e2e8fa27`.
Independent verification checked its actual ABI instructions, source/header
bindings, raw checkpoint call targets, return stacks and complete result masks
`0x7f`/`0xf`. The separately downloaded UFS-model artifact contains the actual
arm64 host library and matches its recorded hash and pinned compiled sources;
all 12 raw command/response pairs and the entire synthetic disk were checked.

These are separate tests: the RAM storage fixture does not call the portable
UFS core. Dali resource initialization, kernel DMA allocation, physical
IOBlockStorageDevice publication, runtime scratch authorization and persistence
remain pending. Source archive/object hashes were recorded by the build;
independent binary inspection used the downloaded final kernel and host library.

## Dali XNU read-only adapter implementation

The board pins generic `3e7392b`, preserving the QUERY result byte so discovery
can distinguish an invalid LUN index from other target failures. All eight
ordinary LUN indices are probed; geometry comes from the descriptor/capacity
agreement, without a fixed LUN or logical block size.

`drivers/DaliUfs` now implements the default-disabled controller adapter and
read-only `IOBlockStorageDevice` nubs. The generated ADT contains its nub only
with explicit `DALI_XNU_UFS_READ_ONLY=true` or `make-dali-adt.py --ufs-readonly`.
The builder validates exact saved-FDT resources, all clock/reset references,
DMA range, supply identities and interrupts before serializing `resources-v1`.
Its source-FDT digest is provenance from this trusted builder, not runtime
authentication of a separately supplied FDT. Runtime additionally requires
`dali-ufs-readonly=1`, early boot, a fresh token, and manual recovery mode.

The worker waits for DaliBootLog's token-bound persisted platform checkpoint
before hardware access. It verifies the actual kernel SHA wrappers and timer/
CPU geometry, then uses the fixed controller recipe and its own prepared,
physically contiguous, below-4-GiB DMA allocation. Explicit copyback memory
uses bounded `dc civac` before device access and `dc ivac` after device access,
with `dsb sy`; firmware buffer pointers are never adopted. HCE, both legacy
doorbells/run bits, IE and MEMCFG must be clear before old base addresses are
erased. Fixed HCS readiness bits may remain set. MCQCFG configures a maximum;
its observed `0x1f00` is not an active-request count.

The read-only nubs reject writes and format operations, serialize transport
access and return one completion. The worker exercises the actual IOMedia read
interface, records geometry/partition metadata and sample hashes, and sends
real ordered SYNC(10). Before manual recovery it closes admission, synchronizes
valid LUNs without replay, stops HCE and verifies stopped queue state. DMA is
retained on unsafe shutdown; a failed sync also refuses the recovery watchdog.
These interfaces have no scratch write grant.

Nineteen resource/controller/range tests execute the production validator and
controller with synthetic FDT/MMIO/SiP. They do not execute IOKit allocation or
matching. Actual Apple compilation/final linkage, instruction inspection,
and the preserved QEMU baseline passed the CI below; source-only XNU hardware
handoff and physical reads remain pending. No new phone candidate or token has
been used for this adapter.

The first actual Apple CI `40210052707` stopped at a C++ type error in partition
traversal: the iterator returns OSObject, while recursion needs IORegistryEntry.
An explicit checked cast fixes that error. Its original failed log is retained;
there is no successful kernel-link or runtime claim from that run.

`ufs_xnu_evidence.py` is the separate offline transcript checker for this
adapter. It reuses the raw-CRC-checked Android baseline, compares actual LUN
geometry, all baseline partition identities/names/ranges, both GPT header hashes
and the current candidate's boot_b block. It also requires the successful
platform checkpoint before hardware, real-path sync counters and stopped-DMA
reports. Nine synthetic negative/positive tests cover stale/mixed/incomplete
records, both block sizes, failed sync/stop, metadata framing and repeated stop.
Device identity and the actual source/kernel/ADT/FD binding remain obligations
of the dedicated `run-xnu-ufs-readonly.py` runner. Text scoring does not grant
writes or qualify persistence.

CI [`40211101567`](https://github.com/localhosts-A/UniXNU-Dali/actions/runs/40211101567)
passed with device `1a92efa`, generic `3e7392b` and firmware `b502bdf`: actual
Apple compilation and final kernel linkage, one-core baseline, both RAM storage
runtime cases and the independent QEMU UFS model. Device `6aab1f2` subsequently
fixes only the host evidence parser's MPIDR mask and its tests; driver code and
build inputs are unchanged. All 117 device tests and 42 generic tests pass.

The final kernel SHA256 is
`0d6af093edb455a72f56a9875a7dc091ba519d38dc1b21ba14f507dbc30200ff`.
Independent verification downloaded all five artifact archives and checked their
GitHub digests. It rehashed both production objects, each archive member, the
complete `libDaliUfs.a` (SHA256
`9f4971e56e62f37fcca7844e83f3d5799ef65bceb9f824c9fc433b4c2fca831f`),
source/header bindings and actual final kernel. Its disassembly confirms both
cache operations with barriers, SMC argument setup, acquire/release checkpoint
operations, the controller/allocation/transport calls, and sync/stop/release
calls. The actual device-sync CDB is `35000000000000000000` with IMMED clear.

Both raw RAM checkpoint reports match this kernel's real call target, return
stack and complete results `0x315346000001007f` / `0x315346000002000f`. The
separate arm64 QEMU UFS library matches its compiled snapshot and pinned source;
all 12 raw response pairs and the complete synthetic disk were checked, with
only block 64 changed. Private evidence is `dali-ufs-platform-ci-40211101567`.
The RAM fixture does not invoke the physical Dali adapter. These checks qualify
source build/link and software behavior, without qualifying MT6991 DMA/cache,
physical XNU media, the full firmware handoff, scratch writing or persistence.

## Combined firmware/source-XNU read-only profile

`DALI_XNU_UFS_READ_ONLY=true` is a separate manual-recovery build profile. It
requires the same fresh token for the physical UEFI provider and source-XNU
adapter, plus the captured live FDT. Packaging verifies the final FD's
FREEFORM/RAW payload bytes against `payload.img`, then checks the actual FAT
kernel, ADT, boot arguments and `dali-boot.conf` against their manifest records.
The recovery file is checked against the firmware parser's exact version-1,
manual-recovery policy; ADT resource bytes and their live-FDT digest are checked
again from the packed candidate. All write-authority and physical-qualification
fields remain false.

`run-xnu-ufs-readonly.py` seals and re-verifies the image, binds the source
artifact and all three repository pins, captures a fresh Android UFS baseline,
and persists a per-device lease and original-only restore command before
flashing `boot_b`. The physical UFS diagnostic runs in source XNU after the
firmware provider's sync/stop gate; a second DarwinBoot quiesce call is
idempotent. The XNU driver then uses its own queue and DMA allocation. Manual
recovery for this profile is the XNU prompt: hold Volume Up to request recovery,
then hold Volume Down through the two-second LK watchdog interval to enter
fastboot. This differs from the UEFI-only menu recovery used by earlier probes.
The scorer requires both the token-bound UEFI BlockIo read/sync/stop transcript
and the later XNU read/sync/stop transcript in the same ramoops capture. It binds
the firmware stop mask to the LUN set observed by UEFI before requiring the XNU
platform checkpoint and controller records.

The runner's current evidence collection reads ramoops from Android after the
original `boot_b` has been restored. That is sufficient to keep this
read-only diagnostic's transcript retryable, but it does not meet the plan's
pre-restore evidence requirement for scratch-write/persistence cycles. No write
token or scratch grant is enabled by this profile, and software tests/dry-run
do not qualify physical XNU media or handoff.

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
