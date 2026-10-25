"""Keep PID1's no-watchdog artifact separate from automatic recovery cycles."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


DEVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pid1_artifact_runner', DEVICE / 'scripts/run-pid1-bootb.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
cycle = runner.cycle


class Pid1ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.unixnu = self.root / 'UniXNU'
        self.device = self.root / 'device'
        self.artifact = self.root / 'artifact'
        for path in (self.unixnu, self.device, self.artifact):
            path.mkdir()
        (self.device / 'board.toml').write_text('[firmware]\nuefi_commit = "' + 'b' * 40 + '"\n')
        (self.unixnu / 'feeds.toml').write_text('[[source]]\nname = "xnu"\ncommit = "' + 'c' * 40 + '"\n')
        (self.artifact / 'kernel.macho').write_bytes(b'host-policy-fixture')
        self.manifest = {
            'schema': 1, 'target': 'dali', 'artifact_name': 'dali-xnu-source',
            'source_only': True, 'retention_days': 1,
            'kernel_bytes': len(b'host-policy-fixture'),
            'kernel_sha256': cycle.sha256(b'host-policy-fixture'),
            'unixnu_commit': 'a' * 40, 'device_commit': 'a' * 40,
            'uefi_commit': 'b' * 40, 'xnu_commit': 'c' * 40,
            'recovery': {'auto_fastboot': False, 'fastboot_timeout_seconds': 120,
                         'device_watchdog_timeout_seconds': 31},
            'pid1': {'minimum_heartbeat_seconds': 30},
        }

    def verify(self, **profile):
        (self.artifact / 'manifest.json').write_text(json.dumps(self.manifest))
        with patch.object(cycle, 'run_capture', return_value='a' * 40):
            return cycle.verify_kernel_artifact(self.artifact, self.unixnu, self.device, **profile)

    def test_pid1_accepts_disabled_watchdog_but_other_runners_refuse_it(self):
        self.assertEqual(self.verify(pid1=True)[1]['recovery']['auto_fastboot'], False)
        for profile in ({}, {'reset_qualification': True}):
            with self.subTest(profile=profile), self.assertRaisesRegex(cycle.CycleError, 'explicit PID1'):
                self.verify(**profile)
        with self.assertRaisesRegex(cycle.CycleError, 'separate artifact profiles'):
            self.verify(pid1=True, reset_qualification=True)

    def test_pid1_refuses_enabled_watchdog_or_mixed_profiles(self):
        self.manifest['recovery']['auto_fastboot'] = True
        with self.assertRaisesRegex(cycle.CycleError, 'auto_fastboot=false'):
            self.verify(pid1=True)
        self.manifest['recovery']['auto_fastboot'] = False
        self.manifest['reset_qualification'] = {}
        with self.assertRaisesRegex(cycle.CycleError, 'own artifact'):
            self.verify(pid1=True)
        del self.manifest['reset_qualification']
        del self.manifest['pid1']
        with self.assertRaisesRegex(cycle.CycleError, 'own artifact'):
            self.verify(pid1=True)

    def test_legacy_cycle_still_requires_enabled_watchdog(self):
        del self.manifest['pid1']
        with self.assertRaisesRegex(cycle.CycleError, 'auto_fastboot=true'):
            self.verify()
        self.manifest['recovery']['auto_fastboot'] = True
        self.assertTrue(self.verify()[1]['recovery']['auto_fastboot'])

    def test_pid1_still_refuses_changed_kernel_bytes(self):
        (self.artifact / 'kernel.macho').write_bytes(b'tampered')
        with self.assertRaisesRegex(cycle.CycleError, 'size/SHA256'):
            self.verify(pid1=True)

    def test_artifact_builder_refuses_pid1_short_watchdog_before_build_inputs(self):
        output = self.root / 'output'
        command = [sys.executable, str(DEVICE / 'scripts/make-kernel-artifact.py'),
                   '--kernel', str(self.artifact / 'kernel.macho'), '--output-dir', str(output),
                   '--unixnu', str(self.unixnu), '--device-repo', str(self.device),
                   '--pid1-directory', str(self.artifact), '--auto-fastboot', 'true']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('PID1 continuation requires auto-fastboot=false', result.stderr)
        self.assertFalse(output.exists())
