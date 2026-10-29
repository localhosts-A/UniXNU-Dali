import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/test_dali_ipados_root_qemu.py'
spec = importlib.util.spec_from_file_location('dali_qemu_root_profile', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DaliQemuRootProfileTests(unittest.TestCase):
    def config(self, changes=None, suffix=''):
        text = (ROOT / 'configs/dali_ipados_qemu_root_defconfig').read_text()
        for old, new in (changes or {}).items():
            self.assertIn(old, text)
            text = text.replace(old, new, 1)
        with tempfile.NamedTemporaryFile('w', delete=False) as target:
            target.write(text + suffix)
            path = Path(target.name)
        self.addCleanup(path.unlink)
        return path

    @staticmethod
    def root_fixture():
        # Synthetic observer report only; no signature, mount or XNU execution.
        anchor = b'VROOTKEY' + b'\xa5' * 384 + b'\x01\x00\x01'
        inputs = {
            'kernel_sha256': '1' * 64, 'trust_cache': {'sha256': '2' * 64},
            'policy': {'owner_anchor_sha256': hashlib.sha256(anchor).hexdigest()},
            'config': {'sha256': '3' * 64}, 'qemu_config': {},
            'images': {'OS': {'sha256': '4' * 64},
                       'SystemOS': {'sha256': '5' * 64, 'first_sector_sha256': '6' * 64}},
        }
        report = {
            'stop_symbol': '_native_owner_root_accepted_checkpoint',
            'bounded_timeout_interrupted': False, 'function_return_stack_matched': True,
            'functional_ipados_boot_demonstrated': False, 'ipados_userspace_boot_demonstrated': False,
            'kernel_sha256': inputs['kernel_sha256'], 'offline_qemu_vmm_present_declared': True,
            'pc': '0xfffffff007001004', 'function_return_target': '0xfffffff007001004',
            'owner_root_binding_audit': {
                'boot_root_accepted': True, '_native_owner_root_accepted': 1,
                '_native_owner_root_decision': 1, '_native_owner_root_trust_enabled': 1,
                '_native_owner_root_apple_autherr': 80, '_native_owner_root_readonly': 1,
                'actual_uuid': module.OS_UUID, '_native_owner_root_mount_device': 0x1000001,
                '_native_owner_root_selected_device': 0x1000001,
                '_native_verified_block_root_device': 0x1000001,
                '_native_verified_block_root_media_matches': 1, '_native_verified_block_root_uuid_match': 1,
            },
            'original_amfi_table_owner_observed': True, 'crypto14_initialization_observed': True,
            'trust_cache_bundle_sha256': inputs['trust_cache']['sha256'],
            'requested_kernel_symbols': {'_verified_root_key': anchor.hex()},
            'native_virtio_globals': {'_native_virtio_ready': 1, '_native_virtio_read_requests': 16,
                                     '_native_virtio_read_failures': 0},
            'native_virtio_integrity': {'_native_virtio_manifest_ready': 1, '_native_virtio_verified_blocks': 32,
                                       '_native_virtio_integrity_failures': 0},
            'virtio_block_input': {'read_only': True, 'device_attached': True, 'source_format': 'UDIF',
                                   'bytes': module.OS_BYTES, 'host_media_writable': False,
                                   'source_sha256': inputs['images']['OS']['sha256']},
            'virtio_extra_block_input': {'read_only': True, 'device_attached': True,
                                         'bytes': module.SYSTEMOS_BYTES, 'first_sector_sha256': '6' * 64},
            'trace_events': [{'symbol': '_vfs_mountroot'}],
        }
        return inputs, report

    def test_checked_in_profile_supports_explicit_n_and_has_one_virtio_link(self):
        summary = module.validate_qemu_root_config(self.config())
        self.assertIn('CONFIG_XNU_NATIVE_VIRTIO_BLOCK', summary['required'])
        self.assertIn('CONFIG_XNU_NATIVE_UFS', summary['forbidden_disabled'])
        self.assertNotIn('libNativeVirtioBlock.a', summary['extra_archives'])
        self.assertTrue(summary['virtio_archive'].endswith('/libNativeVirtioBlock.a'))

    def test_commented_disabled_features_are_also_accepted(self):
        path = self.config({'CONFIG_XNU_NATIVE_UFS=n': '# CONFIG_XNU_NATIVE_UFS is not set'})
        module.validate_qemu_root_config(path)

    def test_physical_fixture_policy_and_duplicate_link_configs_are_refused(self):
        changes = [{f'CONFIG_{key}=n': f'CONFIG_{key}=y'} for key in (
            'XNU_NATIVE_AES', 'XNU_NATIVE_UFS', 'XNU_NATIVE_RAM_BLOCK',
            'XNU_SOURCE_MOCKFS', 'XNU_SOURCE_ENTITLEMENT_FREE')]
        changes += [
            {'CONFIG_XNU_OWNER_ROOT_TRUST=y': 'CONFIG_XNU_OWNER_ROOT_TRUST=n'},
            {'CONFIG_XNU_NATIVE_VIRTIO_BLOCK=y': 'CONFIG_XNU_NATIVE_VIRTIO_BLOCK=n'},
            {'CONFIG_XNU_CRYPTO_BOOTSTRAP="source"': 'CONFIG_XNU_CRYPTO_BOOTSTRAP="prelinked"'},
            {'CONFIG_XNU_NATIVE_UFS=n': ''},
            {'libDaliBootLog.a': 'libDaliUfs.a'},
            {'libDaliBootLog.a': 'libNativeVirtioBlock.a'},
            {'libVerifiedRootDali.a': 'libMissing.a'},
            {'native-virtio-block-qemu-source-storage': 'native-virtio-block'},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate_qemu_root_config(self.config(change))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            module.validate_qemu_root_config(self.config(suffix='CONFIG_XNU_NATIVE_UFS=y\n'))

    def test_qemu_virtio_dt_is_generated_from_real_qemu_machine(self):
        qemu = shutil.which('qemu-system-aarch64')
        if not qemu:
            self.skipTest('QEMU is not installed')
        with tempfile.TemporaryDirectory(prefix='dali-virtio-fdt-test-') as directory:
            output = Path(directory) / 'virtio.dtb'
            digest, command = module.make_qemu_virtio_fdt(qemu, output, Path(directory))
            self.assertEqual(struct.unpack_from('>I', output.read_bytes())[0], 0xd00dfeed)
            self.assertEqual(digest, module.digest(output))
            self.assertIn('virtio-blk-device,drive=probe-os', command)
            self.assertIn('virtio-blk-device,drive=probe-systemos', command)
            self.assertIn('dumpdtb=' + str(output), command[2])
            for index, value in enumerate(command):
                if value == '-blockdev':
                    self.assertIs(json.loads(command[index + 1])['read-only'], True)
            for name in ('dtb-probe-os.raw', 'dtb-probe-systemos.raw'):
                self.assertTrue((Path(directory) / name).is_file())
            with self.assertRaisesRegex(ValueError, 'new file'):
                module.make_qemu_virtio_fdt(qemu, output, Path(directory))

    def test_root_report_requires_actual_matched_checkpoint_and_readonly_binding(self):
        inputs, report = self.root_fixture()
        module.validate_root_report(report, inputs)
        mutations = [
            (('stop_symbol',), '_panic'), (('bounded_timeout_interrupted',), True),
            (('function_return_stack_matched',), False), (('function_return_target',), '0x0'),
            (('kernel_sha256',), '0' * 64), (('offline_qemu_vmm_present_declared',), False),
            (('functional_ipados_boot_demonstrated',), True), (('hardware_boot_demonstrated',), True),
            (('owner_root_binding_audit', 'boot_root_accepted'), False),
            (('owner_root_binding_audit', '_native_owner_root_decision'), 0),
            (('owner_root_binding_audit', '_native_owner_root_accepted'), 0),
            (('owner_root_binding_audit', '_native_owner_root_trust_enabled'), 0),
            (('owner_root_binding_audit', '_native_owner_root_apple_autherr'), 0),
            (('owner_root_binding_audit', '_native_owner_root_readonly'), 0),
            (('owner_root_binding_audit', '_native_owner_root_mount_device'), 0xffffffff),
            (('owner_root_binding_audit', '_native_owner_root_selected_device'), 0x1000002),
            (('owner_root_binding_audit', '_native_verified_block_root_device'), 0x1000002),
            (('owner_root_binding_audit', '_native_verified_block_root_media_matches'), 0),
            (('owner_root_binding_audit', '_native_verified_block_root_uuid_match'), 0),
            (('owner_root_binding_audit', 'actual_uuid'), 'another-volume'),
            (('original_amfi_table_owner_observed',), False),
            (('crypto14_initialization_observed',), False),
            (('trust_cache_bundle_sha256',), '0' * 64),
            (('requested_kernel_symbols', '_verified_root_key'), '00' * module.OWNER_KEY_BYTES),
            (('native_virtio_globals', '_native_virtio_read_requests'), 0),
            (('native_virtio_globals', '_native_virtio_read_failures'), 1),
            (('native_virtio_integrity', '_native_virtio_manifest_ready'), 0),
            (('native_virtio_integrity', '_native_virtio_integrity_failures'), 1),
            (('native_virtio_integrity', '_native_virtio_verified_blocks'), 0),
            (('virtio_block_input', 'read_only'), False),
            (('virtio_block_input', 'host_media_writable'), True),
            (('virtio_block_input', 'source_sha256'), '0' * 64),
            (('virtio_extra_block_input', 'read_only'), False),
            (('virtio_extra_block_input', 'first_sector_sha256'), '0' * 64),
            (('trace_events',), []),
        ]
        for path, value in mutations:
            actual = copy.deepcopy(report)
            parent = actual
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                module.validate_root_report(actual, inputs)
        del report['requested_kernel_symbols']
        with self.assertRaisesRegex(ValueError, 'public anchor'):
            module.validate_root_report(report, inputs)

    def test_workflow_builds_only_the_qemu_root_profile_without_cache_or_device_artifact(self):
        workflow = (ROOT / '.github/workflows/dali-xnu.yml').read_text()
        self.assertIn('ipados-qemu-root', workflow.split('validation_profile:', 1)[1].split('auto_fastboot:', 1)[0])
        self.assertIn('dali_ipados_qemu_root_defconfig', workflow)
        self.assertIn('test_dali_ipados_root_qemu.py', workflow)
        cache = workflow.split('- name: Restore exact-input source kernel cache', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn("env.VALIDATION_PROFILE != 'ipados-qemu-root'", cache)
        probe = workflow.split('- name: Probe actual read-only Apple OS root in QEMU Virtio', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", probe)
        artifact = workflow.split('- name: Validate and stage the one-day kernel artifact', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn("env.VALIDATION_PROFILE != 'ipados-qemu-root'", artifact)

    def test_loader_entry_never_counts_as_launchd_exec(self):
        inputs, report = self.root_fixture()
        report['trace_events'].append({'symbol': '_load_init_program'})
        summary = module.root_summary(report, inputs, '7' * 64)
        self.assertTrue(summary['owner_root_accepted'])
        self.assertTrue(summary['init_program_entry_observed'])
        self.assertFalse(summary['launchd_exec_observed'])
        for field in ('apple_userspace_boot_demonstrated', 'springboard_display_demonstrated',
                      'apple_hardware_root_trust_demonstrated', 'hardware_boot_demonstrated', 'device_flashable'):
            self.assertFalse(summary[field])

    def test_probe_command_uses_only_supported_generic_options(self):
        generic = ROOT.parent / 'UniXNU/scripts/probe-darwinboot-xnu.py'
        if not generic.is_file():
            self.skipTest('generic UniXNU checkout is not available')
        declared = set()
        for node in ast.walk(ast.parse(generic.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument':
                declared.update(arg.value for arg in node.args if isinstance(arg, ast.Constant) and
                                isinstance(arg.value, str) and arg.value.startswith('--'))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / 'run'

            def run(command, **kwargs):
                (output / 'gdb-stop.json').write_text('{}')
                return subprocess.CompletedProcess(command, 0, 'synthetic observer fixture')

            with patch.object(module.subprocess, 'run', side_effect=run) as execute:
                module.run_root_probe(generic.parents[1], base / 'kernel', base / 'prelink', base / 'trust',
                                      base / 'os.dmg', base / 'systemos.dmg', base / 'efi', base / 'fdt', output)
            command = execute.call_args.args[0]
            self.assertEqual(execute.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
            options = {value for value in command if value.startswith('--')}
            self.assertFalse(options - declared, options - declared)
            self.assertNotIn('--prelink-verification', command)
            self.assertNotIn('--config', command)
            self.assertIn('--owner-root-accepted', command)
            self.assertIn('--finish-function', command)
            self.assertIn('--qemu-vmm-present', command)
            self.assertIn('_verified_root_key:395', command)
            self.assertEqual(command[1], '-I')
            self.assertTrue((output / 'probe-command.json').is_file())

    def test_large_media_hash_does_not_use_read_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image'
            data = b'streamed media' * 1000
            path.write_bytes(data)
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-image read')):
                self.assertEqual(module.digest(path), hashlib.sha256(data).hexdigest())

    def test_both_source_media_are_checked_after_run(self):
        with tempfile.TemporaryDirectory() as directory:
            images = {}
            for name in ('OS', 'SystemOS'):
                path = Path(directory) / name
                path.write_bytes(name.encode())
                images[name] = {'path': str(path), 'bytes': path.stat().st_size, 'sha256': module.digest(path)}
            module.verify_source_media({'images': images})
            Path(images['SystemOS']['path']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'SystemOS image changed'):
                module.verify_source_media({'images': images})

    def test_preexisting_attachment_is_never_taken_over(self):
        with tempfile.NamedTemporaryFile() as image:
            info = plistlib.dumps({'images': [{'image-path': image.name}]})
            with patch.object(module.subprocess, 'check_output', return_value=info), \
                    patch.object(module.subprocess, 'run') as execute:
                with self.assertRaisesRegex(ValueError, 'already attached'):
                    module.attach_readonly_root_image(image.name)
            execute.assert_not_called()

    def test_ambiguous_new_attachment_is_cleaned_up_on_refusal(self):
        with tempfile.NamedTemporaryFile() as image:
            attached = plistlib.dumps({'system-entities': [
                {'dev-entry': '/dev/disk12', 'content-hint': 'GUID_partition_scheme'},
                {'dev-entry': '/dev/disk13', 'content-hint': 'GUID_partition_scheme'},
            ]})
            with patch.object(module.subprocess, 'check_output', return_value=plistlib.dumps({'images': []})), \
                    patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, attached)) as execute:
                with self.assertRaisesRegex(ValueError, 'one GPT'):
                    module.attach_readonly_root_image(image.name)
            detached = [call.args[0] for call in execute.call_args_list if call.args[0][1] == 'detach']
            self.assertEqual(detached, [['hdiutil', 'detach', '/dev/disk12'], ['hdiutil', 'detach', '/dev/disk13']])


    @staticmethod
    def synthetic_launchd():
        data = bytearray(96)
        struct.pack_into('<IiiIIIII', data, 0, 0xfeedfacf, 0x100000c, 0, 2, 1, 24, 0, 0)
        struct.pack_into('<IIQQ', data, 32, 0x80000028, 24, 64, 0)
        data[64:80] = b'LCMAIN-ENTRY!!!!'
        return bytes(data)

    def launchd_report_fixture(self, launchd):
        inputs, report = self.root_fixture()
        header = 0xfffffff333000000
        entry = launchd['entry_offset']
        report.update({
            'stop_symbol': 'original-launchd-main',
            'original_launchd_main_executed': True,
            'bounded_timeout_interrupted': False,
            'pstate': '0x0', 'pc': hex(header + entry + 4),
            'kernel_page_globals': {
                '_PAGE_SHIFT_CONST': 12, '_page_shift': 12,
                '_page_shift_user32': 12, '_page_size': 4096,
            },
            'direct_user_commpage_shifts': {'fffff4025:1': 12, 'fffff4037:1': 12},
            'root_selection': {'volume_uuid': module.OS_UUID, 'root_authentication_bypassed': False},
            'original_crypto_table_owner_observed': True,
            'original_crypto_initialized_observed': True,
            'requested_kernel_symbols': {
                **report['requested_kernel_symbols'], '_trust_cache_static_loaded': '01',
            },
            'trace_events': [
                {'symbol': '_vfs_mountroot'}, {'symbol': '_native_owner_root_checkpoint'},
                {'symbol': '_native_owner_root_accepted_checkpoint'},
                {'symbol': '_load_static_trust_cache'},
            ],
            'syscall_start_event': {'pc': '0xfffffff00bcad650', 'lr': '0xfffffff00bcad8d8',
                                    'elapsed_seconds': 17.4},
            'followed_main_entry': {
                'observed': True, 'first_instruction_completed': True,
                'entry_offset': entry, 'pc': hex(header + entry),
                'checkpoint_pc': hex(header + entry + 4), 'mach_header': hex(header),
                'mach_header_bytes': launchd['mach_header'],
                'entry_bytes_after_fault': launchd['entry_prefix'],
                'pstate': '0x0', 'checkpoint_pstate': '0x0',
            },
            'original_launchd_main_input': {
                'sha256': launchd['sha256'], 'entry_offset': entry,
                'entry_prefix': launchd['entry_prefix'], 'mach_header': launchd['mach_header'],
            },
        })
        return inputs, report

    def test_launchd_target_pins_one_backed_arm64_lc_main(self):
        data = self.synthetic_launchd()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'launchd'
            path.write_bytes(data)
            expected = hashlib.sha256(data).hexdigest()
            with patch.object(module, 'LAUNCHD_SHA256', expected):
                launchd = module.validate_launchd_target(path)
                self.assertEqual(launchd['entry_offset'], 64)
                self.assertEqual(launchd['entry_prefix'], data[64:80].hex())
                self.assertEqual(launchd['mach_header'], data[:32].hex())
                path.write_bytes(data + b'tampered')
                with self.assertRaisesRegex(ValueError, 'pinned original'):
                    module.validate_launchd_target(path)
                with self.assertRaisesRegex(ValueError, 'changed during'):
                    module.verify_launchd_target(launchd)

    def test_launchd_summary_requires_real_lc_main_owner_root_and_trust(self):
        data = self.synthetic_launchd()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'launchd'
            path.write_bytes(data)
            with patch.object(module, 'LAUNCHD_SHA256', hashlib.sha256(data).hexdigest()):
                launchd = module.validate_launchd_target(path)
            inputs, report = self.launchd_report_fixture(launchd)
            summary = module.launchd_summary(report, inputs, launchd, '7' * 64, path, path)
            self.assertTrue(summary['launchd_exec_observed'])
            self.assertTrue(summary['trust_cache_static_loaded'])
            self.assertFalse(summary['data_volume_attached'])
            for field in ('apple_hardware_root_trust_demonstrated', 'apple_userspace_boot_demonstrated',
                          'functional_ipados_boot_demonstrated', 'springboard_display_demonstrated',
                          'hardware_boot_demonstrated', 'device_flashable'):
                self.assertFalse(summary[field])
            mutations = [
                (('stop_symbol',), '_panic'), (('original_launchd_main_executed',), False),
                (('followed_main_entry', 'first_instruction_completed'), False),
                (('followed_main_entry', 'entry_bytes_after_fault'), '00' * 16),
                (('followed_main_entry', 'pstate'), '0x5'),
                (('requested_kernel_symbols', '_trust_cache_static_loaded'), '00'),
                (('owner_root_binding_audit', '_native_owner_root_accepted'), 0),
                (('root_selection', 'root_authentication_bypassed'), True),
                (('trace_events',), []), (('virtio_block_input', 'read_only'), False),
                (('trust_cache_bundle_sha256',), '0' * 64),
            ]
            for keys, value in mutations:
                actual = copy.deepcopy(report)
                parent = actual
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                with self.subTest(keys=keys), self.assertRaises(ValueError):
                    module.validate_launchd_report(actual, inputs, launchd)

    def test_launchd_probe_uses_only_supported_options_and_no_data_fixture(self):
        generic = ROOT.parent / 'UniXNU/scripts/probe-darwinboot-xnu.py'
        if not generic.is_file():
            self.skipTest('generic UniXNU checkout is not available')
        declared = set()
        for node in ast.walk(ast.parse(generic.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument':
                declared.update(arg.value for arg in node.args if isinstance(arg, ast.Constant) and
                                isinstance(arg.value, str) and arg.value.startswith('--'))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / 'launchd-run'

            def run(command, **kwargs):
                (output / 'gdb-stop.json').write_text('{}')
                return subprocess.CompletedProcess(command, 0, 'synthetic launchd observer fixture')

            with patch.object(module.subprocess, 'run', side_effect=run) as execute:
                module.run_launchd_probe(generic.parents[1], base / 'kernel', base / 'prelink', base / 'trust',
                                         base / 'os.dmg', base / 'systemos.dmg', base / 'efi', base / 'fdt',
                                         base / 'launchd', output)
            command = execute.call_args.args[0]
            self.assertEqual(execute.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
            options = {value for value in command if value.startswith('--')}
            self.assertFalse(options - declared, options - declared)
            for option in ('--follow-target-main', '--target-main-executable', '--inspect-owner-root',
                           '--check-commpage', '--qemu-vmm-present', '--trust-cache'):
                self.assertIn(option, command)
            self.assertIn('_trust_cache_static_loaded:1', command)
            self.assertIn('--log-syscalls-after', command)
            self.assertEqual(command[command.index('--log-syscalls-after') + 1], '_load_init_program')
            self.assertNotIn('--log-kernel-strings-after', command)
            traced = [command[index + 1] for index, value in enumerate(command[:-1]) if value == '--trace-symbol']
            self.assertNotIn('_load_init_program', traced)
            for option in ('--finish-function', '--owner-root-accepted', '--native-user',
                           '--native-air-init', '--data-ramdisk'):
                self.assertNotIn(option, command)
            self.assertTrue((output / 'probe-command.json').is_file())

    def test_workflow_has_readonly_launchd_source_and_bounded_not_for_device_evidence(self):
        workflow = (ROOT / '.github/workflows/dali-xnu.yml').read_text()
        launchd = workflow.split('- name: Probe original launchd LC_MAIN in QEMU', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('hdiutil attach -readonly', launchd)
        self.assertIn('hdiutil detach', launchd)
        self.assertIn('--follow-launchd', launchd)
        self.assertIn('--target-launchd', launchd)
        self.assertNotIn('--data-ramdisk', launchd)
        artifact = workflow.split('- name: Upload bounded QEMU launchd evidence', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('dali-ipados-qemu-launchd-not-for-device', artifact)
        self.assertIn('launchd-summary.json', artifact)
        self.assertNotIn('dali-ipados-target-launchd', artifact)


if __name__ == '__main__':
    unittest.main()
