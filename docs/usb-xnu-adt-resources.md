# Source-XNU xHCI Apple-DT resource node

`make-dali-adt.py` leaves USB absent by default. A source-XNU USB host build may request `--usb-host`; the generator then accepts only the saved live-FDT xHCI node at `/soc/usb0@16701000/xhci0@16700000` with MMIO `<0 0x16700000 0 0x1000>` and interrupt cells `<0 600 4 0>`.

The generated `/arm-io/xhci0` Apple-DT node has `compatible = "mediatek,mtk-xhci"` and a versioned `resources-v1` property encoded as little-endian fields:

| Offset | Type | Meaning |
| --- | --- | --- |
| 0 | `uint32` | Format version, `1` |
| 4 | `uint64` | Absolute physical MMIO base, `0x16700000` |
| 12 | `uint64` | MMIO aperture size, `0x1000` |
| 20 | four `uint32` | The original FDT interrupt tuple `(type, number, flags, padding)` |

The resource sits outside the current `/arm-io/ranges` window, so it is not encoded as an arm-io-relative `reg` and does not extend or reinterpret that mapping. Keeping the raw FDT interrupt tuple avoids inventing an Apple-DT IRQ convention; a future controller owner must validate and translate it through the XNU GIC provider.

`DaliUsbXhciResources` is an opt-in source-XNU `IOService` matched to the `xhci0` platform device. It requires the exact `mediatek,mtk-xhci` compatible property and decodes the 36-byte wire value byte-by-byte, without casting it to a naturally aligned C structure. Its `copyResourceDescription` method returns the validated tuple to a future controller owner.

This service validates a resource description only. It does not map the MMIO range, read or write controller registers, power the controller, translate the GIC interrupt, or qualify IRQ ownership, DMA/cache behavior, role, or hardware. The production host owner and its separate MMIO, power, IRQ, role, and DMA providers remain required.
