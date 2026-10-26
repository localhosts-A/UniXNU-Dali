"""Execute the production scratch diagnostic across two simulated boots.

The IOKit and storage backend are mocks; crypto, physical UFS and persistence
are qualified elsewhere. This checks open modes, sync and at-most-once control.
"""
import ctypes as C
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ScratchIoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc=shutil.which('clang++') or shutil.which('c++')
        if not cc: raise unittest.SkipTest('C++ compiler required')
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        driver=Path(__file__).resolve().parents[1]/'drivers/DaliUfs'
        source=(driver/'DaliUfs.cpp').read_text()
        body=source[source.index('bool DaliUfs::testScratchRecord('):source.index('\nbool DaliUfs::inspectUserdataLogPartition(')]
        (root/'io.cpp').write_text('''#include "DaliUfsPlatform.h"
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
using IOReturn=int;using UInt64=uint64_t;using IOByteCount=size_t;
constexpr int kIOReturnSuccess=0,kIOReturnNotReady=1,kIODirectionIn=1,kIODirectionOut=2;
constexpr int kIOStorageAccessReader=1,kIOStorageAccessReaderWriter=3;
static uint8_t disk[2][4096];static unsigned writes,syncs,opens,closes,readonlyOpens,mode;
static void *IOMalloc(size_t n){return malloc(n);}static void IOFree(void*p,size_t){free(p);}
static void IOLockLock(void*){}static void IOLockUnlock(void*){}
struct IOBufferMemoryDescriptor{
  uint8_t bytes[4096]{};static IOBufferMemoryDescriptor*withCapacity(uint64_t n,int){return n==4096?new IOBufferMemoryDescriptor:nullptr;}
  size_t writeBytes(size_t at,const void*p,size_t n){if(at+n>4096)return 0;memcpy(bytes+at,p,n);return n;}
  const void*getBytesNoCopy(){return bytes;}void release(){delete this;}
};
struct IOMedia{
  bool writable;bool open(void*,unsigned,int access){++opens;if(access==kIOStorageAccessReader)++readonlyOpens;return writable||access==kIOStorageAccessReader;}
  int read(void*,uint64_t at,IOBufferMemoryDescriptor*b,void*,uint64_t*actual){if(at>4096)return 2;memcpy(b->bytes,disk[at/4096],4096);*actual=4096;return 0;}
  void close(void*){++closes;}
};
struct IOStorageCompletion{void*target;void(*action)(void*,void*,IOReturn,UInt64);void*parameter;};
struct DaliUfsScratchCompletion{IOReturn status=kIOReturnNotReady;UInt64 bytes=0;};
static void daliUfsScratchComplete(void*p,void*,IOReturn status,UInt64 bytes){auto c=static_cast<DaliUfsScratchCompletion*>(p);c->status=status;c->bytes=bytes;}
struct Child{
  bool writable;bool writeEnabled(){return writable;}
  int doAsyncReadWrite(IOBufferMemoryDescriptor*b,uint64_t at,uint64_t,void*,IOStorageCompletion*c){
    if(!writable||at>1)return 2;++writes;memcpy(disk[at],b->bytes,4096);c->action(c->target,nullptr,0,4096);return 0;
  }
  int doSynchronize(uint64_t,uint64_t){++syncs;return mode==1?2:0;}
};
class DaliUfs{
public:
  char token_[33]="0123456789abcdef0123456789abcdef";dali_ufs::ScratchGrant scratchGrant_{};
  Child*scratchChild_=nullptr;uint8_t*staging_=reinterpret_cast<uint8_t*>(1);void*gate_=nullptr;
  uint64_t scratchTestRelativeLba_=UINT64_MAX;
  void record(const char*,uint64_t=0,uint64_t=0,uint64_t=0){}
  bool testScratchRecord(uint8_t,IOMedia*);
};
static bool daliUfsMakeScratchExpected(const char*,const dali_ufs::ScratchGrant&,uint8_t*r,uint8_t*m,uint8_t*d){memset(r,0xa5,4096);memset(m,0x5a,4096);memset(d,0x33,32);return true;}
''' + body + '''
static DaliUfs make(Child*c){DaliUfs s;s.scratchChild_=c;auto&g=s.scratchGrant_;g.enabled=true;
strcpy(g.token,s.token_);memset(g.deviceBinding,1,32);memset(g.candidateSha256,2,32);memset(g.sourceSha256,3,32);memset(g.partitionGuid,4,16);
g.target=dali_ufs::ScratchTarget::Expdb;g.lun=2;g.blockBytes=4096;g.capacityBlocks=64577536;g.bootStartLba=2184;g.bootBlocks=32768;
g.scratchStartLba=2440;g.scratchBlocks=256;g.tailGuardBlocks=32256;g.sequence=1;return s;}
extern "C" void run(unsigned test,uint64_t*out){
memset(disk,0,sizeof disk);writes=syncs=opens=closes=readonlyOpens=0;mode=test;
Child child{true};IOMedia media{true};auto first=make(&child);bool a=first.testScratchRecord(2,&media);
unsigned beforeWrites=writes,beforeSyncs=syncs;
if(test==2)disk[1][0]^=1;
child.writable=false;media.writable=false;auto second=make(&child);bool b=second.testScratchRecord(2,&media);
uint64_t values[]={a,b,beforeWrites,beforeSyncs,writes,syncs,readonlyOpens,opens,closes};
for(unsigned i=0;i<9;++i)out[i]=values[i];
}
''')
        subprocess.run([cc,'-std=c++17','-shared','-fPIC','-Wall','-Wextra','-Werror','-I',str(driver),
                        str(root/'io.cpp'),str(driver/'DaliUfsPlatform.cpp'),'-o',str(root/'io.so')],check=True)
        cls.lib=C.CDLL(str(root/'io.so'));cls.lib.run.argtypes=[C.c_uint,C.POINTER(C.c_uint64)]

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def case(self,mode):
        out=(C.c_uint64*9)();self.lib.run(mode,out);return list(out)

    def test_first_boot_writes_and_syncs_second_boot_opens_reader_and_never_writes(self):
        self.assertEqual(self.case(0),[1,1,2,2,2,2,1,2,2])

    def test_sync_failure_and_existing_partial_record_are_never_replayed(self):
        self.assertEqual(self.case(1),[0,0,1,1,1,1,1,2,2])

    def test_corrupt_persisted_payload_is_rejected_without_writes(self):
        self.assertEqual(self.case(2),[1,0,2,2,2,2,1,2,2])
