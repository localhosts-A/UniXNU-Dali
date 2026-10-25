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


if __name__ == '__main__':
    unittest.main()
