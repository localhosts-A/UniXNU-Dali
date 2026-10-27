# `unixnu_logs` automatic logging design

**Status:** v1/v2 offline decoding is implemented. UEFI source now has an empty-pair-only read scanner, selected-slot grant rebinding, and a PCD-gated automatic call to the existing v1 writer; eight synthetic C host cases pass and the DEBUG firmware build compiles. No device validation has occurred. Full v1/v2 classification, runtime boot identity, rotation, v2 writes, and XNU ownership remain incomplete.

## Existing partition and records

The target is the existing `unixnu_logs` GPT partition on UFS LUN 2: 4 KiB
blocks, start LBA `64,565,240`, length 8,192 blocks (32 MiB), and partition
GUID `cde9ab32-18d8-4777-8ced-691e7cd5c636`. It contains four logical boot
pairs. Each pair has one 4 MiB UEFI slot followed by one 4 MiB XNU slot:

| Pair | UEFI slot LBAs | XNU slot LBAs |
|---|---:|---:|
| 1 | 64,565,240–64,566,263 | 64,566,264–64,567,287 |
| 2 | 64,567,288–64,568,311 | 64,568,312–64,569,335 |
| 3 | 64,569,336–64,570,359 | 64,570,360–64,571,383 |
| 4 | 64,571,384–64,572,407 | 64,572,408–64,573,431 |

The v1 header uses `DALIUFSLOG1`, owner 1/2, and `Sequence` 1–4 to identify
the physical pair. It has SHA-256 body and record digests but no boot
generation or timestamp. The 32-byte ASCII token is currently a build input.
Consequently v1 records cannot be ordered by age from their contents. Preserve
the verified raw backup and keep v1 records readable; do not infer their order
from pair number.

## Version 2 record identity

Keep `Sequence` as the physical pair index for backwards-compatible geometry,
and add a distinct 64-bit `Generation`. Use a new `DALIUFSLOG2` magic and
version 2. Keep the v1 fields and offsets through the record digest at
`0xD4–0xF3`. Add these little-endian fields to each 4 KiB record block:

| Offset | Field |
|---|---|
| `0xF4–0xFB` | 64-bit `Generation` |
| `0xFC–0xFF` | 32-bit flags; bit 0 means body capture was truncated; all other bits must be zero |
| `0x100–0x103` | capture stage: 1=UEFI before handoff, 2=XNU capture |
| `0x104–0x107` | body format: 1=raw UEFI console bytes, 2=`DALI-XNU-BODY1` |
| `0x108–block end` | zero padding |

The record digest covers the v2 fields and zero padding with its own digest
field zeroed. Each record must bind:

- physical pair index, `Generation`, owner, and a per-boot token;
- device binding, block geometry, target slot bounds, and target input digests;
- body format, byte length, capture stage, and truncation flags;
- body digest and a record digest covering all v2 metadata, with its digest
  field zeroed while calculating.

Generate the token from 16 bytes returned by `EFI_RNG_PROTOCOL`, encoded as 32
lowercase hexadecimal characters. Generate it once per boot and pass the same
value to the later XNU capture. Do not derive the token from `Generation` alone:
an interrupted, uncommitted attempt may reuse the next generation on a later
boot. If runtime entropy is unavailable, skip the persistent UFS write and
leave early diagnostics in ramoops.

Increment `Generation` from the highest fully committed v2 generation found
among all records. Use checked addition and refuse to wrap. The UEFI and XNU
records for one boot must carry the same generation, token, and pair index.
`Generation` orders committed records; the random token distinguishes attempts
that did not commit.

The UEFI body consists of captured console bytes and its record flags mark
truncation. The XNU body uses the existing `DALI-XNU-BODY1` payload, which
identifies diagnostics, the real
`msgbuf` ring cursors, console bytes, capture stage, and truncation. UEFI can
only record output available before its handoff; it cannot claim to contain
later DarwinBoot or XNU messages. The XNU record must be produced by an XNU-side
storage owner after it captures those messages.

## Read-only scan and pair selection

Run the scanner only after the device identity, LUN geometry, and primary and
backup GPT have been checked. Restrict every request to the exact 8,192-block
partition extent above, use 4 KiB alignment, serialize access while media is
stable, and cap each transfer at 1 MiB. Never scan or write adjacent `userdata`
sectors. The current UEFI source has a separate read-only parent-LUN scanner
that checks the exact `unixnu_logs` geometry, reads the eight owner slots in
bounded chunks, and selects a fully zero pair before publishing a single-UEFI-
slot grant. Each read remains under the scratch driver's admission fence.

Classify every owner slot as one of:

| State | Rule | Rotation behavior |
|---|---|---|
| Empty | Entire 4 MiB slot is zero | First choice for a new pair |
| Valid v1 | Full v1 header, body and commit verification succeeds | Preserve permanently; never select for automatic writes |
| Valid v2 | Header, commit, body digest, record digest, pair, owner, token and generation agree | Eligible for oldest-generation rotation |
| Interrupted | Recognizable v2 attempt exists without a matching committed final record | Never count as committed; preserve unless a safe empty pair is available |
| Unknown/corrupt | Nonzero bytes do not validate as a known supported schema | Never treat as empty or overwrite automatically |

Choose a wholly empty pair first. When all pairs are occupied, replace the pair
with the lowest valid v2 generation only if both owner slots are empty or valid
records consistent with that pair. Never overwrite a valid v1 slot: older
firmware can still write a new v1 record into an empty slot, and v1 has no
generation with which to order that record against v2. A v1 slot remains pinned
for preservation. If an unknown/corrupt, interrupted, or inconsistent pair
would have to be reused, skip this boot's write and preserve the remaining
records. Never choose a candidate from a corrupt GPT, a mismatched partition
identity, an ambiguous generation, or a failed full-record verification.

This policy can reduce the v2 ring capacity when v1 records occupy pairs; this
is the cost of preserving them without assuming rollback cannot occur. If no
fully empty or safely replaceable v2 pair remains, fail closed rather than
silently migrating or erasing v1 records.

The design requires validating all eight slots and classifying the supported
schemas before selection. The current interim scanner reads every slot but only
distinguishes all-zero from nonzero: every nonzero slot, including valid,
interrupted, corrupt, or unknown data, is treated as occupied. It never rotates
or overwrites a nonzero slot and fails closed when no empty pair remains. It
does not yet establish that a v1 or v2 record is valid. The offline decoder
implements v1/v2 record checks and shows v1 order as unknown, while sorting v2
by generation. Keep SHA-256 as corruption detection; neither format has keyed
authentication or claims tamper resistance.

## Commit sequence and owners

Keep the existing three-stage UEFI write protocol: write an attempt record,
write the body and metadata, then write the final commit record. Synchronize
media between phases and read the entire selected slot back before reporting
success. Bind generation and token into each phase. A failed write, sync,
readback or digest check stops this boot's persistent write; the other valid
pairs remain untouched.

The UEFI integration point is after UFS initialization and before controller
quiesce. The source now uses the read-only scan result to publish a grant for
exactly one UEFI slot and calls the existing v1 writer automatically when the
log PCD is explicitly enabled. The default PCD remains disabled. The 240-byte
build-time authorization wire still provides a preferred physical sequence and
build-time token; authorization rebinds the sequence/window after scanning.
That token is not unique per boot, and this path has no v2 generation, safe
rotation, or XNU writer. The code has passed eight synthetic host cases and a
DEBUG compile using a temporary payload fixture; neither proves device behavior
or produces a bootable image.

XNU persistence is a separate milestone. The current UEFI logger rejects the
XNU owner, the `msgbuf` snapshot helper has no caller, and storage quiesce does
not transfer UFS ownership or provide an XNU driver. Before adding an XNU
writer, define and implement either a verified controller/DMA ownership
handoff or an XNU-side UFS transport. Until then, ramoops remains the early and
failure-path fallback, not a substitute for `unixnu_logs` persistence.

## Enablement gates

Keep the default log PCD disabled until all of the following exist. The current
explicit build profile is experimental and can exercise the v1 auto-call path;
it is not a device-validated persistent logging configuration.

1. Runtime entropy source and v2 boot token generation.
2. Shared v1/v2 offline decoding and bounded read-only slot classification.
3. Dynamic per-slot grants and an oldest-safe-v2 pair allocator.
4. Interrupted-write recovery, fail-closed handling, and explicit truncation
   reporting.
5. An XNU storage owner and handoff that cannot access outside `unixnu_logs`.
6. User-approved host tests for the new scan, rotation and failure cases, then
   separately approved device validation of sync and cross-reboot persistence.

