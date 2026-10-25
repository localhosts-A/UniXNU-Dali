"""Validate USB resource extraction against real DTB fixtures and changed dependencies."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/usb_resources.py"
SPEC = importlib.util.spec_from_file_location("usb_resources", SCRIPT)
USB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(USB)

DTS = r'''/dts-v1/;
/ {
 compatible = "mediatek,MT6991"; #address-cells = <2>; #size-cells = <2>;
 ck: clock { #clock-cells = <1>; };
 pd: domain { #power-domain-cells = <1>; };
 supply: regulator { regulator-name = "vusb33"; };
 efuse: calibration { reg = <0 0x10000000 0 4>; };
 repeater_other: repeater { #phy-cells = <0>; };
 protect: syscon@1002c000 { reg = <0 0x1002c000 0 0x1000>; };
 spmi@1c01a000 {
  compatible = "mediatek,mt6991-spmi";
  reg = <0 0x1c01a000 0 0x8ff>, <0 0x1c01c000 0 0x100>, <0 0x1c018000 0 0x8ff>, <0 0x1c01c800 0 0x100>;
  reg-names = "pmif", "spmimst", "pmif-p", "spmimst-p";
  spmi-dev-mask = <0x85c0>; #address-cells = <2>; #size-cells = <0>;
  mt6379@e {
   compatible = "mediatek,mt6379"; reg = <14 0>; #address-cells = <1>; #size-cells = <0>;
   adc: adc { #io-channel-cells = <1>; };
   charger: chg { otg-supply = <&vbus>;
    vbus: usb-otg-vbus-regulator { regulator-min-microvolt = <4850000>; regulator-max-microvolt = <5500000>; };
   };
   tcpc { compatible = "mediatek,mt6379-tcpc"; interrupts = <120>; interrupt-names = "pd_evt";
    io-channels = <&adc 14>, <&adc 15>; charger = <&charger>;
   };
   repeater: mt6379-eusb2-repeater@d00 { compatible = "mtk,mt6379-eusb2-repeater"; reg = <0xd00>; #phy-cells = <0>; };
  };
 };
 soc {
  #address-cells = <2>; #size-cells = <2>; ranges;
  usb0@16701000 {
   compatible = "mediatek,mt6991-mtu3", "mediatek,mtu3";
   reg = <0 0x16701000 0 0x2e00>, <0 0x16703e00 0 0x100>; reg-names = "mac", "ippc";
   interrupts = <0 599 4 0>; dr_mode = "otg"; maximum-speed = "high-speed";
   clocks = <&ck 34>, <&ck 35>, <&ck 36>; clock-names = "sys_ck", "host_ck", "frmcnt_ck";
   power-domains = <&pd 2>, <&pd 3>; power-domain-names = "u2", "u3";
   phys = <&u2 3>, <&u3 4>; usb-role-switch; mediatek,clk-mgr;
   mediatek,usb-bus-protect = <&protect 0xdc 0xe0 0xd8 0x208 0x80>;
   vusb33-supply = <&supply>; #address-cells = <2>; #size-cells = <2>; ranges;
   xhci0@16700000 {
    compatible = "mediatek,mtk-xhci"; reg = <0 0x16700000 0 0x1000>; reg-names = "mac";
    interrupts = <0 600 4 0>; clocks = <&ck 0>; clock-names = "sys_ck";
   };
  };
  usb-phy0@16730000 {
   compatible = "mediatek,xsphy"; #address-cells = <2>; #size-cells = <2>; ranges; phys = <&repeater_other>, <&repeater>;
   u2: usb2-phy0@16730000 {
    reg = <0 0x16730000 0 0x400>; #phy-cells = <1>; clocks = <&ck 0>; clock-names = "ref";
    nvmem-cells = <&efuse>; nvmem-cell-names = "calibration"; nvmem-cell-masks = <0xff>;
   };
  };
  usb3-phy0@16773000 {
   compatible = "mediatek,xsphy"; #address-cells = <2>; #size-cells = <2>; ranges;
   u3: usb3-phy0@16773400 {
    reg = <0 0x16773400 0 0x500>; #phy-cells = <1>; clocks = <&ck 0>; clock-names = "ref";
    nvmem-cells = <&efuse>; nvmem-cell-names = "calibration"; nvmem-cell-masks = <0xff>;
   };
  };
 };
};'''


def cells(*values):
    return struct.pack(">" + "I" * len(values), *values)


class UsbResourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="dali-usb-fdt-")
        cls.fdt = Path(cls.directory.name) / "usb.dtb"
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", str(cls.fdt), "-"],
                       input=DTS, check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.tree = USB.load_fdt(self.fdt)
        self.usb = "/soc/usb0@16701000"
        self.tcpc = "/spmi@1c01a000/mt6379@e/tcpc"

    def test_complete_saved_graph(self):
        report = USB.extract_usb(self.tree)
        self.assertEqual(report["mmio"]["mac"], [0x16701000, 0x2e00])
        self.assertEqual(report["spmi"]["sid"], 14)
        self.assertEqual(report["spmi"]["master"], "m")
        self.assertEqual(report["gic_interrupt_id"], [631, 632])
        self.assertFalse(report["hardware_qualified"])
        self.assertFalse(report["dma_mapping_qualified"])
        self.assertFalse(report["device_access_performed"])
        self.assertFalse(report["calibration_values_read"])

    def test_resource_changes_refused(self):
        changes = [
            (self.usb, "interrupts", cells(0, 598, 4, 0)),
            (self.usb, "reg", cells(0, 0x16701000, 0, 0x3000, 0, 0x16703e00, 0, 0x100)),
            (self.usb, "maximum-speed", b"super-speed\0"),
            (self.usb, "clock-names", b"sys_ck\0host_ck\0host_ck\0"),
            (self.usb, "clocks", cells(0xdead)),
            (self.usb, "phys", cells(0xdead, 3)),
            (self.usb, "usb-role-switch", cells(1)),
            ("/soc", "ranges", cells(0, 0, 0, 0, 0, 0x20000000)),
            ("/spmi@1c01a000/mt6379@e", "reg", cells(4, 0)),
            (self.tcpc, "io-channels", cells(0xdead, 1)),
            (self.tcpc, "charger", cells(0xdead)),
            ("/clock", "status", b"disabled\0"),
            ("/clock", "#clock-cells", cells(5)),
            ("/soc/usb-phy0@16730000/usb2-phy0@16730000", "nvmem-cell-masks", cells(0)),
            ("/spmi@1c01a000/mt6379@e/chg/usb-otg-vbus-regulator", "regulator-max-microvolt", cells(4900000)),
        ]
        for path, name, value in changes:
            with self.subTest(path=path, property=name):
                tree = USB.load_fdt(self.fdt)
                tree.nodes[path][name] = value
                with self.assertRaises(ValueError):
                    USB.extract_usb(tree)

    def test_duplicate_phandle_refused(self):
        self.tree.nodes["/duplicate"] = {"phandle": self.tree.nodes["/clock"]["phandle"]}
        with self.assertRaisesRegex(ValueError, "phandle"):
            USB.extract_usb(self.tree)

    def test_bus_protection_aperture_refused(self):
        values = list(USB.Resources(self.tree).cells(self.usb, "mediatek,usb-bus-protect"))
        values[1] = 0x1000
        self.tree.nodes[self.usb]["mediatek,usb-bus-protect"] = cells(*values)
        with self.assertRaisesRegex(ValueError, "bus-protect"):
            USB.extract_usb(self.tree)


if __name__ == "__main__":
    unittest.main()
