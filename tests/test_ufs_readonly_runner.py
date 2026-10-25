"""Physical-mode lifecycle with fake devices; never hardware qualification."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import test_ufs_probe_runner as legacy
import test_ufs_readonly_evidence as fixtures

runner = legacy.runner
TOKEN = legacy.TOKEN


class PhysicalRunnerTests(unittest.TestCase):
    def setup_trial(self, temp, *, dry_run=False):
        root, a, records, stack = legacy.LifecycleTests().setup_trial(temp, dry_run=dry_run)
        a.mode = 'read-only-blockio'
        a.baseline_dir = root / 'baseline-input'
        report = fixtures.save_fixture(a.baseline_dir)
        report['serial'] = a.serial
        (a.baseline_dir / 'baseline.json').write_text(json.dumps(report))
        original = (a.baseline_dir / 'original.img').read_bytes()
        (a.backup / 'boot_b.img').write_bytes(original)
        records['boot_b'].update(bytes=len(original), sha256=runner.cycle.sha256(original))
        image = root / 'candidate.img'
        image.write_bytes(fixtures.CANDIDATE)
        manifest = {'sha256': runner.cycle.sha256(fixtures.CANDIDATE), 'ufs_host': {'run_token': TOKEN}}
        runner.verify_candidate.return_value = image, manifest
        baseline = runner.readonly.load_baseline(a.baseline_dir, serial=a.serial, original_boot_b=a.backup / 'boot_b.img')
        return root, a, records, stack, baseline

    def runtime(self, stack, a, records, baseline):
        def live(args, run, expected):
            shutil.copytree(run / 'baseline', run / 'live-baseline')
            return runner.readonly.load_baseline(run / 'live-baseline', serial=args.serial,
                                                 original_boot_b=args.backup / 'boot_b.img')

        def adb(serial, command):
            self.assertEqual(serial, a.serial)
            if command == 'cat /sys/firmware/fdt':
                return b'live-fdt'
            if command == 'ls /sys/fs/pstore':
                return b'console-ramoops-0'
            if command == 'cat /sys/fs/pstore/console-ramoops-0':
                return fixtures.trace(baseline).encode()
            raise AssertionError('unexpected device command: ' + command)

        def restore(*args, **kwargs):
            kwargs['post_boot']()

        mocks = {}
        for name, options in (
            ('current_android', {'return_value': {'product': 'dali', 'slot_suffix': '_b', 'root': 'uid=0(root)'}}),
            ('compare_partition_hashes', {'return_value': {'boot_b': records['boot_b']['sha256']}}),
            ('adb_output', {'side_effect': adb}), ('request_fastboot_from_android', {}),
            ('verify_fastboot', {}), ('verify_original_boot_b', {}),
            ('fastboot_command', {}), ('wait_fastboot_disconnect', {'return_value': True}),
            ('wait_fastboot', {'return_value': True}), ('restore_android_safely', {'side_effect': restore})):
            mocks[name] = stack.enter_context(mock.patch.object(runner.cycle, name, **options))
        mocks['live'] = stack.enter_context(mock.patch.object(runner, 'collect_live_readonly_baseline', side_effect=live))
        return mocks

    def test_dry_run_snapshots_raw_baseline_without_contact_or_token_consumption(self):
        with tempfile.TemporaryDirectory() as temp:
            root, a, _, stack, baseline = self.setup_trial(temp, dry_run=True)
            with stack, runner.DeviceLease(a.serial, root / 'leases') as lease, \
                 mock.patch.object(runner.cycle, 'adb_output') as adb, \
                 mock.patch.object(runner, 'collect_live_readonly_baseline') as live, \
                 mock.patch.object(runner.cycle, 'request_fastboot_from_android') as reboot:
                self.assertEqual(runner.run_trial(a, lease), 0)
                adb.assert_not_called(); live.assert_not_called(); reboot.assert_not_called()
                lease.require_new(TOKEN)
                run = next(a.evidence_root.iterdir())
                state = json.loads((run / 'restore-state.json').read_text())
                self.assertEqual(state['kind'], runner.KINDS[a.mode])
                self.assertIn('--mode', state['restore_command'])
                self.assertNotIn('--baseline-dir', state['restore_command'])
                result = runner.trial_scorer(a, run, state)(fixtures.trace(baseline), TOKEN)
                self.assertTrue(result['uefi_read_only_pass'])
                self.assertFalse(result['uefi_read_only_qualified'])
                self.assertFalse(result['device_identity_bound_to_run'])

    def test_complete_run_binds_raw_evidence_and_clears_only_verified_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root, a, records, stack, baseline = self.setup_trial(temp)
            mocks = self.runtime(stack, a, records, baseline)
            with stack, runner.DeviceLease(a.serial, root / 'leases') as lease:
                def flash(serial, image):
                    state = json.loads((image.parent / 'restore-state.json').read_text())
                    self.assertTrue(state['boot_b_may_be_modified'])
                    self.assertTrue(state['device_verified_before_flash'])
                    self.assertEqual(state['baseline_folder'], 'live-baseline')
                    self.assertEqual(lease.data['pending_run'], str(image.parent))
                with mock.patch.object(runner.cycle, 'flash_boot_b', side_effect=flash) as flashed:
                    self.assertEqual(runner.run_trial(a, lease), 0)
                self.assertEqual(flashed.call_count, 1)
                self.assertIsNone(lease.data['pending_run'])
                self.assertEqual(lease.data['used_tokens'], [TOKEN])
                run = next(a.evidence_root.iterdir())
                result = json.loads((run / 'probe-result.json').read_text())
                self.assertTrue(result['uefi_read_only_qualified'])
                self.assertTrue(result['uefi_sync_command_qualified'])
                self.assertTrue(result['diagnostic_stop_qualified'])
                self.assertTrue(result['android_restored'])
                self.assertFalse(result['complete_ufs_read_write_pass'])
                self.assertFalse(result['physical_ufs_qualified'])
                state = json.loads((run / 'restore-state.json').read_text())
                (run / 'candidate-boot.img').chmod(0o600)
                (run / 'candidate-boot.img').write_bytes(b'changed')
                with mock.patch.object(runner.cycle, 'adb_output') as adb:
                    rescored = runner.collect_now(a.serial, run, TOKEN, runner.trial_scorer(a, run, state))
                self.assertFalse(rescored['uefi_read_only_qualified'])
                self.assertIn('binding_error', rescored)
                mocks['live'].assert_called_once()

    def test_partial_flash_preserves_new_kind_and_original_only_restore_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root, a, records, stack, baseline = self.setup_trial(temp)
            self.runtime(stack, a, records, baseline)
            with stack, runner.DeviceLease(a.serial, root / 'leases') as lease, \
                 mock.patch.object(runner.cycle, 'fastboot_online', return_value=False), \
                 mock.patch.object(runner.cycle, 'flash_boot_b', side_effect=runner.cycle.CycleError('partial flash')) as flash:
                with self.assertRaisesRegex(runner.cycle.CycleError, 'partial flash'):
                    runner.run_trial(a, lease)
                self.assertEqual(flash.call_count, 1)
                run = Path(lease.data['pending_run'])
                state = json.loads((run / 'restore-state.json').read_text())
                self.assertEqual(state['kind'], 'dali-ufs-read-only-blockio')
                self.assertEqual(state['restore_command'][-2:], ['--mode', a.mode])
                self.assertTrue(state['boot_b_may_be_modified'])

    def test_bad_baseline_stops_before_device_contact(self):
        with tempfile.TemporaryDirectory() as temp:
            root, a, _, stack, _ = self.setup_trial(temp)
            (a.baseline_dir / 'primary_header.bin').write_bytes(b'corrupt')
            with stack, runner.DeviceLease(a.serial, root / 'leases') as lease, \
                 mock.patch.object(runner.cycle, 'adb_output') as adb, \
                 mock.patch.object(runner.cycle, 'current_android') as android, \
                 mock.patch.object(runner.cycle, 'flash_boot_b') as flash:
                with self.assertRaisesRegex(ValueError, 'hash/length'):
                    runner.run_trial(a, lease)
                adb.assert_not_called(); android.assert_not_called(); flash.assert_not_called()
                lease.require_new(TOKEN)

    def test_restore_only_does_not_need_candidate_or_baseline_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root, a, records, stack, _ = self.setup_trial(temp, dry_run=True)
            with stack, runner.DeviceLease(a.serial, root / 'leases') as lease:
                self.assertEqual(runner.run_trial(a, lease), 0)
                run = next(a.evidence_root.iterdir())
                state = json.loads((run / 'restore-state.json').read_text())
                state['dry_run'] = False
                lease.arm(run, state)
                shutil.rmtree(run / 'baseline'); (run / 'candidate-boot.img').unlink()
                a.restore_run = run; a.dry_run = False
                with mock.patch.object(runner.cycle, 'adb_online', return_value=True), \
                     mock.patch.object(runner.cycle, 'current_android', return_value={'slot_suffix': '_b'}), \
                     mock.patch.object(runner.cycle, 'compare_partition_hashes', return_value={}), \
                     mock.patch.object(runner.cycle, 'adb_output', side_effect=runner.cycle.CycleError('no log')), \
                     mock.patch.object(runner.cycle, 'wait_fastboot') as fastboot:
                    runner.restore_pending(a, lease)
                self.assertIsNone(lease.data['pending_run'])
                self.assertFalse(json.loads((run / 'probe-result.json').read_text())['uefi_read_only_qualified'])
                fastboot.assert_not_called()

    def test_collection_store_corruption_cannot_block_needed_original_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root, a, _, stack, _ = self.setup_trial(temp, dry_run=True)
            with stack, runner.DeviceLease(a.serial, root / 'leases') as lease:
                runner.run_trial(a, lease)
                run = next(a.evidence_root.iterdir())
                state = json.loads((run / 'restore-state.json').read_text())
                state['dry_run'] = False
                lease.arm(run, state)
                (run / 'collect').write_text('corrupt directory')
                a.restore_run = run
                def restore(*args, **kwargs):
                    result = kwargs['post_boot']()
                    self.assertIn('collection_store_error', result)
                with mock.patch.object(runner.cycle, 'adb_online', return_value=True), \
                     mock.patch.object(runner.cycle, 'current_android', return_value={'slot_suffix': '_b'}), \
                     mock.patch.object(runner.cycle, 'compare_partition_hashes', side_effect=[runner.cycle.CycleError('boot_b differs'), {}]), \
                     mock.patch.object(runner.cycle, 'request_fastboot_from_android') as reboot, \
                     mock.patch.object(runner.cycle, 'wait_fastboot', return_value=True), \
                     mock.patch.object(runner.cycle, 'restore_android_safely', side_effect=restore) as restored:
                    runner.restore_pending(a, lease)
                reboot.assert_called_once(); restored.assert_called_once()
                self.assertIsNone(lease.data['pending_run'])
                result = json.loads((run / 'probe-result.json').read_text())
                self.assertTrue(result['android_restored'])
                self.assertFalse(result['uefi_read_only_qualified'])

    def test_collection_result_write_error_returns_unqualified_without_raising(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            with mock.patch.object(runner.cycle, 'adb_output', side_effect=[b'console-ramoops-0', legacy.snapshot().encode()]), \
                 mock.patch.object(runner, 'durable_json', side_effect=PermissionError('result store unavailable')):
                result = runner.collect_now('device', run, TOKEN)
            self.assertIn('collection_store_error', result)
            self.assertFalse(result['snapshot_pass'])
            self.assertFalse(result['uefi_read_only_qualified'])


class PhysicalCandidateMetadataTests(unittest.TestCase):
    def test_mu_source_declarations_must_match_firmware_workspace(self):
        head = 'c' * 40
        valid = {'target': 'dali', 'ufs_probe': {'enabled': False, 'mode': 'off', 'run_token': None,
                 'physical_storage_qualified': False, 'bds_verified_in_final_fd': True},
                 'ufs_host': {'enabled': True, 'mode': 'read-only-blockio', 'run_token': TOKEN, 'log_version': 1,
                 'controller_commands': True, 'mmio_writes': True, 'media_writes': False, 'write_authorized': False,
                 'physical_storage_qualified': False, 'host_verified_in_final_fd': True},
                 'payload': {'recovery': {'auto_fastboot': False, 'reset_method': None}}}
        valid.update({key: head for key in ('uefi_commit', 'unixnu_commit', 'device_commit', 'mu_commit', 'upstream_commit')})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'SILICIUM_UEFI.fd').write_bytes(b'not a candidate')
            for key in ('mu_commit', 'upstream_commit'):
                bad = copy.deepcopy(valid); bad[key] = 'f' * 40
                (root / 'manifest.json').write_text(json.dumps(bad))
                with mock.patch.object(runner.cycle, 'run_capture', return_value=head):
                    with self.assertRaisesRegex(runner.cycle.CycleError, key):
                        runner.verify_candidate(root, root, root, root, 'read-only-blockio')

    def test_wrong_mode_writes_probe_and_token_are_refused_before_source_or_device_calls(self):
        valid = {'target': 'dali', 'ufs_probe': {'enabled': False, 'mode': 'off', 'run_token': None,
                 'physical_storage_qualified': False, 'bds_verified_in_final_fd': True},
                 'ufs_host': {'enabled': True, 'mode': 'read-only-blockio', 'run_token': TOKEN, 'log_version': 1,
                 'controller_commands': True, 'mmio_writes': True, 'media_writes': False, 'write_authorized': False,
                 'physical_storage_qualified': False, 'host_verified_in_final_fd': True}}
        cases = []
        for key, value in (('media_writes', True), ('write_authorized', True), ('log_version', 2), ('run_token', 'bad')):
            bad = copy.deepcopy(valid); bad['ufs_host'][key] = value; cases.append(bad)
        bad = copy.deepcopy(valid); bad['ufs_probe']['enabled'] = True; cases.append(bad)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'SILICIUM_UEFI.fd').write_bytes(b'not a candidate')
            for manifest in cases:
                (root / 'manifest.json').write_text(json.dumps(manifest))
                with mock.patch.object(runner.cycle, 'run_capture') as command:
                    with self.assertRaises(runner.cycle.CycleError):
                        runner.verify_candidate(root, root, root, root, 'read-only-blockio')
                    command.assert_not_called()
            (root / 'manifest.json').write_text(json.dumps(valid))
            with self.assertRaisesRegex(runner.cycle.CycleError, 'register-only'):
                runner.verify_candidate(root, root, root, root)


if __name__ == '__main__':
    unittest.main()
