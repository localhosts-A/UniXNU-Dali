#!/usr/bin/env python3
"""Validate the Dali USB dependency graph in a saved FDT; never access a device."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct


class Resources:
    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.handles = {}
        for path, props in self.nodes.items():
            declared = []
            for name in ("phandle", "linux,phandle"):
                if name in props:
                    values = self.cells(path, name)
                    if len(values) != 1 or values[0] in (0, 0xffffffff):
                        raise ValueError(f"invalid phandle at {path}")
                    declared.append(values[0])
            if declared:
                if len(set(declared)) != 1 or declared[0] in self.handles:
                    raise ValueError(f"duplicate/inconsistent phandle at {path}")
                self.handles[declared[0]] = path

    def cells(self, path, name):
        value = self.nodes[path].get(name)
        if value is None or len(value) % 4:
            raise ValueError(f"missing/malformed cells {path}:{name}")
        return struct.unpack(">" + "I" * (len(value) // 4), value)

    def strings(self, path, name):
        raw = self.nodes[path].get(name, b"")
        if not raw:
            return []
        if not raw.endswith(b"\0"):
            raise ValueError(f"unterminated strings {path}:{name}")
        result = raw[:-1].split(b"\0")
        if any(not value for value in result):
            raise ValueError(f"empty string {path}:{name}")
        return [value.decode("ascii") for value in result]

    def enabled(self, path):
        while path:
            if self.nodes.get(path, {}).get("status", b"okay\0") not in (b"okay\0", b"ok\0"):
                return False
            path = path.rsplit("/", 1)[0]
        return True

    def unique(self, compatible):
        result = [p for p in self.nodes if compatible in self.strings(p, "compatible") and self.enabled(p)]
        if len(result) != 1:
            raise ValueError(f"expected one enabled {compatible}, found {len(result)}")
        return result[0]

    def provider(self, handle):
        path = self.handles.get(handle)
        if path is None or not self.enabled(path):
            raise ValueError(f"unresolved/disabled provider {handle:#x}")
        return path

    def refs(self, path, prop, provider_cells):
        raw = self.cells(path, prop)
        result, cursor = [], 0
        while cursor < len(raw):
            provider = self.provider(raw[cursor])
            widths = self.cells(provider, provider_cells)
            if len(widths) != 1 or widths[0] > 4 or cursor + 1 + widths[0] > len(raw):
                raise ValueError(f"invalid provider width {provider}:{provider_cells}")
            end = cursor + 1 + widths[0]
            result.append({"provider": provider, "args": list(raw[cursor + 1:end])})
            cursor = end
        return result

    def named_refs(self, path, prop, width_property, names_property):
        result = self.refs(path, prop, width_property)
        names = self.strings(path, names_property)
        if len(names) != len(result) or len(set(names)) != len(names):
            raise ValueError(f"reference names differ: {path}:{names_property}")
        return [{"name": name, **entry} for name, entry in zip(names, result)]

    def mmio(self, path):
        # The verified platform uses root/identity buses. Do not silently treat
        # a future translated bus address as a physical register address.
        parent = path.rsplit("/", 1)[0]
        while parent and parent != "/":
            if self.nodes[parent].get("ranges") != b"":
                raise ValueError(f"non-identity or unspecified bus translation at {parent}")
            parent = parent.rsplit("/", 1)[0]
        result = self.tree.reg(path)
        if any(not size or base & 3 or base > 0xffffffffffffffff - size for base, size in result):
            raise ValueError(f"invalid MMIO extent at {path}")
        return result

    def supply(self, path, name):
        values = self.cells(path, name)
        if len(values) != 1:
            raise ValueError(f"malformed supply {path}:{name}")
        return self.provider(values[0])

    def nvmem(self, path):
        handles = self.cells(path, "nvmem-cells")
        names = self.strings(path, "nvmem-cell-names")
        masks = self.cells(path, "nvmem-cell-masks")
        if len(handles) != len(names) or len(names) != len(masks) or len(set(names)) != len(names) or not all(masks):
            raise ValueError(f"PHY calibration fields differ at {path}")
        return [{"name": name, "provider": self.provider(handle), "mask": mask}
                for handle, name, mask in zip(handles, names, masks)]


def extract_usb(tree):
    r = Resources(tree)
    if "mediatek,MT6991" not in r.strings("/", "compatible"):
        raise ValueError("not an MT6991 FDT")
    usb = r.unique("mediatek,mt6991-mtu3")
    host = r.unique("mediatek,mtk-xhci")
    if not host.startswith(usb + "/"):
        raise ValueError("xHCI is not owned by USB0")
    if r.mmio(usb) != [(0x16701000, 0x2e00), (0x16703e00, 0x100)] or r.strings(usb, "reg-names") != ["mac", "ippc"]:
        raise ValueError("USB0 MAC/IPPC resource differs")
    if r.mmio(host) != [(0x16700000, 0x1000)] or r.strings(host, "reg-names") != ["mac"]:
        raise ValueError("USB0 xHCI resource differs")
    if r.cells(usb, "interrupts") != (0, 599, 4, 0) or r.cells(host, "interrupts") != (0, 600, 4, 0):
        raise ValueError("USB0 interrupt resource differs")
    if r.strings(usb, "maximum-speed") != ["high-speed"] or r.strings(usb, "dr_mode") != ["otg"]:
        raise ValueError("USB2 dual-role contract differs")
    for flag in ("usb-role-switch", "mediatek,clk-mgr"):
        if tree.nodes[usb].get(flag) != b"":
            raise ValueError(f"required USB0 flag differs: {flag}")
    clocks = r.named_refs(usb, "clocks", "#clock-cells", "clock-names")
    if [entry["name"] for entry in clocks] != ["sys_ck", "host_ck", "frmcnt_ck"]:
        raise ValueError("USB0 clock contract differs")
    host_clocks = r.named_refs(host, "clocks", "#clock-cells", "clock-names")
    if [entry["name"] for entry in host_clocks] != ["sys_ck"]:
        raise ValueError("xHCI clock contract differs")
    domains = r.named_refs(usb, "power-domains", "#power-domain-cells", "power-domain-names")
    if [entry["name"] for entry in domains] != ["u2", "u3"]:
        raise ValueError("USB0 power domains differ")
    phys = r.refs(usb, "phys", "#phy-cells")
    if len(phys) != 2 or phys[0]["args"] != [3] or phys[1]["args"] != [4]:
        raise ValueError("USB0 USB2/USB3 PHY references differ")
    if r.mmio(phys[0]["provider"]) != [(0x16730000, 0x400)] or r.mmio(phys[1]["provider"]) != [(0x16773400, 0x500)]:
        raise ValueError("USB0 PHY MMIO differs")
    phy_clocks = [r.named_refs(p["provider"], "clocks", "#clock-cells", "clock-names") for p in phys]
    if any([v["name"] for v in values] != ["ref"] for values in phy_clocks):
        raise ValueError("PHY reference clocks differ")
    calibration = [r.nvmem(p["provider"]) for p in phys]
    protect = r.cells(usb, "mediatek,usb-bus-protect")
    if len(protect) != 6 or not protect[-1]:
        raise ValueError("USB0 bus-protect contract differs")
    protect_provider = r.provider(protect[0])
    protect_registers = r.mmio(protect_provider)
    if len(protect_registers) != 1 or any(offset & 3 or offset + 4 > protect_registers[0][1] for offset in protect[1:5]):
        raise ValueError("USB0 bus-protect register is out of bounds")
    pmic = r.unique("mediatek,mt6379")
    if r.cells(pmic, "reg") != (14, 0):
        raise ValueError("MT6379 is not SPMI SID14")
    spmi = pmic.rsplit("/", 1)[0]
    if "mediatek,mt6991-spmi" not in r.strings(spmi, "compatible"):
        raise ValueError("MT6379 parent is not MT6991 SPMI")
    spmi_regs = r.mmio(spmi)
    if (r.strings(spmi, "reg-names") != ["pmif", "spmimst", "pmif-p", "spmimst-p"] or
            spmi_regs != [(0x1c01a000, 0x8ff), (0x1c01c000, 0x100), (0x1c018000, 0x8ff), (0x1c01c800, 0x100)]):
        raise ValueError("PMIF/SPMI register contract differs")
    mask = r.cells(spmi, "spmi-dev-mask")
    if len(mask) != 1 or mask[0] > 0xffff:
        raise ValueError("SPMI device route mask is invalid")
    tcpc = r.unique("mediatek,mt6379-tcpc")
    repeater = r.unique("mtk,mt6379-eusb2-repeater")
    if tcpc.rsplit("/", 1)[0] != pmic or repeater.rsplit("/", 1)[0] != pmic:
        raise ValueError("TCPC/repeater parent differs from SID14")
    if r.cells(tcpc, "interrupts") != (120,) or r.strings(tcpc, "interrupt-names") != ["pd_evt"]:
        raise ValueError("MT6379 TCPC IRQ contract differs")
    if r.cells(repeater, "reg") != (0xd00,) or r.cells(repeater, "#phy-cells") != (0,):
        raise ValueError("MT6379 eUSB2 repeater contract differs")
    adc = r.refs(tcpc, "io-channels", "#io-channel-cells")
    if len(adc) != 2 or any(not item["provider"].startswith(pmic + "/") for item in adc):
        raise ValueError("MT6379 TCPC ADC providers differ")
    charger = r.supply(tcpc, "charger")
    if charger.rsplit("/", 1)[0] != pmic:
        raise ValueError("TCPC charger is not on SID14")
    vbus = r.supply(charger, "otg-supply")
    if vbus.rsplit("/", 1)[0] != charger:
        raise ValueError("VBUS regulator is not the MT6379 charger child")
    minimum = r.cells(vbus, "regulator-min-microvolt")
    maximum = r.cells(vbus, "regulator-max-microvolt")
    if len(minimum) != 1 or len(maximum) != 1 or not minimum[0] <= 5_000_000 <= maximum[0]:
        raise ValueError("VBUS regulator cannot supply the USB2 5V profile")
    usb2_parent = phys[0]["provider"].rsplit("/", 1)[0]
    repeaters = r.refs(usb2_parent, "phys", "#phy-cells")
    if len(repeaters) != 2 or repeater not in [item["provider"] for item in repeaters]:
        raise ValueError("USB2 repeater chain differs")
    source = r.supply(usb, "vusb33-supply")
    return {"schema": 1, "controller": usb, "host": host,
            "mmio": {"mac": list(r.mmio(usb)[0]), "ippc": list(r.mmio(usb)[1]), "xhci": list(r.mmio(host)[0])},
            "spi": [599, 600], "gic_interrupt_id": [631, 632], "maximum_speed": "high-speed",
            "clocks": clocks, "host_clocks": host_clocks, "power_domains": domains,
            "phys": phys, "phy_clocks": phy_clocks, "repeaters": repeaters, "phy_calibration": calibration,
            "calibration_values_read": False,
            "bus_protect": {"provider": protect_provider, "registers": protect_registers, "args": list(protect[1:])},
            "spmi": {"node": spmi, "registers": spmi_regs, "device_mask": mask[0], "sid": 14,
                     "master": "p" if mask[0] & (1 << 14) else "m"},
            "tcpc": {"node": tcpc, "pmic_irq": 120, "adc": adc, "charger": charger},
            "vusb33": source, "vbus": vbus, "vbus_microvolt_range": [minimum[0], maximum[0]], "dma_address_bits": 32,
            "dma_mapping_qualified": False, "hardware_qualified": False, "device_access_performed": False}


def load_fdt(path):
    spec = importlib.util.spec_from_file_location("dali_usb_adt", Path(__file__).with_name("make-dali-adt.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Fdt(path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fdt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = extract_usb(load_fdt(args.fdt))
        report["fdt_sha256"] = hashlib.sha256(args.fdt.read_bytes()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print("PASS: saved FDT USB resource graph; DMA/hardware remain unqualified")
    except (OSError, ValueError, KeyError, struct.error) as error:
        raise SystemExit(f"USB resource verification failed: {error}") from None


if __name__ == "__main__":
    main()
