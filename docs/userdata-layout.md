# Dali provisioned storage

The owner explicitly allowed discarding Android applications/files and carving
32 GiB out of userdata on 2026-10-26. This is a permanent layout change. The
original GPT and adjacent flashinfo were captured before any write.

Actual LUN: 2. Logical block: 4096 bytes. Boundaries below are inclusive UFS LBAs.

| GPT name | Number | First LBA | Last LBA | Bytes | Purpose |
| --- | --- | --- | --- | --- | --- |
| userdata | 95 | 3454240 | 56184823 | 215984472064 | Android F2FS |
| unixnu_data | 97 | 56184824 | 64565239 | 34326183936 | exFAT; IPSW and other files |
| unixnu_logs | 98 | 64565240 | 64573431 | 33554432 | Raw diagnostic journal |
| flashinfo | 96 | 64573432 | 64577527 | 16777216 | Existing partition, preserved |

The two new partitions total 34359738368 bytes (32 GiB). Original userdata began
at the same LBA and ended at 64573431. Every other old GPT row is unchanged.
The full 96-entry table was extended to 128 entries inside existing GPT gaps:
primary array 2..5, backup array 64577528..64577531. Header locations and usable
boundaries remain unchanged; the zero disk GUID was preserved.

`scripts/ufs_userdata_layout.py` validates reciprocal GPT CRCs, computes the split,
and produces four bounded patches. Its transaction writes backup entries/header
before primary entries/header, with a durable attempt record, real flush and
complete readback for each patch. A failed write is never automatically replayed.

Provisioning used TWRP 3.7.1 with root ADB. Data was unmounted before the GPT
transaction, then Recovery was rebooted to discover the new block devices. The
first format attempt refused an unused device-mapper holder. After confirming
open_count=0 and its sole dependency sdc95, that mapping was removed and TWRP
formatted the reduced userdata successfully. Its two F2FS superblocks declare
215984472064 bytes. exFAT creation and unmount/remount manifest readback passed;
the raw log partition was zeroed, synced and fully hashed.

Evidence is private under `device-backups/dali-userdata-log-20261026/` in the
workspace. The dedicated transaction/pending record blocks legacy UFS runners
until Android and the new geometry have been verified. Restoring original GPT
metadata cannot recover intentionally discarded Android files.

The data filesystem contains `IPSW`, `Logs`, `Payloads`, and `Manifests`.
`Manifests/layout.json` records its actual partition identities. The log partition
is a separate block device; do not mount it or extend exFAT into it. Existing
boot_b/expdb grants do not authorize this region. The explicit
`userdata-logs-diagnostic-v1` profile binds a fresh token to one adjacent 4 MiB
UEFI/XNU owner-slot pair. UEFI and XNU write snapshots with real device syncs and
readbacks. The XNU body includes the kernel msgbuf ring and indices, a controller
diagnostic structure, and the Dali console ring.

After a trial returns to rooted Android and original `boot_b` is restored, the
host runner reads the whole raw partition twice, validates each slot, and copies
`unixnu_logs.img`, decoded owner files, and captured ramoops into
`/sdcard/UniXNU/Logs/<token>`. It mounts exFAT at `/data/media/0/UniXNU` during
export so Android applications can read the files. Manual raw inspection is also
possible from rooted Android:

```sh
su -c 'dd if=/dev/block/by-name/unixnu_logs bs=4096 count=8192 status=none'
```

The raw partition is currently verified empty. Software wiring and host tests do
not qualify physical writes or persistence. Ramoops remains enabled, and the
automatic-fastboot/ramoops transition stays disabled until same-candidate write,
sync, and restart readback succeed.
