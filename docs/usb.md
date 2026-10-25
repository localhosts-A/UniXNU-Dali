# Dali USB / USB-OTG offline milestone

The independent USB branch targets firmware and source-only XNU USB2 CDC ACM
logging, host/hub keyboards and mice, BOT external disks with FAT32 file
read/write, and MT6379 Type-C role selection. The UFS controller, its scratch
policy and the Linux unattended reader retain their existing ownership.

Current implementation consists of the portable protocol/ownership core and a
composed MTU3 USB2 CDC device MAC using QMU, EP0, notification IN1, log IN2 and
discard-only OUT2 in the pinned generic repository, plus the source and
saved-FDT resource verification tools here. Five production C fixtures pass
Clang/GCC ASan/UBSan; five C units compile freestanding AArch64. The combined
host-driven model covers CDC enumeration/DTR/log DMA, legal ZLP, reset/disconnect,
control abort/toggle preservation and retained DMA buffers after stop failure.
Actual UEFI/XNU binding and PHY/power/PMIF/TCPC/DMA ownership establishment,
host/hub/OS integration, FAT/HID source archives, complete
firmware/macOS builds and QEMU USB gates are still pending. Nothing in this
milestone enables USB in the ordinary boot profile. `hardware_qualified=false`.

## Fixed inputs

`references/usb-sources.toml` fixes the Linux, MTK device-module, IOHIDFamily,
msdosfs and IOStorageFamily commits. Fetch into a separate directory:

```sh
python3 scripts/fetch-usb-references.py --output /private/usb-inputs
python3 scripts/fetch-usb-references.py --output /private/usb-inputs --verify-only
```

The fetcher accepts full commit pins, requires clean independent repositories,
checks required source files and records their SHA-256 in `source-manifest.json`.
It preserves third-party source trees and their original license files. It
does not contact a phone. Existing UFS reference checkouts are never modified.

## Saved FDT verification

```sh
python3 scripts/usb_resources.py --fdt /private/runtime.dtb --output /private/usb-resources.json
python3 -m unittest discover -s tests -p 'test_usb_resources.py' -v
```

The extractor checks USB0 MAC/IPPC/xHCI addresses and interrupt specifiers,
identity bus translation, clock/domain/PHY provider references, calibration
references and masks, bus-protection offsets, SPMI SID14 routing, TCPC ADC and
charger/VBUS ownership, repeater chain and the 5V regulator range. A changed
contract, disabled or unresolved provider, duplicate handle or invalid extent
is refused. Actual efuse values, clock state, MMIO ownership and DMA mappings
are not established by these static checks and remain explicitly unqualified.

Android's observed UDC/device/sink high-speed connection identifies the stock
Linux path only. It does not qualify UEFI, XNU, host operation or role switching.
Offline protocol tests, register models, QEMU and hardware evidence must remain
separate. Any later device inspection must acquire the existing nonblocking
device lease and skip an occupied lease or pending restoration. This offline
milestone performs no flash, reboot, role override, module change or physical
media write.
