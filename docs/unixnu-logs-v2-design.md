# `unixnu_logs` automatic logging design

**Status:** v1/v2 offline decoding and bounded eight-slot classification are implemented. UEFI selects an empty pair first; when none exists, it can rotate only the oldest fully verified UEFI v2 slot whose XNU sibling is all zero, and only when another committed UEFI v2 record remains. Before replacement, the writer revalidates the victim identity and full body, clears and syncs the complete 4 MiB slot, verifies the zero readback, then writes and verifies the new three-phase v2 record. Host simulations pass: scanner 11 cases, selector/grant 14 cases, writer/token/replacement 9 cases. A DEBUG build verified 59 images with a temporary seven-byte non-bootable payload fixture. Logs were disabled; build artifacts, fixture and temporary Python dependencies were removed. Runtime entropy, XNU ownership, device writes, and cross-reboot behavior remain unverified.

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
generation or timestamp. Existing records use a build-time token. The updated
UEFI call path supplies a separate runtime record token for new writes; it does
not alter the build-time grant token. V1 records still cannot be ordered by age
from their contents. Preserve the verified raw backup and keep v1 records
readable; do not infer their order from pair number.

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

The current Dali integration adds `RngDxe` with the CPU `BaseRngLib` RNDR
source. The provider checks the runtime CPU capability before exposing RNDR;
BDS skips persistent logging when the protocol or entropy request is unavailable
and rejects an all-zero result. The DEBUG firmware build compiles this provider and token path, but logging
was disabled in that build, so it did not request a token or write a record.
Target-device entropy availability remains unverified. The existing BDS recovery token
is a separate value and is not used as log identity.

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
sectors. The current UEFI source has a separate read-only parent-LUN scanner that
checks the exact `unixnu_logs` geometry and classifies all eight owner slots in
bounded chunks. It selects an empty pair first or a verified rotation victim
within the limits below before publishing a single-UEFI-slot grant. Each read
remains under the scratch driver's admission fence.

Classify every owner slot as one of:

| State | Rule | Rotation behavior |
|---|---|---|
| Empty | Entire 4 MiB slot is zero | First choice for a new pair |
| Valid v1 | Full v1 header, body and commit verification succeeds | Preserve permanently; never select for automatic writes |
| Valid v2 | Header, commit, body digest, record digest, pair, owner, token and generation agree | UEFI v2 is eligible only with an empty XNU sibling and at least two committed UEFI v2 records overall; a pair containing XNU data remains pinned |
| Interrupted | Recognizable v2 attempt exists without a matching committed final record | Never count as committed or select for rotation; preserve |
| Unknown/corrupt | Nonzero bytes do not validate as a known supported schema | Never treat as empty or overwrite automatically |

Choose a wholly empty pair first. The current UEFI grant covers one UEFI slot,
not its XNU sibling, so replacement is limited to a fully verified UEFI v2
record whose XNU sibling is entirely empty. Do not reclaim a pair containing a
valid XNU record until an XNU owner and pair-scoped authority exist. Require at
least two committed UEFI v2 records before replacement so an interrupted
in-place clear/write leaves another verified UEFI record. Select the lowest
generation among eligible victims. Never overwrite a valid v1 slot: older
firmware can still write a new v1 record into an empty slot, and v1 has no
generation with which to order that record against v2. A v1 slot remains pinned
for preservation. If an unknown/corrupt, interrupted, inconsistent, or XNU-owned
pair would have to be reused, skip this boot's write and preserve the remaining
records. Never choose a candidate from a corrupt GPT, a mismatched partition
identity, an ambiguous generation, or a failed full-record verification.

This policy can reduce the v2 ring capacity when v1 records occupy pairs; this
is the cost of preserving them without assuming rollback cannot occur. If no
fully empty or safely replaceable v2 pair remains, fail closed rather than
silently migrating or erasing v1 records.

The UEFI source now classifies all eight slots before selection. It verifies
record block digests and padding, the three-record attempt/body/commit identity,
owner and physical pair sequence, device binding, body digest and zero padding.
It also checks the `DALI-XNU-BODY1` header fields that the offline decoder uses.
It recognizes an attempt without a matching final record as interrupted, keeps
other nonzero failures unknown/corrupt, and derives `max_generation + 1` only
from fully verified committed UEFI v2 records. Duplicate committed generations
and generation overflow fail closed. Orphaned or mismatched v2 owner records
are reported as inconsistent and remain pinned; they do not block selection of
an independent empty pair or eligible victim.

Selection consumes this same scan result, so it reads the partition once while
the scratch admission fence is held. It prefers a wholly empty pair, otherwise
it records the selected oldest eligible UEFI v2 victim and its generation,
token, input digests and body digest in the scan result. The selector refuses
rotation when fewer than two committed UEFI v2 records exist. Unknown, corrupt,
interrupted, v1 and nonempty XNU sibling slots remain preserved. The offline
decoder additionally verifies the exported XNU body and presents v1 order as unknown while sorting v2 by
generation. SHA-256 detects corruption; neither format has keyed authentication
or claims tamper resistance.

## Commit sequence and owners

Keep the existing three-stage UEFI write protocol: write an attempt record,
write the body and metadata, then write the final commit record. For a selected
victim, first re-read and verify all three v2 records, record hashes, body digest,
generation, token, input digests and current device binding against the scan
identity. Clear the complete 4 MiB slot, synchronize it and read it back as
zero before writing the new attempt/body/commit records. Synchronize media
between phases and read the entire selected slot back before reporting success.
Bind generation and token into each phase. A failed victim check performs no
write; a failure during clear, sync, write, readback or digest checking stops
this boot's persistent write immediately and never tries another victim. An
interrupted replacement can lose its victim, which is why another committed
UEFI v2 record must remain outside that victim.

The UEFI integration point is after UFS initialization and before controller
quiesce. The source uses the read-only scan result to publish a grant for exactly
one UEFI slot and writes v2 records when the log PCD is explicitly enabled.
The scanner's checked `NextGeneration` is passed separately from physical pair
`Sequence` and the runtime record token. The live grant remains unchanged. The
logger writes to an empty pair or to the selected eligible victim. For a victim,
it performs a full identity/body recheck and clear/readback before binding the
new generation and token into attempt, body and commit records. It synchronizes
between phases and verifies the full slot after commit. Rotation remains limited
to an empty XNU sibling; XNU owner and token handoff are not implemented. The
default PCD remains disabled.
The log protocol is revision 3: WRITE requests generation 0 so the host chooses
the next value; READBACK uses generation 0 for legacy v1 or an explicit nonzero
generation and token for v2.

The v1/v2 scanner passed 11 synthetic cases, the selector/grant suite passed 14,
and the v2 writer/token/replacement suite passed 9. Host SHA implementations
are deterministic test shims, not cryptographic SHA-256 verification. A DEBUG
build verified 59 images with a temporary seven-byte non-bootable payload
fixture; temporary Python dependencies, build output and fixture were removed.
The log PCD remained disabled, so this build did not perform a runtime write or
prove target entropy availability. No device behavior has been validated.

XNU persistence is a separate milestone. The current UEFI logger rejects the
XNU owner, the `msgbuf` snapshot helper has no caller, and storage quiesce does
not transfer UFS ownership or provide an XNU driver. Before adding an XNU
writer, define and implement either a verified controller/DMA ownership
handoff or an XNU-side UFS transport. Until then, ramoops remains the early and
failure-path fallback, not a substitute for `unixnu_logs` persistence.

## Enablement gates

Keep the default log PCD disabled until all of the following exist. The current
explicit build profile can exercise empty-first v2 logging and limited UEFI-slot
rotation; it is not a device-validated persistent logging configuration.

1. Runtime entropy source verified on the target and shared-token handoff to XNU; v2 token/generation encoding is implemented.
2. Shared v1/v2 offline decoding and bounded read-only slot classification are implemented and host-tested.
3. UEFI single-slot dynamic grants and oldest eligible UEFI v2 rotation are implemented; pair-wide reclamation remains gated on XNU ownership and handoff.
4. Interrupted-write recovery, fail-closed handling, and explicit truncation
   reporting.
5. An XNU storage owner and handoff that cannot access outside `unixnu_logs`.
6. Host tests for the scan, rotation and failure cases, then separately
   authorized device validation of sync and cross-reboot persistence.

