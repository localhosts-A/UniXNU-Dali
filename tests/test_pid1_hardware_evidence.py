"""Refuse stale, partial, reordered or non-hardware PID1 console evidence."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import tempfile
from types import SimpleNamespace

path = Path(__file__).resolve().parents[1] / 'scripts/run-pid1-bootb.py'
spec = importlib.util.spec_from_file_location('pid1_hardware', path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class HardwareEvidenceTests(unittest.TestCase):
    def test_pending_restore_uses_original_restorer_and_refuses_changed_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            backup = run / 'backup'
            state = {'token': 'a' * 32, 'serial': 'dali-serial', 'fastboot_serial': 'dali-serial',
                     'backup': str(backup), 'original_boot_b_sha256': 'b' * 64,
                     'boot_b_may_be_modified': True}
            (run / 'restore-state.json').write_text(json.dumps(state))
            args = SimpleNamespace(restore_run=run, backup=backup, serial='dali-serial',
                                   fastboot_serial='dali-serial', fastboot_timeout=120)
            with patch.object(runner.cycle, 'verify_backup', return_value=({}, {'boot_b': {'sha256': 'b' * 64}})), \
                    patch.object(runner.cycle, 'adb_online', return_value=False), \
                    patch.object(runner.cycle, 'wait_fastboot', return_value=True), \
                    patch.object(runner.cycle, 'restore_android_safely') as restore, \
                    patch.object(runner.cycle, 'flash_boot_b', side_effect=AssertionError('no candidate flash')), \
                    patch.object(runner, 'collect_result') as collect:
                self.assertEqual(runner.restore_pending(args), 0)
                restore.assert_called_once()
                collect.assert_called_once()
            self.assertFalse(json.loads((run / 'restore-state.json').read_text())['boot_b_may_be_modified'])
            args.serial = 'foreign-phone'
            with patch.object(runner.cycle, 'verify_backup', side_effect=AssertionError('identity first')), \
                    self.assertRaises(runner.cycle.CycleError):
                runner.restore_pending(args)

    def test_actual_token_and_full_elapsed_sequence_are_required(self):
        token = 'abc01234' * 4
        lines = [f'DALI-PID1: run={token} stage={"PASS" if i == 30 else "heartbeat"} '
                 f'pid=1 seq={i} elapsed-ns={i * 1_000_000_000} '
                 'root=mockfs readonly=1 pagesize=4096 backend=ramoops' for i in range(31)]
        source = ('\n'.join(lines) + '\n').encode()
        self.assertEqual(runner.validate_pid1_log(source, token)['heartbeats'], 31)
        bad_values = [source.replace(token.encode(), b'0' * 32),
                      source.replace(b'backend=ramoops', b'backend=qemu'),
                      source.replace(b'pid=1', b'pid=2'),
                      source.replace(b'readonly=1', b'readonly=0'),
                      source.replace(b'30000000000', b'29999999999'),
                      '\n'.join(lines[:-1]).encode(),
                      '\n'.join(lines + [lines[0]]).encode(),
                      '\n'.join([lines[1], lines[0], *lines[2:]]).encode(),
                      source + f'DALI-PID1: run={token} stage=refused seq=0 readonly-root=0\n'.encode()]
        for index, bad in enumerate(bad_values):
            with self.subTest(index=index), self.assertRaises(runner.cycle.CycleError):
                runner.validate_pid1_log(bad, token)
