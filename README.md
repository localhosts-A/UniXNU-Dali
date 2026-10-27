# UniXNU-Dali

Dali / MT6991 bring-up for UniXNU Format 2, targeting iOS 26.6.2 (23G90).
The migration is in progress. `board.toml` and `porting.toml` establish the
Format 2 device identity and staged record, but the board remains an example:
its CPU identities and reserved-memory map are unresolved, and its device tree,
firmware and payload are not wired for Format 2. It is not bootable yet.

The existing 32 MiB `unixnu_logs` partition uses four pairs of 4 MiB UEFI and
XNU slots. Its offline decoder and geometry checks were imported from
UniXNU-Dali-ufs-scratch commit `9cc8d87c636261abd8bc17415fc6feb0e380d771`.
To export a separately captured raw image without accessing a device:

```sh
python3 scripts/decode-userdata-logs.py /private/path/unixnu_logs.img /private/new-export
```

The output directory must not exist. This checks each record's integrity and
keeps a raw copy; a valid record does not by itself prove physical writes or
cross-reboot persistence. Never commit raw images, device identifiers, IPSW,
kernelcache, vendor firmware or other private input.

The proposed v2 schema, bounded slot scan, and oldest-safe-pair rotation rules
are recorded in [`docs/unixnu-logs-v2-design.md`](docs/unixnu-logs-v2-design.md).
The offline decoder handles v1 and the proposed v2 record metadata and
generation ordering; the focused host cases passed. Automatic writes and the
XNU storage owner remain unimplemented.

The pinned OS profile separates the nearest public XNU source tag from the
23G90 runtime kernelcache. The tag does not match the runtime XNU version; ABI,
prelink and target component compatibility remain open. See
[`docs/target-ios-26.6.2.md`](docs/target-ios-26.6.2.md) and
[`docs/hardware-inputs.md`](docs/hardware-inputs.md), plus the [migration
guide](https://unixnu.com/zh/porting/migration) and [porting
records](https://unixnu.com/zh/porting/records).
