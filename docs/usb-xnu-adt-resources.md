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

This property describes a resource only. It does not map or power the controller and does not qualify interrupt ownership, DMA/cache behavior, role, or hardware. No XNU USB controller consumes this node yet; a driver must refuse startup until its separate MMIO, power, IRQ, role, and DMA providers are qualified.
