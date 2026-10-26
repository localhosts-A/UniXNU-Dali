import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_ufs_expdb as fixtures
from ufs_scratch_authorization import build_xnu_scratch_grant

spec=importlib.util.spec_from_file_location('expdb_recovery_runner',Path(__file__).resolve().parents[1]/'scripts/restore-expdb-log.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class RecoveryRunnerTests(unittest.TestCase):
    def fixture(self, root):
        root=root.resolve()
        backup=root/'backup';backup.mkdir();inputs=fixtures.ExpdbTests().fixture(backup)
        run=root/'run';run.mkdir();base=run/'ufs-baseline';base.mkdir();expdb=run/'expdb-baseline';expdb.mkdir()
        for path in backup.iterdir():
            if path.name.startswith('ufs-'):shutil.copy2(path,base/path.name)
            if path.name.startswith(('primary-','backup-')) or path.name=='expdb.img':shutil.copy2(path,expdb/path.name)
        raw=json.dumps(inputs['expdb_baseline']).encode();(expdb/'baseline.json').write_bytes(raw)
        grant,wire,_=build_xnu_scratch_grant(**inputs)
        candidate=run/'candidate';candidate.mkdir()
        (candidate/'grant.json').write_text(json.dumps(grant));(candidate/'grant.bin').write_bytes(wire)
        (candidate/'source-kernel.macho').write_bytes(inputs['kernel_bytes'])
        (candidate/'source-kernel-manifest.json').write_text(json.dumps(inputs['source_manifest']))
        manifest=b'{"target":"dali"}';(candidate/'manifest.json').write_bytes(manifest)
        (candidate/'UniXNU-dali-uefi-boot.img').write_bytes(inputs['candidate_image'])
        state=dict(schema=1,kind=runner.KIND,dry_run=False,serial=inputs['serial'],fastboot_serial=inputs['serial'],
                   backup=str(backup),token=inputs['token'],candidate_verified_before_flash=True,
                   device_verified_before_flash=True,original_boot_b_sha256=inputs['baseline']['original_boot_b_sha256'],
                   baseline_sha256='a'*64,expdb_baseline_sha256=hashlib.sha256(raw).hexdigest(),
                   expdb_original_sha256=inputs['expdb_baseline']['sha256'],
                   candidate_sha256=hashlib.sha256(inputs['candidate_image']).hexdigest(),
                   candidate_record_sha256=hashlib.sha256(manifest).hexdigest(),
                   log_binding=dict(token=grant['token'],device_binding=grant['device_binding_sha256'],
                                    candidate_sha256=grant['candidate_sha256'],source_sha256=grant['source_sha256'],
                                    block_bytes=grant['block_bytes'],owner=1,sequence=grant['sequence']))
        (run/'restore-state.json').write_text(json.dumps(state))
        args=SimpleNamespace(serial=inputs['serial'],fastboot_serial=inputs['serial'],backup=backup,restore_run=run,wait_seconds=600)
        lease=SimpleNamespace(data=dict(pending_run=str(run),pending_expdb=True),restored=Mock())
        records={'boot_b':dict(sha256=inputs['baseline']['original_boot_b_sha256'])}
        baseline=dict(inputs['baseline'],baseline_sha256='a'*64)
        return args,lease,records,baseline,state

    def test_sealed_baselines_kernel_source_wire_and_canonical_pending_must_match(self):
        with tempfile.TemporaryDirectory() as temp:
            args,lease,records,baseline,state=self.fixture(Path(temp))
            with patch.object(runner.cycle,'verify_backup',return_value=({},records)),patch.object(runner,'load_restore_baseline',return_value=baseline):
                _,_,_,image,expected=runner.validate_run(args,lease)
                self.assertEqual(image['device_binding'],expected['device_binding'])
                for key,value in [('schema',True),('kind','dali-source-xnu-ufs-read-only'),('token','b'*32),
                                  ('candidate_sha256','f'*64),('expdb_original_sha256','f'*64)]:
                    changed=copy.deepcopy(state);changed[key]=value
                    (args.restore_run/'restore-state.json').write_text(json.dumps(changed))
                    with self.assertRaises((ValueError,runner.cycle.CycleError)):runner.validate_run(args,lease)
                (args.restore_run/'restore-state.json').write_text(json.dumps(state))
                lease.data['pending_expdb']=False
                with self.assertRaises(runner.cycle.CycleError):runner.validate_run(args,lease)

    def test_already_restored_android_still_captures_and_restores_expdb_before_clearing(self):
        with tempfile.TemporaryDirectory() as temp:
            args,lease,records,_,state=self.fixture(Path(temp));events=[]
            with patch.object(runner,'validate_run',return_value=(args.restore_run,state,records,{},state['log_binding'])), \
                 patch.object(runner.cycle,'adb_online',return_value=True), \
                 patch.object(runner.cycle,'current_android',return_value={'product':'dali','slot_suffix':'_b','root':'uid=0(root)'}), \
                 patch.object(runner.cycle,'compare_partition_hashes',return_value={'boot_b':records['boot_b']['sha256']}), \
                 patch.object(runner.probe,'collect_now',side_effect=lambda *a:events.append('capture') or {}), \
                 patch.object(runner,'recover_window',side_effect=lambda *a,**k:events.append('expdb') or {'expdb_restore_verified':True}), \
                 patch.object(runner.cycle,'restore_android_safely') as boot:
                runner.restore(args,lease)
                boot.assert_not_called();self.assertEqual(events,['capture','expdb'])
                lease.restored.assert_called_once();self.assertTrue(state['expdb_restore_verified'])
                self.assertFalse(state['expdb_may_be_modified'])

    def test_uncertain_window_recovery_cannot_clear_pending_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            args,lease,records,_,state=self.fixture(Path(temp))
            with patch.object(runner,'validate_run',return_value=(args.restore_run,state,records,{},state['log_binding'])), \
                 patch.object(runner.cycle,'adb_online',return_value=False), \
                 patch.object(runner.cycle,'wait_fastboot',return_value=True), \
                 patch.object(runner.cycle,'restore_android_safely') as boot, \
                 patch.object(runner.probe,'collect_now',return_value={}), \
                 patch.object(runner,'recover_window',side_effect=TimeoutError('uncertain write')) as recovery:
                with self.assertRaises(TimeoutError):runner.restore(args,lease)
                boot.assert_called_once();recovery.assert_called_once();lease.restored.assert_not_called()
                self.assertTrue(lease.data['pending_expdb'])

    def test_ramoops_capture_error_keeps_pending_and_never_overwrites_expdb(self):
        for field in ('collection_error','collection_store_error'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as temp:
                args,lease,records,_,state=self.fixture(Path(temp))
                with patch.object(runner,'validate_run',return_value=(args.restore_run,state,records,{},state['log_binding'])), \
                     patch.object(runner.cycle,'adb_online',return_value=True), \
                     patch.object(runner.cycle,'current_android',return_value={}), \
                     patch.object(runner.cycle,'compare_partition_hashes',return_value={}), \
                     patch.object(runner.probe,'collect_now',return_value={field:''}), \
                     patch.object(runner,'recover_window') as recovery:
                    with self.assertRaises(runner.cycle.CycleError):runner.restore(args,lease)
                    recovery.assert_not_called();lease.restored.assert_not_called()
                    self.assertTrue(lease.data['pending_expdb'])


if __name__=='__main__':unittest.main()
