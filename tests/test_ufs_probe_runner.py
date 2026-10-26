import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('ufs_probe_runner', Path(__file__).resolve().parents[1] / 'scripts/run-ufs-probe.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
TOKEN = '1234567890abcdef' * 2


def snapshot(*, mcq=False, token=TOKEN, schema=1):
    prefix = 'DALI-UFS: run=' + token + ' '
    offsets = [0, 8, 0x34, 0x30, 0x20, 0x24, 0x18, 0x50, 0x54, 0x58, 0x60, 0x70, 0x74, 0x78, 0x80]
    if mcq:
        offsets += [4, 0x300, 0x380]
    if schema == 2:
        offsets += [0x2240, 0x144, 0x140, 0x160, 0x14c]
    reads = [0x168a0104, 0x168a0004] + [0x16810000 + at for at in offsets]
    lines = [f'begin version={schema} mode=read-only writes=0']
    lines += [f'read32={at:X}' for at in reads]
    lines += ['gates=00000000,00000000 iommu-property-bytes=0 result=4',
              f'cap={0x40000000 if mcq else 0:08X} ver=00000400 hce=00000001 hcs=00000001 is=00000000 ie=00000000 ahit=00000000',
              'utrl=0000000012345000 db=00000000 rs=00000001 utmrl=0000000012346000 db=00000000 rs=00000001',
              f'mcq-read={int(mcq)} mcqcap=00000000 memcfg=00000000 mcqcfg=00000000']
    if schema == 2:
        lines += ['mtk-ip=01460000 refclk=00000003 xoufs=00000080 opt=00000000 otsd=00000000 mphy-ver=1']
    lines += ['end result=4 dma-qualified=0 media-qualified=0']
    return '\n'.join(prefix + line for line in lines)


class SnapshotTests(unittest.TestCase):
    def test_v2_vendor_read_order_and_schema_are_required_without_upgrading_v1(self):
        for mcq in (False, True):
            log = snapshot(mcq=mcq, schema=2)
            result = runner.score_snapshot(log, TOKEN)
            self.assertTrue(result['snapshot_pass'])
            self.assertEqual(result['probe_log_version'], 2)
            self.assertEqual(result['mtk_ip_version'], 0x01460000)
            self.assertEqual(result['reference_clock'], 3)
            self.assertFalse(result['dma_qualified'])
            for bad in (log.replace('version=2', 'version=1'), log.replace('version=2', 'version=3'),
                        log.replace('read32=16812240', 'read32=16812244'), log.replace('mphy-ver=1', 'mphy-ver=0'),
                        '\n'.join(line for line in log.splitlines() if 'mtk-ip=' not in line)):
                with self.subTest(mcq=mcq, bad=bad[-60:]):
                    self.assertFalse(runner.score_snapshot(bad, TOKEN)['snapshot_pass'])
        old = runner.score_snapshot(snapshot(), TOKEN)
        self.assertTrue(old['snapshot_pass'])
        self.assertNotIn('mtk_ip_version', old)

    def test_complete_legacy_and_mcq_remain_unqualified_for_media(self):
        for mcq in (False, True):
            with self.subTest(mcq=mcq):
                result = runner.score_snapshot(snapshot(mcq=mcq), TOKEN)
                self.assertTrue(result['snapshot_pass'])
                self.assertEqual(result['utrl_address'], 0x12345000)
                self.assertFalse(result['dma_qualified'])
                self.assertFalse(result['physical_ufs_qualified'])
                self.assertFalse(result['media_writes_qualified'])

    def test_old_token_duplicate_truncated_gated_and_wrong_read_cannot_pass(self):
        complete = snapshot()
        prefix = 'DALI-UFS: run=' + TOKEN + ' '
        cases = [snapshot(token='a' * 32), complete + '\n' + complete,
                 '\n'.join(complete.splitlines()[:-1]),
                 complete.replace('gates=00000000', 'gates=00000001'),
                 complete.replace('read32=16810050', 'read32=1681005C'),
                 complete.replace('mcq-read=0', 'mcq-read=1')]
        lines = complete.splitlines()
        cases += ['\n'.join([lines[1], lines[0]] + lines[2:]),
                  complete + '\n' + prefix + 'unexpected record']
        for text in cases:
            with self.subTest(text=text[-90:]):
                self.assertFalse(runner.score_snapshot(text, TOKEN)['snapshot_pass'])

    def test_refused_probe_is_evidence_without_a_success(self):
        text = snapshot().splitlines()[0] + '\nDALI-UFS: run=' + TOKEN + ' end result=2 dma-qualified=0 media-qualified=0'
        result = runner.score_snapshot(text, TOKEN)
        self.assertTrue(result['observation_complete'])
        self.assertFalse(result['snapshot_pass'])


class LeaseTests(unittest.TestCase):
    def test_expdb_pending_survives_restart_and_old_boot_only_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=root/'run';state={'token':TOKEN,'expdb_may_be_modified':True,'expdb_restore_verified':True}
            with runner.DeviceLease('device',root) as lease:
                lease.arm(run,state)
                self.assertFalse(state['expdb_restore_verified'],'a new trial cannot inherit old restoration proof')
            with runner.DeviceLease('device',root) as lease:
                for old_state in ({'token':TOKEN},dict(state),dict(state,expdb_may_be_modified=False),
                                  dict(state,expdb_may_be_modified=False,expdb_restore_verified='true')):
                    with self.assertRaisesRegex(runner.cycle.CycleError,'expdb restoration remains pending'):
                        lease.restored(run,old_state)
                    self.assertEqual(lease.data['pending_run'],str(run))
                    self.assertTrue(json.loads(lease.path.read_text())['pending_expdb'])
                state.update(expdb_may_be_modified=False,expdb_restore_verified=True)
                lease.restored(run,state)
            with runner.DeviceLease('device',root) as lease:
                self.assertIsNone(lease.data['pending_run']);self.assertFalse(lease.data['pending_expdb'])

    def test_invalid_expdb_flag_cannot_arm_a_trial(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);run=root/'run'
            with runner.DeviceLease('device',root) as lease:
                with self.assertRaisesRegex(runner.cycle.CycleError,'explicit boolean'):
                    lease.arm(run,{'token':TOKEN,'expdb_may_be_modified':'true'})
                self.assertIsNone(lease.data['pending_run'])
                self.assertFalse((run/'restore-state.json').exists())

    def test_device_ledger_root_is_anchored_to_shared_backup_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / 'device-backups' / 'dali-backup'
            backup.mkdir(parents=True)
            expected = backup.resolve().parent / '.ufs-device-state'
            self.assertEqual(runner.device_state_root(backup), expected)
            self.assertEqual(runner.device_state_root(backup.resolve()), expected)

    def test_restore_only_run_cannot_overlap_shared_device_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / 'device-backups' / 'dali-backup'
            backup.mkdir(parents=True)
            restore_run = runner.device_state_root(backup) / 'nested-run'
            argv = ['run-ufs-probe.py', '--serial', 'device', '--backup', str(backup),
                    '--restore-run', str(restore_run), '--wait-seconds', '120']
            with mock.patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as error:
                runner.main()
            self.assertEqual(error.exception.code, 2)

    def test_live_lock_pending_restore_and_used_token_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run'
            state = {'token': TOKEN}
            with runner.DeviceLease('device', root) as lease:
                with self.assertRaisesRegex(runner.cycle.CycleError, 'another live'):
                    with runner.DeviceLease('device', root):
                        pass
                lease.arm(run, state)
                self.assertTrue(json.loads((run / 'restore-state.json').read_text())['boot_b_may_be_modified'])
            with runner.DeviceLease('device', root) as lease:
                with self.assertRaisesRegex(runner.cycle.CycleError, 'restore is pending'):
                    lease.require_new('a' * 32)
                lease.restored(run, state)
            with runner.DeviceLease('device', root) as lease:
                with self.assertRaisesRegex(runner.cycle.CycleError, 'already used'):
                    lease.require_new(TOKEN)
                lease.require_new('a' * 32)

    def test_corrupt_ledger_fails_closed_and_releases_live_lock(self):
        for value in ('{', '[]', '{"used_tokens": [123]}',
                      '{"used_tokens": [], "pending_run": null, "pending_expdb": true}',
                      '{"used_tokens": [], "pending_run": "run", "pending_expdb": "true"}'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                lease = runner.DeviceLease('device', root)
                lease.folder.mkdir()
                ledger = lease.folder / 'state.json'
                ledger.write_text(value)
                with self.assertRaises((ValueError, runner.cycle.CycleError)):
                    lease.__enter__()
                self.assertIsNone(lease.fd)
                ledger.unlink()
                with runner.DeviceLease('device', root):
                    pass


class CollectionTests(unittest.TestCase):
    def test_cached_flags_are_recomputed_from_bound_raw_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            raw = snapshot().encode()
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=[b'console-ramoops-0', raw]):
                self.assertTrue(runner.collect_now('device', run, TOKEN)['snapshot_pass'])
            path = run / 'collect/result.json'
            cached = json.loads(path.read_text())
            cached.update(snapshot_pass=False, physical_ufs_qualified=True, dma_qualified=True)
            path.write_text(json.dumps(cached))
            with mock.patch.object(runner.cycle, 'adb_output') as adb:
                result = runner.collect_now('device', run, TOKEN)
                self.assertTrue(result['snapshot_pass'])
                self.assertFalse(result['physical_ufs_qualified'])
                self.assertFalse(result['dma_qualified'])
                adb.assert_not_called()

    def test_tampered_raw_retries_instead_of_trusting_cached_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=[b'console-ramoops-0', snapshot().encode()]):
                result = runner.collect_now('device', run, TOKEN)
            (run / 'collect' / result['files'][0]['name']).write_text('changed')
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=[b'console-ramoops-0', b'old-token-only']):
                self.assertFalse(runner.collect_now('device', run, TOKEN)['snapshot_pass'])
            self.assertEqual(len(list((run / 'collect').glob('capture-*'))), 2)

    def test_partial_capture_and_corrupt_json_do_not_block_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            partial = run / 'collect/capture-partial'
            partial.mkdir(parents=True)
            (partial / 'console-ramoops-0').write_text('partial')
            (run / 'collect/result.json').write_text('{')
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=[b'console-ramoops-0', snapshot().encode()]):
                self.assertTrue(runner.collect_now('device', run, TOKEN)['snapshot_pass'])
            self.assertEqual((partial / 'console-ramoops-0').read_text(), 'partial')

    def test_failed_capture_remains_retryable_and_cannot_qualify_media(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=runner.cycle.CycleError('USB lost')):
                result = runner.collect_now('device', run, TOKEN)
                self.assertFalse(result['snapshot_pass'])
                self.assertFalse(result['physical_ufs_qualified'])
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=[b'console-ramoops-0', snapshot().encode()]):
                self.assertTrue(runner.collect_now('device', run, TOKEN)['snapshot_pass'])


class LifecycleTests(unittest.TestCase):
    def setup_trial(self, temp, *, dry_run=False):
        # Match CLI path normalization, including macOS /var -> /private/var.
        root = Path(temp).resolve()
        backup = root / 'backup'
        backup.mkdir()
        (backup / 'runtime.dtb').write_bytes(b'saved-fdt')
        image = root / 'candidate.img'
        image.write_bytes(b'candidate')
        args = SimpleNamespace(serial='device', fastboot_serial='device', backup=backup,
                               uefi=root / 'uefi', unixnu=root / 'unixnu', candidate_dir=root / 'candidate',
                               evidence_root=root / 'evidence', wait_seconds=120, dry_run=dry_run)
        manifest = {'sha256': runner.cycle.sha256(image.read_bytes()), 'ufs_probe': {'run_token': TOKEN}}
        records = {'boot_b': {'bytes': 1024, 'sha256': 'original'}}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(runner.cycle, 'verify_clean_repository'))
        stack.enter_context(mock.patch.object(runner.cycle, 'verify_backup', return_value=({}, records)))
        stack.enter_context(mock.patch.object(runner, 'verify_candidate', return_value=(image, manifest)))
        stack.enter_context(mock.patch.object(runner.cycle, 'verify_live_layout'))
        return root, args, records, stack

    def test_dry_run_never_contacts_device_or_consumes_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root, args, _, stack = self.setup_trial(temp, dry_run=True)
            with stack, runner.DeviceLease('device', root / 'leases') as lease, \
                 mock.patch.object(runner.cycle, 'adb_output') as adb, \
                 mock.patch.object(runner.cycle, 'request_fastboot_from_android') as reboot, \
                 mock.patch.object(runner.cycle, 'flash_boot_b') as flash:
                self.assertEqual(runner.run_trial(args, lease), 0)
                adb.assert_not_called()
                reboot.assert_not_called()
                flash.assert_not_called()
                lease.require_new(TOKEN)
                state = json.loads(next(args.evidence_root.glob('*/restore-state.json')).read_text())
                self.assertTrue(state['dry_run'])
                self.assertFalse(state['boot_b_may_be_modified'])

    def test_partial_flash_retains_durable_original_only_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root, args, _, stack = self.setup_trial(temp)
            with stack, runner.DeviceLease('device', root / 'leases') as lease, \
                 mock.patch.object(runner.cycle, 'current_android'), \
                 mock.patch.object(runner.cycle, 'compare_partition_hashes'), \
                 mock.patch.object(runner.cycle, 'adb_output', return_value=b'live-fdt'), \
                 mock.patch.object(runner.cycle, 'request_fastboot_from_android'), \
                 mock.patch.object(runner.cycle, 'verify_fastboot'), \
                 mock.patch.object(runner.cycle, 'verify_original_boot_b'), \
                 mock.patch.object(runner.cycle, 'fastboot_online', return_value=False):
                def fail_flash(serial, image):
                    state = json.loads(next(args.evidence_root.glob('*/restore-state.json')).read_text())
                    self.assertTrue(state['boot_b_may_be_modified'])
                    ledger = json.loads(lease.path.read_text())
                    self.assertEqual(ledger['used_tokens'], [TOKEN])
                    self.assertEqual(ledger['pending_run'], str(image.parent))
                    raise runner.cycle.CycleError('USB failed during flash')
                with mock.patch.object(runner.cycle, 'flash_boot_b', side_effect=fail_flash) as flash:
                    with self.assertRaisesRegex(runner.cycle.CycleError, 'USB failed'):
                        runner.run_trial(args, lease)
                    self.assertEqual(flash.call_count, 1)
                with self.assertRaisesRegex(runner.cycle.CycleError, 'restore is pending'):
                    lease.require_new('a' * 32)

    def test_restore_hash_failure_cannot_clear_pending_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run'
            args = SimpleNamespace(serial='device', fastboot_serial='device', backup=root / 'backup', wait_seconds=120)
            state = {'token': TOKEN}
            with runner.DeviceLease('device', root / 'leases') as lease:
                lease.arm(run, state)
                with mock.patch.object(runner.cycle, 'restore_android_safely'), \
                     mock.patch.object(runner.cycle, 'compare_partition_hashes', side_effect=runner.cycle.CycleError('partition changed')):
                    with self.assertRaisesRegex(runner.cycle.CycleError, 'partition changed'):
                        runner.finish_restore(args, run, state, {}, lease)
                self.assertEqual(lease.data['pending_run'], str(run))
                self.assertTrue(state['boot_b_may_be_modified'])

    def test_restore_only_rejects_dry_run_or_foreign_identity_before_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run'
            run.mkdir()
            args = SimpleNamespace(serial='device', fastboot_serial='device', backup=root / 'backup', restore_run=run)
            base = {'kind': 'dali-ufs-register-probe', 'dry_run': False, 'serial': 'device',
                    'fastboot_serial': 'device', 'backup': str(args.backup), 'token': TOKEN}
            for changes in ({'dry_run': True}, {'serial': 'foreign'}, {'token': 42}, {'backup': '/foreign'}):
                (run / 'restore-state.json').write_text(json.dumps(dict(base, **changes)))
                with runner.DeviceLease('device', root / 'leases') as lease, \
                     mock.patch.object(runner.cycle, 'verify_backup') as backup, \
                     mock.patch.object(runner.cycle, 'wait_fastboot') as fastboot:
                    with self.assertRaisesRegex(runner.cycle.CycleError, 'identity/token/kind'):
                        runner.restore_pending(args, lease)
                    backup.assert_not_called()
                    fastboot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
