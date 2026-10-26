# Temporary expdb write diagnostic

The operator explicitly changed the diagnostic target to expdb on 2026-10-26.
This replaces the boot_b-only write boundary for the new profile. It does not
reduce the UEFI/XNU, synchronization, three fresh-token persistence rounds or
original Android recovery requirements. Historical boot_b grants and evidence
retain their original meaning.

Expdb contains vendor crash/log data. Its apparently zero region is not a
documented free-space allocation. The temporary diagnostic preserves a complete
original backup and limits submissions to the 1 MiB window at partition offset
1 MiB. All bytes before and after that window remain outside write admission;
the latter includes at least 4 MiB of tail guard. The actual captured device has
4096-byte logical blocks, expdb at LUN2/LBA2184+32768 and a proposed writable
range LBA2440..2695. These are observations, not defaults for another device.

## Wire and runtime policy

| Profile | ADT property | Version / flags | Size | Target |
| --- | --- | --- | --- | --- |
| `boot-b-scratch-write-test-v1` | `scratch-grant-v2` | 2 / 1 | 232 B | legacy boot_b |
| `expdb-scratch-write-test-v1` | `scratch-grant-v3` | 3 / 2 | 236 B | appended uint32 target=1 |

The new schema shares the first 232 byte field offsets and appends an explicit
target word. A 232-byte v3, a 236-byte v2, unknown target, duplicate properties
or cross-version property/wire combination is rejected. A boot_b wire cannot be
turned into an expdb grant by replacing its LBAs. Both grant and independently
verified partition observation must name the same target at the write gate.

Internal `bootStartLba` / `bootBlocks` fields describe the selected partition.
For expdb, `payloadEndBytes` is zero: the candidate boot image remains in boot_b.
The candidate SHA still binds the complete source-only kernel payload, and the
source SHA still binds the four compiled revisions and that kernel digest.
Identity, logical block size, capacity, GUID, range, token, device WP, provenance
and published scratch-media checks remain required before lower submissions.
The matching partition name is selected explicitly; ordinary LUN devices remain
read-only. Real synchronization, sticky errors and no uncertain-write replay
continue to apply.

## Host evidence

`build_xnu_scratch_grant(..., target_partition="expdb", expdb_baseline=...,
expdb_dir=...)` requires the existing original-Android baseline, its identity
capture directory and original boot_b bytes, plus the new expdb baseline.
The validator rereads the complete regular `expdb.img`, all four bounded GPT
captures, reciprocal CRCs, the partition identity/geometry/WP, and checks the
table against both baselines. The backup hash must equal both device reads.
The fixed window must contain zero bytes for the diagnostic's record format.
The proposal retains full-partition, original-window, head and after-window
hashes. SHA values in JSON alone cannot substitute for these file bytes.
Candidate-container validation is separate from the expdb partition extent.

These helpers perform no device operations and do not establish freshness or
runtime authorization. A future runner must acquire the canonical lease,
recapture/check current originals, seal the final candidate and fresh token,
and maintain identity-bound pending recovery through every failure path.

## Qualification and recovery still required

The implementation has native policy/wire/IO fault tests and a replay of the
actual backed-up expdb. UEFI RO/off ARM64 builds are separate evidence. The
changed XNU adapter still needs its own source-only Apple build and hardware
qualification. None of these results is an expdb device-write or persistence pass.

UEFI's static live grant remains zero. No expdb runtime grant producer or
complete write runner is enabled. Standard fastboot fetch was tested and is
unavailable on this LK; a real pre-restore evidence-export path remains required.
No existing post-restore Android pstore collector may be counted as that path.

For each future round, keep the same candidate through write and read-only
reboot verification. The read-only child opens with reader access and cannot
replay a marker or payload. Export the evidence before restoring original data.
Restore expdb's original bytes and verify them at the defined pre-Android stage,
then restore the complete original boot_b and verify Android B/root/all 18
original partition hashes. Vendor writes to expdb during original Android boot
must be reported separately; they cannot be hidden as an exact restore match.
Pending or failed restoration prohibits the next round.
