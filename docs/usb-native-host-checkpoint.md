# Native USB2 host engine

The isolated USB board now pins firmware
97f743aaed072d2163026f441dd37a78a92e45f3. This checkpoint carries forward
native-only MTK USB2 periodic endpoint scheduling and adds EDK2 source-hub
activation of a valid high-speed multi-TT alternate setting. The host library
compiles actual Mu transfer/register/schedule/memory source, owns the HC
lifecycle and publishes a standard USB2 HC/path pair. Default board USB remains
disabled; no UFS or kernel profile changed.

The engine checks capabilities and explicit USB2 ports, selects supported
4 KiB HC pages, verifies complete schedules before running, and retains queues
and DMA after failed halt. The native async worker checks remap/rearm errors
before callback; callback cancellation and failed cancellation are tested using
actual source TRBs/event decoding. Source allocation/cache/partial cleanup
changes are reproduced by the new reviewed Mu patch.

Firmware tests use real source C, DEBUG assertions, Clang/GCC ASan/UBSan and
modeled MMIO/DMA/Boot Services. The actual EDK2 AArch64 build verifies 33 custom production units across seven
USB archives, plus the separately counted 12-object BaseFdtLib dependency. `DaliUsbNativeHostLib.lib` SHA-256 is
`d05641d92d31b9dcfc2a84942ac43d7f600d8d05eea1246bf113c97518bc594a`. Read
../UEFI-Dali/tools/dali/README-usb-native-host.md in the sibling firmware tree
for API and lifecycle details.

This checkpoint does not install a Dali hardware controller worker in the FV.
Actual board source/power/electrical/shared ownership and real DMA mapping
remain external qualifications. The native-only MTK post-short-transfer event
filter and USB2 high-speed/TT interrupt bandwidth scheduler are implemented and
source-model tested. ESIT saturation, 32/64-bit endpoint contexts, TT admission,
Y6 avoidance and reservation rollback pass Clang/GCC sanitizer tests. For a
high-speed multi-TT hub, UsbHubDxe now selects validated interface alternate
setting 1 / protocol 2 before port power or hub-interrupt submission; missing or
invalid alt 1 keeps single-TT mode. The selector fixture compiles the actual
Mu source, and the actual EDK2 archive checks its definition and UsbHub caller.
No test yet executes a Configure Endpoint failure after the hub accepted alt 1.
The native host fail-stops on that command error and does not attempt an
automatic alt0 rollback through uncertain context.
Firmware rebuilds reverse the owned Mu patch list before reapplying it, so
overlapping source patches return to the exact reviewed patch set.
The native host now publishes the shared `DaliUsbNativeHostHandoff` protocol.
DarwinBoot enumerates all USB2 host-controller handles after closing input files
and before the final memory map. It calls `PrepareHandoff` for native hosts and
uses standard `SetState(Halt)` for other controllers; unknown protocol revisions
or failed halt proofs refuse boot. The native callback is idempotent and runs
outside DarwinBoot's GetMemoryMap/EBS retry loop. Source QEMU tests exercise a
one-shot mock native provider and the standard-host fallback; these tests do not
emulate the native controller. Native-path QEMU, CDC/platform role integration
and source-only XNU work remain pending.
Existing PCI xHCI QEMU coverage is a separate gate, not native-path proof.
hardware_qualified=false; no physical device or medium was used.
