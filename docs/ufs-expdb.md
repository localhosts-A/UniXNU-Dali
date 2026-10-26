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

UEFI now has a default-off runtime producer: it validates the v3 wire against
independent RAM provenance and the compiled token before controller access,
then compares the observed UFS identity, geometry/WP and reciprocal GPT layout
after all four GPT reads. Only that final callback can fill the live grant.
The publisher requires an authorizer and follows the live grant for revocation;
stop and quiesce clear it before further submissions, including deferred busy
shutdown. The optional console-log consumer now has native fault tests and
RO/enabled/off ARM64 builds. The enabled build used a compile-only fixture wire;
it was not packaged or flashed. A dedicated trial builder is still required.
Standard fastboot fetch is unavailable on this LK. The operator explicitly
cancelled QR and the former pre-restore-export requirement; collection uses
original boot_b restoration followed by Android ramoops, as requested.

For each future round, keep the same candidate through write and read-only
reboot verification. The read-only child opens with reader access and cannot
replay a marker or payload. Keep ramoops enabled until actual log writing, device
sync and reboot/readback succeed, then reconnect/verify automatic fastboot before
disabling ramoops. None of the current software results qualifies that transition.

`restore-expdb-log.py --serial SERIAL --backup BACKUP --restore-run RUN` is a
recovery-only entry for an already armed `dali-expdb-log-trial` in the canonical
lease. It does not create or flash a test candidate. It reopens the sealed kernel,
source manifest, v3 grant, both baselines, full expdb image and raw GPT/identity
captures, and refuses a different pending run/token/device.

Recovery restores original boot_b, starts original Android B, stores ramoops and
exports the complete current expdb into a new private attempt folder. It decodes
the window against the sealed identity and retains corrupt/torn bytes as a failed
diagnostic. Collection/storage errors keep recovery pending. Empty successfully
collected pstore or an invalid expdb record cannot qualify writes/persistence.

Before its sole window write, the Android backend rechecks UFS identity, actual
LUN/block size/capacity, sysfs and Unit Descriptor write protection, reciprocal
GPT, GUID and partition range, Android B/root and all 18 original hashes. The
head and all bytes after the window must match the original backup. The complete
original window is staged as a regular file and checked remotely for exact length
and SHA before a bounded dd with fsync, followed by blockdev flush and a complete
partition hash readback. Actual Android Toybox lacks `iflag=fullblock`; no block
write consumes a potentially short input pipe. No write/sync failure is retried
automatically. Another explicit recovery can inspect and restore original bytes.

Only after the entire expdb matches its original hash and Android B/root/all 18
original hashes pass may the separate pending_expdb ledger flag be cleared.
Vendor modifications outside the window are exported and reported; they cannot
be hidden by overwriting the guard or labelled an exact restoration. Pending or
failed restoration prohibits the next round.
