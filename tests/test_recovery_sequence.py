from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecoverySequenceTests(unittest.TestCase):
    def test_actual_property_decoder_respects_fortified_subobject_bounds(self):
        compiler = shutil.which('clang++')
        self.assertIsNotNone(compiler)
        driver = (ROOT / 'drivers/DaliBootLog/DaliBootLog.cpp').read_text()
        start = driver.index('static DaliRecoveryContract decodeRecoveryContract(')
        end = driver.index('static __attribute__((noinline)) void recoverToFastboot(', start)
        decoder = driver[start:end]
        source = r'''
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include "DaliRecovery.h"
#undef memcpy
#define memcpy(d,s,n) ((n) > __builtin_object_size((d), 1) ? (__builtin_trap(), (void *)0) : __builtin_memcpy((d),(s),(n)))
''' + decoder + r'''
int main() {
  const uint64_t resource[] = {0x1c010000, 0x1000};
  const uint32_t reboot[] = {0x24, 0xf, 3};
  auto c = decodeRecoveryContract(resource, reboot);
  assert(daliRecoveryContractValid(c));
}
'''
        with tempfile.TemporaryDirectory() as temp:
            test = Path(temp) / 'decode.cpp'
            binary = Path(temp) / 'decode'
            test.write_text(source)
            subprocess.run([compiler, '-std=c++14', '-O2', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-function', '-I', ROOT / 'drivers/DaliBootLog',
                            test, '-o', binary], check=True)
            subprocess.run([binary], check=True)

    def test_production_sequence_preserves_registers_and_refuses_invalid_state(self):
        compiler = shutil.which('clang++')
        self.assertIsNotNone(compiler)
        source = r'''
#include <assert.h>
#include <string.h>
#include "DaliRecovery.h"
struct State {
  uint32_t marker = 0x40002003, mode = 0x135;
  unsigned resets = 0, fences = 0;
  bool recordFailure = false;
  const char *last = "";
};
static uint32_t readReg(void *p, uint32_t offset) {
  State &s = *static_cast<State *>(p);
  assert(offset == 0 || offset == 0x24);
  return offset == 0x24 ? s.marker : s.mode;
}
static void reset(void *p) {
  State &s = *static_cast<State *>(p);
  assert(s.fences == 1 && strcmp(s.last, "issue") == 0);
  ++s.resets;
}
static void fence(void *p) { ++static_cast<State *>(p)->fences; }
static bool record(void *p, const char *stage, uint32_t marker, uint32_t) {
  State &s = *static_cast<State *>(p); s.last = stage;
  assert(marker == s.marker);
  return !s.recordFailure;
}
static DaliRecoveryOps ops(State &s) { return {&s, readReg, fence, record, reset}; }
int main() {
  assert(daliPsciFeatureSupported(0) && daliPsciFeatureSupported(1));
  assert(!daliPsciFeatureSupported(0xffffffffU) && !daliPsciFeatureSupported(0xfffffffdU));
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
  assert(daliIssueSystemReset(ops(s)));
  assert(s.resets == 1 && s.fences == 2);
  assert(s.mode == 0x135 && s.marker == 0x40002003 && strcmp(s.last, "issue") == 0);
  for (uint32_t marker : {0U, 2U, 0xffffffffU}) {
    State t; t.marker = marker;
    assert(!daliIssueSystemReset(ops(t)) && t.resets == 0);
  }
  for (uint32_t mode : {0U, 0x13cU, 0x13dU, 0xffffffffU}) {
    State t; t.mode = mode;
    assert(!daliIssueSystemReset(ops(t)) && t.resets == 0);
  }
  State lost; lost.recordFailure = true;
  assert(!daliIssueSystemReset(ops(lost)) && lost.resets == 0 && lost.fences == 0);
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
