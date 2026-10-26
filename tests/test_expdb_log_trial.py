import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('expdb_log_trial',Path(__file__).resolve().parents[1]/'scripts/run-expdb-log.py')
trial=importlib.util.module_from_spec(spec);spec.loader.exec_module(trial)


class TrialTests(unittest.TestCase):
    def fixture(self,root):
        run=root/'run';(run/'candidate').mkdir(parents=True)
        data=b'sealed candidate';(run/'candidate/UniXNU-dali-uefi-boot.img').write_bytes(data)
        args=SimpleNamespace(serial='SERIAL',fastboot_serial='SERIAL',backup=root/'backup',wait_seconds=600)
        state=dict(schema=1,kind=trial.recovery.KIND,token='a'*32,candidate_sha256=hashlib.sha256(data).hexdigest(),
                   boot_b_may_be_modified=False,expdb_may_be_modified=False,expdb_restore_verified=True,
                   device_verified_before_flash=False,restore_command=['restore-expdb-log.py','--restore-run',str(run)])
        return run,args,state

    def test_timeout_keeps_independent_pending_expdb_armed_before_the_only_flash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run,args,state=self.fixture(root);events=[]
            with trial.probe.DeviceLease('SERIAL',root/'ledger') as lease:
                def flash(serial,path):
                    saved=json.loads((run/'restore-state.json').read_text())
                    self.assertTrue(saved['boot_b_may_be_modified']);self.assertTrue(saved['expdb_may_be_modified'])
                    self.assertFalse(saved['expdb_restore_verified']);self.assertTrue(lease.data['pending_expdb'])
                    self.assertEqual(lease.data['pending_run'],str(run));events.append('flash')
                with patch.object(trial,'verify_live_originals',return_value={}), \
                     patch.object(trial.cycle,'request_fastboot_from_android'),patch.object(trial.cycle,'verify_fastboot'), \
                     patch.object(trial.cycle,'verify_original_boot_b'),patch.object(trial.cycle,'flash_boot_b',side_effect=flash), \
                     patch.object(trial.cycle,'fastboot_command',side_effect=lambda *a,**k:events.append('reboot')), \
                     patch.object(trial.cycle,'wait_fastboot_disconnect',return_value=True), \
                     patch.object(trial.cycle,'wait_fastboot',return_value=False), \
                     patch.object(trial.cycle,'fastboot_online',return_value=False),patch.object(trial.recovery,'restore') as restore:
                    with self.assertRaises(trial.cycle.CycleError):trial.execute_trial(args,lease,run,state,{'boot_b':{}},{})
                    self.assertEqual(events,['flash','reboot']);restore.assert_not_called()
                    self.assertTrue(lease.data['pending_expdb'])
                    with self.assertRaises(trial.cycle.CycleError):lease.require_new('b'*32)

    def test_returned_fastboot_invokes_original_recovery_once_and_never_reflashes_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run,args,state=self.fixture(root)
            with trial.probe.DeviceLease('SERIAL',root/'ledger') as lease:
                def recover(a,actual_lease):
                    self.assertEqual(a.restore_run,run);self.assertIs(actual_lease,lease)
                    saved=json.loads((run/'restore-state.json').read_text())
                    saved.update(expdb_may_be_modified=False,expdb_restore_verified=True)
                    actual_lease.restored(run,saved)
                    return {'persistence_qualified':False,'expdb_restore_verified':True}
                with patch.object(trial,'verify_live_originals',return_value={}), \
                     patch.object(trial.cycle,'request_fastboot_from_android'),patch.object(trial.cycle,'verify_fastboot'), \
                     patch.object(trial.cycle,'verify_original_boot_b'),patch.object(trial.cycle,'flash_boot_b') as flash, \
                     patch.object(trial.cycle,'fastboot_command') as reboot, \
                     patch.object(trial.cycle,'wait_fastboot_disconnect',return_value=True), \
                     patch.object(trial.cycle,'wait_fastboot',return_value=True), \
                     patch.object(trial.recovery,'restore',side_effect=recover) as restore:
                    self.assertEqual(trial.execute_trial(args,lease,run,state,{'boot_b':{}},{}),2)
                    flash.assert_called_once();reboot.assert_called_once();restore.assert_called_once()
                    self.assertIsNone(lease.data['pending_run']);self.assertFalse(lease.data['pending_expdb'])

    def test_live_expdb_or_sealed_candidate_mismatch_refuses_to_arm_or_flash(self):
        for fault in ('live','candidate'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);run,args,state=self.fixture(root)
                if fault=='candidate':(run/'candidate/UniXNU-dali-uefi-boot.img').write_bytes(b'changed')
                with trial.probe.DeviceLease('SERIAL',root/'ledger') as lease, \
                     patch.object(trial,'verify_live_originals',side_effect=ValueError('different') if fault=='live' else None,return_value={}), \
                     patch.object(trial.cycle,'request_fastboot_from_android'),patch.object(trial.cycle,'verify_fastboot'), \
                     patch.object(trial.cycle,'verify_original_boot_b'),patch.object(trial.cycle,'flash_boot_b') as flash:
                    with self.assertRaises((ValueError,trial.cycle.CycleError)):trial.execute_trial(args,lease,run,state,{'boot_b':{}},{})
                    flash.assert_not_called();self.assertIsNone(lease.data['pending_run'])

    def test_generic_builder_clears_inherited_log_permission_and_only_explicit_path_enables_it(self):
        class StopBuild(Exception):pass
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);fdt=root/'runtime.dtb';fdt.write_bytes(b'fdt');wire=root/'grant.bin';wire.write_bytes(b'wire')
            args=SimpleNamespace(uefi=root/'uefi',unixnu=root/'unixnu');recovery=dict(auto_fastboot=False,device_watchdog_timeout_seconds=31)
            env=dict(DALI_UFS_LOG_ENABLED='true',DALI_UFS_LOG_WIRE='/ambient',DALI_UFS_MEASURE_PAYLOAD='true')
            with patch.dict('os.environ',env),patch.object(trial.cycle,'run_logged',side_effect=StopBuild) as build:
                with self.assertRaises(StopBuild):
                    trial.cycle.build_candidate(args,root,root/'kernel',root/'adt',recovery,'a'*32,root,xnu_ufs_readonly=True,live_fdt=fdt)
                self.assertNotIn('DALI_UFS_LOG_ENABLED',build.call_args.kwargs['env'])
                with self.assertRaises(StopBuild):
                    trial.cycle.build_candidate(args,root,root/'kernel',root/'adt',recovery,'a'*32,root,xnu_ufs_readonly=True,live_fdt=fdt,expdb_log_wire=wire)
                actual=build.call_args.kwargs['env'];self.assertEqual(actual['DALI_UFS_PROFILE'],'expdb-log-diagnostic')
                self.assertEqual(actual['DALI_UFS_LOG_WIRE'],str(wire));self.assertEqual(actual['DALI_AUTO_FASTBOOT'],'false')
                with self.assertRaises(trial.cycle.CycleError):
                    trial.cycle.build_candidate(args,root,root/'kernel',root/'adt',recovery,'a'*32,root,expdb_log_wire=wire)

    def test_complete_shared_builder_accepts_only_matching_log_mode_and_wire(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);out=root/'uefi/out';out.mkdir(parents=True)
            kernel=root/'kernel';kernel.write_bytes(b'kernel');adt=root/'adt';adt.write_bytes(b'adt')
            fdt=root/'runtime.dtb';fdt.write_bytes(b'fdt');wire=root/'grant.bin';wire.write_bytes(b'wire')
            boot=out/'UniXNU-dali-uefi-boot.img';boot.write_bytes(b'candidate')
            digest=lambda data:hashlib.sha256(data).hexdigest()
            token='a'*32;recovery=dict(auto_fastboot=False,device_watchdog_timeout_seconds=31)
            profile=dict(enabled=True,run_token=token,write_authorized=False,physical_ufs_qualified=False,live_fdt_sha256=digest(b'fdt'))
            manifest=dict(bytes=9,sha256=digest(b'candidate'),device_commit='a'*40,unixnu_commit='a'*40,uefi_commit='a'*40,
                payload=dict(xnu_kernel_sha256=digest(b'kernel'),xnu_adt_sha256=digest(b'adt'),xnu_run_token=token,
                    recovery=dict(auto_fastboot=False,watchdog_timeout_seconds=31,run_token=token),xnu_ufs_read_only=profile),
                ufs_xnu=profile,ufs_host=dict(enabled=True,mode='expdb-log-diagnostic',run_token=token,media_writes=True,
                    write_authorized=False,runtime_authorization_required=True),
                ufs_log=dict(enabled=True,profile='expdb-log-diagnostic',run_token=token,target_partition='expdb',
                    wire_sha256=digest(b'wire'),ramoops_retained=True,automatic_fastboot_enabled=False))
            args=SimpleNamespace(uefi=root/'uefi',unixnu=root/'unixnu')
            def build(value,explicit=True):
                (out/'manifest.json').write_text(json.dumps(value))
                return trial.cycle.build_candidate(args,root,kernel,adt,recovery,token,root,
                    xnu_ufs_readonly=True,live_fdt=fdt,expdb_log_wire=wire if explicit else None)
            with patch.object(trial.cycle,'run_logged'),patch.object(trial.cycle,'run_capture',return_value='a'*40):
                self.assertEqual(build(manifest)[1],manifest)
                with self.assertRaises(trial.cycle.CycleError):build(manifest,explicit=False)
                for change in ({'wire_sha256':'b'*64},{'enabled':False},{'ramoops_retained':False},{'automatic_fastboot_enabled':True}):
                    bad=dict(manifest,ufs_log=dict(manifest['ufs_log'],**change))
                    with self.assertRaises(trial.cycle.CycleError):build(bad)


if __name__=='__main__':unittest.main()
