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
    STORAGE_SYMBOL = '__ZN7IOMedia13attachToChildEP15IORegistryEntryPK15IORegistryPlane'
    STORAGE_SYNCHRONIZE_SYMBOL = '__ZN7IOMedia11synchronizeEP9IOServiceyyj'
    BLOCK_STORAGE_PROPERTY_SYMBOL = '__ZNK20IOBlockStorageDevice11getPropertyEPK8OSSymbol'
    IOMEDIA_D2_SYMBOL = '__ZN7IOMediaD2Ev'
    PARTITION_SCHEME_D2_SYMBOL = '__ZN17IOPartitionSchemeD2Ev'

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
                "original_sha256": "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7",
                "instruction_relocation_complete": False,
                "abi_qualified": False,
                "runtime_qualified": False,
                "loadable": False,
                "source_interfaces": ["com.apple.iokit.IOStorageFamily"],
                "source_interface_bindings": [
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff00596ee88,
                        "symbol": self.BLOCK_STORAGE_PROPERTY_SYMBOL,
                        "native_va": 0xfffffff00bf94be4,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff005975f0c,
                        "symbol": self.IOMEDIA_D2_SYMBOL,
                        "native_va": 0xfffffff00bf9c368,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff0059762a8,
                        "symbol": self.STORAGE_SYMBOL,
                        "native_va": 0xfffffff00bf9c704,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff005976eb0,
                        "symbol": self.STORAGE_SYNCHRONIZE_SYMBOL,
                        "native_va": 0xfffffff00bf9d3a8,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff00597c344,
                        "symbol": self.PARTITION_SCHEME_D2_SYMBOL,
                        "native_va": 0xfffffff00bfa2e38,
                        "reference_count": 1,
                    },
                ],
                "bindings": [
                    {
                        "target": 0xfffffff0059762a8,
                        "new_target": 0xfffffff00bf9c704,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff005976eb0,
                        "new_target": 0xfffffff00bf9d3a8,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff00596ee88,
                        "new_target": 0xfffffff00bf94be4,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff005975f0c,
                        "new_target": 0xfffffff00bf9c368,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff00597c344,
                        "new_target": 0xfffffff00bfa2e38,
                        "kind": "source_interface",
                    },
                ],
                "codeless_components": ["com.apple.iokit.IOStorageFamily"],
                "components": [],
                "source_interface_transform": {
                    "version": 1,
                    "original_kernelcache_sha256": "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7",
                    "rewritten_prelink_input_sha256": "1" * 64,
                    "entries": {"com.apple.iokit.IOStorageFamily": {
                        "dependencies": ["com.apple.kpi.iokit"],
                        "personalities": ["StorageFixture"],
                        "segments": [{"name": "__TEXT_EXEC", "va": 0xfffffff00596c000, "size": 0x14358}],
                    }},
                },
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
                "original_sha256": "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7",
                "instruction_relocation_complete": False,
                "abi_qualified": False,
                "runtime_qualified": False,
                "loadable": False,
                "source_interfaces": ["com.apple.iokit.IOStorageFamily"],
                "source_interface_bindings": [
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff00596ee88,
                        "symbol": self.BLOCK_STORAGE_PROPERTY_SYMBOL,
                        "native_va": 0xfffffff00bf94be4,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff005975f0c,
                        "symbol": self.IOMEDIA_D2_SYMBOL,
                        "native_va": 0xfffffff00bf9c368,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff0059762a8,
                        "symbol": self.STORAGE_SYMBOL,
                        "native_va": 0xfffffff00bf9c704,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff005976eb0,
                        "symbol": self.STORAGE_SYNCHRONIZE_SYMBOL,
                        "native_va": 0xfffffff00bf9d3a8,
                        "reference_count": 1,
                    },
                    {
                        "bundle": "com.apple.iokit.IOStorageFamily",
                        "donor_va": 0xfffffff00597c344,
                        "symbol": self.PARTITION_SCHEME_D2_SYMBOL,
                        "native_va": 0xfffffff00bfa2e38,
                        "reference_count": 1,
                    },
                ],
                "bindings": [
                    {
                        "target": 0xfffffff0059762a8,
                        "new_target": 0xfffffff00bf9c704,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff005976eb0,
                        "new_target": 0xfffffff00bf9d3a8,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff00596ee88,
                        "new_target": 0xfffffff00bf94be4,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff005975f0c,
                        "new_target": 0xfffffff00bf9c368,
                        "kind": "source_interface",
                    },
                    {
                        "target": 0xfffffff00597c344,
                        "new_target": 0xfffffff00bfa2e38,
                        "kind": "source_interface",
                    },
                ],
                "codeless_components": ["com.apple.iokit.IOStorageFamily"],
                "components": [],
                "source_interface_transform": {
                    "version": 1,
                    "original_kernelcache_sha256": "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7",
                    "rewritten_prelink_input_sha256": "1" * 64,
                    "entries": {"com.apple.iokit.IOStorageFamily": {
                        "dependencies": ["com.apple.kpi.iokit"],
                        "personalities": ["StorageFixture"],
                        "segments": [{"name": "__TEXT_EXEC", "va": 0xfffffff00596c000, "size": 0x14358}],
                    }},
                },
            }
            report.write_text(json.dumps({**base, "candidate_sha256": "0" * 64}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interfaces": []}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interface_bindings": []}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            bad_mapping = [{**base["source_interface_bindings"][0], "native_va": 0}]
            report.write_text(json.dumps({**base, "source_interface_bindings": bad_mapping}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            bad_bindings = [{**base["bindings"][0], "new_target": 0xfffffff00bf9c708}]
            report.write_text(json.dumps({**base, "bindings": bad_bindings}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            contradictory = [base["bindings"][0], {
                **base["bindings"][0], "new_target": 0xfffffff00bf9c708,
            }, base["bindings"][1]]
            report.write_text(json.dumps({**base, "bindings": contradictory}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interface_bindings": [{
                **base["source_interface_bindings"][0], "reference_count": True,
            }]}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interface_bindings": [
                *base["source_interface_bindings"], base["source_interface_bindings"][0],
            ]}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "loadable": True}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interface_transform": {
                **base["source_interface_transform"], "rewritten_prelink_input_sha256": "invalid",
            }}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)


if __name__ == "__main__":
    unittest.main()
