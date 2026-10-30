import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/test_dali_ipados_root_qemu.py'
spec = importlib.util.spec_from_file_location('dali_gate_trace', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


APPLEIMAGE4_TEXT = 0xfffffff004a8c000
APPLEIMAGE4_TEXT_SIZE = 0x1dfb4
EXEC_VA = 0xfffffff004a00000
EXEC_FILEOFF = 0x1000
EXEC_SIZE = 0xb0000
STUB = 0xfffffff004aa9d5c
DATA_VA = 0xfffffff005cee000
DATA_FILEOFF = 0xb1000
DATA_SIZE = 0x2000
SLOT = 0xfffffff005cefe00
TARGET = 0xfffffff00b890b6c
TARGET_VA = 0xfffffff00b890000
TARGET_FILEOFF = 0xb3000
TARGET_SIZE = 0x1000
CALLSITE = APPLEIMAGE4_TEXT + module.APPLEIMAGE4_GATE_WAIT_CALLSITE_OFFSET


def macho_segment(name, va, size, fileoff, initprot):
    return struct.pack('<II16sQQQQiiII', 0x19, 72, name.encode().ljust(16, b'\0'),
                       va, size, fileoff, size, 7, initprot, 0, 0)


def write_candidate(directory, *, target=TARGET, call_word=None, data_protection=1):
    directory = Path(directory)
    kernel = directory / 'kernel.macho'
    prelink = directory / 'kernel.json'
    commands = b''.join((
        macho_segment('__PLK_TEXT_EXEC', EXEC_VA, EXEC_SIZE, EXEC_FILEOFF, 5),
        macho_segment('__PLK_DATA_CONST', DATA_VA, DATA_SIZE, DATA_FILEOFF, data_protection),
        macho_segment('__TEXT_EXEC', TARGET_VA, TARGET_SIZE, TARGET_FILEOFF, 5),
    ))
    image = bytearray(TARGET_FILEOFF + TARGET_SIZE)
    header = struct.pack('<IiiIIIII', 0xfeedfacf, 0x100000c, 0, 2, 3, len(commands), 0, 0)
    image[:32] = header
    image[32:32 + len(commands)] = commands

    call_delta = STUB - CALLSITE
    call_imm = (call_delta >> 2) & 0x03ffffff
    struct.pack_into('<I', image, EXEC_FILEOFF + CALLSITE - EXEC_VA,
                     call_word if call_word is not None else 0x94000000 | call_imm)

    stub_offset = EXEC_FILEOFF + STUB - EXEC_VA
    page_delta = ((SLOT & ~0xfff) - (STUB & ~0xfff)) >> 12
    page_imm = page_delta & 0x1fffff
    adrp = (0x90000010 | ((page_imm & 3) << 29) | (((page_imm >> 2) & 0x7ffff) << 5))
    ldr_imm = (SLOT & 0xfff) // 8
    ldr = 0xf9400210 | (ldr_imm << 10)
    struct.pack_into('<3I', image, stub_offset, adrp, ldr, 0xd61f0200)
    struct.pack_into('<Q', image, DATA_FILEOFF + SLOT - DATA_VA, target)

    kernel.write_bytes(image)
    report = {
        'candidate_sha256': hashlib.sha256(image).hexdigest(),
        'extents': [{
            'module': 'com.apple.security.AppleImage4',
            'segment': '__TEXT_EXEC',
            'va': APPLEIMAGE4_TEXT,
            'size': APPLEIMAGE4_TEXT_SIZE,
        }],
    }
    prelink.write_text(json.dumps(report))
    return kernel, prelink


class AppleImage4GateTraceTests(unittest.TestCase):
    def resolve(self, kernel, prelink):
        with patch.object(module.subprocess, 'check_output',
                          return_value=f'{TARGET:016x} S _lck_mtx_gate_wait\n') as run:
            result = module.resolve_image4_gate_wait_callsite(kernel, prelink)
        run.assert_called_once_with(['xcrun', 'nm', '-U', kernel.resolve()], text=True)
        return result

    def test_resolves_callsite_through_readonly_stub_to_exact_gate_wait_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            kernel, prelink = write_candidate(directory)
            expected_candidate_sha256 = hashlib.sha256(kernel.read_bytes()).hexdigest()
            result = self.resolve(kernel, prelink)
        self.assertEqual(result['address'], hex(CALLSITE))
        self.assertEqual(result['candidate_sha256'], expected_candidate_sha256)
        self.assertEqual(result['module_offset'], '0x8994')
        self.assertEqual(result['import_stub'], hex(STUB))
        self.assertEqual(result['slot'], hex(SLOT))
        self.assertEqual(result['target_symbol'], '_lck_mtx_gate_wait')
        self.assertEqual(result['target'], hex(TARGET))
        self.assertTrue(result['read_only'])

    def test_rejects_report_for_a_different_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            kernel, prelink = write_candidate(directory)
            report = json.loads(prelink.read_text())
            report['candidate_sha256'] = '0' * 64
            prelink.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'exact prelink candidate'):
                module.resolve_image4_gate_wait_callsite(kernel, prelink)

    def test_rejects_callsite_that_is_not_the_candidate_direct_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            kernel, prelink = write_candidate(directory, call_word=0xd503201f)
            with self.assertRaisesRegex(ValueError, 'not a direct BL'):
                module.resolve_image4_gate_wait_callsite(kernel, prelink)

    def test_rejects_writable_slot_or_wrong_import_target(self):
        with tempfile.TemporaryDirectory() as directory:
            kernel, prelink = write_candidate(directory, data_protection=3)
            with self.assertRaisesRegex(ValueError, 'required protection'):
                self.resolve(kernel, prelink)
        with tempfile.TemporaryDirectory() as directory:
            kernel, prelink = write_candidate(directory, target=TARGET + 4)
            with self.assertRaisesRegex(ValueError, 'does not resolve uniquely'):
                self.resolve(kernel, prelink)


if __name__ == '__main__':
    unittest.main()
