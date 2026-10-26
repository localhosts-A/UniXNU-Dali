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
responses, delayed-control power/reset deadlines and wrap, stop→restart reuse,
attach retry beyond the reset deadline, partial-child cleanup on unplug,
disconnect, overcurrent, route-depth limits and single/multi-TT. This is protocol
code only: the actual
MMIO controller worker, child-service owner, XNU IOHIDDevice/IOBlockStorageDevice
publication and Dali DMA resources remain separate.

An opt-in source-only XNU host-core profile is available separately from the
ordinary Dali early-boot profile. On the macOS XNU build host, configure and build
it in its own output root:

```sh
./unixnu defconfig dali_usb_defconfig -D ../UniXNU-Dali --out out/dali-usb
./unixnu build kernel --source-only -D ../UniXNU-Dali --out out/dali-usb
```

That profile builds `UsbCore.c`, `UsbHost.c`, and the serialized BOT block-I/O
adapter from the XNU C compile record into `libDaliUsbHost.a` and force-loads
the archive. The adapter bounds/chunks IOStorage requests, keeps BOT commands
serialized, reports only complete chunks, and closes admission before cache
synchronization or detach. SCSI MODE SENSE(6) probes the device write-protect
bit; it tries page `0x3f`, falls back to page `0`, and follows the common disk
policy by treating two rejected probes as writable while preserving failures
from the BOT transport itself.

The same profile now builds and force-loads `libDaliUsbStorage.a`, a concrete
source-XNU `IOBlockStorageDevice` child for BOT LUN 0. The host owner can publish
it with `dali_usb_storage_publish()`, then must call `dali_usb_storage_quiesce()`
before stopping the controller, prove endpoint/slot DMA has stopped before
`dali_usb_storage_detach(..., true)`, and terminate it only after that proof.
The service owns its 64 KiB bounce buffer and refuses to free it without the
controller-stop proof. No production XNU host owner calls this API yet, so this
archive does not claim USB enumeration, filesystem access, or runtime binding.

`build-dali-usb-storage.py` compiles against the fixed public IOStorageFamily
header using XNU's C++ compile record; after the kernel link,
`--verify-kernel` checks the concrete class, public publication API, BOT
adapter, and source `IOBlockStorageDevice` definitions in the actual Mach-O.
The `dali-usb-xnu.yml` macOS workflow builds the opt-in source-only profile and
retains bounded text evidence. It performs no device access or physical-media
test.
The macOS 26/Xcode 26.6 [source-only build 40243105789](https://github.com/localhosts-A/UniXNU-Dali/actions/runs/40243105789)
passes at device commit `0a9a1d1c977c61e7bcdab7d743a1d30c2171b059`.
It compiles the actual USB C core, this C++ child, and public IOStorageFamily,
then verifies their definitions in the linked source XNU kernel (SHA-256
`e580ea62f9f9468d60535a3af7664580c9e2c707c4e7771323813116d6d2cd36`).
The retained CI evidence contains reports and logs; kernel/archive binaries
are not uploaded. Runtime binding and hardware remain unqualified.
The existing `dali_defconfig`
leaves `XNU_NATIVE_USB_HOST` unset, so its archive builder skips the USB core.
The same profile passes `--usb-host` to the ADT generator when packaging a
payload, adding the versioned xHCI resource node only after the saved live FDT
matches the fixed MMIO and interrupt contract. The default profile omits that
node. See `docs/usb-xnu-adt-resources.md` for its binary layout and limitations.
The enabled source archive is not yet a registered XNU controller/class driver;
the block service is linked but is not instantiated until a production host
owner is connected. The XNU USB controller, HID report service, runtime FAT32
test, and native USB QEMU path remain open gates.

The opt-in profile now selects `XNU_SOURCE_HID_FAMILY` and the fixed public
IOHIDFamily-2031.120.4 source build. The builder follows the Embedded target's
source phase and generates its three real IIG bridges. The 2031 import and
Xcode 26.6 public SDK omit generated event declarations; these are now
separately pinned from Apple's IOHIDFamily-1915.140.3 public source. The builder
records their hashes. An explicit source port uses the published 1915 event
layout: digitizer twist uses its real field, nonzero M1–M4 controller buttons
are refused, and synthesized estimated-roll fields are unsupported. This does
not reproduce the unpublished 2031 event ABI. Source compile/link and runtime
remain unpassed gates. The private FastPath user-client source is
absent from the fixed public Git tree; an explicit source port leaves unknown
client types to the superclass rather than supplying a replacement class.
The source port replaces unpublished lifecycle, protection and type property
keys with project-scoped internal keys, including protected-client admission
and privileged-property filtering. Private property forwarding and unpublished
Apple vendor usages are disabled; standard USB Boot keyboard/mouse paths remain
enabled. Candidate first-party devices keep conservative access protection
when private usage classification is unavailable. The real XNU Reporter header
and private XNU/flat event include paths are used. Run 40249339788 generated all
three real IIG bridges and compiled 38 of 48 public units; the current follow-up
addresses its remaining errors. Run 40278738004 compiles the keyboard/consumer
support sources and real XNU reporter path; IOHIDSystem still failed on the
unpublished NXEvent usage identifiers. The follow-up imports the real official
IOHIDFamily-1035.41.2 Apple usage table at commit
`12d918c6280fcd5e8312e4fb47b4e0e9c48a0fac`, scoped inside a private C++ namespace
only for the existing NXEvent wrapper. It preserves event construction and
dispatch, and does not enable general Apple vendor report parsing. The complete
family archive now compiles in Action 40280750435 (SHA-256
`294b427c33c3d2dcb75bf339bf2209882620312a5da3bfdb30236ed4c2de1b63`).
That run later stopped at an msdosfs SDK include. The follow-up
Action 40299886122 built the Dali HID child, storage child, source IOHIDFamily,
IOStorageFamily and msdosfs, then verified their required symbols against one
unstripped source-XNU kernel. That closes compilation and linking only; actual
controller ownership and runtime delivery remain pending.

`DaliUsbHid` is a concrete source `IOHIDDevice` child. It snapshots the USB
interface identity, supplies descriptors matching the forced Boot protocol,
and copies each 8-byte keyboard or 3-byte mouse report before calling the real
`handleReport`. Its host owner must close admission, let the current report
finish, send a neutral report, prove endpoint/slot DMA stopped, then detach and
terminate. A retained lifetime lease survives failed quiescence/stop proof;
framework stop requests during a report are completed after the report returns.
Clang/GCC sanitizer tests execute this production child through a test-only
IOKit seam. Those models do not prove kernel ABI or real HID descriptor parsing.
The opt-in builder compiles against the actual source-family headers and checks
the linked kernel symbols. Action 40299886122 verifies this child and its report
API in the final kernel. Action 40313584406 also verifies the corrected
`xhci0` resource service in the final kernel. No production controller/child
owner publishes the HID child yet.

The generic USB core now contains `UsbXhci.c` for read-only xHCI capability and
USB2 root-port discovery plus coherent DMA32 command/transfer/event-ring
primitives. Clang/GCC sanitizer models check reserved page-size bits, DMA sync
direction and ownership, cycle wrap, ordered completion, tail invalidation,
malformed event pointers and descriptor-memory retention until an explicit stop
proof. The opt-in source-XNU archive builder and EDK2 library builder now include
and verify these production symbols. This portable layer does not map the
controller, start xHCI, ring doorbells, own an interrupt/DMA provider, implement
the complete `usb_host_ops`, or publish children. Its archive/link evidence is
not runtime controller qualification.

The opt-in `DaliUsbXhciResources` service matches the Dali `xhci0` platform
device, checks its exact compatible string, and decodes the versioned resource
property without casting its packed wire bytes to a native structure. Its copy
API supplies the validated range to `DaliUsbXhciOwner`, which maps that absolute
range with an inhibit-cache `IODeviceMemory` mapping and retains it until
`stop()`. The mapping model checks exact range, options, allocation failures,
and teardown. The owner performs no register reads/writes; it does not acquire
power, clocks, an IRQ, or qualified DMA, so it does not start or enumerate the
controller. Action 40313584406 verified only the earlier resource service. The
new owner archive/kernel link is pending; runtime enumeration and hardware
qualification remain separate.

The opt-in XNU profile also compiles the portable MTU3 gadget units for QMU,
EP0, endpoint setup and the CDC device/log-ring lifecycle into
`libDaliUsbGadget.a`, with final-kernel symbol checks. Its optional
`dali_usb_cdc_log_write()` producer is inert until a future provider binds an
initialized gadget; it returns immediately and drops bytes when no sink is
bound. The provider must unbind and drain in-flight writers before stopping or
destroying the gadget object. The opt-in Dali profile enables a source-XNU tap
at `_cnputc` and the direct serial `kprintf` path, which forwards emitted
characters to this producer. Console policy can suppress output before the tap,
and this does not capture all `OSLog` records. The kernel also has no MTU3
provider, device-role or VBUS/clock/PHY lease, qualified DMA/IRQ worker, or CDC
runtime. UEFI preserves early `SerialPortWrite()` output in ramoops and has no
installed USB function provider. The archive sends no CDC bytes over USB.

The UEFI resource-context revision6 also copies the four CPU-endian cells from
the xHCI FDT `interrupts` property (`0, 600, 4, 0`) with the register window.
The native worker rejects an unexpected tuple before calling provider revision2.
These cells remain untranslated: they are not a GIC vector or an IRQ ownership
proof, and the production MT6991 provider remains absent.

The profile also selects `XNU_SOURCE_MSDOSFS` and the fixed
msdosfs-608.120.4 source target. Its builder compiles all six actual kernel C
units using XNU's VFS compile record and a built-in registration wrapper. The
wrapper calls the upstream `msdosfs_module_start` after `vfsinit`; registration
failure stops boot. The archive is force-loaded and part of the kernel input
fingerprint. The final verifier requires the real VFS/vnode definitions and a
compiled `bsd_init.o` call to the wrapper. XNU now has a private local-args
mount API that only accepts a filesystem which explicitly registers kernel
local-args support; the generated msdosfs build copy uses `bcopy` only when the
mount carries that per-mount marker and preserves `copyin` for ordinary user
mounts. The fixed upstream source checkout stays unmodified. The generic tests
cover the exact source transform, drift refusal and copyin preservation, and
the VFS patch applies to the pinned XNU d8 source files. This local-args change
still needs its own macOS compile/link run. No runtime registration, USB mount
or FAT32 file operation is qualified. The registration hook performs no mount
or media I/O. The pre-user FAT32 fixture remains pending because the current
source-XNU QEMU runner has no USB mass-storage device path; its input disk is
read-only virtio, not a USB fixture. No physical media test is claimed.

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
