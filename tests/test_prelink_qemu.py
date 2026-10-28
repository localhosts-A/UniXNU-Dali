import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import tomllib
import unittest
from unittest import mock


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
            "native_sha256": "1" * 64,
            "original_sha256": cls.KERNELCACHE_SHA256,
            "slide": int(cls.board["kernel"]["prelink"]["slide"], 0),
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

    @classmethod
    def make_verification(cls, kernel, report):
        return {
            "schema": 1,
            "verification": "independent-prelink-structure-and-pointer-table",
            "native_sha256": report["native_sha256"],
            "original_sha256": report["original_sha256"],
            "candidate_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
            "slide": report["slide"],
            "component_closure_verified": True,
            "pointer_table_rebindings_verified": len(report["bindings"]),
            "source_interface_bindings_verified": len(report.get("source_interface_bindings", [])),
            "component_headers_and_bytes_verified": len(report["components"]),
            "kmod_and_sparse_index_verified": True,
            "instruction_relocation_complete": False,
            "abi_qualified": False,
            "runtime_qualified": False,
            "loadable": False,
        }

    def test_prelink_qemu_captures_security_kext_panic_path(self):
        source = (ROOT / "scripts/test-dali-xnu-qemu.py").read_text()
        probe_source = (ROOT.parent / "UniXNU/scripts/probe-darwinboot-xnu.py").read_text()

        self.assertEqual(DALI_QEMU.IMAGE4_CRYPTO_HASH_METHOD, "sha2-384")
        self.assertIn('["--crypto-hash-method", IMAGE4_CRYPTO_HASH_METHOD,', source)
        self.assertTrue(DALI_QEMU.AMFI_ONLY_PLATFORM_CODE)
        self.assertIn('"--amfi-platform-only",', source)
        self.assertIn('"--prelink-report", args.prelink_report.resolve()', source)
        self.assertIn("verification_path = args.prelink_verification.resolve()", source)
        self.assertIn('"--trust-cache", args.trust_cache.resolve()', source)
        self.assertIn('if not args.trust_cache:', source)
        self.assertIn('report.get("trust_cache_bundle_sha256") != trust_cache["sha256"]', source)
        self.assertIn('cases["trust-cache"] = "_load_static_trust_cache"', source)
        self.assertIn('validate_trust_cache_runtime(reports["trust-cache"], trust_cache["sha256"])', source)
        self.assertIn('"trust_cache_runtime_consumed": bool(trust_cache_runtime)', source)
        self.assertIn('command += source_storage_metaclass_read_args(kernel)', source)
        self.assertIn('"source_storage_metaclass_observation": (', source)
        self.assertIn('"--kext-logs"', source)
        self.assertIn('parser.add_argument("--qemu-firmware", type=Path', source)
        self.assertIn('common += ["--qemu-firmware", args.qemu_firmware.resolve()]', source)
        self.assertIn("parser.add_argument('--qemu-firmware', type=Path", probe_source)
        self.assertIn("gdb_command += ['--firmware', args.qemu_firmware.resolve()]", probe_source)
        self.assertIn('"--trace-symbol", "_register_and_init_prng"', source)
        for symbol in ("_OSRuntimeInitializeCPP", "__ZN11OSMetaClass11postModLoadEPv",
                       "__ZN6OSKext5startEb", "__ZN12KLDBootstrap22loadSecurityExtensionsEv"):
            self.assertIn(f'"--trace-symbol", "{symbol}"', source)
        with tempfile.TemporaryDirectory() as temp:
            source_kernel = Path(temp) / "kernel.macho"
            source_kernel.write_bytes(b"source kernel")
            nm = "".join(
                f"{0xfffffff00c100000 + index * 0x100:016x} S {symbol}\n"
                for index, (_, symbol, _) in enumerate(DALI_QEMU.SOURCE_STORAGE_METACLASSES))
            with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=nm):
                source_reads = DALI_QEMU.source_storage_metaclass_read_args(source_kernel)
        self.assertEqual(len(source_reads), 30)
        for _, symbol, _ in DALI_QEMU.SOURCE_STORAGE_METACLASSES:
            self.assertIn(f"{symbol}:0x28", source_reads)
        self.assertIn("--read-indirect-optional", source_reads)
        self.assertEqual(len(DALI_QEMU.donor_storage_metaclass_read_args()), 32)
        self.assertIn("--read-memory", DALI_QEMU.donor_storage_metaclass_read_args())
        self.assertIn("--read-kernel-symbol", DALI_QEMU.donor_storage_metaclass_read_args())
        self.assertIn("--read-indirect-optional", DALI_QEMU.donor_storage_metaclass_read_args())
        self.assertIn("parser.add_argument('--read-memory', action='append'", probe_source)
        self.assertIn("gdb_command += ['--read-memory', request]", probe_source)
        self.assertIn("parser.add_argument('--read-indirect-optional', action='append'", probe_source)
        self.assertIn('for value in args.read_indirect_optional:', probe_source)
        self.assertIn("gdb_command += ['--read-indirect-optional', request]", probe_source)
        self.assertIn("expected_donor_runtime_terminal", source)
        self.assertIn('validate_donor_runtime_audit_candidate(', source)
        self.assertIn("validate_prelink_audit_candidate(kernel, report_path)", source)
        self.assertIn("validate_prelink_structure_audit(kernel, prelink_report, verification_path)", source)

    def test_nonloadable_prelink_candidate_skips_source_allocator_negative_control(self):
        self.assertFalse(DALI_QEMU.should_run_default_budget_control({"loadable": False}))
        self.assertTrue(DALI_QEMU.should_run_default_budget_control(None))
        source = (ROOT / "scripts/test-dali-xnu-qemu.py").read_text()
        self.assertIn('"default_kalloc_budget_assertion_reproduced": budget_control["assertion_reproduced"]', source)

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

    def test_independent_structure_audit_binds_exact_candidate_and_keeps_loadability_false(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = root / "kernel.macho"
            kernel.write_bytes(b"exact diagnostic candidate")
            report = self.make_report(kernel)
            verification_path = root / "kernel.verification.json"
            verification = self.make_verification(kernel, report)
            verification_path.write_text(json.dumps(verification))
            checked = DALI_QEMU.validate_prelink_structure_audit(kernel, report, verification_path)
            self.assertEqual(checked["pointer_table_rebindings_verified"], 84)
            self.assertIs(checked["loadable"], False)

            verification_path.write_text(json.dumps({**verification, "candidate_sha256": "0" * 64}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_structure_audit(kernel, report, verification_path)
            verification_path.write_text(json.dumps({**verification, "abi_qualified": True}))
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_prelink_structure_audit(kernel, report, verification_path)

    def make_donor_report(self, kernel, native_kernel):
        return {
            "candidate_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
            "native_sha256": hashlib.sha256(native_kernel.read_bytes()).hexdigest(),
            "original_sha256": self.KERNELCACHE_SHA256,
            "slide": int(self.board["kernel"]["prelink"]["slide"], 0),
            "instruction_relocation_complete": False,
            "abi_qualified": False,
            "runtime_qualified": False,
            "loadable": False,
            "source_interfaces": [],
            "source_interface_bindings": [],
            "bindings": [{"target": 0x1000, "new_target": 0x2000, "kind": "component"}],
            "components": [self.STORAGE_ID],
            "codeless_components": [],
            "source_interface_transform": {
                "version": 2,
                "original_kernelcache_sha256": self.KERNELCACHE_SHA256,
                "rewritten_prelink_input_sha256": self.KERNELCACHE_SHA256,
                "entries": {},
            },
        }

    def test_donor_runtime_candidate_keeps_original_kext_and_excludes_native_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = root / "kernel.macho"
            native = root / "native.macho"
            config = root / "config"
            report_path = root / "kernel.json"
            kernel.write_bytes(b"donor runtime candidate")
            native.write_bytes(b"native kernel without IOStorageFamily")
            config.write_text(
                "# CONFIG_XNU_SOURCE_STORAGE_FAMILY is not set\n"
                "# CONFIG_XNU_VERIFIED_ROOT_PROVIDER is not set\n"
                'CONFIG_XNU_EXTRA_LD_ARCHIVES="-Wl,-force_load,{tree}/build/dali-xnu-platform/libDaliBootLog.a"\n'
            )
            report = self.make_donor_report(kernel, native)
            report_path.write_text(json.dumps(report))
            symbol_output = f"{0xfffffff00c147638:016x} S {DALI_QEMU.IO_SERVICE_METACLASS_SYMBOL}\n"
            with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=symbol_output):
                parsed = DALI_QEMU.validate_donor_runtime_audit_candidate(
                    kernel, report_path, native, config)
            self.assertIs(parsed["loadable"], False)

            verification_path = root / "kernel.verification.json"
            verification_path.write_text(json.dumps(self.make_verification(kernel, report)))
            checked = DALI_QEMU.validate_prelink_structure_audit(
                kernel, report, verification_path, expected_map_count=0)
            self.assertEqual(checked["source_interface_bindings_verified"], 0)
            self.assertIs(checked["loadable"], False)

            for bad_report in (
                {**report, "codeless_components": [self.STORAGE_ID]},
                {**report, "source_interfaces": [self.STORAGE_ID]},
                {**report, "abi_qualified": True},
                {**report, "loadable": True},
            ):
                report_path.write_text(json.dumps(bad_report))
                with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=symbol_output), \
                        self.assertRaises(SystemExit):
                    DALI_QEMU.validate_donor_runtime_audit_candidate(kernel, report_path, native, config)

            config.write_text(config.read_text().replace("# CONFIG_XNU_SOURCE_STORAGE_FAMILY is not set",
                                                       "CONFIG_XNU_SOURCE_STORAGE_FAMILY=y"))
            report_path.write_text(json.dumps(report))
            with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=symbol_output), \
                    self.assertRaises(SystemExit):
                DALI_QEMU.validate_donor_runtime_audit_candidate(kernel, report_path, native, config)

    def test_donor_runtime_metaclass_capture_checks_size_parent_and_pointers_only(self):
        service_address = 0xfffffff00c147638
        requests = DALI_QEMU.donor_storage_metaclass_requests()
        memory = {}
        indirect_memory = {}
        for index, (class_name, symbol, class_size) in enumerate(DALI_QEMU.SOURCE_STORAGE_METACLASSES, start=1):
            raw = bytearray(DALI_QEMU.OSMETA_CLASS_OBJECT_BYTES)
            struct.pack_into("<Q", raw, 0x00, 0xfffffff007000000 + index * 0x100)
            if DALI_QEMU.DONOR_STORAGE_PARENTS[class_name] == DALI_QEMU.IO_SERVICE_METACLASS_SYMBOL:
                parent = service_address
            else:
                parent = requests["IOStorage"]["runtime_va"]
            struct.pack_into("<Q", raw, 0x10, parent)
            struct.pack_into("<Q", raw, 0x18, 0xfffffff007100000 + index * 0x100)
            struct.pack_into("<I", raw, 0x20, class_size)
            memory[requests[class_name]["request"]] = raw.hex()
            indirect_memory[requests[class_name]["class_name_metadata_request"]] = {
                "pointer": hex(0xfffffff007100000 + index * 0x100),
                "address": hex(0xfffffff007100010 + index * 0x100),
                "bytes": struct.pack("<QQ", 0x00040020, 0xfffffff005800000 + index * 0x100).hex(),
            }
            indirect_memory[requests[class_name]["vtable_prefix_request"]] = {
                "pointer": hex(0xfffffff005800000 + index * 0x100),
                "address": hex(0xfffffff005800000 + index * 0x100),
                "bytes": struct.pack("<8Q", *[0xfffffff004000000 + index * 0x100 + slot * 8
                                                for slot in range(8)]).hex(),
            }
        service = bytearray(DALI_QEMU.OSMETA_CLASS_OBJECT_BYTES)
        memory_raw = {"memory": memory, "requested_indirect_memory": indirect_memory,
                      "requested_kernel_symbols": {
            DALI_QEMU.IO_SERVICE_METACLASS_SYMBOL: service.hex(),
        }}
        nm = f"{service_address:016x} S {DALI_QEMU.IO_SERVICE_METACLASS_SYMBOL}\n"
        with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=nm):
            observation = DALI_QEMU.inspect_donor_storage_metaclasses(memory_raw, Path("native.macho"))
        self.assertTrue(observation["capture_complete"])
        self.assertTrue(observation["all_class_sizes_match_expected"])
        self.assertTrue(observation["all_required_pointers_nonzero"])
        self.assertTrue(observation["all_superclasses_match_expected"])
        self.assertTrue(observation["all_class_name_metadata_reads_available"])
        self.assertTrue(observation["all_metaclass_vtable_prefix_reads_available"])
        self.assertTrue(observation["source_io_service_metaclass_matches_native_symbol"])
        first_record = observation["classes"][DALI_QEMU.SOURCE_STORAGE_METACLASSES[0][0]]
        self.assertEqual(len(first_record["metaclass_vtable_prefix"]["entries"]), 8)
        self.assertIn("not authenticated or dereferenced", first_record["scope"])
        self.assertFalse(observation["abi_qualified"])
        self.assertFalse(observation["loadable"])

        first = DALI_QEMU.SOURCE_STORAGE_METACLASSES[0][0]
        damaged = dict(memory)
        raw = bytearray.fromhex(damaged[requests[first]["request"]])
        struct.pack_into("<I", raw, 0x20, 0xDEAD)
        damaged[requests[first]["request"]] = raw.hex()
        with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=nm):
            mismatch = DALI_QEMU.inspect_donor_storage_metaclasses(
                {"memory": damaged, "requested_kernel_symbols": memory_raw["requested_kernel_symbols"]},
                Path("native.macho"))
        self.assertFalse(mismatch["all_class_sizes_match_expected"])
        self.assertFalse(mismatch["abi_qualified"])

    def test_source_runtime_metaclass_capture_keeps_auxiliary_reads_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            kernel = Path(temp) / "kernel.macho"
            kernel.write_bytes(b"candidate")
            nm = "".join(
                f"{0xfffffff00c100000 + index * 0x100:016x} S {symbol}\n"
                for index, (_, symbol, _) in enumerate(DALI_QEMU.SOURCE_STORAGE_METACLASSES))
            with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=nm):
                requests = DALI_QEMU.source_storage_metaclass_requests(kernel)
                symbols = {}
                indirect = {}
                for index, (class_name, symbol, class_size) in enumerate(
                        DALI_QEMU.SOURCE_STORAGE_METACLASSES, start=1):
                    raw = bytearray(DALI_QEMU.OSMETA_CLASS_OBJECT_BYTES)
                    struct.pack_into("<Q", raw, 0x00, 0xfffffff007200000 + index * 0x100)
                    struct.pack_into("<Q", raw, 0x10, 0xfffffff00c300000 + index * 0x100)
                    struct.pack_into("<Q", raw, 0x18, 0xfffffff007300000 + index * 0x100)
                    struct.pack_into("<I", raw, 0x20, class_size)
                    symbols[symbol] = raw.hex()
                    indirect[requests[class_name]["class_name_metadata_request"]] = {
                        "bytes": struct.pack("<QQ", 0x00040020, 0xfffffff007400000 + index * 0x100).hex(),
                    }
                    indirect[requests[class_name]["vtable_prefix_request"]] = {
                        "bytes": struct.pack("<8Q", *[0xfffffff004100000 + index * 0x100 + slot * 8
                                                        for slot in range(8)]).hex(),
                    }
                observation = DALI_QEMU.inspect_source_storage_metaclasses({
                    "requested_kernel_symbols": symbols,
                    "requested_indirect_memory": indirect,
                }, kernel)
            self.assertTrue(observation["capture_complete"])
            self.assertTrue(observation["all_class_sizes_match_source_layout"])
            self.assertTrue(observation["all_class_name_metadata_reads_available"])
            self.assertTrue(observation["all_metaclass_vtable_prefix_reads_available"])
            self.assertFalse(observation["abi_qualified"])
            self.assertFalse(observation["loadable"])

    def test_expected_donor_runtime_stop_is_limited_to_the_known_amfi_panic(self):
        report = {"stop_symbol": "_panic", "panic_caller_component": {
            "module": "com.apple.driver.AppleMobileFileIntegrity"}}
        self.assertTrue(DALI_QEMU.expected_donor_runtime_terminal(report, "AMFI: No PMGR?"))
        self.assertFalse(DALI_QEMU.expected_donor_runtime_terminal(report, "unrelated AMFI panic"))
        self.assertFalse(DALI_QEMU.expected_donor_runtime_terminal(
            {**report, "stop_symbol": "_Assert"}, "AMFI: No PMGR?"))

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

    def test_qemu_trust_cache_input_must_be_pinned_and_well_formed(self):
        module = (struct.pack("<I16sI", 2, bytes(range(16)), 1) +
                  bytes([0x42]) * 20 + bytes([2, 0, 0, 0]))
        bundle = struct.pack("<3I", 2, 12, 12 + len(module)) + module + module
        digest = hashlib.sha256(bundle).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trust-cache.bundle"
            path.write_bytes(bundle)
            report = DALI_QEMU.validate_trust_cache(path, [digest])
            self.assertEqual(report["sha256"], digest)
            self.assertEqual(len(report["modules"]), 2)
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_trust_cache(path, ["0" * 64])
            path.write_bytes(b"invalid trust cache")
            with self.assertRaises(SystemExit):
                DALI_QEMU.validate_trust_cache(path, [hashlib.sha256(path.read_bytes()).hexdigest()])

    def test_runtime_checkpoint_requires_xnu_to_load_the_exact_adt_cache(self):
        digest = "a" * 64
        valid = {
            "trust_cache_bundle_sha256": digest,
            "requested_kernel_symbols": {"_trust_cache_static_loaded": "01"},
            "trace_events": [
                {"symbol": "_trust_cache_runtime_init"},
                {"symbol": "_load_static_trust_cache"},
            ],
        }
        result = DALI_QEMU.validate_trust_cache_runtime(valid, digest)
        self.assertTrue(result["adt_handoff_verified"])
        self.assertTrue(result["runtime_consumed"])
        self.assertEqual(result["static_loaded_flag"], 1)

        for invalid in (
            {**valid, "trust_cache_bundle_sha256": "0" * 64},
            {**valid, "requested_kernel_symbols": {"_trust_cache_static_loaded": "00"}},
            {**valid, "trace_events": [{"symbol": "_load_static_trust_cache"}]},
            {**valid, "trace_events": [
                {"symbol": "_load_static_trust_cache"},
                {"symbol": "_trust_cache_runtime_init"},
            ]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                DALI_QEMU.validate_trust_cache_runtime(invalid, digest)

    def test_source_storage_metaclass_snapshot_decodes_layout_but_never_qualifies_abi(self):
        raw_objects = {}
        for index, (_, symbol, class_size) in enumerate(DALI_QEMU.SOURCE_STORAGE_METACLASSES, start=1):
            data = bytearray(DALI_QEMU.OSMETA_CLASS_OBJECT_BYTES)
            struct.pack_into("<Q", data, 0x00, 0xfffffff00b000000 + index * 0x100)
            struct.pack_into("<Q", data, 0x10, 0xfffffff00b001000 + index * 0x100)
            struct.pack_into("<Q", data, 0x18, 0xfffffff00b002000 + index * 0x100)
            struct.pack_into("<I", data, 0x20, class_size)
            raw_objects[symbol] = data.hex()

        nm = "".join(
            f"{0xfffffff00c100000 + index * 0x100:016x} S {symbol}\n"
            for index, (_, symbol, _) in enumerate(DALI_QEMU.SOURCE_STORAGE_METACLASSES))
        with mock.patch.object(DALI_QEMU.subprocess, "check_output", return_value=nm):
            observation = DALI_QEMU.inspect_source_storage_metaclasses(
                {"requested_kernel_symbols": raw_objects}, Path("kernel.macho"))
            self.assertTrue(observation["capture_complete"])
            self.assertTrue(observation["all_class_sizes_match_source_layout"])
            self.assertTrue(observation["all_required_pointers_nonzero"])
            self.assertFalse(observation["abi_qualified"])
            self.assertFalse(observation["loadable"])

            wrong_size = dict(raw_objects)
            first_symbol = DALI_QEMU.SOURCE_STORAGE_METACLASSES[0][1]
            first = bytearray.fromhex(wrong_size[first_symbol])
            struct.pack_into("<I", first, 0x20, 0xDEAD)
            wrong_size[first_symbol] = first.hex()
            mismatch = DALI_QEMU.inspect_source_storage_metaclasses(
                {"requested_kernel_symbols": wrong_size}, Path("kernel.macho"))
            self.assertTrue(mismatch["capture_complete"])
            self.assertFalse(mismatch["all_class_sizes_match_source_layout"])
            self.assertFalse(mismatch["abi_qualified"])

            incomplete = DALI_QEMU.inspect_source_storage_metaclasses(
                {"requested_kernel_symbols": {}}, Path("kernel.macho"))
            self.assertFalse(incomplete["capture_complete"])
            self.assertFalse(incomplete["all_class_sizes_match_source_layout"])
            self.assertEqual(len(incomplete["errors"]), 5)

    def test_qemu_timeout_error_uses_the_bounded_stop_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "gdb-stop.json"
            log_path = root / "cpu.log"
            report_path.write_text(json.dumps({
                "bounded_timeout_interrupted": True,
                "pc": "0x7fd65e04",
                "qemu_firmware": {"code_sha256": "a" * 64},
                "system_registers": {
                    "ELR_EL1": "0x7ca63f74",
                    "TTBR0_EL1": "0x47fff000",
                    "TTBR1_EL1": "0x0",
                },
            }))
            (root / "gdb-uart.log").write_text("\x1b[2JUEFI boot\nSynchronous Exception at 0x45a64548\n")

            message = DALI_QEMU.format_qemu_probe_failure(
                "cpu", "FAIL: active translation bases are missing", report_path, log_path)

            self.assertIn("timed out before reaching its requested XNU checkpoint", message)
            self.assertIn("PC=0x7fd65e04", message)
            self.assertIn("TTBR1_EL1=0x0", message)
            self.assertIn("firmware_code_sha256=" + "a" * 64, message)
            self.assertIn("Synchronous Exception at 0x45a64548", message)
            self.assertNotIn("\x1b[", message)
            self.assertNotIn("active translation bases are missing", message)

    def test_qemu_failure_without_timeout_keeps_probe_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "missing-gdb-stop.json"
            log_path = root / "cpu.log"
            message = DALI_QEMU.format_qemu_probe_failure(
                "cpu", "FAIL: assertion in guest", report_path, log_path)

            self.assertIn("QEMU probe cpu failed", message)
            self.assertIn("FAIL: assertion in guest", message)
            self.assertIn(str(log_path), message)

    def test_qemu_firmware_and_uart_diagnostics_are_retained_by_the_runner(self):
        source = (ROOT / "scripts/test-dali-xnu-qemu.py").read_text()
        qemu_entry = (ROOT.parent / "UniXNU/scripts/qemu-entry/gdb-stop.py").read_text()
        workflow = (ROOT / ".github/workflows/dali-xnu.yml").read_text()

        self.assertIn("firmware_info = dict(code_file=code.name", qemu_entry)
        self.assertIn("report['qemu_firmware'] = firmware_info", qemu_entry)
        self.assertIn("message += f\"\\nUEFI UART tail", source)
        self.assertIn("*/gdb-uart.log", workflow)
        self.assertIn("*/gdb-stderr.log", workflow)


if __name__ == "__main__":
    unittest.main()
