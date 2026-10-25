# Native USB2 host engine

The isolated USB board now pins firmware
6be2e23, which carries the current DaliUsbNativeHostLib checkpoint. The library composes the
actual Mu transfer/register/schedule/memory source with the checked MMIO/DMA
adapter, owns the HC lifecycle and publishes a standard USB2 HC/path pair.
Default board USB remains disabled. No UFS or kernel profile changed.

The engine checks capabilities and explicit USB2 ports, selects supported
4 KiB HC pages, verifies complete schedules before running, and retains queues
and DMA after failed halt. The native async worker checks remap/rearm errors
before callback; callback cancellation and failed cancellation are tested using
actual source TRBs/event decoding. Source allocation/cache/partial cleanup
changes are reproduced by the new reviewed Mu patch.

Firmware tests use real source C, DEBUG assertions, Clang/GCC ASan/UBSan and
modeled MMIO/DMA/Boot Services. The actual EDK2 AArch64 build adds five native
host translation units to the existing 27 custom units: seven USB archives,
plus the separately counted 12-object BaseFdtLib dependency. Read
../UEFI-Dali/tools/dali/README-usb-native-host.md in the sibling firmware tree
for API and lifecycle details.

This checkpoint does not install a Dali hardware controller worker in the FV.
Actual board source/power/electrical/shared ownership and real DMA mapping
remain external qualifications. The native-only MTK post-short-transfer event
filter is now implemented and source-model tested. MTK periodic endpoint/TT
bandwidth scheduling still needs a native binding before this engine can be
used on Dali.
The loader must call PrepareHandoff before the final memory map and refuse
failed halt; this DarwinBoot wiring is still pending. Native-path QEMU,
CDC/platform role integration and source-only XNU work are also pending.
Existing PCI xHCI QEMU coverage is a separate gate, not native-path proof.
hardware_qualified=false; no physical device or medium was used.
