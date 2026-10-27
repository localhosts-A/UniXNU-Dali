import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dali_qemu", ROOT / "scripts/test-dali-xnu-qemu.py")
DALI_QEMU = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DALI_QEMU)


class PrelinkQemuTests(unittest.TestCase):
    def test_prelink_qemu_captures_security_kext_panic_path(self):
        source = (ROOT / "scripts/test-dali-xnu-qemu.py").read_text()

        self.assertIn('"--prelink-report", args.prelink_report.resolve()', source)
        self.assertIn('"--kext-logs"', source)
        self.assertIn('"--trace-symbol", "_register_and_init_prng"', source)
        for symbol in ("_OSRuntimeInitializeCPP", "__ZN11OSMetaClass11postModLoadEPv",
                       "__ZN6OSKext5startEb", "__ZN12KLDBootstrap22loadSecurityExtensionsEv"):
            self.assertIn(f'"--trace-symbol", "{symbol}"', source)
        self.assertIn("validate_prelink_audit_candidate(kernel, report_path)", source)

    def test_prelink_requires_the_target_crypto_table_owner(self):
        valid = {
            "crypto14_initialization_observed": True,
            "original_crypto_table_owner_observed": True,
            "original_crypto_initialized_observed": True,
        }
        DALI_QEMU.validate_corecrypto_state(valid, True)

        for field in ("original_crypto_table_owner_observed", "original_crypto_initialized_observed"):
            invalid = {**valid, field: False}
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_corecrypto_state(invalid, True)

        with self.assertRaises(SystemExit):
            DALI_QEMU.validate_corecrypto_state({**valid, "crypto14_initialization_observed": False}, False)

    def test_candidate_must_match_report_and_stay_explicitly_non_loadable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = root / "kernel.macho"
            kernel.write_bytes(b"exact diagnostic candidate")
            report = root / "kernel.json"
            report.write_text(json.dumps({
                "candidate_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
                "instruction_relocation_complete": False,
                "abi_qualified": False,
                "runtime_qualified": False,
                "loadable": False,
            }))

            parsed = DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            self.assertIs(parsed["loadable"], False)

    def test_candidate_rejects_hash_mismatch_and_any_loadability_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = root / "kernel.macho"
            kernel.write_bytes(b"candidate")
            report = root / "kernel.json"
            base = {
                "candidate_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
                "instruction_relocation_complete": False,
                "abi_qualified": False,
                "runtime_qualified": False,
                "loadable": False,
            }
            report.write_text(json.dumps({**base, "candidate_sha256": "0" * 64}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "loadable": True}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)


if __name__ == "__main__":
    unittest.main()
