"""Keep the opt-in XNU xHCI Apple-DT resource ABI explicit and bounded."""
import importlib.util
from pathlib import Path
import struct
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/make-dali-adt.py"
SPEC = importlib.util.spec_from_file_location("make_dali_adt", SCRIPT)
ADT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADT)

XHCI = "/soc/usb0@16701000/xhci0@16700000"


def cells(*values):
    return struct.pack(">" + "I" * len(values), *values)


def tree():
    result = ADT.Fdt.__new__(ADT.Fdt)
    result.nodes = {
        "/": {},
        "/soc": {},
        "/soc/usb0@16701000": {
            "#address-cells": cells(2),
            "#size-cells": cells(2),
        },
        XHCI: {
            "compatible": b"mediatek,mtk-xhci\0",
            "reg": cells(0, 0x16700000, 0, 0x1000),
            "interrupts": cells(0, 600, 4, 0),
        },
    }
    return result


class DaliXhciAdtTests(unittest.TestCase):
    def test_default_off_does_not_require_or_emit_xhci(self):
        result = ADT.Fdt.__new__(ADT.Fdt)
        result.nodes = {"/": {}}
        self.assertIsNone(ADT.xhci_adt_node(result, False))

    def test_enabled_node_preserves_absolute_mmio_and_raw_fdt_irq(self):
        resources = struct.pack("<IQQ4I", 1, 0x16700000, 0x1000, 0, 600, 4, 0)
        expected = ADT.apple_node("xhci0", [
            ("compatible", b"mediatek,mtk-xhci\0"),
            ("resources-v1", resources),
        ])
        self.assertEqual(ADT.xhci_adt_node(tree(), True), expected)

    def test_enabled_node_rejects_changed_resources(self):
        mutations = (
            ("compatible", b"mediatek,other-xhci\0"),
            ("reg", cells(0, 0x16701000, 0, 0x1000)),
            ("interrupts", cells(0, 599, 4, 0)),
        )
        for name, value in mutations:
            with self.subTest(property=name):
                current = tree()
                current.nodes[XHCI][name] = value
                with self.assertRaises(ValueError):
                    ADT.xhci_adt_node(current, True)


if __name__ == "__main__":
    unittest.main()
