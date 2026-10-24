"""Execute the exact pre-MMU patch instructions against persistent RAM rings."""
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
        instructions = "\n".join(line[1:] for line in patch.read_text().splitlines()
                                 if line.startswith("+") and not line.startswith("+++"))
        signature, capacity = 0x43474244, 0x200000 - 12
        cases = [(signature, 0, 0), (signature, 7, 7), (0, 0, 0),
                 (signature, 3, 20), (signature, capacity + 1, capacity + 1),
                 (signature, capacity - 8, capacity - 8),
                 (signature, capacity - 9, capacity - 9)]
        with tempfile.TemporaryDirectory(prefix="dali-entry-") as directory:
            work = Path(directory)
            (work / "entry.S").write_text(""".text
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
  ldr x0, =0x1122334455667788
  mov x19, x0
  bl breadcrumb
  cmp x0, x19
  cset x0, eq
  ldr x1, =0x81000000
  str x0, [x1]
1: b 1b
breadcrumb:
""" + instructions + "\nret\n.ltorg\n")
            subprocess.run(["clang", "--target=aarch64-none-elf", "-c", work / "entry.S", "-o", work / "entry.o"], check=True)
            subprocess.run(["ld.lld", "-m", "aarch64elf", "-Ttext=0x80000000", "-e", "_start",
                            work / "entry.o", "-o", work / "entry.elf"], check=True)
            subprocess.run(["llvm-objcopy", "-O", "binary", work / "entry.elf", work / "entry.bin"], check=True)
            for sig, start, size in cases:
                with self.subTest(signature=sig, start=start, size=size):
                    before = bytearray(b"\x5a" * 0x200000)
                    struct.pack_into("<III", before, 0, sig, start, size)
                    (work / "ring.bin").write_bytes(before)
                    expected = before.copy()
                    if sig == signature and start == size and start < capacity - 8:
                        expected[12 + start:20 + start] = b"DALI:X0\n"
                        struct.pack_into("<II", expected, 4, start + 8, size + 8)
                    qmp_path = work / "qmp"
                    qmp_path.unlink(missing_ok=True)
                    command = ["qemu-system-aarch64", "-M", "virt,virtualization=on", "-cpu", "cortex-a76",
                               "-m", "4G", "-smp", "1", "-display", "none", "-serial", "none", "-monitor", "none", "-S",
                               "-qmp", f"unix:{qmp_path},server=on,wait=off",
                               "-device", f"loader,file={work / 'entry.bin'},addr=0x80000000,cpu-num=0,force-raw=on",
                               "-device", f"loader,file={work / 'ring.bin'},addr=0xaec0f000,force-raw=on"]
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
                            for address, length, filename in [(0xaec0f000, 0x200000, "actual.bin"),
                                                              (0x81000000, 8, "abi.bin")]:
                                result = qmp("human-monitor-command", **{"command-line":
                                    f'pmemsave {address:#x} {length:#x} "{work / filename}"'})
                                self.assertFalse(result, result)
                            qmp("quit")
                        process.wait(timeout=5)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                    self.assertEqual((work / "actual.bin").read_bytes(), expected)
                    self.assertEqual(struct.unpack("<Q", (work / "abi.bin").read_bytes())[0], 1,
                                     "x0 boot_args changed")
