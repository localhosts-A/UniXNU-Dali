import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location('storage_qemu', Path(__file__).resolve().parents[1] / 'scripts/test-source-storage-qemu.py')
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)


class StorageEvidenceTests(unittest.TestCase):
    def report(self, mode=1):
        return {'kernel_sha256': 'a' * 64, 'kernel_arguments': 'case-args',
                'bounded_timeout_interrupted': False, 'native_4k_mmu_observed': True,
                'crypto14_initialization_observed': True, 'stop_symbol': '_source_storage_test_checkpoint',
                'pc': '0x1234000', 'function_return_target': '0x1234000',
                'x0': hex((0x31534600 << 32) | (mode << 16) | (0x7f if mode == 1 else 0xf))}

    def msdosfs_report(self):
        return {'kernel_sha256': 'a' * 64, 'kernel_arguments': 'rd=md0 case-args',
                'bounded_timeout_interrupted': False, 'native_4k_mmu_observed': True,
                'crypto14_initialization_observed': True, 'stop_symbol': '_source_msdosfs_test_checkpoint',
                'pc': '0x1234000', 'function_return_target': '0x1234000',
                'x0': hex((0x4d534446 << 32) | 0x3f)}

    def test_both_real_interface_cases_require_all_completion_bits(self):
        for mode, bits in ((1, 7), (2, 4)):
            report = self.report(mode)
            storage.validate(report, 'a' * 64, 'case-args', mode)
            for bit in range(bits):
                failed = dict(report, x0=hex(int(report['x0'], 16) & ~(1 << bit)))
                with self.subTest(mode=mode, missing=bit), self.assertRaises(ValueError):
                    storage.validate(failed, 'a' * 64, 'case-args', mode)

    def test_wrong_kernel_case_panic_timeout_or_unreturned_checkpoint_is_rejected(self):
        changes = [{'kernel_sha256': 'b' * 64}, {'kernel_arguments': 'other'},
                   {'bounded_timeout_interrupted': True}, {'native_4k_mmu_observed': False},
                   {'crypto14_initialization_observed': False}, {'stop_symbol': '_panic'},
                   {'pc': '0x100'}, {'pc': None, 'function_return_target': None},
                   {'x0': self.report(2)['x0']}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                storage.validate(dict(self.report(), **change), 'a' * 64, 'case-args', 1)

    def test_production_msdosfs_gate_requires_mount_read_and_readonly_bits(self):
        report = self.msdosfs_report()
        storage.validate_msdosfs_mount(report, 'a' * 64, 'rd=md0 case-args')
        for bit in range(6):
            failed = dict(report, x0=hex(int(report['x0'], 16) & ~(1 << bit)))
            with self.subTest(missing=bit), self.assertRaises(ValueError):
                storage.validate_msdosfs_mount(failed, 'a' * 64, 'rd=md0 case-args')

    def test_msdosfs_gate_rejects_wrong_kernel_bootargs_or_stop_site(self):
        report = self.msdosfs_report()
        changes = [{'kernel_sha256': 'b' * 64}, {'kernel_arguments': 'other'},
                   {'bounded_timeout_interrupted': True}, {'native_4k_mmu_observed': False},
                   {'crypto14_initialization_observed': False}, {'stop_symbol': '_panic'},
                   {'pc': '0x100'}, {'pc': None, 'function_return_target': None}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                storage.validate_msdosfs_mount(dict(report, **change), 'a' * 64, 'rd=md0 case-args')

    def test_fat32_ram_result_is_reported_separately_from_usb_qualification(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts/test-source-storage-qemu.py').read_text()
        self.assertIn("'production_msdosfs_fat32_ram_runtime_pass': args.msdosfs_mount_test", script)
        self.assertIn("'usb_host_storage_path_qualified': False", script)
        self.assertIn("'media_source': 'volatile SourceStorageFixture RAM'", script)

    def test_macos_usb_workflow_runs_the_production_msdosfs_qemu_gate(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/dali-usb-xnu.yml').read_text()
        self.assertIn('build-source-msdosfs.py" --verify-kernel', workflow)
        self.assertIn('Test production XNU msdosfs FAT32 mount and read in QEMU', workflow)
        self.assertIn('--msdosfs-mount-test', workflow)


if __name__ == '__main__':
    unittest.main()
