"""Run the actual Dali map, MMU-on markers and C writer at physical and KVA PCs."""
import json
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def added_file(patch, name):
    section = patch.split("+++ b/" + name + "\n", 1)[1]
    section = re.split(r"\n(?:--- |diff --git )", section, maxsplit=1)[0]
    return "\n".join(line[1:] for line in section.splitlines()
                     if line.startswith("+") and not line.startswith("+++")) + "\n"


class BootstrapConsoleTests(unittest.TestCase):
    def test_actual_mapping_mmu_transition_and_c_writer(self):
        tools = ("clang", "ld.lld", "llvm-nm", "qemu-system-aarch64")
        if not all(shutil.which(tool) for tool in tools):
            self.skipTest("AArch64 assembler/linker and QEMU required")
        early = added_file((ROOT / "patches/xnu/dali-entry-breadcrumb.patch").read_text(),
                           "osfmk/arm64/start.s")
        macro = early[early.index("\t\t.macro dali_early_mark"):early.index("\t\t.endm") + len("\t\t.endm")]
        patch = (ROOT / "patches/xnu/zzzz-dali-bootstrap-console.patch").read_text()
        start = added_file(patch, "osfmk/arm64/start.s")
        mapping = start[start.index("\t/* DALI_BOOTSTRAP_MAP_BEGIN"):start.index("\t/* DALI_BOOTSTRAP_MAP_END */")]
        mapping = mapping.replace("EXT(dali_bootstrap_console)@pageoff", ":lo12:dali_bootstrap_console")
        mapping = mapping.replace("EXT(dali_bootstrap_console)@page", "dali_bootstrap_console")
        writer = added_file(patch, "osfmk/arm64/dali_bootstrap_log.c")
        final_patch = (ROOT / "patches/xnu/zzzzz-dali-vm-console.patch").read_text()
        final_source = added_file(final_patch, "osfmk/arm64/arm_vm_init.c")
        final_mapper = final_source[final_source.index("/* DALI_FINAL_CONSOLE_MAP_BEGIN"):
                                    final_source.index("/* DALI_FINAL_CONSOLE_MAP_END */")]
        signature, capacity = 0x43474244, 0x200000 - 12
        console, pool = 0xaec0f000, 0x91000000
        flags = 0x006000000000060d  # Device MAIR index 3, AF, outer-shareable, PXN/UXN.
        cases = ("mapped", "non-dali", "bad-root-type", "root-outside-pool",
                 "occupied-first", "occupied-second", "bad-signature", "wrapped-ring",
                 "almost-full-ring", "full-ring", "over-capacity", "final-mapped",
                 "final-non-dali", "final-occupied-first", "final-occupied-second",
                 "final-bad-root", "final-bad-table", "final-tight-gap", "final-aperture-overlap")
        with tempfile.TemporaryDirectory(prefix="dali-bootstrap-console-") as directory:
            work = Path(directory)
            (work / "writer.c").write_text(writer)
            # Compile the final production writer, after both follow-up patches.
            for filename in ("zzzzz-dali-vm-console.patch", "zzzzzz-dali-startup-console.patch"):
                text = (ROOT / "patches/xnu" / filename).read_text()
                marker = "--- a/osfmk/arm64/dali_bootstrap_log.c\n"
                section = marker + text.split(marker, 1)[1]
                section = re.split(r"\n(?:--- a/|diff --git )", section, maxsplit=1)[0] + "\n"
                subprocess.run(["patch", "--batch", "--fuzz=0", str(work / "writer.c")],
                               input=section, text=True, check=True, stdout=subprocess.PIPE)
            assembly = r'''
#define PGBYTES 4096
#define BOOTSTRAP_TABLE_BYTES 65536
#define TTE_SHIFT 3
#define ARM_TT_L1_INDEX_MASK 0x3fc0000000
#define ARM_TT_L1_SHIFT 30
#define ARM_TTE_TABLE_MASK 0x0000fffffffff000
#define ARM_TT_L2_INDEX_MASK 0x3fe00000
#define ARM_TT_L2_SHIFT 21
#define ARM_TTE_BLOCK_L2_MASK 0x0000ffffffe00000
#define ARM_TT_L2_SIZE 0x200000
#define ARM_TTE_VALID 1
#define ARM_TTE_TYPE_MASK 2
#define ARM_TTE_TYPE_TABLE 2
#define ARM_TTE_TYPE_BLOCK 0
#define ARM_TTE_EMPTY 0
#define ARM_PGBYTES 4096
// Match pinned native pmap.h and vm_param.h: these differ by 64 GiB.
#define VM_MIN_KERNEL_ADDRESS 0xffffffe000000000UL
#define LOW_GLOBAL_BASE_ADDRESS 0xfffffff000000000UL
#define ARM_TTE_BLOCK_AF 0x400
#define ARM_TTE_BLOCK_SH(x) ((x) << 8)
#define SH_OUTER_MEMORY 2
#define ARM_TTE_BLOCK_ATTRINDX(x) ((x) << 2)
#define CACHE_ATTRINDX_DISABLE 3
#define ARM_TTE_BLOCK_PNX 0x0020000000000000
#define ARM_TTE_BLOCK_NX 0x0040000000000000
.macro MOV64 register, value
  movz \register, #((\value) & 0xffff)
  movk \register, #(((\value) >> 16) & 0xffff), lsl #16
  movk \register, #(((\value) >> 32) & 0xffff), lsl #32
  movk \register, #(((\value) >> 48) & 0xffff), lsl #48
.endm
.macro check_daif
  ldr x16, =0xfffffff008700020
  ldr x16, [x16]
  mrs x17, daif
  cmp x16, x17
  b.ne failed
.endm
.text
.global _start
_start:
  mrs x9, CurrentEL
  cmp x9, #8
  b.ne el1
  mov x9, #0x80000000
  msr hcr_el2, x9
  mov x9, #3
  msr cnthctl_el2, x9
  msr cntvoff_el2, xzr
  adr x9, el1
  msr elr_el2, x9
  mov x9, #0x3c5
  msr spsr_el2, x9
  eret
el1:
  ldr x22, =0xfffffff007004000
  ldr x23, =0x90804000
  mov x20, #0x20
  mov x21, xzr
  mov x24, #0x24
  ldr x25, =0x91000000
  mov x26, #0x26
  ldr x16, =0x91f00000
  ldr x28, [x16, #8]
  // The fixture runs the DT-guarded mapper with its actual input register.
  mrs x9, sctlr_el1
  orr x9, x9, #2
  msr sctlr_el1, x9
  isb
''' + macro + "\n" + mapping + r'''
  ldr x16, =0x91f00000
  ldr x16, [x16, #16]
  cbz x16, finished
  ldr x9, =0x22a51a251a
  msr tcr_el1, x9
  msr ttbr0_el1, x25
  add x9, x25, #0x1000
  msr ttbr1_el1, x9
  ldr x9, =0x0c0804ff00bb44ff
  msr mair_el1, x9
  tlbi vmalle1
  dsb sy
  ic iallu
  dsb sy
  isb
  msr SPSel, #0
  ldr x9, =0xfffffff007400000
  mov sp, x9
  ldr x0, =0x3454593d
  msr sctlr_el1, x0
  isb
  dali_early_mark 7
  // Stage 7 must preserve the SCTLR argument across its scratch registers.
  ldr x9, =0x3454593d
  cmp x0, x9
  b.ne failed
  ldr x0, =0xfffffff008144000
  dali_early_mark 8
  ldr x9, =0xfffffff008144000
  cmp x0, x9
  b.ne failed
  adr x30, high_entry
  add x30, x30, x22
  sub x30, x30, x23
  ret
high_entry:
  dali_early_mark 9
  ldr x16, =0x91f00000
  ldr x16, [x16, #32]
  msr daif, x16
  adrp x0, message
  add x0, x0, :lo12:message
  bl dali_bootstrap_log
  check_daif
  adrp x0, stage_message
  add x0, x0, :lo12:stage_message
  bl dali_bootstrap_stage
  check_daif
  adrp x0, overlong
  add x0, x0, :lo12:overlong
  bl dali_bootstrap_log
  check_daif
  adrp x0, overlong
  add x0, x0, :lo12:overlong
  bl dali_bootstrap_stage
  check_daif
  ldr x16, =0x91f00000
  ldr x0, [x16, #24]
  cbz x0, end_bootstrap
  bl fixture_final_map
  cbz x0, end_bootstrap
  mov x19, x0
  ldr x9, =0x91004000
  dsb sy
  msr ttbr1_el1, x9
  isb
  adrp x9, dali_bootstrap_console
  add x9, x9, :lo12:dali_bootstrap_console
  str x19, [x9]
  adrp x0, final_live
  add x0, x0, :lo12:final_live
  bl dali_bootstrap_log
  check_daif
  ldr x9, =0x91006000
  msr ttbr0_el1, x9
  isb
  tlbi vmalle1
  dsb sy
  isb
  adrp x0, ttbr0_off
  add x0, x0, :lo12:ttbr0_off
  bl dali_bootstrap_log
  check_daif
  bl dali_bootstrap_log_end
  check_daif
  adrp x0, poison
  add x0, x0, :lo12:poison
  bl dali_bootstrap_log
  // TTBR0 is empty: even the completion store must now use a kernel VA.
  ldr x16, =0xfffffff008700000
  b complete
end_bootstrap:
  bl dali_bootstrap_log_end
  check_daif
  adrp x0, poison
  add x0, x0, :lo12:poison
  bl dali_bootstrap_log
  mov x9, #0x20
  cmp x20, x9
  b.ne failed
  cbnz x21, failed
  cmp x24, #0x24
  b.ne failed
  cmp x26, #0x26
  b.ne failed
finished:
  ldr x16, =0x91f00000
complete:
  mov x9, #0x55
  str x9, [x16]
  dsb sy
1: b 1b
failed:
  b failed
.ltorg
.section .rodata
message: .asciz "DALI:C0 fixture reached\n"
stage_message: .asciz "fixture stage"
overlong:
.rept 192
.byte 120
.endr
.byte 0
poison: .asciz "MUST NOT WRITE AFTER BOOTSTRAP END\n"
final_live: .asciz "DALI:V8 fixture final TTBR1 live\n"
ttbr0_off: .asciz "DALI:V9 fixture TTBR0 removed\n"
'''
            (work / "entry.S").write_text(assembly)
            constants = assembly[:assembly.index(".macro MOV64")]
            (work / "final.c").write_text(constants + r'''
typedef unsigned long vm_offset_t;
typedef unsigned long vm_map_address_t;
typedef unsigned long pmap_paddr_t;
typedef unsigned long tt_entry_t;
extern volatile unsigned long dali_bootstrap_console;
void dali_bootstrap_log(const char *);
void dali_bootstrap_log_end(void);
static vm_offset_t gVirtBase, gPhysBase, avail_start, real_avail_end;
static tt_entry_t *cpu_tte;
static vm_offset_t phystokv(pmap_paddr_t pa) { return gVirtBase + pa - gPhysBase; }
''' + final_mapper + r'''
vm_offset_t fixture_final_map(unsigned long mode);
vm_offset_t fixture_final_map(unsigned long mode) {
    gVirtBase = mode == 2 ? VM_MIN_KERNEL_ADDRESS + 0x400000 : 0xfffffff007004000;
    gPhysBase = 0x90804000;
    avail_start = 0x91008000;
    real_avail_end = mode == 3 ? 0xb0804000 : 0xa0804000;
    cpu_tte = (tt_entry_t *)(0xfffffff007004000UL + 0x91004000UL - gPhysBase);
    return dali_console_map_final();
}
''')
            subprocess.run(["clang", "--target=aarch64-none-elf", "-c", work / "entry.S", "-o", work / "entry.o"], check=True)
            subprocess.run(["clang", "--target=aarch64-none-elf", "-O2", "-ffreestanding", "-fno-builtin",
                            "-Wall", "-Wextra", "-Werror", "-Wmissing-prototypes", "-Wmissing-variable-declarations",
                            "-fno-stack-protector", "-mgeneral-regs-only", "-c", work / "writer.c",
                            "-o", work / "writer.o"], check=True)
            subprocess.run(["clang", "--target=aarch64-none-elf", "-O2", "-ffreestanding", "-fno-builtin",
                            "-Wall", "-Wextra", "-Werror", "-Wmissing-prototypes", "-Wmissing-variable-declarations",
                            "-fno-stack-protector", "-mgeneral-regs-only", "-c", work / "final.c",
                            "-o", work / "final.o"], check=True)
            subprocess.run(["ld.lld", "-m", "aarch64elf", "-Ttext=0x90804000", "-e", "_start",
                            work / "entry.o", work / "writer.o", work / "final.o", "-o", work / "entry.elf"], check=True)
            for case in cases:
                with self.subTest(case=case):
                    tables = bytearray(0x8000)
                    # The same L1 entry contains both kernel and console; keep it.
                    struct.pack_into("<Q", tables, 2 * 8, pool + 0x2000 | 3)
                    high_index = (0xfffffff007004000 & 0x3fc0000000) >> 30
                    struct.pack_into("<Q", tables, 0x1000 + high_index * 8, pool + 0x3000 | 3)
                    for index in range(128):
                        physical = 0x90800000 + index * 0x200000
                        struct.pack_into("<Q", tables, 0x2000 + (132 + index) * 8, physical | 0x601)
                        struct.pack_into("<Q", tables, 0x3000 + (56 + index) * 8, physical | 0x601)
                    struct.pack_into("<Q", tables, 0x4000 + high_index * 8, pool + 0x5000 | 3)
                    tables[0x5000:0x6000] = tables[0x3000:0x4000]
                    if case.startswith("final-occupied"):
                        struct.pack_into("<Q", tables, 0x5000 + (1 + (case == "final-occupied-second")) * 8, 0x12345678)
                    if case == "final-bad-root":
                        struct.pack_into("<Q", tables, 0x4000 + high_index * 8, pool + 0x5000 | 1)
                    if case == "final-bad-table":
                        struct.pack_into("<Q", tables, 0x4000 + high_index * 8, pool + 0x8000 | 3)
                    if case == "bad-root-type":
                        struct.pack_into("<Q", tables, 16, pool + 0x2000 | 1)
                    if case == "root-outside-pool":
                        struct.pack_into("<Q", tables, 16, pool + 0x10000 | 3)
                    if case.startswith("occupied"):
                        struct.pack_into("<Q", tables, 0x2000 + (374 + (case == "occupied-second")) * 8, 0x12345678)
                    negative_map = case in ("bad-root-type", "root-outside-pool", "occupied-first", "occupied-second")
                    expected_tables = tables.copy()
                    if case not in ("non-dali", "final-non-dali") and not negative_map:
                        struct.pack_into("<QQ", expected_tables, 0x2000 + 374 * 8,
                                         0xaec00000 | flags, 0xaee00000 | flags)
                    if case == "final-mapped":
                        struct.pack_into("<QQ", expected_tables, 0x5000 + 8,
                                         0xaec00000 | flags, 0xaee00000 | flags)
                    ring = bytearray(b"\x5a" * 0x200000)
                    initial_start = {"wrapped-ring": 3, "almost-full-ring": capacity - 33,
                                     "full-ring": capacity, "over-capacity": capacity + 1}.get(case, 0)
                    struct.pack_into("<III", ring, 0, 0 if case == "bad-signature" else signature,
                                     initial_start, 20 if case == "wrapped-ring" else initial_start)
                    expected_ring = ring.copy()
                    suffix = b""
                    if negative_map:
                        suffix = b"DALI:XM\n"
                    elif case == "mapped":
                        suffix = b"DALI:X7\nDALI:X8\nDALI:X9\nDALI:C0 fixture reached\nDALI:K fixture stage\n"
                    elif case == "almost-full-ring":
                        suffix = b"DALI:X7\nDALI:X8\nDALI:X9\n"
                    elif case.startswith("final-") and case != "final-non-dali":
                        suffix = b"DALI:X7\nDALI:X8\nDALI:X9\nDALI:C0 fixture reached\n"
                        suffix += b"DALI:K fixture stage\n"
                        if case == "final-mapped":
                            suffix += b"DALI:V8 fixture final TTBR1 live\nDALI:V9 fixture TTBR0 removed\n"
                        else:
                            suffix += b"DALI:VM final console unavailable; sink ending\n"
                    if suffix:
                        expected_ring[12 + initial_start:12 + initial_start + len(suffix)] = suffix
                        struct.pack_into("<II", expected_ring, 4,
                                         initial_start + len(suffix), initial_start + len(suffix))
                    (work / "tables.bin").write_bytes(tables)
                    (work / "ring.bin").write_bytes(ring)
                    (work / "mode.bin").write_bytes(struct.pack("<QQQQQ", 0,
                        0 if case in ("non-dali", "final-non-dali") else console,
                        0 if negative_map else 1,
                        ({"final-tight-gap": 2, "final-aperture-overlap": 3}.get(case, 1))
                        if case.startswith("final-") else 0,
                        (0x3c0, 0x100, 0x280, 0)[cases.index(case) % 4]))
                    qmp_path = work / "qmp"
                    qmp_path.unlink(missing_ok=True)
                    command = ["qemu-system-aarch64", "-M", "virt,virtualization=on", "-cpu", "cortex-a76",
                               "-m", "4G", "-smp", "1", "-display", "none", "-serial", "none", "-monitor", "none", "-S",
                               "-qmp", f"unix:{qmp_path},server=on,wait=off",
                               "-device", f"loader,file={work / 'entry.elf'},cpu-num=0",
                               "-device", f"loader,file={work / 'tables.bin'},addr={pool:#x},force-raw=on",
                               "-device", f"loader,file={work / 'ring.bin'},addr={console:#x},force-raw=on",
                               "-device", f"loader,file={work / 'mode.bin'},addr=0x91f00000,force-raw=on"]
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
                            time.sleep(0.15)
                            qmp("stop")
                            for address, length, filename in ((console, 0x200000, "ring-after.bin"),
                                                              (pool, 0x8000, "tables-after.bin"),
                                                              (0x91f00000, 8, "result.bin")):
                                result = qmp("human-monitor-command", **{"command-line":
                                    f'pmemsave {address:#x} {length:#x} "{work / filename}"'})
                                self.assertFalse(result, result)
                            qmp("quit")
                        process.wait(timeout=5)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                    self.assertEqual((work / "tables-after.bin").read_bytes(), expected_tables,
                                     "mapper replaced a root or an existing translation")
                    self.assertEqual((work / "ring-after.bin").read_bytes(), expected_ring)
                    self.assertEqual(struct.unpack("<Q", (work / "result.bin").read_bytes())[0], 0x55,
                                     "MMU/high-VA entry/C writer faulted or changed live registers")
