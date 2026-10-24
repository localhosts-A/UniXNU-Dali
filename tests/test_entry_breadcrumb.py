"""Execute exact pre-MMU instructions, including 1 GiB non-Dali isolation."""
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import unittest


class EntryBreadcrumbTests(unittest.TestCase):
    def test_real_instructions_preserve_boot_args_and_validate_ring(self):
        required = ("clang", "ld.lld", "llvm-objcopy", "qemu-system-aarch64")
        if not all(shutil.which(tool) for tool in required):
            self.skipTest("AArch64 assembler, ELF linker and QEMU required")
        patch = Path(__file__).resolve().parents[1] / "patches/xnu/dali-entry-breadcrumb.patch"
        start_patch = patch.read_text().split("--- a/osfmk/arm64/genassym.c")[0]
        instructions = "\n".join(line[1:] for line in start_patch.splitlines()
                                 if line.startswith("+") and not line.startswith("+++"))
        signature, capacity = 0x43474244, 0x200000 - 12
        rings = [(signature, 0, 0), (signature, 7, 7), (0, 0, 0),
                 (signature, 3, 20), (signature, capacity + 1, capacity + 1),
                 (signature, capacity - 8, capacity - 8),
                 (signature, capacity - 9, capacity - 9),
                 (signature, capacity - 17, capacity - 17)]

        def adt(properties):
            data = struct.pack("<II", len(properties), 0)
            for name, value in properties:
                data += struct.pack("<32sI", name, len(value)) + value + bytes(-len(value) % 4)
            return data

        valid = adt([(b"name", b"device-tree\0"), (b"odd", b"abc"),
                     (b"compatible", b"xiaomi,dali\0")])
        # The positive and negative paths use an ADT aligned only to four
        # bytes, with strict SCTLR alignment checking enabled.
        cases = [(f"ring-{index}", "4G", valid, {}, ring, True) for index, ring in enumerate(rings)]
        negative = {
            "missing-compatible": (adt([(b"name", b"device-tree\0")]), {}),
            "qemu-compatible": (adt([(b"compatible", b"linux,dummy-virt\0")]), {}),
            "bad-property-name": (adt([(b"compatibleX", b"xiaomi,dali\0")]), {}),
            "compatible-prefix": (adt([(b"compatible", b"xiaomi,dali\0extra")]), {}),
            "truncated-value": (valid[:-4], {}),
            "excessive-properties": (struct.pack("<II", 65, 0), {}),
            "empty-properties": (struct.pack("<II", 0, 0), {}),
            "virtual-underflow": (valid, {0x60: 1}),
            "outside-boot-memory": (valid, {0x18: 0x300010}),
            "unaligned-dt": (valid, {0x60: 0xfffffff007304005}),
            "too-short-dt": (valid, {0x68: 4}),
            "too-large-dt": (valid, {0x68: 0x10001}),
            "physical-overflow": (valid, {0x10: 0xfffffffffffffff0}),
        }
        cases += [(name, "1G", data, overrides, rings[0], False)
                  for name, (data, overrides) in negative.items()]
        with tempfile.TemporaryDirectory(prefix="dali-entry-") as directory:
            work = Path(directory)
            (work / "entry.S").write_text(""".text
.set BA_VIRT_BASE, 8
.set BA_PHYS_BASE, 16
.set BA_MEM_SIZE, 24
.set BA_DEVICE_TREE, 0x60
.set BA_DEVICE_TREE_LENGTH, 0x68
.global _start
_start:
  mrs x9, CurrentEL
  cmp x9, #8
  b.ne 2f
  mrs x9, sctlr_el2
  orr x9, x9, #2
  msr sctlr_el2, x9
  b 3f
2: mrs x9, sctlr_el1
  orr x9, x9, #2
  msr sctlr_el1, x9
3: isb
  ldr x0, =0x48200000
  mov x19, x0
  mov x20, #0x20
  mov x21, xzr
  mov x22, #0x22
  mov x23, #0x23
  mov x24, #0x24
  mov x25, #0x25
  mov x26, #0x26
  bl breadcrumb
  cmp x0, x19
  cset x0, eq
  cmp x20, #0x20
  cset x2, eq
  and x0, x0, x2
  cmp x21, #0
  cset x2, eq
  and x0, x0, x2
  cmp x22, #0x22
  cset x2, eq
  and x0, x0, x2
  cmp x23, #0x23
  cset x2, eq
  and x0, x0, x2
  cmp x24, #0x24
  cset x2, eq
  and x0, x0, x2
  cmp x25, #0x25
  cset x2, eq
  and x0, x0, x2
  cmp x26, #0x26
  cset x2, eq
  and x0, x0, x2
  ldr x1, =0x48201000
  str x0, [x1]
1: b 1b
breadcrumb:
""" + instructions + "\nret\n.ltorg\n")
            subprocess.run(["clang", "--target=aarch64-none-elf", "-c", work / "entry.S", "-o", work / "entry.o"], check=True)
            subprocess.run(["ld.lld", "-m", "aarch64elf", "-Ttext=0x48000000", "-e", "_start",
                            work / "entry.o", "-o", work / "entry.elf"], check=True)
            subprocess.run(["llvm-objcopy", "-O", "binary", work / "entry.elf", work / "entry.bin"], check=True)
            for name, memory, dt, overrides, (sig, start, size), dali in cases:
                with self.subTest(case=name):
                    boot_args = bytearray(0x480)
                    values = {8: 0xfffffff007004000, 16: 0x48000000, 24: 0x10000000,
                              0x60: 0xfffffff007304004, 0x68: len(dt)}
                    values.update(overrides)
                    for offset, value in values.items():
                        struct.pack_into("<I" if offset == 0x68 else "<Q", boot_args, offset, value)
                    (work / "args.bin").write_bytes(boot_args)
                    (work / "dt.bin").write_bytes(dt)
                    before = bytearray(b"\x5a" * 0x200000)
                    struct.pack_into("<III", before, 0, sig, start, size)
                    (work / "ring.bin").write_bytes(before)
                    expected = before.copy()
                    if dali and sig == signature and start == size:
                        for stage in range(7):
                            if start >= capacity - 8:
                                break
                            expected[12 + start:20 + start] = f"DALI:X{stage}\n".encode()
                            start += 8
                            struct.pack_into("<II", expected, 4, start, start)
                    qmp_path = work / "qmp"
                    qmp_path.unlink(missing_ok=True)
                    command = ["qemu-system-aarch64", "-M", "virt,virtualization=on", "-cpu", "cortex-a76",
                               "-m", memory, "-smp", "1", "-display", "none", "-serial", "none", "-monitor", "none", "-S",
                               "-qmp", f"unix:{qmp_path},server=on,wait=off",
                               "-device", f"loader,file={work / 'entry.bin'},addr=0x48000000,cpu-num=0,force-raw=on",
                               "-device", f"loader,file={work / 'args.bin'},addr=0x48200000,force-raw=on",
                               "-device", f"loader,file={work / 'dt.bin'},addr=0x48300004,force-raw=on"]
                    if dali:
                        command += ["-device", f"loader,file={work / 'ring.bin'},addr=0xaec0f000,force-raw=on"]
                    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        deadline = time.monotonic() + 5
                        while not qmp_path.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.01)
                        with socket.socket(socket.AF_UNIX) as connection:
                            connection.settimeout(5)
                            connection.connect(str(qmp_path))
                            stream = connection.makefile("rwb")
                            stream.readline()

                            def qmp(name, **arguments):
                                stream.write(json.dumps({"execute": name, "arguments": arguments}).encode() + b"\n")
                                stream.flush()
                                while True:
                                    response = json.loads(stream.readline())
                                    if "return" in response:
                                        return response["return"]
                                    if "error" in response:
                                        self.fail(str(response["error"]))

                            qmp("qmp_capabilities")
                            qmp("cont")
                            time.sleep(0.1)
                            qmp("stop")
                            reads = [(0x48201000, 8, "abi.bin")]
                            if dali:
                                reads.append((0xaec0f000, 0x200000, "actual.bin"))
                            for address, length, filename in reads:
                                result = qmp("human-monitor-command", **{"command-line":
                                    f'pmemsave {address:#x} {length:#x} "{work / filename}"'})
                                self.assertFalse(result, result)
                            qmp("quit")
                        process.wait(timeout=5)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                    if dali:
                        self.assertEqual((work / "actual.bin").read_bytes(), expected)
                    self.assertEqual(struct.unpack("<Q", (work / "abi.bin").read_bytes())[0], 1,
                                     "breadcrumb faulted or changed live boot_args/bootstrap registers")
