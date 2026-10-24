from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManualRecoveryTests(unittest.TestCase):
    def test_deliberate_key_required_and_only_watchdog_registers_written(self):
        source = r'''
#include <assert.h>
#include "DaliManualRecovery.h"
struct State { unsigned count = 0, fences = 0; uint32_t mode = 0x17c, length = 0; bool reject = false; };
uint32_t read(void *p, uint32_t off) {
  auto &s = *static_cast<State *>(p);
  assert(off == 0 || off == 4);
  return off == 0 ? s.mode : (s.reject ? 0 : s.length);
}
void write(void *p, uint32_t off, uint32_t value) {
  auto &s = *static_cast<State *>(p);
  if (s.count == 0) { assert(off == 4 && value == 0x1008); s.length = value; }
  if (s.count == 1) { assert(off == 8 && value == 0x1971); }
  if (s.count == 2) { assert(off == 0 && value == 0x22000135); s.mode = value & 0xffffff; }
  assert(s.count < 3); ++s.count;
}
void fence(void *p) { ++static_cast<State *>(p)->fences; }
int main() {
  DaliManualKey key;
  for (unsigned i = 0; i < 1000; ++i) assert(!daliManualKeySample(key, true));
  for (unsigned i = 0; i < 20; ++i) assert(!daliManualKeySample(key, false));
  for (unsigned i = 0; i < 50; ++i) {
    for (unsigned j = 0; j < 10; ++j) assert(!daliManualKeySample(key, true));
    assert(!daliManualKeySample(key, false));
  }
  for (unsigned i = 0; i < 69; ++i) assert(!daliManualKeySample(key, true));
  assert(daliManualKeySample(key, true));
  State s; DaliManualWdtOps ops = {&s, read, write, fence};
  assert(daliArmManualWatchdog(ops));
  assert(s.count == 3 && s.fences == 2 && s.mode == 0x135);
  for (uint32_t mode : {0xffffffffU, 0x135U}) {
    State bad; bad.mode = mode;
    assert(!daliArmManualWatchdog({&bad, read, write, fence}) && bad.count == 0);
  }
  State bad; bad.reject = true;
  assert(!daliArmManualWatchdog({&bad, read, write, fence}) && bad.count == 2);
}
'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'manual.cpp'
            path.write_text('#include <initializer_list>\n' + source)
            binary = Path(temp) / 'manual'
            subprocess.run([shutil.which('clang++'), '-std=c++14', '-Wall', '-Wextra', '-Werror',
                            '-I', ROOT / 'drivers/DaliBootLog', path, '-o', binary], check=True)
            subprocess.run([binary], check=True)
