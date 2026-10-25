"""Offline lifecycle checks for the combined source-XNU UFS read-only runner."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
DEVICE = HERE.parent
sys.path.insert(0, str(HERE))
import test_ufs_readonly_evidence as ro_fixtures
import test_ufs_xnu_evidence as xnu_fixtures

spec = importlib.util.spec_from_file_location('xnu_ufs_readonly_runner', DEVICE / 'scripts/run-xnu-ufs-readonly.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def manifest_for(token, kernel_sha, fdt_sha):
    return {
        'target': 'dali',
        'payload': {
            'xnu_ufs_read_only': {'enabled': True, 'run_token': token, 'write_authorized': False,
                                  'live_fdt_sha256': fdt_sha},
            'xnu_run_token': token, 'xnu_kernel_sha256': kernel_sha,
            'xnu_ramdisk_sha256': None, 'xnu_trust_cache_sha256': None,
            'recovery': {'auto_fastboot': False, 'reset_method': None},
        },
        'ufs_xnu': {'enabled': True, 'run_token': token, 'write_authorized': False,
                    'physical_ufs_qualified': False, 'live_fdt_sha256': fdt_sha},
        'ufs_host': {'enabled': True, 'mode': 'read-only-blockio', 'run_token': token,
                     'media_writes': False, 'write_authorized': False},
    }


class SourceXnuRunnerTests(unittest.TestCase):
    def test_build_candidate_sets_both_readonly_owners_and_binds_final_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uefi, unixnu, backup, run = (root / name for name in ('uefi', 'unixnu', 'backup', 'run'))
            (uefi / 'out').mkdir(parents=True)
            unixnu.mkdir(); backup.mkdir(); run.mkdir()
            kernel = root / 'kernel.macho'; kernel.write_bytes(b'compiled XNU')
            adt = root / 'kernel.macho.adt'; adt.write_bytes(b'compiled ADT')
            live_fdt = root / 'runtime.dtb'; live_fdt.write_bytes(b'live Dali FDT')
            image = uefi / 'out/UniXNU-dali-uefi-boot.img'; image.write_bytes(b'candidate boot image')
            token = '1234567890abcdef' * 2
            fdt_sha = hashlib.sha256(live_fdt.read_bytes()).hexdigest()
            commits = {str(unixnu): 'b' * 40, str(DEVICE): 'a' * 40, str(uefi): 'c' * 40}
            manifest = {
                'bytes': image.stat().st_size, 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                'unixnu_commit': commits[str(unixnu)], 'device_commit': commits[str(DEVICE)],
                'uefi_commit': commits[str(uefi)],
                'payload': {
                    'xnu_kernel_sha256': hashlib.sha256(kernel.read_bytes()).hexdigest(),
                    'xnu_adt_sha256': hashlib.sha256(adt.read_bytes()).hexdigest(),
                    'xnu_run_token': token,
                    'recovery': {'auto_fastboot': False, 'watchdog_timeout_seconds': 31,
                                 'run_token': token, 'reset_method': None},
                    'xnu_ufs_read_only': {'enabled': True, 'run_token': token,
                        'write_authorized': False, 'live_fdt_sha256': fdt_sha},
                },
                'ufs_xnu': {'enabled': True, 'run_token': token, 'write_authorized': False,
                    'physical_ufs_qualified': False, 'live_fdt_sha256': fdt_sha},
                'ufs_host': {'enabled': True, 'mode': 'read-only-blockio', 'run_token': token,
                    'media_writes': False, 'write_authorized': False},
            }
            (uefi / 'out/manifest.json').write_text(json.dumps(manifest))
            args = argparse.Namespace(uefi=uefi, unixnu=unixnu)
            recovery = {'auto_fastboot': False, 'device_watchdog_timeout_seconds': 31}
            calls = []

            def capture(command, *, cwd, **_):
                return commits[str(cwd)]

            def build(command, log_path, *, env, **_):
                calls.append((command, env))

            with mock.patch.object(runner.cycle, 'run_capture', side_effect=capture), \
                 mock.patch.object(runner.cycle, 'run_logged', side_effect=build):
                actual_image, actual_manifest = runner.cycle.build_candidate(
                    args, backup, kernel, adt, recovery, token, run,
                    xnu_ufs_readonly=True, live_fdt=live_fdt)
            self.assertEqual(actual_image, image)
            self.assertEqual(actual_manifest, manifest)
            self.assertEqual(len(calls), 1)
            env = calls[0][1]
            self.assertEqual(env['DALI_UFS_READ_ONLY'], 'true')
            self.assertEqual(env['DALI_UFS_PROFILE'], 'read-only-blockio')
            self.assertEqual(env['DALI_UFS_PROBE'], 'false')
            self.assertEqual(env['DALI_XNU_UFS_READ_ONLY'], 'true')
            self.assertEqual(env['DALI_XNU_UFS_LIVE_FDT'], str(live_fdt.resolve()))
            self.assertEqual(env['DALI_AUTO_FASTBOOT'], 'false')
            self.assertNotIn('DALI_RESET_METHOD', env)

    def test_dry_run_builds_and_records_candidate_without_touching_device(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup, baseline_dir = root / 'backup', root / 'baseline-input'
            artifact, evidence_root = root / 'artifact', root / 'evidence'
            for path in (backup, artifact):
                path.mkdir()
            ro_fixtures.save_fixture(baseline_dir)
            original = (baseline_dir / 'original.img').read_bytes()
            (backup / 'boot_b.img').write_bytes(original)
            (backup / 'runtime.dtb').write_bytes(b'saved-live-fdt')
            kernel = artifact / 'kernel.macho'; kernel.write_bytes(b'compiled source kernel')
            (artifact / 'manifest.json').write_text('{"artifact":"source-only"}')
            records = {'boot_b': {'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest()}}
            args = argparse.Namespace(serial='test-device', fastboot_serial='test-device', backup=backup,
                baseline_dir=baseline_dir, uefi=root / 'uefi', unixnu=root / 'unixnu', artifact_dir=artifact,
                evidence_root=evidence_root, dry_run=True, wait_seconds=120)
            device_commit = 'a' * 40
            kernel_manifest = {'kernel_sha256': hashlib.sha256(kernel.read_bytes()).hexdigest(),
                'device_commit': device_commit, 'unixnu_commit': 'b' * 40, 'xnu_commit': 'c' * 40,
                'uefi_commit': 'd' * 40, 'recovery': {'auto_fastboot': False}}

            def build_candidate(fake_args, run, *_):
                sealed = run / 'candidate'; sealed.mkdir()
                image = sealed / 'UniXNU-dali-uefi-boot.img'; image.write_bytes(ro_fixtures.CANDIDATE)
                manifest = {'sha256': hashlib.sha256(ro_fixtures.CANDIDATE).hexdigest()}
                (sealed / 'manifest.json').write_text(json.dumps(manifest))
                return sealed, manifest

            def make_adt(command, log, **_):
                Path(command[command.index('--output') + 1]).write_bytes(b'adt')

            with runner.probe.DeviceLease(args.serial, root / 'leases') as lease, \
                 mock.patch.object(runner.cycle, 'verify_clean_repository'), \
                 mock.patch.object(runner.cycle, 'verify_backup', return_value=({}, records)), \
                 mock.patch.object(runner.cycle, 'verify_kernel_artifact', return_value=(kernel, kernel_manifest)), \
                 mock.patch.object(runner.cycle, 'run_capture', return_value=device_commit), \
                 mock.patch.object(runner.cycle, 'run_logged', side_effect=make_adt), \
                 mock.patch.object(runner.cycle, 'verify_live_layout'), \
                 mock.patch.object(runner, 'private_candidate', side_effect=build_candidate), \
                 mock.patch.object(runner.cycle, 'current_android') as android, \
                 mock.patch.object(runner.cycle, 'flash_boot_b') as flash:
                self.assertEqual(runner.run_trial(args, lease), 0)
                android.assert_not_called(); flash.assert_not_called()
                self.assertEqual(lease.data['used_tokens'], [])
                run = next(evidence_root.iterdir())
                self.assertTrue((run / 'candidate.json').is_file())
                state = json.loads((run / 'restore-state.json').read_text())
                self.assertEqual(state['baseline_folder'], 'baseline')
                self.assertTrue(state['candidate_verified_before_flash'])

    def test_candidate_binding_requires_one_token_fdt_and_no_write_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            fdt = Path(temp) / 'runtime.dtb'; fdt.write_bytes(b'live FDT')
            token = '1234567890abcdef' * 2
            kernel_sha = '1' * 64
            kernel = {'kernel_sha256': kernel_sha}
            manifest = manifest_for(token, kernel_sha, hashlib.sha256(fdt.read_bytes()).hexdigest())
            runner.candidate_binding(manifest, token, kernel, fdt)
            for change in (
                lambda item: item['ufs_xnu'].__setitem__('write_authorized', True),
                lambda item: item['payload']['xnu_ufs_read_only'].__setitem__('run_token', 'f' * 32),
                lambda item: item['ufs_host'].__setitem__('media_writes', True),
            ):
                bad = json.loads(json.dumps(manifest)); change(bad)
                with self.assertRaises(runner.cycle.CycleError):
                    runner.candidate_binding(bad, token, kernel, fdt)

    def test_scoring_marks_only_complete_valid_transcript_and_restored_run_qualified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); backup = root / 'backup'; backup.mkdir()
            live = root / 'run' / 'live-baseline'; live.parent.mkdir()
            ro_fixtures.save_fixture(live)
            original = (live / 'original.img').read_bytes(); (backup / 'boot_b.img').write_bytes(original)
            baseline = runner.probe.readonly.load_baseline(live, serial='test-device', original_boot_b=backup / 'boot_b.img')
            candidate_dir = live.parent / 'candidate'; candidate_dir.mkdir()
            candidate = candidate_dir / 'UniXNU-dali-uefi-boot.img'
            candidate.write_bytes(xnu_fixtures.CANDIDATE)
            token = xnu_fixtures.TOKEN
            manifest = {'payload': {'xnu_run_token': token}, 'ufs_xnu': {'run_token': token}}
            record = json.dumps(manifest).encode()
            (candidate_dir / 'manifest.json').write_bytes(record)
            state = {'kind': runner.KIND, 'serial': 'test-device', 'fastboot_serial': 'test-device',
                'backup': str(backup), 'token': token, 'dry_run': False,
                'candidate_verified_before_flash': True, 'device_verified_before_flash': True,
                'baseline_folder': 'live-baseline', 'baseline_sha256': baseline['baseline_sha256'],
                'candidate_record_sha256': hashlib.sha256(record).hexdigest(),
                'candidate_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
                'restore_verified': True, 'all_partition_hashes_match': True,
                'android_root_verified': True, 'original_boot_b_restored': True}
            args = argparse.Namespace(serial='test-device', fastboot_serial='test-device', backup=backup)
            score = runner.xnu_scorer(args, live.parent, state)
            xnu_log = '\n'.join(line for line in xnu_fixtures.trace(baseline).splitlines()
                                 if not line.startswith('DALI-UFS-HANDOFF:') and
                                 not line.startswith('DALI-UFS-HOST: run=' + token + ' quiesce='))
            firmware_log = ro_fixtures.trace(baseline)
            result = score(firmware_log + '\n' + xnu_log, token)
            self.assertTrue(result['observation_complete'], result)
            self.assertTrue(result['xnu_ufs_read_only_qualified'], result)
            self.assertTrue(result['uefi_read_only_qualified'], result)
            self.assertTrue(result['firmware_xnu_handoff_qualified'], result)
            self.assertFalse(result['media_writes_qualified'])
            partial = score(firmware_log + '\n' + '\n'.join(xnu_log.splitlines()[:-2]), token)
            self.assertFalse(partial['observation_complete'], partial)
            self.assertFalse(partial['xnu_ufs_read_only_qualified'])

    def test_restore_only_run_may_not_overlap_backup_or_device_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); backup = root / 'backup'; backup.mkdir()
            for run in (backup / 'run', runner.probe.STATE_ROOT / 'nested-run'):
                argv = ['run-xnu-ufs-readonly.py', '--serial', 'test-device', '--backup', str(backup),
                        '--restore-run', str(run), '--wait-seconds', '120']
                with mock.patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as error:
                    runner.main()
                self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
