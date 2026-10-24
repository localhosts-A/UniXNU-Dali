"""Inspect the exact production SMC wrappers, without issuing hardware SMCs."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PsciAbiTests(unittest.TestCase):
    def test_actual_aarch64_wrappers_and_abi_negative(self):
        if not all(shutil.which(name) for name in ('clang++', 'llvm-nm', 'llvm-objdump')):
            self.skipTest('AArch64 clang and LLVM inspection tools required')
        spec = importlib.util.spec_from_file_location('reset_gate', ROOT / 'scripts/test-dali-xnu-qemu.py')
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / 'abi.cpp').write_text('''#include "DaliPsci.h"
extern "C" uint32_t fixture(uint32_t mode, uint32_t type) {
  if (mode == 0) return recoveryPsciFeatures();
  if (mode == 1) return recoveryPsciColdReset();
  return recoveryPsciReset2(type);
}
''')
            obj = work / 'abi.o'
            subprocess.run(['clang++', '--target=aarch64-none-elf', '-std=c++14', '-ffreestanding',
                            '-O2', '-Wall', '-Wextra', '-Werror', '-I', ROOT / 'drivers/DaliBootLog',
                            '-c', work / 'abi.cpp', '-o', obj], check=True)
            symbols = subprocess.check_output(['llvm-nm', '--defined-only', obj], text=True)
            bodies = {}
            for fragment in ('recoveryPsciFeatures', 'recoveryPsciColdReset', 'recoveryPsciReset2'):
                symbol, = [line.split()[-1] for line in symbols.splitlines() if fragment in line]
                bodies[fragment] = subprocess.check_output(
                    ['llvm-objdump', '-d', '--disassemble-symbols=' + symbol, obj], text=True)
            gate.validate_psci_abis(bodies)
            bad = dict(bodies)
            bad['recoveryPsciColdReset'] = ''
            with self.assertRaises(ValueError):
                gate.validate_psci_abis(bad)
