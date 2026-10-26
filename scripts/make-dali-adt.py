#!/usr/bin/env python3
"""Build a minimal Apple DT from a verified live MT6991 FDT for single-core XNU."""
import argparse
import json
import os
from pathlib import Path
import secrets
import struct
import zlib
from dali_ufs_resources import encode_scratch_grant, resource_contract


class Fdt:
    def __init__(self, data):
        if len(data) < 40:
            raise ValueError("truncated FDT header")
        values = struct.unpack_from(">10I", data)
        magic, total, structure, strings, reserve, version, _, _, string_size, structure_size = values
        if magic != 0xD00DFEED or version < 17 or total > len(data):
            raise ValueError("unsupported FDT header")
        if (structure + structure_size > total or strings + string_size > total or reserve >= total):
            raise ValueError("FDT section is outside its total size")
        self.nodes = {}
        stack = []
        cursor = structure
        end = structure + structure_size
        while cursor < end:
            token = struct.unpack_from(">I", data, cursor)[0]
            cursor += 4
            if token == 1:
                name_end = data.index(b"\0", cursor, end)
                stack.append(data[cursor:name_end].decode("ascii"))
                cursor = (name_end + 4) & ~3
                path = "/" + "/".join(stack[1:])
                self.nodes[path] = {}
            elif token == 2:
                if not stack:
                    raise ValueError("FDT node stack underflow")
                stack.pop()
            elif token == 3:
                length, name_offset = struct.unpack_from(">II", data, cursor)
                cursor += 8
                if name_offset >= string_size or cursor + length > end:
                    raise ValueError("FDT property is outside its section")
                name_end = data.index(b"\0", strings + name_offset, strings + string_size)
                name = data[strings + name_offset:name_end].decode("ascii")
                if not stack:
                    raise ValueError("FDT property has no node")
                path = "/" + "/".join(stack[1:])
                self.nodes[path][name] = data[cursor:cursor + length]
                cursor = (cursor + length + 3) & ~3
            elif token == 4:
                continue
            elif token == 9:
                break
            else:
                raise ValueError(f"unknown FDT structure token {token:#x}")
        if not self.nodes or stack:
            raise ValueError("incomplete FDT structure")

    def string_list(self, path, name):
        return self.nodes.get(path, {}).get(name, b"").rstrip(b"\0").split(b"\0")

    def cells(self, path, name):
        value = self.nodes.get(path, {}).get(name)
        if value is None or len(value) % 4:
            raise ValueError(f"missing or malformed {path}:{name}")
        return struct.unpack(">" + "I" * (len(value) // 4), value)

    def reg(self, path):
        parent = path.rsplit("/", 1)[0] or "/"
        address_cells = int.from_bytes(self.nodes.get(parent, {}).get("#address-cells", b"\0\0\0\2"), "big")
        size_cells = int.from_bytes(self.nodes.get(parent, {}).get("#size-cells", b"\0\0\0\1"), "big")
        raw = self.cells(path, "reg")
        width = address_cells + size_cells
        if address_cells not in (1, 2) or size_cells not in (1, 2) or len(raw) % width:
            raise ValueError(f"invalid reg encoding at {path}")
        result = []
        for offset in range(0, len(raw), width):
            base = 0
            size = 0
            for item in raw[offset:offset + address_cells]:
                base = (base << 32) | item
            for item in raw[offset + address_cells:offset + width]:
                size = (size << 32) | item
            result.append((base, size))
        return result


def apple_node(name, properties=(), children=()):
    properties = [("name", name.encode("ascii") + b"\0"), *properties]
    result = struct.pack("<II", len(properties), len(children))
    for key, value in properties:
        result += struct.pack("<32sI", key.encode("ascii"), len(value))
        result += value + bytes(-len(value) % 4)
    return result + b"".join(children)


def empty_nvram_proxy():
    def header(name, size):
        value = bytearray(struct.pack("<BBH12s", 0x70, 0, size // 16, name))
        checksum = sum(value)
        while checksum > 255:
            checksum = (checksum & 255) + (checksum >> 8)
        value[1] = checksum
        return value

    image = bytearray(8192)
    image[:16] = header(b"nvram", 32)
    image[32:48] = header(b"common", 16 + 2048)
    free = 32 + 16 + 2048
    image[free:free + 16] = header(b"free", len(image) - free)
    struct.pack_into("<I", image, 16, zlib.adler32(image[20:]))
    return [
        ("nvram-bank-size", struct.pack("<I", len(image))),
        ("nvram-bank-count", struct.pack("<I", 1)),
        ("nvram-current-bank", struct.pack("<I", 0)),
        ("nvram-proxy-data", bytes(image)),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dram-size", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--fdt", type=Path, default=os.environ.get("DALI_RUNTIME_FDT"),
                        help="private live FDT; or set DALI_RUNTIME_FDT")
    parser.add_argument("--cpu-regs", default="0")
    parser.add_argument("--psci-method", choices=("smc", "hvc"), default="smc")
    parser.add_argument("--ramdisk", action="store_true", help="zero RAMDisk slot for DarwinBoot to fill")
    parser.add_argument("--trust-cache", action="store_true", help="zero TrustCache slot for the explicit own test input")
    parser.add_argument("--ufs-readonly", action="store_true", help="validate and include the optional physical UFS nub")
    parser.add_argument("--ufs-scratch-grant", type=Path, help="explicit private grant JSON for a boot_b scratch-write test")
    parser.add_argument("--run-token", help="fresh run token; must match the explicit scratch grant")
    args = parser.parse_args()
    if args.fdt is None:
        parser.error("a private live Dali FDT is required (--fdt or DALI_RUNTIME_FDT)")
    try:
        raw_fdt = args.fdt.read_bytes()
        tree = Fdt(raw_fdt)
        ufs_mode = os.environ.get("DALI_XNU_UFS_READ_ONLY", "false")
        if ufs_mode not in ("true", "false"):
            raise ValueError("DALI_XNU_UFS_READ_ONLY must be true or false")
        scratch_mode = os.environ.get("DALI_XNU_UFS_SCRATCH_WRITE", "false")
        if scratch_mode not in ("true", "false"):
            raise ValueError("DALI_XNU_UFS_SCRATCH_WRITE must be true or false")
        if bool(args.ufs_scratch_grant) != (scratch_mode == "true"):
            raise ValueError("scratch-write mode requires exactly one explicit grant JSON")
        scratch_grant = None
        if args.ufs_scratch_grant:
            if not (args.ufs_readonly or ufs_mode == "true") or not args.run_token:
                raise ValueError("scratch grant requires the physical UFS node and the matching run token")
            document = json.loads(args.ufs_scratch_grant.read_text())
            if not isinstance(args.run_token, str) or len(args.run_token) != 32 or args.run_token.lower() != args.run_token:
                raise ValueError("invalid scratch grant run token")
            if not isinstance(document, dict) or document.get("token") != args.run_token:
                raise ValueError("scratch grant token differs from the candidate run token")
            scratch_grant = encode_scratch_grant(document)
        ufs_resources = resource_contract(tree, raw_fdt) if args.ufs_readonly or ufs_mode == "true" else None
        root = tree.nodes.get("/", {})
        if b"mediatek,MT6991" not in tree.string_list("/", "compatible"):
            raise ValueError("FDT is not MT6991")
        memory = tree.reg("/memory")
        if memory != [(0x80000000, args.dram_size)]:
            raise ValueError(f"DRAM differs from configured size: {memory}")
        cpu_regs = [int(value.strip(), 0) for value in args.cpu_regs.split(",") if value.strip()]
        if cpu_regs != [0]:
            raise ValueError("Dali early-boot profile must expose only the boot CPU MPIDR 0")
        cpus = tree.nodes.get("/cpus", {})
        cpu0 = tree.nodes.get("/cpus/cpu@0", {})
        if (int.from_bytes(cpus.get("#address-cells", b""), "big") != 1 or
                tree.cells("/cpus/cpu@0", "reg") != (0,)):
            raise ValueError("runtime FDT does not identify CPU0 as MPIDR 0")
        timer_hz = tree.cells("/timer", "clock-frequency")
        if timer_hz != (13_000_000,):
            raise ValueError(f"unexpected architected timer rate: {timer_hz}")
        gic_regs = tree.reg("/interrupt-controller")
        if (b"arm,gic-v3" not in tree.string_list("/interrupt-controller", "compatible") or
                gic_regs != [(0x0C400000, 0x40000), (0x0C440000, 0x200000)]):
            raise ValueError(f"runtime GICv3 resources differ: {gic_regs}")
        psci = tree.nodes.get("/psci", {})
        if (args.psci_method != "smc" or b"arm,psci-1.0" not in tree.string_list("/psci", "compatible") or
                b"smc" not in tree.string_list("/psci", "method")):
            raise ValueError("runtime PSCI method differs from the verified SMC conduit")
        watchdog = tree.nodes.get("/watchdog@1c010000", {})
        reboot = tree.nodes.get("/watchdog@1c010000/reboot-mode", {})
        if (tree.reg("/watchdog@1c010000") != [(0x1C010000, 0x1000)] or
                b"mediatek,mt6991-wdt" not in tree.string_list("/watchdog@1c010000", "compatible") or
                b"syscon-reboot-mode" not in tree.string_list("/watchdog@1c010000/reboot-mode", "compatible") or
                tree.cells("/watchdog@1c010000/reboot-mode", "offset") != (0x24,) or
                tree.cells("/watchdog@1c010000/reboot-mode", "mask") != (0x0F,) or
                tree.cells("/watchdog@1c010000/reboot-mode", "mode-bootloader") != (0x03,)):
            raise ValueError("runtime reboot-mode watchdog contract differs")
        if (tree.reg("/soc/pinctrl@1002d000")[:1] != [(0x1002D000, 0x1000)] or
                tree.cells("/gpio-keys/volumedown", "gpios") !=
                (*tree.cells("/soc/pinctrl@1002d000", "phandle"), 60, 1)):
            raise ValueError("runtime GPIO60 manual recovery contract differs")
        chosen_fdt = tree.nodes.get("/chosen", {})
        if (chosen_fdt.get("aee,enable") != b"mini\0" or
                chosen_fdt.get("mrdump,lk") != b"MRDUMP11\0" or
                len(chosen_fdt.get("mrdump,cblock", b"")) != 8):
            raise ValueError("runtime AEE mini/MRDUMP11 recovery contract differs")
    except (OSError, ValueError, KeyError, struct.error) as error:
        raise SystemExit(f"Dali FDT validation failed: {error}") from None

    cpu = apple_node("cpu0", [
        ("state", b"running\0"), ("reg", struct.pack("<I", 0)),
        ("device_type", b"cpu\0"), ("compatible", b"arm,armv8\0"),
        ("timebase-frequency", struct.pack("<I", timer_hz[0])),
    ])
    intc = apple_node("intc", [
        ("compatible", b"arm,gic-v3\0"),
        ("reg", struct.pack("<4Q", 0x400000, 0x40000, 0x440000, 0x200000)),
    ])
    boot_log = apple_node("dali-boot-log", [
        ("compatible", b"localhosts,dali-boot-log\0"),
        # Absolute resources, deliberately separate from arm-io's GIC bus.
        # All fields were checked against the live FDT above.
        ("recovery-rgu", struct.pack("<2Q", *tree.reg("/watchdog@1c010000")[0])),
        ("recovery-reboot-mode", struct.pack("<3I", 0x24, 0x0F, 3)),
        ("manual-recovery-gpio", struct.pack("<2Q", 0x1002D000, 0x1000)),
        # Original Dali mrdump.ko: C4000012, vendor bit | KE(2)<<8 | AEE(1).
        # No control-block or DDR reserve registers are written by this port.
        ("recovery-aee-reset-type", struct.pack("<I", 0x80000201)),
    ])
    dram_base = memory[0][0]
    chosen = apple_node("chosen", [
        ("random-seed", secrets.token_bytes(64)),
        ("osenvironment", b"main\0"),
        ("dram-base", struct.pack("<Q", dram_base)),
        ("dram-size", struct.pack("<Q", args.dram_size)),
        # Only DarwinBoot may fill the measurement of the actual loaded file.
        *([("darwinboot-kernel-sha256", bytes(32))] if scratch_grant is not None else []),
        *empty_nvram_proxy(),
    ], [apple_node("memory-map", [
        *([("RAMDisk", bytes(16))] if args.ramdisk else []),
        *([("TrustCache", bytes(16))] if args.trust_cache else []),
    ])])
    root_node = apple_node("device-tree", [
        ("model", b"REDMI K80 Ultra\0"),
        ("compatible", b"xiaomi,dali\0"),
    ], [
        chosen,
        apple_node("cpus", children=[cpu]),
        apple_node("arm-io", [
            ("device_type", b"arm-io\0"),
            ("#address-cells", struct.pack("<I", 2)),
            ("#size-cells", struct.pack("<I", 2)),
            ("ranges", struct.pack("<QQQ", 0, 0x0C000000, 0x01000000)),
        ], [intc, boot_log, *([apple_node("dali-ufs", [
            ("compatible", b"localhosts,dali-ufs\0"),
            ("resources-v1", ufs_resources),
            *([("scratch-grant-v2", scratch_grant)] if scratch_grant is not None else []),
        ])] if ufs_resources is not None else [])]),
        apple_node("defaults", [("sleep-disabled", struct.pack("<I", 1))]),
        apple_node("options"),
        apple_node("product", [
            ("product-name", b"REDMI K80 Ultra\0"),
            ("model", b"dali\0"),
        ]),
    ])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(root_node)
    print(f"Dali Apple DT: {len(root_node)} bytes, CPU MPIDR 0, GICv3, timer {timer_hz[0]} Hz")


if __name__ == "__main__":
    main()
