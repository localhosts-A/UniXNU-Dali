# Native USB2 host engine

The isolated USB board now pins firmware
2c5079a7b2f9ba2a56f9be0ee4cc236f4c9d2cc4. This checkpoint adds native-only
MTK USB2 periodic endpoint scheduling to DaliUsbNativeHostLib. It compiles the
actual Mu transfer/register/schedule/memory source, owns the HC lifecycle and
publishes a standard USB2 HC/path pair. Default board USB remains disabled; no
UFS or kernel profile changed.

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
Y6 avoidance and reservation rollback pass Clang/GCC sanitizer tests. Multi-TT
is activated only after interface protocol 2 is successfully selected; the
source UsbHub class still leaves alt 0 active and does not select alt 1. The
model does not exercise the hardware Configure Endpoint command path.
The loader must call PrepareHandoff before the final memory map and refuse
failed halt; this DarwinBoot wiring is still pending. Native-path QEMU,
CDC/platform role integration and source-only XNU work are also pending.
Existing PCI xHCI QEMU coverage is a separate gate, not native-path proof.
hardware_qualified=false; no physical device or medium was used.
