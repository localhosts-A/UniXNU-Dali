# `unixnu_logs` automatic logging design

**Status:** design proposal; automatic writes and v2 decoding are not implemented.

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
version 2. Each record must bind:

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

Use explicit body schemas. The UEFI body should identify the captured console
range and UEFI end stage. The XNU body should identify diagnostics, the real
`msgbuf` ring cursors, console bytes, capture stage, and truncation. UEFI can
only record output available before its handoff; it cannot claim to contain
later DarwinBoot or XNU messages. The XNU record must be produced by an XNU-side
storage owner after it captures those messages.

## Read-only scan and pair selection

Run the scanner only after the device identity, LUN geometry, and primary and
backup GPT have been checked. Restrict every request to the exact 8,192-block
partition extent above, use 4 KiB alignment, serialize access while media is
stable, and cap each transfer at 1 MiB. Never scan or write adjacent `userdata`
sectors. The current single-slot grant cannot perform this scan; add a separate
read-only parent-LUN scanner before publishing a selected-slot grant.

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

The scanner validates all eight slots before selection. It may read in bounded
chunks, but it must validate the whole candidate slot before calling it valid.
The offline decoder should implement the same v1/v2 rules and show v1 order as
unknown, while sorting v2 by generation. Keep SHA-256 as corruption detection;
the existing format has no keyed authentication and the new schema must not
claim tamper resistance.

## Commit sequence and owners

Keep the existing three-stage UEFI write protocol: write an attempt record,
write the body and metadata, then write the final commit record. Synchronize
media between phases and read the entire selected slot back before reporting
success. Bind generation and token into each phase. A failed write, sync,
readback or digest check stops this boot's persistent write; the other valid
pairs remain untouched.

The UEFI integration point is after UFS initialization and before controller
quiesce. Replace build-time fixed slot selection with the result of the
read-only scan, then publish a grant for exactly one UEFI slot. The current
build-time 240-byte authorization record, fixed `Sequence`, manual menu, and
default-disabled PCD do not support that path.

XNU persistence is a separate milestone. The current UEFI logger rejects the
XNU owner, the `msgbuf` snapshot helper has no caller, and storage quiesce does
not transfer UFS ownership or provide an XNU driver. Before adding an XNU
writer, define and implement either a verified controller/DMA ownership
handoff or an XNU-side UFS transport. Until then, ramoops remains the early and
failure-path fallback, not a substitute for `unixnu_logs` persistence.

## Enablement gates

Keep automatic writes disabled until all of the following exist:

1. Runtime entropy source and v2 boot token generation.
2. Shared v1/v2 offline decoding and bounded read-only device slot scanning.
3. Dynamic per-slot grants and an oldest-safe-pair allocator.
4. Interrupted-write recovery, fail-closed handling, and explicit truncation
   reporting.
5. An XNU storage owner and handoff that cannot access outside `unixnu_logs`.
6. User-approved host tests for the new scan, rotation and failure cases, then
   separately approved device validation of sync and cross-reboot persistence.

