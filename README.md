# UniXNU-Dali

The `codex/dali-pid1-offline` branch resumes the owned static PID1 milestone on a
read-only mockfs RAM root. Work first runs entirely on the host and macOS CI;
real-device ramoops and original Android recovery remain required for completion.
The [unattended diagnostic cycle](docs/unattended-cycle.md), physical UFS and
native USB/OTG are separate efforts, not dependencies of this RAM-root milestone.
Official MiCode base/device/vendor-module inputs are pinned in
[the source inventory](references/mt6991-linux.toml).
The source profile now includes mockfs. Its original BCM2837 development
configuration has no Image4/AMFI provider, so trust-cache runtime initialization
stays disabled: enabling it panics before platform start. The one-entry own
test trust cache is a loader input; its runtime trust membership is unverified.
The profile enables the existing native ARM PAN handling for user-memory copies;
the source A53 configuration otherwise leaves those accesses unbracketed on
MT6991 and QEMU A710. Actual user execution still requires the QEMU/hardware gates.
The explicit source entitlement-free option handles the absent AMFI query
context only for valid ad-hoc signatures with no XML/DER entitlement bytes or
nonzero signed entitlement slots. It leaves MACF and code-page hash checks in
place and rejects unsupported entitlement-bearing programs. CI requires the
actual kernel acceptance counter for the positive process and a separate signed
entitlement-bearing process to be refused before EL0. This provides no AMFI
implementation or Apple trust claim; actual qualification is pending.
Task entitlement queries in this development profile return false when the
provider is absent, including the privileged x18/UEXC ABI queries during exec.
The real provider path remains in use when present. QEMU observes the compiled
query-denial counter as well as the separate rejection of entitlement-bearing inputs.
`dali-pid1=1 dali-auto-fastboot=0` selects continuation after
the platform checkpoint, retaining the single-CPU early diagnostic sink. The
existing early-boot menu remains the hardware default until RAM-root packaging
and the new path are qualified. Do not enable its 31-second recovery watchdog
for the 30-second user test.

The owned program exercises getpid, HW_PAGESIZE, write-open refusal (EROFS) and
one-second select timeouts, then submits 31 sequential heartbeat requests through
`kern.dali_pid1`. The kernel checks the actual caller is PID1, the actual root
mount is read-only mockfs, the 4K ABI and at least 30 seconds of kernel monotonic
time. Only after its saved PASS does hardware request the existing AEE reset;
this does not establish automatic fastboot. The explicitly opted-in QEMU backend
requires the synthetic DT model and never touches MT6991 MMIO or claims ramoops
persistence. Full compilation, QEMU and hardware qualification are pending.

The explicit hardware profile is `package/unixnu-pid1.toml`. After a successful
CI run, `scripts/run-pid1-bootb.py` can first package a candidate with `--dry-run`
using the saved verified FDT, without contacting the device. Its required paths
are `--backup`, `--evidence-root`, `--uefi`, `--unixnu`, `--artifact-dir`, and the
backed-up `--serial`. A real run rechecks the live rooted Android B/FDT and all
18 protected hashes before using the existing literal `boot_b` writer. It disables
the firmware watchdog, checks exact artifact and payload identities, and requires
31 current-token ramoops messages with at least 30 seconds of kernel time after
restoring Android. If fastboot is unavailable after the bounded observation,
the pending restore is recorded and physical recovery is required. Neither USB
re-enumeration nor the end-reset request proves unattended fastboot.
The record includes the exact `--restore-run` command, stable device serial,
backup path and original boot hash. That mode only restores the original boot_b
and collects evidence; it never starts another candidate experiment.
Select `validation_profile=pid1` when dispatching `dali-xnu.yml` on this branch.
That profile forces `auto_fastboot=false`; its artifact records the PID1 boot
entry and cannot be used by the automatic early-checkpoint cycle runner.
QEMU reports are retained for one day on successful and failed qualification.

Device repository for REDMI K80 Ultra (dali / MT6991). The current target extends
the single-core XNU checkpoint through UniXNU and DarwinBoot into the owned
RAM-root PID1. Physical storage and complete iOS userland remain outside this stage.

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
`dali-xnu.yml` GitHub Action. The short-lived public artifact contains the
unprelinked arm64 kernel Mach-O, SHA-256 manifest and, for the PID1 profile,
the owned process and explicit one-entry test trust bundle; no IPSW, kernelcache,
device FDT, boot image, or ramoops logs are uploaded. The Action now defaults to
`reset-qualification`, disabling hardware WDT and firmware reset deadlines;
PID1 requires its separate explicit profile. Each hardware cycle builds a private
payload with a unique XNU log token and an explicit reset method.
The MT6991 watchdog interval is at most 31 seconds; the 120-second default is
the host's fastboot wait deadline. Feeds/BaseTools, source XNU, and EFI package
caches are separate. The kernel key hashes the checked-out tracked source graph,
all board settings except revision pins, and installed Xcode/SDK/LLVM/build-tool
versions. Only audited DarwinBoot-only inputs and root READMEs are excluded;
unknown inputs and other gitlinks invalidate the key. An exact hit reuses the
completed source kernel after checking its SHA-256, Mach-O and input stamp,
avoiding repeated archive compilation. A missing or invalid stamp rebuilds it.
There are no fallback cache keys. Every run still executes the complete DarwinBoot
fixtures and one-core source XNU QEMU gate, and artifact manifests require both
exact current repository pins. The first run of this cache format builds once.

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
stages through cache maintenance and immediately before MMU disable. The
transition then uses assembly without C calls or stack references; a guarded,
stack-free Dali kernel breadcrumb records cold-entry execution before its first
register access.
The entry checks a bounded Apple DT root `compatible="xiaomi,dali"` before
touching Dali persistent RAM; the generic 1 GiB QEMU machine skips this probe.
This early marker does not satisfy the platform/GIC/timer acceptance gate.
The previous hardware cycle had periodic binary overlays in its console. After
removing the post-MMU firmware C logging dependency, the next cycle recorded
`DALI:X0`, proving execution of the actual XNU assembly entry. Its console was
complete and readable (14,659 bytes, no NUL bytes). The owner confirmed the menu
entry with Power at 5 seconds and entered fastboot manually; automatic menu
timeout and fastboot recovery were not demonstrated. Android and all 18 backed
partition hashes were restored. No Dali platform/GIC/timer pass has been claimed.

The following hardware cycle recorded every `X0`–`X6` marker and an automatic
menu timeout. The owner used keys to return to fastboot; its last visible menu
frame did not describe the actual execution stage. Android and all 18 partition
hashes were restored. The next cycle proved every `X0`–`X9` and `C0`–`C9`
stage, including MMU enable, the high-VA C entry and CPU bootstrap. The owner
reported an unattended reboot while the display retained the menu countdown;
automatic fastboot was not reached. Android/root and all 18 partition hashes
were restored after manual fastboot. The exact reset cause remains unproved.

The current source kernel extends the bounded entry markers with a bootstrap
console mapping. It reuses the existing identity L2 table and requires both
console blocks to be empty and the table to lie in the owned bootstrap pool.
Only `[0xAEC00000, 0xAF000000)` within the verified ramoops reservation is added,
as Device-nGnRnE with execution disabled. A conflict records `DALI:XM` and skips
the extra sink. Generic machines skip it after the Dali DT guard. The mapping
approach was cross-checked against the earlier
[ios-sweet Dali port](https://github.com/localhosts-A/ios-sweet/tree/port/dali-mt6991),
with its 16 KiB assumptions replaced by checked 4 KiB table traversal.

| Marker | Last completed stage |
| --- | --- |
| `DALI:X0` | Cold entry and Dali Apple DT identification |
| `DALI:X1` | Debug OS lock register write |
| `DALI:X2` | Low exception vector register setup |
| `DALI:X3` | Boot arguments, stack registers and C entry address prepared |
| `DALI:X4` | Physical and kernel-virtual bootstrap tables populated |
| `DALI:X5` | TCR, TTBR and MAIR setup |
| `DALI:X6` | Ready to enable the MMU; enable itself is not yet proved |
| `DALI:X7` | MMU enabled and bootstrap console accessible |
| `DALI:X8` | Ready to return through the kernel-virtual entry bridge |
| `DALI:X9` | Kernel-virtual entry bridge executed |

Bounded `DALI:C0`–`DALI:C9` records then bracket C entry, pre-sign setup,
relocation, platform setup, CPU topology/bootstrap, startup and CPU init. This
first hardware version ended its sink before `arm_vm_init`; the absence of later
logs did not establish a stop inside that function. The current diagnostic adds
`V0`–`VD` records through VM setup and `CA`–`CI` through serial, platform and CPU
timebase setup. Before removing TTBR0, it installs two empty Device blocks in the
final TTBR1 low-globals/kernel gap. It checks the gap, table type, allocation
bounds, both empty entries and exclusion from the managed write-back physical
aperture, then moves the sink to that alias. A failed check
disables the sink without replacing any existing mapping. The current version
continues through `machine_startup` and the existing kernel bootstrap stage logs.
Each bounded write and shutdown saves, masks local IRQ/FIQ and restores DAIF.
This provides single-CPU ownership only. When `DaliBootLog` first enters, it
records `DL` and shuts down the early sink before using its ordinary mapping.
The platform/GIC/timer checkpoint is still required.
Local tests execute the exact mappers, MMU enable, high-VA return, final TTBR1
switch, TTBR0 removal and C writer, including conflicting tables, insufficient
VA gaps, invalid or full rings, overlong records and DAIF restoration on success
and rejection paths.

The next hardware cycle reached `V6` (final tables prepared), then safely refused
the alias because the first implementation used `VM_MIN_KERNEL_ADDRESS`, which
is 64 GiB below low globals in this XNU ABI. The mapper now uses
`LOW_GLOBAL_BASE_ADDRESS`; the instruction fixture uses both distinct native
constants. That cycle required manual fastboot and restored Android/root and all
18 original partition hashes. The corrected alias was then verified on hardware:
all `V0`–`VD` and `CA`–`CI` records appeared, proving final-table switch, TTBR0
removal, pmap/VM return, serial, platform and CPU timebase initialization.
Android/root and all 18 hashes were restored again after manual fastboot. The
next diagnostic continues through `machine_conf`, kernel bootstrap and IOKit;
those later stages remain unverified.

The next hardware cycle completed `machine_conf` and the kernel version print;
its last persistent stage was `zalloc` inside VM bootstrap. The current diagnostic
records startup callback entry/return addresses and zone initialization steps.
It also records assertion file/expression pointers and line numbers, panic
format/caller pointers, opaque context and raw ESR/FAR/ELR. Static pointers can be
resolved on the host using the exact kernel artifact; the guest does not inspect
panic format arguments or dereference the context. These records diagnose the
stop and do not bypass allocator checks. Android/root and all 18 original hashes
were restored after that cycle; full platform/GIC/timer acceptance is pending.

The callback trace subsequently proved that zone initialization returned. The
stop is a fatal `kalloc.c` assertion in `kalloc_init`:
`n_zones_sig + n_zones_type + 1 <= 32`. The source-only kernel's typed-allocation
metadata cannot use the upstream fixed-zone budget. Both hardware entry points
now pass `kt_fixed_zones=120`, retaining typed segregation and the bounds check.
The QEMU gate reads those same arguments instead of silently inheriting the
probe's budget. It also omits the budget in a separate run of the same kernel
and requires the original fatal assertion as a negative control. Hardware
acceptance and unattended fastboot recovery remain pending.

The first hardware cycle with the corrected budget completed the actual XNU
checkpoint: Darwin 23.6.0 / 10063.141.1, CPU/platform/GIC all 1, timer 1 and
MPIDR 0x81000000. Android/root and all 18 original hashes were restored. The
owner used keys, so this is an XNU success and does not count toward the three
unattended recovery cycles.

When the firmware recovery switch is enabled, packaging now forwards
`dali-auto-fastboot=1`. After a successful checkpoint, DaliBootLog validates the
FDT-derived RGU resource, verifies the existing bootloader marker and enabled
watchdog, then requests feature-checked vendor AEE PSCI SYSTEM_RESET2 using the live FDT's verified SMC conduit.
It keeps all RGU registers unchanged and writes no boot-mode or storage field.
The persistent `DALI-RESET` record precedes the secure call; a returned/refused
request fails the evidence gate. Disabling recovery forwards zero and skips this
path. Host gates require the matching reset record as well as XNU and Android
restoration. USB enumeration alone does not prove an unattended cycle; owner
observation must still exclude physical keys.

The first controlled reset trial also reached the XNU checkpoint unattended,
but exposed a fortified-copy trap while decoding the recovery properties.
Property decoding now copies complete local arrays before assigning fields.
CI compiles that actual decoder with subobject bounds checks and checks the
Mach-O call chain, PSCI FEATURES/RESET2 IDs/SMCs, DAIF save/restore and Device-memory barriers. Generic
QEMU success alone cannot validate this Dali-only reset path.

The repaired property decoder then reached the XNU checkpoint unattended and
issued SWRST, but CPU execution continued after 100 ms and automatic fastboot
failed. Android/root and all 18 original hashes were restored. The original
Android watchdog module also issues SWRST, but Linux registers its
PSCI restart handler at priority 129 ahead of watchdog priority 128. The following
SYSTEM_RESET trial then reached fastboot and restored Android/root
and all 18 original hashes, but lost all ramoops evidence. It cannot satisfy full
acceptance. Android reported PSCI 1.1. The current diagnostic queries RESET2
support before requesting architectural warm reset (type 0, cookie 0); a returned
call records feature/result status. Warm-reset retention remains unverified.

The architectural warm-reset trial also reached fastboot but lost
ramoops. The next controlled trial uses the original Dali mrdump module's vendor
AEE reset type `0x80000201` (KE domain reason), with the same native RESET2 function
and zero cookie. Packaging requires the live FDT's AEE mini/MRDUMP11 handoff;
the port does not write the mrdump control block or DDR-reserve registers.
This diagnostic reset is still unqualified for RAM retention and LK dump behavior.

Correction (2026-10-25): the owner recalled short-pressing Power to confirm the
menu in those cold/warm trials. Both are excluded from fully unattended
acceptance; the original records are retained with separate corrections. This
does not establish a held Power forced reset. The current qualification runner
retests each method without competing watchdogs or physical menu confirmation.

For the current ramoops diagnostic, build with `DALI_AUTO_FASTBOOT=false`.
This disables the firmware recovery watchdog/deadline and the XNU PSCI request.
After a saved XNU checkpoint, the kernel waits without arming a reset. Release
Volume Up first, then hold Volume Down and hold Volume Up for 0.7 seconds to
request a two-second watchdog reset; keep Volume Down held for LK fastboot.
GPIO60 is sampled without changing its mux or direction. This manual recovery
path is intended to avoid the power-key/PSCI resets that lost ramoops, and still
requires hardware qualification. It is not an unattended acceptance cycle.

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
