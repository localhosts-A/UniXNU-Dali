from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecoverySequenceTests(unittest.TestCase):
    def test_production_sequence_preserves_mode_and_refuses_invalid_state(self):
        compiler = shutil.which('clang++')
        self.assertIsNotNone(compiler)
        source = r'''
#include <assert.h>
#include <string.h>
#include "DaliRecovery.h"
struct State {
  uint32_t marker = 0x40002003, mode = 0x13d;
  unsigned writes = 0, resets = 0, fences = 0;
  bool badReadback = false, changedMarker = false, recordFailure = false;
  const char *last = "";
};
static uint32_t readReg(void *p, uint32_t offset) {
  State &s = *static_cast<State *>(p);
  assert(offset == 0 || offset == 0x24);
  if (offset == 0x24) return s.marker ^ (s.changedMarker && s.writes ? 0x10 : 0);
  return s.mode | (s.badReadback && s.writes ? 8 : 0);
}
static void writeReg(void *p, uint32_t offset, uint32_t value) {
  State &s = *static_cast<State *>(p);
  ++s.writes;
  if (offset == 0) {
    assert(value == 0x22000135);
    s.mode = value & 0x00ffffff;
  } else {
    assert(offset == 0x14 && value == 0x1209);
    assert(s.fences >= 2 && strcmp(s.last, "issue") == 0);
    ++s.resets;
  }
}
static void fence(void *p) { ++static_cast<State *>(p)->fences; }
static bool record(void *p, const char *stage, uint32_t marker, uint32_t) {
  State &s = *static_cast<State *>(p); s.last = stage;
  assert(marker == s.marker);
  return !s.recordFailure;
}
static DaliRecoveryOps ops(State &s) { return {&s, readReg, writeReg, fence, record}; }
int main() {
  DaliRecoveryContract good = {0x1c010000, 0x1000, 0x24, 0xf, 3};
  assert(daliRecoveryContractValid(good));
  for (unsigned i = 0; i < 5; ++i) {
    auto bad = good;
    if (i == 0) ++bad.base;
    if (i == 1) ++bad.size;
    if (i == 2) ++bad.offset;
    if (i == 3) ++bad.mask;
    if (i == 4) ++bad.bootloader;
    assert(!daliRecoveryContractValid(bad));
  }
  State s;
  assert(daliIssueSoftwareReset(ops(s)));
  assert(s.writes == 2 && s.resets == 1 && s.fences == 3);
  assert(s.mode == 0x135 && s.marker == 0x40002003 && strcmp(s.last, "returned") == 0);
  for (uint32_t marker : {0U, 2U, 0xffffffffU}) {
    State t; t.marker = marker;
    assert(!daliIssueSoftwareReset(ops(t)) && t.writes == 0);
  }
  for (uint32_t mode : {0U, 0x13cU, 0xffffffffU}) {
    State t; t.mode = mode;
    assert(!daliIssueSoftwareReset(ops(t)) && t.writes == 0);
  }
  for (unsigned i = 0; i < 2; ++i) {
    State t; t.badReadback = i == 0; t.changedMarker = i == 1;
    assert(!daliIssueSoftwareReset(ops(t)) && t.writes == 1 && t.resets == 0);
    assert(strcmp(t.last, "mode-refused") == 0);
  }
  State lost; lost.recordFailure = true;
  assert(!daliIssueSoftwareReset(ops(lost)) && lost.writes == 1 && lost.resets == 0);
}
'''
        # initializer_list is used by the test only, never by kernel code.
        source = '#include <initializer_list>\n' + source
        with tempfile.TemporaryDirectory() as temp:
            test = Path(temp) / 'recovery.cpp'
            binary = Path(temp) / 'recovery'
            test.write_text(source)
            subprocess.run([compiler, '-std=c++14', '-Wall', '-Wextra', '-Werror',
                            '-I', ROOT / 'drivers/DaliBootLog', test, '-o', binary], check=True)
            subprocess.run([binary], check=True)


if __name__ == '__main__':
    unittest.main()
