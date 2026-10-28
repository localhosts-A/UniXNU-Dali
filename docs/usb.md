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
The source tree now contains a QEMU-only XNU xHCI runtime that connects the
portable host coordinator to the HID and BOT child publishers. Its local
production-body models pass, and a source-XNU test now mounts a disposable USB
FAT32 volume read-only, then writes, syncs, unmounts, remounts and verifies a
file from the image. The exact macOS compile and QEMU run remain pending.
Asynchronous HID report polling, the MT6991 provider and post-EBS role ownership
remain open.
The default-off and enabled firmware packages build, and the source PCI xHCI
QEMU class-stack gate passes separately; neither result proves the Dali native
MMIO controller. Nothing in this milestone enables USB in the ordinary boot
profile. `hardware_qualified=false`.

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
An offline IOKit/BOT model compiles the actual storage child and generic host
code together, exercises read/write completions, failed/retried synchronization
and stop, stop-proven detach, and service calls after host state is cleared.
The model uses an in-memory disk and test-only IOKit types; it does not prove
the Apple kernel ABI, service-plane lifetime or hardware behavior.

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
`xhci0` resource service in the final kernel. The later QEMU-only PCI runtime
publishes this HID child in source; actual XNU/QEMU execution and HID report
delivery remain pending.

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

The portable host teardown has a two-step API for a serialized OS owner. First,
quiesce child services: the raw-report XNU HID child sends its own neutral report
and the storage child closes requests and syncs. Then `usb_host_stop_device()`
closes host admission and obtains a backend stop proof. After stop-proven child
detach, `usb_host_release_stopped_device()` emits any legacy translated-input
releases and clears host/BOT state. A failed stop retains the faulted host for
retry; a second stop after success does not repeat the backend operation.
Reentrant lifecycle calls from stop or release callbacks return BUSY; callers
must still serialize host operations and callbacks. `usb_host_disconnect()`
remains a convenience stop-and-release path for devices without retained OS
children. The legacy `usb_host_poll_input()` also clears host state on a
disconnect; an XNU owner with children must use the copied raw-report API and
the split teardown instead. Production sanitizer models cover stop failure,
reentry, storage detach after proof and final state release. These models do not
supply a qualified source-XNU MMIO/IRQ/DMA/power/role backend.

The opt-in `DaliUsbXhciResources` service matches the Dali `xhci0` platform
device, checks its exact compatible string, and decodes the versioned resource
property without casting its packed wire bytes to a native structure. Its copy
API supplies the validated range to `DaliUsbXhciOwner`, which maps that absolute
range with an inhibit-cache `IODeviceMemory` mapping. The owner can bind bounded
`usb_mmio` callbacks only while a caller-supplied platform guard succeeds; every
32-bit read/write rechecks the guard and the mapping bounds, and output remains
unchanged on failure. An owner lock serializes callbacks, revocation and stop;
the guard must be nonblocking and non-reentrant. Binding itself does not touch
registers. Binding retains the owner until revocation, and `stop()` retains the
mapping while callbacks are bound. Revocation requires a controller-stop proof
while the platform lease is still valid. These callbacks are a production MMIO
seam, not the MT6991 controller owner: no production board provider binds the
guard, starts the xHCI core, or acquires qualified power/clocks/IRQ/DMA.
The production-body model covers guarded MMIO and stop-proof retention; its
controller/host path still uses synthetic MMIO. Source-XNU Action 40386387528
verified the earlier resource service and mapping-owner link. Runtime enumeration
and hardware qualification remain separate.

The QEMU-only `DaliUsbQemuXhciRuntime` now binds the guarded PCI BAR and bypass
DMA seams to the portable xHCI host adapter. Its workloop drains root-port events,
resets powered USB2 ports, enumerates connected devices, publishes HID/BOT
services, and scans external-hub port status through class control requests.
Repeatedly polling a synchronous interrupt endpoint is unsafe in the current
controller API: an idle transfer timeout fault-stops the controller. Therefore
the runtime does not yet submit HID reports; an asynchronous completion and
cancel path is still required. Root reset/enumeration and hub callback errors
retry with a bounded 500 ms delay while the controller remains healthy. A PSC
event for a port outside the USB2 bitmap fail-stops the runtime without calling
USB2-only status/ack APIs; this owner does not manage USB3, and the supported
QEMU profile uses `p3=0`. The local runtime fixture uses test stubs for the host adapter,
enumeration and child services. Separate generic models exercise the real
xHCI/host/BOT stack, but no single fixture runs that entire runtime stack through
this new runtime owner.

The source-XNU QEMU gate now builds a 66,584-sector FAT32 superfloppy with a
known README, attaches it to the USB2 BOT device, and selects the unique
`DaliUsbStorage` whole-media path without a RAM fallback. The kernel test first
checks read-only mount/content/`EROFS`, then remounts writable, creates a file,
calls `VNOP_FSYNC`, unmounts and remounts read-only, and reads the file back.
After QEMU exits, the host verifies the file bytes in the raw image and requires
the image hash to have changed. The checkpoint also binds to the exact BOT
capacity/sector geometry and kernel/image hashes. The source, runner and host
tests are present, but the current Linux host has no `xcrun`, so the source
archive/kernel link and QEMU gate still require macOS CI. QEMU evidence does not
qualify the MT6991 DMA path or physical hardware.

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
runtime. UEFI's default-off `DALI_USB_CDC_LOG` option adds a shared CDC log
protocol service and a DXE SerialPort tee that writes ramoops first. Early and
runtime-only serial paths keep the original ramoops implementation, and earlier
bytes are not replayed. There is no installed USB function provider, so no CDC
bytes are sent over USB.

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
the VFS patch applies to the pinned XNU d8 source files. A boot-arg-gated QEMU
test is now wired to mount a FAT32-sized volatile `SourceStorageFixture` volume
through its published `/dev/diskN` path and read `/mnt/README.TXT` with
production VFS/msdosfs before launchd. macOS Action `40592547320` passes the
source-XNU compile/link, all post-link verifiers and the QEMU `rw4096`,
`readonly512`, and FAT32 cases. The msdosfs checkpoint returns `0x4d5344460000003f`
with error 0, covering read-only mount, file content readback and write refusal;
the final kernel and storage link reports bind to SHA-256
`0b23b18df34dbe2493bf4845951f4240ff37da117a7c78faf4e75daf02e09691`. This
validates XNU VFS/msdosfs over volatile RAM-backed IOStorage. A separate new
mode selects the Dali BOT whole-media, writes and syncs a disposable FAT32 file,
unmounts/remounts and checks readback plus the changed image hash. Its source and
host-side gates are implemented, but exact macOS compilation and QEMU execution
remain pending. Neither gate qualifies MT6991 DMA, physical media or hardware;
no physical-media test is claimed.

The device tree contains a production SID14 PMIF-M transport with actual identity
gating, atomic shared-lock operations, bounded completion/error handling,
late-response ownership and explicit write uncertainty. Production TCPCI/MT6379
VBUS, both USB2 repeaters, GPIO72/I2C6, clock/domain/PHY and IPPC primitives are
also present with Clang/GCC register-model coverage. These pieces are not yet
assembled by an installed MT6991 UEFI provider. The shared PMIF/resource
acquisition, IRQ/DMA qualification and ownership leases still must be supplied
before any controller start. `docs/usb-platform-contract.md` records fixed source
locations and unresolved platform dependencies.

`DaliUsbRoleWorker` connects debounced TCPCI polling to the generic
`usb_role_switch()` stop/sync/cancel/DMA/VBUS/start sequence. It latches the
fresh CC orientation before configuring a role, turns off host source VBUS only
after filesystem sync, request cancellation and DMA stop all succeed, and keeps
the current termination and VBUS on after a failed stop proof. Its explicit
`stop()` retries cleanup while retaining dependencies. The actual production C
worker runs in the Clang/GCC Type-C fixture and its object and three exports are
verified in the AArch64 EDK2 `DaliUsbLib` archive. This remains a polling API:
there is no installed DXE timer/provider or production host/gadget lifecycle
caller, and the callback model does not establish hardware qualification.

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
