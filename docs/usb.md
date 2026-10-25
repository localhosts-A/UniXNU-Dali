# Dali USB / USB-OTG offline milestone

The independent USB branch targets firmware and source-only XNU USB2 CDC ACM
logging, host/hub keyboards and mice, BOT external disks with FAT32 file
read/write, and MT6379 Type-C role selection. The UFS controller, its scratch
policy and the Linux unattended reader retain their existing ownership.

Current implementation consists of the portable protocol/ownership core and a
composed MTU3 USB2 CDC device MAC using QMU, EP0, notification IN1, log IN2 and
discard-only OUT2 in the pinned generic repository, plus the source and
saved-FDT resource verification tools here. Seven production C fixtures pass
Clang/GCC ASan/UBSan, and the standalone EDK2 AArch64 archive builds all six
portable/device C units. The combined
host-driven model covers CDC enumeration/DTR/log DMA, legal ZLP, reset/disconnect,
control abort/toggle preservation and retained DMA buffers after stop failure.
Actual UEFI/XNU binding and PHY/power/PMIF/TCPC/DMA ownership establishment,
native controller and hub child-service integration, source-XNU IOHID/storage
consumers, final-map handoff and native-engine QEMU gates are still pending. The
default-off and enabled firmware packages build, and the source PCI xHCI QEMU
class-stack gate passes separately; neither result proves the Dali native MMIO
controller. Nothing in this milestone enables USB in the ordinary boot profile.
`hardware_qualified=false`.

The generic repository now contains an OS-independent USB host coordinator for
xHCI-style address assignment, bounded configuration parsing, boot keyboard/mouse,
Mass Storage BOT, and USB2 external hubs. The hub service validates descriptors
and interrupt bitmaps, powers ports, resets new connections, emits child lifecycle
events, and builds single/multi-TT routes with think-time metadata. Multi-TT hubs
are explicitly switched to their alternate setting. Its Clang/GCC ASan/UBSan
models cover port bitmap boundaries through 127 ports, malformed/short/overlong
responses, power/reset deadlines and wrap, attach retry beyond the reset deadline,
partial-child cleanup on unplug, disconnect, overcurrent, route-depth limits and
single/multi-TT. This is protocol code only: the actual
MMIO controller worker, child-service owner, XNU IOHIDDevice/IOBlockStorageDevice
publication and Dali DMA resources remain separate.

An opt-in source-only XNU host-core profile is available separately from the
ordinary Dali early-boot profile. On the macOS XNU build host, configure and build
it in its own output root:

```sh
./unixnu defconfig dali_usb_defconfig -D ../UniXNU-Dali --out out/dali-usb
./unixnu build kernel --source-only -D ../UniXNU-Dali --out out/dali-usb
```

That profile builds `UsbCore.c` and `UsbHost.c`, including the hub coordinator,
from the XNU C compile record into `libDaliUsbHost.a` and force-loads the archive.
The existing `dali_defconfig`
leaves `XNU_NATIVE_USB_HOST` unset, so its archive builder skips the USB core.
The enabled source archive is not yet a registered XNU controller/class driver;
its final link and runtime gates remain open until those consumers are connected.

The device tree also contains a production SID14 PMIF-M transport under
`drivers/DaliUsb`, with actual identity gating, atomic shared-lock operations,
bounded completion/error handling, late-response ownership and explicit write
uncertainty. Its host register model passes Clang/GCC ASan/UBSan and AArch64
compilation. Runtime keypad/shared-lock wiring, TCPC, both repeaters and port
power/PHY initialization remain pending; the transport alone enables no port.
`docs/usb-platform-contract.md` records the fixed source locations and unresolved
platform dependencies for that work.

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
