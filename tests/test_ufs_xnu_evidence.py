"""Reject incomplete, mixed, stale or inconsistent source-XNU UFS evidence."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ufs_xnu_evidence as evidence
from test_ufs_readonly_evidence import save_fixture, TOKEN, CANDIDATE
from ufs_readonly_evidence import load_baseline


def trace(baseline, token=TOKEN):
    rows = []
    def record(stage, a=0, b=0, c=0):
        rows.append(f'DALI-XNU-UFS: run={token} stage={stage} a={a:x} b={b:x} c={c:x}')
    def text(kind, value):
        data = value.encode()
        for offset in range(0, len(data), 32):
            record('text', kind, offset, len(data)); record(data[offset:offset+32].hex())
    def sample(lun, lba, base, size, block, kind, digest):
        record('sample', lun, lba, block); record(kind, base, size, block); record(digest)
    block, count, lun = baseline['block_size'], baseline['last_lba'] + 1, baseline['lun']
    record('start')
    rows.append(f'DALI-XNU: darwin=24.5.0 build=10063.141.1 run={token} bootarg=1 registered=1 platform=1 gic=1 timer=12 mpidr=0x0 checkpoint=PASS')
    record('sha-pass'); record('controller',7,7); record('dma',0x90000000,0x22000,64)
    record('geometry',lun,count,block); record('initialized',1)
    record('media',lun,count*block,block)
    for lba in (0,1,count-1):
        sample(lun,lba,0,count*block,block,'whole',baseline['header_hashes'].get(lba,'a'*64))
    for part in baseline['partitions'].values():
        start, size = part['first_lba']*block, part['blocks']*block
        record('partition',lun,start,size); text(1,part['name']); text(2,part['guid'].upper())
        if part['name']=='boot_b':
            sample(lun,0,start,size,block,'boot_b',evidence.sha256(CANDIDATE[:block]))
    record('sync',lun,0,1); record('diagnostics-end',1,4,1)
    record('quiesce-sync',0,2); record('quiesce',8,1,0)
    return '\n'.join(rows)


class XnuEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.folder=Path(self.tmp.name)/'baseline'
        save_fixture(self.folder)
        self.baseline=load_baseline(self.folder,serial='test-device',original_boot_b=self.folder/'original.img')
    def tearDown(self):
        self.tmp.cleanup()
    def score(self,text=None,candidate=CANDIDATE,baseline=None):
        return evidence.score_xnu_readonly(trace(self.baseline) if text is None else text,TOKEN,
                                          self.baseline if baseline is None else baseline,candidate)
    def test_complete_transcript_reports_only_named_xnu_read_path(self):
        out=self.score(); self.assertTrue(out['xnu_read_only_pass'],out)
        self.assertTrue(out['xnu_sync_report_pass']); self.assertTrue(out['xnu_stop_report_pass'])
        self.assertEqual(out['verified_content_luns'],[2])
        for key in ('physical_ufs_qualified','device_identity_verified_by_scorer',
                    'firmware_xnu_handoff_qualified','media_writes_qualified','persistence_qualified'):
            self.assertFalse(out[key])
    def test_actual_512_byte_geometry_and_candidate_binding(self):
        folder=Path(self.tmp.name)/'small'; save_fixture(folder,512)
        baseline=load_baseline(folder,serial='test-device',original_boot_b=folder/'original.img')
        self.assertTrue(self.score(trace(baseline),baseline=baseline)['xnu_read_only_pass'])
        self.assertFalse(self.score(candidate=b'X'*8192)['xnu_read_only_pass'])
        self.assertFalse(self.score(candidate=b'X')['xnu_read_only_pass'])
    def test_platform_sha_controller_and_full_dma_bounds_are_required(self):
        log=trace(self.baseline)
        for old,new in (('checkpoint=PASS','checkpoint=FAIL'),('registered=1','registered=2'),
                        ('stage=sha-pass','stage=sha-failed'),('stage=controller a=7','stage=controller a=4'),
                        ('a=90000000 b=22000','a=fffff000 b=22000'),('c=40\n','c=3f\n')):
            with self.subTest(old=old):
                self.assertNotEqual(log,log.replace(old,new))
                self.assertFalse(self.score(log.replace(old,new))['xnu_read_only_pass'])
    def test_gpt_geometry_guid_name_and_both_header_hashes_are_required(self):
        log=trace(self.baseline)
        guid=self.baseline['partitions'][71]['guid'].upper().encode().hex()[:64]
        for bad in (log.replace(self.baseline['header_hashes'][1],'f'*64),
                    log.replace(self.baseline['header_hashes'][self.baseline['last_lba']],'e'*64),
                    log.replace('stage=partition a=2 b=200000','stage=partition a=2 b=201000'),
                    log.replace('626f6f745f62','626f6f745f61'),log.replace(guid,('36' if guid[:2]!='36' else '35')+guid[2:])):
            self.assertNotEqual(bad,log)
            self.assertFalse(self.score(bad)['xnu_read_only_pass'])
        changed=copy.deepcopy(self.baseline); changed['partitions'][72]=copy.deepcopy(changed['partitions'][71]); changed['partitions'][72]['guid']='12345678-1234-1234-1234-123456789abc'
        self.assertFalse(self.score(baseline=changed)['xnu_read_only_pass'])
    def test_sync_failure_missing_shutdown_or_unstopped_dma_never_pass(self):
        log=trace(self.baseline)
        for old,new in (('stage=sync a=2 b=0 c=1','stage=sync a=2 b=1 c=1'),
                        ('stage=quiesce-sync a=0 b=2','stage=quiesce-sync a=0 b=1'),
                        ('stage=quiesce-sync a=0','stage=quiesce-sync a=3'),
                        ('stage=quiesce a=8 b=1 c=0','stage=quiesce a=8 b=1 c=1'),
                        ('stage=diagnostics-end a=1 b=4','stage=diagnostics-end a=1 b=3')):
            self.assertFalse(self.score(log.replace(old,new))['xnu_read_only_pass'])
        self.assertFalse(self.score('\n'.join(log.splitlines()[:-2]))['xnu_read_only_pass'])
    def test_duplicate_trailing_truncated_foreign_and_reordered_records_fail(self):
        log=trace(self.baseline); rows=log.splitlines()
        for bad in (log+'\n'+log,log.replace(TOKEN,'b'*32),'\n'.join(rows[:-1]),
                    '\n'.join([rows[1],rows[2],rows[0]]+rows[3:]),
                    log+'\n'+f'DALI-XNU-UFS: run={TOKEN} stage=unknown a=0 b=0 c=0'):
            self.assertFalse(self.score(bad)['xnu_read_only_pass'])
        self.assertTrue(self.score('noise\n'+trace(self.baseline,'b'*32)+'\n'+log)['xnu_read_only_pass'])
    def test_metadata_chunks_are_bounded_and_cannot_supply_forged_record(self):
        log=trace(self.baseline)
        for bad in (log.replace('stage=text a=2 b=20 c=24','stage=text a=2 b=1f c=24'),
                    log.replace('stage=text a=1 b=0 c=6','stage=text a=1 b=0 c=81'),
                    log.replace('626f6f745f62','626f00665f62')):
            self.assertFalse(self.score(bad)['xnu_read_only_pass'])
    def test_idempotent_stop_must_not_replay_sync(self):
        log=trace(self.baseline); repeat='\n'.join(log.splitlines()[-2:])
        self.assertTrue(self.score(log+'\n'+repeat)['xnu_read_only_pass'])
        self.assertFalse(self.score(log+'\n'+repeat.replace('b=2','b=3'))['xnu_read_only_pass'])
