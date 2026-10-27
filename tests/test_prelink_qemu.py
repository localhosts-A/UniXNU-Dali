import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dali_qemu", ROOT / "scripts/test-dali-xnu-qemu.py")
DALI_QEMU = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DALI_QEMU)


class PrelinkQemuTests(unittest.TestCase):
    STORAGE_ID = "com.apple.iokit.IOStorageFamily"
    KERNELCACHE_SHA256 = "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7"

    @classmethod
    def setUpClass(cls):
        cls.board = tomllib.loads((ROOT / "board.toml").read_text())

    @classmethod
    def make_report(cls, kernel):
        prelink = cls.board["kernel"]["prelink"]
        mappings = []
        pointer_bindings = []
        for index, binding in enumerate(prelink["source_interface_bindings"]):
            donor_va = int(binding["donor_va"], 0)
            target_kind = binding.get("target_kind", "code")
            native_va = 0xfffffff00b000000 + index * 0x100
            record = {
                "bundle": binding["bundle"], "donor_va": donor_va,
                "symbol": binding["symbol"], "target_kind": target_kind,
                "native_va": native_va, "reference_count": 1,
            }
            if target_kind == "data_const":
                record.update({
                    "donor_segment": "__DATA_CONST", "donor_section": "__const",
                    "donor_initprot": 1, "donor_maxprot": 1,
                    "native_segment": "__DATA_CONST", "native_section": "__const",
                    "native_initprot": 1, "native_maxprot": 1,
                })
            elif target_kind == "data_rw":
                record.update({
                    "donor_segment": "__DATA", "donor_section": "__common",
                    "donor_initprot": 3, "donor_maxprot": 3,
                    "native_segment": "__DATA", "native_section": "__common",
                    "native_initprot": 3, "native_maxprot": 3,
                })
            mappings.append(record)
            pointer_bindings.append({
                "target": donor_va, "new_target": native_va, "kind": "source_interface",
            })

        segments = [
            {"name": "__TEXT_EXEC", "va": 0xfffffff00596c000, "size": 0x14358,
             "initprot": 5, "maxprot": 5, "sections": [{"name": "__text", "va": 0xfffffff00596c000, "size": 0x13d58}]},
            {"name": "__DATA_CONST", "va": 0xfffffff006cb3200, "size": 0x5c10,
             "initprot": 1, "maxprot": 1, "sections": [{"name": "__const", "va": 0xfffffff006cb3780, "size": 0x4c70}]},
            {"name": "__DATA", "va": 0xfffffff007add000, "size": 0x3b1,
             "initprot": 3, "maxprot": 3,
             "sections": [{"name": "__data", "va": 0xfffffff007add000, "size": 0x170},
                          {"name": "__common", "va": 0xfffffff007add170, "size": 0x218}]},
        ]
        return {
            "candidate_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
            "original_sha256": cls.KERNELCACHE_SHA256,
            "instruction_relocation_complete": False,
            "abi_qualified": False,
            "runtime_qualified": False,
            "loadable": False,
            "source_interfaces": [cls.STORAGE_ID],
            "source_interface_bindings": mappings,
            "bindings": pointer_bindings,
            "codeless_components": [cls.STORAGE_ID],
            "components": [],
            "source_interface_transform": {
                "version": 2,
                "original_kernelcache_sha256": cls.KERNELCACHE_SHA256,
                "rewritten_prelink_input_sha256": "1" * 64,
                "entries": {cls.STORAGE_ID: {
                    "dependencies": ["com.apple.kpi.iokit"],
                    "personalities": ["StorageFixture"],
                    "segments": segments,
                }},
            },
        }

    def test_prelink_qemu_captures_security_kext_panic_path(self):
        source = (ROOT / "scripts/test-dali-xnu-qemu.py").read_text()

        self.assertEqual(DALI_QEMU.IMAGE4_CRYPTO_HASH_METHOD, "sha2-384")
        self.assertIn('["--crypto-hash-method", IMAGE4_CRYPTO_HASH_METHOD,', source)
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

    def test_candidate_report_covers_all_code_and_typed_data_mappings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = root / "kernel.macho"
            kernel.write_bytes(b"exact diagnostic candidate")
            report = root / "kernel.json"
            report.write_text(json.dumps(self.make_report(kernel)))

            parsed = DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            self.assertIs(parsed["loadable"], False)
            records = parsed["source_interface_bindings"]
            self.assertEqual(len(records), 84)
            self.assertEqual(sum(item["target_kind"] == "code" for item in records), 73)
            self.assertEqual(sum(item["target_kind"] == "data_const" for item in records), 6)
            self.assertEqual(sum(item["target_kind"] == "data_rw" for item in records), 5)

    def test_candidate_rejects_hash_mapping_and_loadability_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = root / "kernel.macho"
            kernel.write_bytes(b"candidate")
            report = root / "kernel.json"
            base = self.make_report(kernel)

            report.write_text(json.dumps({**base, "candidate_sha256": "0" * 64}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interfaces": []}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "source_interface_bindings": []}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            bad_mapping = [dict(item) for item in base["source_interface_bindings"]]
            bad_mapping[0]["native_va"] = 0
            report.write_text(json.dumps({**base, "source_interface_bindings": bad_mapping}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            data_mapping = [dict(item) for item in base["source_interface_bindings"]]
            data_record = next(item for item in data_mapping if item["target_kind"] == "data_const")
            data_record["native_section"] = "__data"
            report.write_text(json.dumps({**base, "source_interface_bindings": data_mapping}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            bad_bindings = [dict(item) for item in base["bindings"]]
            bad_bindings[0]["new_target"] += 8
            report.write_text(json.dumps({**base, "bindings": bad_bindings}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            missing_donor_reference = {**base, "bindings": base["bindings"][:-1]}
            report.write_text(json.dumps(missing_donor_reference))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            contradictory = [dict(base["bindings"][0]), {
                **base["bindings"][0], "new_target": base["bindings"][0]["new_target"] + 8,
            }]
            report.write_text(json.dumps({**base, "bindings": contradictory}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            bad_reference_count = [dict(item) for item in base["source_interface_bindings"]]
            bad_reference_count[0]["reference_count"] = True
            report.write_text(json.dumps({**base, "source_interface_bindings": bad_reference_count}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            duplicate_mapping = [*base["source_interface_bindings"], base["source_interface_bindings"][0]]
            report.write_text(json.dumps({**base, "source_interface_bindings": duplicate_mapping}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            report.write_text(json.dumps({**base, "loadable": True}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)

            bad_transform = {**base["source_interface_transform"], "rewritten_prelink_input_sha256": "invalid"}
            report.write_text(json.dumps({**base, "source_interface_transform": bad_transform}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_audit_candidate(kernel, report)


if __name__ == "__main__":
    unittest.main()
