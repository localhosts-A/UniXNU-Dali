"""The real reset runner must not turn USB or stale logs into unattended PASS."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import types

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reset_runner', ROOT / 'scripts/run-reset-qualification.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ResetRunnerTests(unittest.TestCase):
    def test_score_requires_log_identity_method_no_keys_and_fresh_fastboot(self):
        token = 'a' * 32
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / 'collect').mkdir()
            path = run / 'collect/result.json'
            good = {'files': ['console-ramoops-0'], 'failures': [],
                    'xnu': {'pass': True, 'run_token': token},
                    'recovery': {'qualification': {'pass': True, 'method': 'psci-cold'}}}
            path.write_text(json.dumps(good))
            result = runner.score_result(run, token, 'psci-cold', True)
            self.assertTrue(result['technical_evidence_pass'])
            self.assertFalse(result['complete_unattended_pass'])
            self.assertIsNone(result['automatic_fastboot'])
            self.assertFalse(runner.score_result(run, token, 'psci-cold', False)['technical_evidence_pass'])
            self.assertFalse(runner.score_result(run, 'b' * 32, 'psci-cold', True)['technical_evidence_pass'])
            self.assertFalse(runner.score_result(run, token, 'psci-warm', True)['technical_evidence_pass'])
            good['io'] = {'keypad_events': [{'unicode': 13}]}
            path.write_text(json.dumps(good))
            self.assertFalse(runner.score_result(run, token, 'psci-cold', True)['technical_evidence_pass'])

    def test_restore_only_refuses_foreign_identity_and_only_calls_original_restorer(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp).resolve()
            a = types.SimpleNamespace(restore_run=run, backup=run/'backup', uefi=run/'uefi',
                                      serial='dali', fastboot_serial='dali', fastboot_timeout=120)
            state = {'serial': 'dali', 'fastboot_serial': 'dali', 'backup': str(a.backup),
                     'token': 'a'*32, 'reset_method': 'psci-cold', 'original_boot_b_sha256': 'original'}
            (run/'restore-state.json').write_text(json.dumps(state))
            with mock.patch.object(runner.cycle, 'verify_backup', return_value=({}, {'boot_b': {'sha256':'original'}})), \
                 mock.patch.object(runner.cycle, 'adb_online', return_value=False), \
                 mock.patch.object(runner.cycle, 'wait_fastboot', return_value=True), \
                 mock.patch.object(runner.cycle, 'flash_boot_b') as flash, \
                 mock.patch.object(runner, 'finish_restore') as restore, \
                 mock.patch.object(runner, 'score_result'):
                self.assertEqual(runner.restore_pending(a), 0)
                restore.assert_called_once()
                flash.assert_not_called()
                state['serial'] = 'other'
                (run/'restore-state.json').write_text(json.dumps(state))
                with self.assertRaises(runner.cycle.CycleError):
                    runner.restore_pending(a)

    def test_restore_online_android_with_candidate_requests_original_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp).resolve()
            a = types.SimpleNamespace(restore_run=run, backup=run/'backup', uefi=run/'uefi',
                                      serial='dali', fastboot_serial='dali', fastboot_timeout=120)
            state = {'serial': 'dali', 'fastboot_serial': 'dali', 'backup': str(a.backup),
                     'token': 'a'*32, 'reset_method': 'psci-cold', 'original_boot_b_sha256': 'original'}
            (run/'restore-state.json').write_text(json.dumps(state))
            with mock.patch.object(runner.cycle, 'verify_backup', return_value=({}, {'boot_b': {'sha256':'original'}})), \
                 mock.patch.object(runner.cycle, 'adb_online', return_value=True), \
                 mock.patch.object(runner.cycle, 'current_android'), \
                 mock.patch.object(runner.cycle, 'compare_partition_hashes', side_effect=runner.cycle.CycleError('boot_b differs')), \
                 mock.patch.object(runner.cycle, 'request_fastboot_from_android') as reboot, \
                 mock.patch.object(runner.cycle, 'flash_boot_b') as flash, \
                 mock.patch.object(runner, 'collect_now') as collect, \
                 mock.patch.object(runner, 'finish_restore') as restore, \
                 mock.patch.object(runner, 'score_result'):
                self.assertEqual(runner.restore_pending(a), 0)
                collect.assert_called_once()
                reboot.assert_called_once_with('dali', 'dali', 120)
                restore.assert_called_once()
                flash.assert_not_called()
