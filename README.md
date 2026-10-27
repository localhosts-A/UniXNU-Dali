# UniXNU-Dali

Dali / MT6991 bring-up for UniXNU Format 2, targeting iOS 26.6.2 (23G90).
The migration is in progress: this branch does not yet contain a complete board
configuration, bootable firmware integration or qualified iOS 26 kernel.

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

Existing Dali hardware evidence covers earlier XNU and firmware milestones,
not the target OS. The new branch still needs SoC and board descriptions,
firmware integration, the 26.6.2 OS profile and ABI, and device validation.
See the [migration guide](https://unixnu.com/zh/porting/migration) and
[porting records](https://unixnu.com/zh/porting/records).
