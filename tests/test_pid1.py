"""Exercise the production PID1 evidence state machine, including false-PASS refusals."""
from pathlib import Path
import copy
import importlib.util
import struct
import shutil
import subprocess
import tempfile
import unittest


class Pid1Tests(unittest.TestCase):
    def test_evidence_gate_refuses_indirect_or_mismatched_results(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/test-dali-pid1-qemu.py'
        spec = importlib.util.spec_from_file_location('pid1_gate', path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        report = {'kernel_sha256': 'a', 'native_user_image_sha256': 'b',
                  'trust_cache_bundle_sha256': 'c', 'native_user_el0_observed': True,
                  'native_user_4k_abi_observed': True,
                  'native_user_syscalls': {'pid': 1, 'sysctl_result': 0, 'page_size': 4096,
                                           'old_length': 4, 'getpid_carry': 0, 'sysctl_carry': 0},
                  'dali_pid1': {'active': 1, 'offline': 1, 'pid': 1, 'sequence': 30,
                                'readonly_root': 1, 'failures': 0, 'complete': 1,
                                'reserved': 0, 'elapsed_ns': 30_000_000_000},
                  'native_user_memory': {'_pid1_failure': '00' * 8,
                                         '_readonly_result': (30).to_bytes(8, 'little').hex() +
                                          (1 << 29).to_bytes(8, 'little').hex()}}
        def evaluate(value):
            data = copy.deepcopy(value)
            state = data.pop('dali_pid1')
            fields = ('active', 'offline', 'pid', 'sequence', 'readonly_root', 'failures',
                      'complete', 'reserved', 'first_ns', 'elapsed_ns')
            data['requested_kernel_symbols'] = {'_dali_pid1_report': struct.pack(
                '<8I2Q', *(state.get(k, 0) for k in fields)).hex()}
            return gate.validate_report(data, 'a', 'b', 'c')
        self.assertEqual(evaluate(report)['pid'], 1)
        mutations = [('dali_pid1', 'elapsed_ns', 29_999_999_999),
                     ('dali_pid1', 'readonly_root', 0), ('dali_pid1', 'complete', 0),
                     ('dali_pid1', 'sequence', 29), ('dali_pid1', 'failures', 1),
                     ('native_user_syscalls', 'pid', 2), ('native_user_syscalls', 'page_size', 16384)]
        for group, key, value in mutations:
            bad = copy.deepcopy(report)
            bad[group][key] = value
            with self.subTest(group=group, key=key), self.assertRaises(ValueError):
                evaluate(bad)
        for key in ('kernel_sha256', 'native_user_image_sha256', 'trust_cache_bundle_sha256'):
            bad = copy.deepcopy(report)
            bad[key] = 'wrong'
            with self.subTest(identity=key), self.assertRaises(ValueError):
                evaluate(bad)

    def test_real_time_sequence_and_rejections(self):
        compiler = shutil.which('clang++') or shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler required')
        include = Path(__file__).resolve().parents[1] / 'drivers/DaliBootLog'
        code = r'''
#include "DaliPid1.h"
#include <assert.h>
int main() {
  DaliPid1Message m = {0x444c5031,1,0,1,4096,0,30,0};
  DaliPid1State s;
  assert(daliPid1Advance(s,m,2,true,0)==DaliPid1Result::Reject);
  assert(daliPid1Advance(s,m,1,false,0)==DaliPid1Result::Reject);
  m.pageSize=16384;
  assert(daliPid1Advance(s,m,1,true,0)==DaliPid1Result::Reject);
  m.pageSize=4096; m.readonlyError=0;
  assert(daliPid1Advance(s,m,1,true,0)==DaliPid1Result::Reject);
  m.readonlyError=30; m.syscallErrors=1;
  assert(daliPid1Advance(s,m,1,true,0)==DaliPid1Result::Reject);
  m.syscallErrors=0; m.sequence=1;
  assert(daliPid1Advance(s,m,1,true,0)==DaliPid1Result::Reject);
  m.sequence=0;
  assert(daliPid1Advance(s,m,1,true,1000000000)==DaliPid1Result::Heartbeat);
  assert(daliPid1Advance(s,m,1,true,2000000000)==DaliPid1Result::Reject);
  m.sequence=1;
  assert(daliPid1Advance(s,m,1,true,999999999)==DaliPid1Result::Reject);
  assert(daliPid1Advance(s,m,1,true,1899999999)==DaliPid1Result::Reject);
  for (uint32_t i=1;i<=30;i++) {
    m.sequence=i;
    auto r=daliPid1Advance(s,m,1,true,1000000000ULL+i*1000000000ULL);
    assert(r==(i==30?DaliPid1Result::Complete:DaliPid1Result::Heartbeat));
  }
  assert(s.complete && s.lastNs-s.firstNs>=30000000000ULL);
  assert(daliPid1Advance(s,m,1,true,32000000000ULL)==DaliPid1Result::Reject);
  DaliPid1State quick;
  for (uint32_t i=0;i<30;i++) {
    m.sequence=i;
    assert(daliPid1Advance(quick,m,1,true,i*900000000ULL)==DaliPid1Result::Heartbeat);
  }
  m.sequence=30;
  assert(daliPid1Advance(quick,m,1,true,29999999999ULL)==DaliPid1Result::Reject);
  assert(!quick.complete && quick.nextSequence==30);
  assert(daliPid1Advance(quick,m,1,true,30000000000ULL)==DaliPid1Result::Complete);
}
'''
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'pid1.cpp'
            source.write_text(code)
            exe = Path(d) / 'pid1'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-I', str(include), str(source), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)
