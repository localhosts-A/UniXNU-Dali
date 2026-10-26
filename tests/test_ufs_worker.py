"""Compile the production worker body; verify ordering, not mocked hardware IO."""
import ctypes
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UfsWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('clang++') or shutil.which('c++')
        if not compiler:
            raise unittest.SkipTest('native C++ compiler required')
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        source = (Path(__file__).resolve().parents[1] / 'drivers/DaliUfs/DaliUfs.cpp').read_text()
        begin = source.index('void DaliUfs::worker(')
        end = source.index('\nbool DaliUfs::start(', begin)
        (base / 'worker.cpp').write_text('''#include <stdint.h>
#include <stddef.h>
using wait_result_t=int;
static unsigned initialized_calls,diagnosed_calls,quiesces,at_wait,first_sync;
static unsigned failed_records,releases,terminated,diagnostic_result;
static bool initialize_result,diagnose_result;
static bool dali_ufs_platform_ready(const char*) {return true;}
static void IOSleep(unsigned) {}
static void IOLockLock(void*) {}
static void IOLockUnlock(void*) {}
static void *current_thread() {return nullptr;}
static void thread_terminate(void*) {++terminated;}
struct Completion {unsigned status=7,opcode=0,interrupt=1,transaction=0,lun=0,tag=0;
  unsigned senseKey=6,asc=0x29,ascq=2,target=2,ocs=0,polls=1,bytes=0,residual=0;};
struct Transport {Completion completion() {return {};}};
class DaliUfs;
struct Child {bool attach(DaliUfs*){return true;} bool start(DaliUfs*){return true;} void detach(DaliUfs*){}};
class DaliUfs {
public:
  bool closed_=false,scratchWriteMode_=false,stopAfterDiagnostics_=false;
  bool userdataLogMode_=false,userdataLogAttempted_=false;uint64_t syncs_=0;
  void *gate_=nullptr; char token_[33]{}; Child *children_[8]{}; Transport transport_;
  bool initialize() {++initialized_calls;return initialize_result;}
  bool diagnose() {++diagnosed_calls;return diagnose_result;}
  bool persistUserdataLog() {userdataLogAttempted_=true;return true;}
  void record(const char *stage,uint64_t a=0,uint64_t=0,uint64_t=0) {
    if(stage[0]=='f')++failed_records;
    if(stage[0]=='d')diagnostic_result=(unsigned)a;
  }
  bool quiesce(bool sync=false) {if(!quiesces)first_sync=sync;++quiesces;closed_=true;return true;}
  static bool stopBeforeReset(void *p) {return static_cast<DaliUfs*>(p)->quiesce(true);}
  void release() {++releases;}
  static void worker(void*,wait_result_t);
};
static void dali_ufs_wait_for_manual_recovery(const char*,bool(*)(void*),void*) {at_wait=quiesces;}
''' + source[begin:end] + '''
extern "C" void run(unsigned mode,uint64_t out[9]) {
  initialized_calls=diagnosed_calls=quiesces=at_wait=first_sync=0;
  failed_records=releases=terminated=diagnostic_result=0;
  initialize_result=(mode&4)==0;diagnose_result=(mode&8)==0;
  DaliUfs self;self.stopAfterDiagnostics_=(mode&1)!=0;self.scratchWriteMode_=(mode&2)!=0;
  DaliUfs::worker(&self,0);
  uint64_t values[]={at_wait,first_sync,initialized_calls,diagnosed_calls,failed_records,
                     releases,terminated,quiesces,diagnostic_result};
  for(unsigned i=0;i<9;++i)out[i]=values[i];
}
''')
        subprocess.run([compiler,'-std=c++17','-shared','-fPIC','-Wall','-Wextra','-Werror',
                        str(base/'worker.cpp'),'-o',str(base/'worker.so')],check=True)
        cls.lib=ctypes.CDLL(str(base/'worker.so'))
        cls.lib.run.argtypes=[ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self,mode):
        output=(ctypes.c_uint64*9)();self.lib.run(mode,output);return list(output)

    def test_explicit_stop_happens_with_required_sync_before_manual_recovery(self):
        self.assertEqual(self.run_case(1),[1,1,1,1,0,1,1,2,1])
        self.assertEqual(self.run_case(0),[0,0,1,1,0,1,1,1,1])
        # A diagnostic failure cannot skip the explicit close/sync/stop path.
        self.assertEqual(self.run_case(1|8),[1,1,1,1,0,1,1,2,0])

    def test_failed_initialization_preserves_failure_metadata_before_stop(self):
        self.assertEqual(self.run_case(1|4),[1,0,1,0,5,1,1,2,0])
        self.assertEqual(self.run_case(2|8),[1,0,1,1,0,1,1,2,0])
