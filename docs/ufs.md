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

The pinned firmware offers `DALI_UFS_PROBE=true` only for a three-argument
UEFI-only build, with `DALI_AUTO_FASTBOOT=false`, no reset method and a fresh
32-lowercase-hex `DALI_RUN_TOKEN`. The option defaults off. It checks the exact
live HCI/PHY/clock bindings and all 14 clock references, reads existing AO gate
status, then snapshots standard HCI and queue registers only when the required
clocks are already enabled. MCQ registers additionally require the capability
and version. It does not enable clocks, reset hardware, allocate DMA or submit
storage commands. Each bounded log line carries the build's unique token.

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
