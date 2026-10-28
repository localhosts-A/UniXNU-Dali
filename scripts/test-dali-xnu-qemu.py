#!/usr/bin/env python3
"""Run bounded single-CPU DarwinBoot/XNU QEMU checkpoints for the Dali build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tomllib


DEVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEVICE_ROOT / "scripts"))
from dali_ipados_trust_cache import parse_bundle

IMAGE4_CRYPTO_HASH_METHOD = "sha2-384"
AMFI_ONLY_PLATFORM_CODE = True
AMFI_PMGRAUDIT_COMPONENT = "com.apple.driver.AppleMobileFileIntegrity"
AMFI_PMGRAUDIT_COMPONENT_SHA256 = "a1088aaabc259418ed503d08df29b6146470b8c962696c688122a1f60a141db6"
AMFI_PMGRAUDIT_KERNELCACHE_SHA256 = "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7"
AMFI_PMGRAUDIT_ENTITLEMENT = "com.apple.private.amfi.developer-mode-control"
AMFI_PMGRAUDIT_TRACE_POINTS = (
    (0xfffffff004aaeb10, "serviceMatching-return"),
    (0xfffffff004aaeb24, "waitForMatchingService-return"),
    (0xfffffff004aaeb34, "safeMetaCast-entry"),
    (0xfffffff004aaeb38, "safeMetaCast-return"),
    (0xfffffff004aaeb40, "provider-read-entry"),
    (0xfffffff004aaeb44, "provider-read-return"),
    (0xfffffff004aaeb50, "provider-write-entry"),
    (0xfffffff004aaeb54, "provider-write-return"),
)
AMFI_PMGRAUDIT_GATE_TRACE_POINTS = (
    (0xfffffff004aaee14, "outer-guard-branch"),
    (0xfffffff004aaeafc, "inner-guard-branch"),
)
AMFI_PMGRAUDIT_ENTITLEMENT_TRACE_POINTS = (
    (0xfffffff004aaa9e0, "developer-mode-entitlement-result"),
    (0xfffffff004aaa9e8, "developer-mode-entitlement-accepted-path"),
)
AMFI_PMGRAUDIT_ALL_TRACE_POINTS = (
    AMFI_PMGRAUDIT_TRACE_POINTS + AMFI_PMGRAUDIT_GATE_TRACE_POINTS +
    AMFI_PMGRAUDIT_ENTITLEMENT_TRACE_POINTS
)
AMFI_PMGRAUDIT_GATE_ADDRESS = 0xfffffff005cfc470
AMFI_PMGRAUDIT_GATE_LENGTH = 2
AMFI_PMGRAUDIT_PROVIDER_READS = (
    (0xfffffff004aaeb38, "safeMetaCast-result-provider"),
    (0xfffffff004aaeb40, "read-entry-provider"),
)
OSMETA_CLASS_OBJECT_BYTES = 0x28
OSMETA_CLASS_NAME_FIELD_OFFSET = 0x18
OSSTRING_METADATA_OFFSET = 0x10
OSSTRING_METADATA_BYTES = 0x10
OSMETA_CLASS_VTABLE_PREFIX_BYTES = 0x40
IO_SERVICE_METACLASS_SYMBOL = "__ZN9IOService10gMetaClassE"
SOURCE_STORAGE_METACLASSES = (
    ("IOBlockStorageDevice", "__ZN20IOBlockStorageDevice10gMetaClassE", 0x90),
    ("IOMedia", "__ZN7IOMedia10gMetaClassE", 0xD0),
    ("IOMediaBSDClient", "__ZN16IOMediaBSDClient10gMetaClassE", 0xB0),
    ("IOPartitionScheme", "__ZN17IOPartitionScheme10gMetaClassE", 0xB0),
    ("IOStorage", "__ZN9IOStorage10gMetaClassE", 0x90),
)
DONOR_STORAGE_PARENTS = {
    "IOBlockStorageDevice": IO_SERVICE_METACLASS_SYMBOL,
    "IOMedia": "__ZN9IOStorage10gMetaClassE",
    "IOMediaBSDClient": IO_SERVICE_METACLASS_SYMBOL,
    "IOPartitionScheme": "__ZN9IOStorage10gMetaClassE",
    "IOStorage": IO_SERVICE_METACLASS_SYMBOL,
}


def validate_psci_abis(assembly):
    def function_id(body, reg, low, high):
        return bool(re.search(r'\bmov\s+[wx]' + str(reg) + r', #(?:' + hex(low) + '|' + str(low) + r')\b', body) and
                    re.search(r'\bmovk\s+[wx]' + str(reg) + r', #(?:' + hex(high) + '|' + str(high) + r'), lsl #16\b', body))
    features, cold, reset2 = (assembly[name] for name in ('recoveryPsciFeatures', 'recoveryPsciColdReset', 'recoveryPsciReset2'))
    if not function_id(features, 0, 10, 0x8400) or not function_id(features, 1, 18, 0xC400) or \
            not function_id(cold, 0, 9, 0x8400) or not function_id(reset2, 0, 18, 0xC400):
        raise ValueError('compiled PSCI function IDs are missing')
    for body in (features, cold, reset2):
        if len(re.findall(r'\bsmc\s+#(?:0x0|0)\b', body)) != 1 or not all(
                re.search(r'\bmov\s+[wx]' + str(reg) + r', (?:#0x0|#0|[wx]zr)\b', body) for reg in range(2, 8)):
            raise ValueError('compiled SMC or zero cookie/reserved registers are missing')
    if not re.search(r'\bmov\s+[wx]1, (?:#0x0|#0|[wx]zr)\b', cold) or \
            not re.search(r'\bsxtw\s+x1, w\d+\b', reset2):
        raise ValueError('cold reset argument zero or RESET2 type sign extension missing')
    for body in (cold, reset2):
        if not re.search(r'\bmrs\s+x\d+, DAIF\b', body, re.I) or \
                not re.search(r'\bmsr\s+DAIFSet, #(?:0x3|3)\b', body, re.I) or \
                not re.search(r'\bmsr\s+DAIF,', body, re.I) or not re.search(r'\bdsb\s+sy\b', body):
            raise ValueError('compiled reset DAIF save/restore or barrier missing')


def hardware_arguments():
    board = tomllib.loads((DEVICE_ROOT / 'board.toml').read_text())
    menu = tomllib.loads((DEVICE_ROOT / 'package/unixnu.toml').read_text())
    arguments = board['payload']['kernel_args']
    entry, = (entry for entry in menu['entry'] if entry['id'] == 'xnu')
    if arguments != shlex.split(entry['args']):
        raise SystemExit('Board payload and hardware menu must use the same XNU arguments')
    return arguments


def validate_budget_refusal(refusal, kernel_sha256, kernel_args):
    regions = refusal.get('entry_regions', {})
    def argument_string(request):
        return bytes.fromhex(regions.get(request, {}).get('bytes', '')).split(b'\0', 1)[0].decode()
    # XNU Assert(file, line, expression) has three arguments. x3 is unrelated
    # caller state, not a fatal/nonfatal flag.
    if refusal.get('kernel_sha256') != kernel_sha256 or \
            refusal.get('kernel_arguments') != kernel_args or \
            refusal.get('bounded_timeout_interrupted') or refusal.get('stop_symbol') != '_Assert' or \
            argument_string('0:0:40') != 'kalloc.c' or \
            argument_string('2:0:60') != 'n_zones_sig + n_zones_type + 1 <= 32':
        raise SystemExit('Default-budget control did not reproduce the real kalloc scratch-array assertion')


def validate_prelink_audit_candidate(kernel, report_path):
    """Bind diagnostic QEMU runs to an exact candidate explicitly marked non-loadable."""
    report = json.loads(report_path.read_text())
    digest = hashlib.sha256(kernel.read_bytes()).hexdigest()
    if report.get('candidate_sha256') != digest:
        raise SystemExit('Prelink report does not identify this exact candidate')
    if any(report.get(key) is not False for key in (
            'instruction_relocation_complete', 'abi_qualified', 'runtime_qualified', 'loadable')):
        raise SystemExit('This QEMU path only accepts an explicitly non-loadable prelink audit candidate')
    board = tomllib.loads((DEVICE_ROOT / 'board.toml').read_text())
    prelink = board['kernel']['prelink']
    transform = report.get('source_interface_transform', {})
    expected_interfaces = sorted(prelink.get('source_interfaces', []))
    if report.get('original_sha256') != prelink.get('kernelcache_sha256') or \
            transform.get('version') != 2 or \
            transform.get('original_kernelcache_sha256') != prelink.get('kernelcache_sha256') or \
            not re.fullmatch(r'[0-9a-f]{64}', transform.get('rewritten_prelink_input_sha256', '')) or \
            report.get('source_interfaces') != expected_interfaces:
        raise SystemExit('Prelink report does not bind the source-interface transform to the pinned kernelcache')
    storage = 'com.apple.iokit.IOStorageFamily'
    storage_record = transform.get('entries', {}).get(storage, {})
    if storage not in report.get('source_interfaces', []) or \
            storage not in report.get('codeless_components', []) or \
            storage in report.get('components', []) or \
            not storage_record.get('dependencies') or \
            not storage_record.get('personalities') or \
            not storage_record.get('segments'):
        raise SystemExit('Prelink must use the source-linked IOStorageFamily as codeless metadata')
    expected_bindings = sorted((
        {'bundle': item['bundle'], 'donor_va': int(item['donor_va'], 0),
         'symbol': item['symbol'], 'target_kind': item.get('target_kind', 'code')}
        for item in prelink.get('source_interface_bindings', [])
    ), key=lambda item: (item['bundle'], item['donor_va'], item['symbol']))
    if len({(item['bundle'], item['donor_va']) for item in expected_bindings}) != len(expected_bindings):
        raise SystemExit('Board declares duplicate source-interface donor bindings')
    reported_bindings = report.get('source_interface_bindings')
    if not isinstance(reported_bindings, list) or len(reported_bindings) != len(expected_bindings):
        raise SystemExit('Prelink report does not contain exactly the board-pinned source-interface bindings')
    candidate_bindings = report.get('bindings', [])
    if not isinstance(candidate_bindings, list):
        raise SystemExit('Prelink report pointer bindings are malformed')
    for expected, actual in zip(expected_bindings, reported_bindings):
        base_fields = {'bundle', 'donor_va', 'symbol', 'target_kind', 'native_va', 'reference_count'}
        data_fields = {'donor_segment', 'donor_section', 'donor_initprot', 'donor_maxprot',
                       'native_segment', 'native_section', 'native_initprot', 'native_maxprot'}
        expected_fields = base_fields | (data_fields if expected['target_kind'] in ('data_const', 'data_rw') else set())
        if not isinstance(actual, dict) or set(actual) != expected_fields or \
                any(actual.get(key) != value for key, value in expected.items()):
            raise SystemExit('Prelink report source-interface binding differs from the board declaration')
        native_va = actual.get('native_va')
        reference_count = actual.get('reference_count')
        alignment = 8 if expected['target_kind'] in ('data_const', 'data_rw') else 4
        if type(native_va) is not int or not 0xffff000000000000 <= native_va < 1 << 64 or \
                native_va % alignment or type(reference_count) is not int or reference_count < 1:
            raise SystemExit('Prelink report source-interface binding has an invalid native address or reference count')
        if expected['target_kind'] in ('data_const', 'data_rw'):
            layout = ({'segment': '__DATA_CONST', 'section': '__const', 'prot': 1}
                      if expected['target_kind'] == 'data_const' else
                      {'segment': '__DATA', 'section': '__common', 'prot': 3})
            for prefix in ('donor', 'native'):
                if (actual.get(f'{prefix}_segment'), actual.get(f'{prefix}_section'),
                        actual.get(f'{prefix}_initprot'), actual.get(f'{prefix}_maxprot')) != (
                        layout['segment'], layout['section'], layout['prot'], layout['prot']):
                    raise SystemExit('Prelink data binding does not preserve the pinned section/protection layout')
            donor_segments = storage_record.get('segments', [])
            donor_matches = [segment for segment in donor_segments
                             if segment.get('name') == actual['donor_segment'] and
                             segment.get('va', 0) <= expected['donor_va'] <
                             segment.get('va', 0) + segment.get('size', 0)]
            donor_sections = ([section for section in donor_matches[0].get('sections', [])
                               if section.get('name') == actual['donor_section'] and
                               section.get('va', 0) <= expected['donor_va'] <
                               section.get('va', 0) + section.get('size', 0)]
                              if len(donor_matches) == 1 else [])
            if len(donor_sections) != 1 or \
                    donor_matches[0].get('initprot') != actual['donor_initprot'] or \
                    donor_matches[0].get('maxprot') != actual['donor_maxprot']:
                raise SystemExit('Prelink data binding is not backed by its donor section manifest')
        donor_relocations = [binding for binding in candidate_bindings
                             if isinstance(binding, dict) and binding.get('target') == expected['donor_va']]
        if len(donor_relocations) != reference_count or any(
                binding.get('new_target') != native_va or binding.get('kind') != 'source_interface'
                for binding in donor_relocations):
            raise SystemExit('Prelink candidate pointer bindings do not match the source-interface mapping report')
    return report


def validate_donor_runtime_audit_candidate(kernel, report_path, native_kernel, config_path):
    """Require the isolated donor-executable profile and an unmodified source kernel."""
    report = json.loads(report_path.read_text())
    candidate_digest = hashlib.sha256(kernel.read_bytes()).hexdigest()
    native_digest = hashlib.sha256(native_kernel.read_bytes()).hexdigest()
    if report.get("candidate_sha256") != candidate_digest or report.get("native_sha256") != native_digest:
        raise SystemExit("Donor-runtime prelink report does not bind these exact kernel files")
    if any(report.get(key) is not False for key in (
            "instruction_relocation_complete", "abi_qualified", "runtime_qualified", "loadable")):
        raise SystemExit("Donor-runtime audit candidates must remain explicitly non-loadable")

    board = tomllib.loads((DEVICE_ROOT / "board.toml").read_text())
    base = board["kernel"]["prelink"]
    profile = base.get("profiles", {}).get("donor-runtime-audit")
    if not isinstance(profile, dict):
        raise SystemExit("Dali board is missing the donor-runtime audit prelink profile")
    storage = "com.apple.iokit.IOStorageFamily"
    expected_cache = base.get("kernelcache_sha256")
    transform = report.get("source_interface_transform", {})
    if report.get("original_sha256") != expected_cache or \
            transform.get("version") != 2 or \
            transform.get("original_kernelcache_sha256") != expected_cache or \
            transform.get("rewritten_prelink_input_sha256") != expected_cache or \
            transform.get("entries") != {} or report.get("source_interfaces") != [] or \
            report.get("source_interface_bindings") != [] or \
            storage not in report.get("components", []) or storage in report.get("codeless_components", []):
        raise SystemExit("Prelink report does not retain the original executable IOStorageFamily donor")

    config = {}
    for line in config_path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            if value == "y":
                config[key[7:]] = True
            elif value == "n":
                config[key[7:]] = False
            elif value.startswith('"') and value.endswith('"'):
                config[key[7:]] = value[1:-1]
            else:
                config[key[7:]] = value
    for key in ("XNU_SOURCE_STORAGE_FAMILY", "XNU_VERIFIED_ROOT_PROVIDER",
                "XNU_OWNER_ROOT_AUDIT", "XNU_OWNER_ROOT_TRUST"):
        if config.get(key) in (True, 1, "1", "y"):
            raise SystemExit(f"Donor-runtime defconfig must disable {key}")
    archives = config.get("XNU_EXTRA_LD_ARCHIVES", "")
    dependencies = config.get("XNU_EXTRA_KERNEL_ARCHIVE_DEPENDENCIES", "")
    if any(name in archives or name in dependencies
           for name in ("libIOStorageFamily.a", "libDaliUfs.a", "libVerifiedRootDali.a")):
        raise SystemExit("Donor-runtime source link still contains a source storage/root archive")
    symbols = subprocess.check_output(["llvm-nm", "--defined-only", native_kernel], text=True)
    defined = {line.split()[-1] for line in symbols.splitlines() if line.split()}
    class_symbols = {symbol for _, symbol, _ in SOURCE_STORAGE_METACLASSES}
    class_symbols.update((
        "__ZTV20IOBlockStorageDevice", "__ZTV7IOMedia", "__ZTV16IOMediaBSDClient",
        "__ZTV17IOPartitionScheme", "__ZTV9IOStorage",
    ))
    duplicates = sorted(defined & class_symbols)
    if duplicates:
        raise SystemExit("Native source XNU still defines donor IOStorageFamily classes: " + ", ".join(duplicates))
    return report


def format_qemu_probe_failure(name, stdout, report_path, log_path):
    """Prefer the stop report's cause when a probe exits after its deadline."""
    try:
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        report = {}
    if isinstance(report, dict) and report.get("bounded_timeout_interrupted") is True:
        registers = report.get("system_registers", {})
        if not isinstance(registers, dict):
            registers = {}
        details = ", ".join(
            f"{register}={registers.get(register, 'unknown')}"
            for register in ("ELR_EL1", "TTBR0_EL1", "TTBR1_EL1")
        )
        firmware = report.get("qemu_firmware", {})
        if isinstance(firmware, dict) and firmware.get("code_sha256"):
            details += f", firmware_code_sha256={firmware['code_sha256']}"
        message = (f"QEMU probe {name} timed out before reaching its requested XNU checkpoint "
                   f"(PC={report.get('pc', 'unknown')}, {details}); "
                   f"see {report_path} and {log_path}")
        uart_path = report_path.with_name("gdb-uart.log")
        if uart_path.is_file():
            try:
                uart = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", uart_path.read_text(errors="replace"))[-2000:]
            except OSError:
                uart = ""
            if uart.strip():
                message += f"\nUEFI UART tail ({uart_path}):\n{uart.rstrip()}"
        return message
    return f"QEMU probe {name} failed; see {log_path}\n{stdout[-5000:]}"


def expected_donor_runtime_terminal(report, output):
    caller = report.get("panic_caller_component", {}) if isinstance(report, dict) else {}
    return (report.get("stop_symbol") == "_panic" and isinstance(caller, dict) and
            caller.get("module") == "com.apple.driver.AppleMobileFileIntegrity" and
            "AMFI: No PMGR?" in output)


def validate_prelink_structure_audit(kernel, report, verification_path, expected_map_count=None):
    """Require the separate verifier's exact-image structural audit for QEMU diagnostics."""
    verification = json.loads(verification_path.read_text())
    digest = hashlib.sha256(kernel.read_bytes()).hexdigest()
    board = tomllib.loads((DEVICE_ROOT / 'board.toml').read_text())
    expected_maps = (len(board['kernel']['prelink'].get('source_interface_bindings', []))
                     if expected_map_count is None else expected_map_count)
    if verification.get('verification') != 'independent-prelink-structure-and-pointer-table' or \
            verification.get('candidate_sha256') != digest or \
            verification.get('native_sha256') != report.get('native_sha256') or \
            verification.get('original_sha256') != report.get('original_sha256') or \
            verification.get('slide') != report.get('slide'):
        raise SystemExit('Independent prelink verification does not bind the exact candidate and inputs')
    if verification.get('component_closure_verified') is not True or \
            verification.get('kmod_and_sparse_index_verified') is not True or \
            verification.get('component_headers_and_bytes_verified') != len(report.get('components', [])) or \
            verification.get('source_interface_bindings_verified') != expected_maps or \
            type(verification.get('pointer_table_rebindings_verified')) is not int or \
            verification['pointer_table_rebindings_verified'] < 1:
        raise SystemExit('Independent prelink structural audit is incomplete')
    if any(verification.get(key) is not False for key in
           ('instruction_relocation_complete', 'abi_qualified', 'runtime_qualified', 'loadable')):
        raise SystemExit('Structural audit cannot promote instruction, ABI, runtime, or loadability gates')
    return verification


def validate_corecrypto_state(report, require_prelinked_provider):
    if not report.get("crypto14_initialization_observed"):
        raise SystemExit("QEMU did not observe initialized corecrypto and PRNG state")
    if require_prelinked_provider and (
            not report.get("original_crypto_table_owner_observed") or
            not report.get("original_crypto_initialized_observed")):
        raise SystemExit("QEMU crypto table is not owned by the pinned iPadOS corecrypto component")


def validate_trust_cache(path, expected_hashes):
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest not in expected_hashes:
        raise SystemExit("QEMU trust-cache input is not one of the bundle hashes pinned by board.toml")
    try:
        modules = parse_bundle(data)
    except ValueError as error:
        raise SystemExit(f"QEMU trust-cache input is invalid: {error}") from None
    return {"sha256": digest, "modules": modules}


def source_storage_metaclass_requests(kernel):
    names = {symbol for _, symbol, _ in SOURCE_STORAGE_METACLASSES}
    output = subprocess.check_output(["llvm-nm", "--defined-only", str(kernel)], text=True)
    addresses = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[-1] not in names:
            continue
        symbol = fields[-1]
        if symbol in addresses:
            raise SystemExit(f"Duplicate source metaclass symbol address for {symbol}")
        try:
            addresses[symbol] = int(fields[0], 16)
        except ValueError:
            raise SystemExit(f"Invalid source metaclass symbol address for {symbol}") from None
    requests = {}
    for class_name, symbol, _ in SOURCE_STORAGE_METACLASSES:
        address = addresses.get(symbol)
        if address is None or not 0 <= address < 1 << 64:
            raise SystemExit(f"Missing source metaclass symbol address for {symbol}")
        name_field = address + OSMETA_CLASS_NAME_FIELD_OFFSET
        if name_field + 8 > 1 << 64 or address + OSMETA_CLASS_VTABLE_PREFIX_BYTES > 1 << 64:
            raise SystemExit(f"Invalid bounded source metaclass read range for {symbol}")
        requests[class_name] = {
            "symbol": symbol,
            "runtime_va": address,
            "class_name_metadata_request": (
                f"{name_field:x}:{OSSTRING_METADATA_OFFSET:x}:{OSSTRING_METADATA_BYTES:x}"),
            "vtable_prefix_request": f"{address:x}:0:{OSMETA_CLASS_VTABLE_PREFIX_BYTES:x}",
        }
    return requests


def source_storage_metaclass_read_args(kernel):
    requests = source_storage_metaclass_requests(kernel)
    arguments = [argument for _, symbol, _ in SOURCE_STORAGE_METACLASSES
                 for argument in ("--read-kernel-symbol", f"{symbol}:0x{OSMETA_CLASS_OBJECT_BYTES:x}")]
    for request in requests.values():
        arguments += ["--read-indirect-optional", request["class_name_metadata_request"]]
        arguments += ["--read-indirect-optional", request["vtable_prefix_request"]]
    return arguments


def donor_storage_metaclass_requests():
    board = tomllib.loads((DEVICE_ROOT / "board.toml").read_text())
    prelink = board["kernel"]["prelink"]
    slide = int(prelink["slide"], 0)
    bindings = {}
    for entry in prelink.get("source_interface_bindings", []):
        symbol = entry["symbol"]
        if symbol in {item[1] for item in SOURCE_STORAGE_METACLASSES}:
            if symbol in bindings:
                raise SystemExit(f"Duplicate donor metaclass address for {symbol}")
            bindings[symbol] = int(entry["donor_va"], 0)
    requests = {}
    for class_name, symbol, _ in SOURCE_STORAGE_METACLASSES:
        if symbol not in bindings:
            raise SystemExit(f"Missing pinned donor metaclass address for {symbol}")
        donor_va = bindings[symbol]
        runtime_va = donor_va + slide
        name_field = runtime_va + OSMETA_CLASS_NAME_FIELD_OFFSET
        if (not 0 <= runtime_va < 1 << 64 or
                runtime_va + OSMETA_CLASS_OBJECT_BYTES > 1 << 64 or
                name_field + 8 > 1 << 64 or
                runtime_va + OSMETA_CLASS_VTABLE_PREFIX_BYTES > 1 << 64):
            raise SystemExit(f"Invalid slid donor metaclass address for {symbol}")
        requests[class_name] = {
            "symbol": symbol,
            "donor_va": donor_va,
            "runtime_va": runtime_va,
            "request": f"{runtime_va:x}:{OSMETA_CLASS_OBJECT_BYTES:x}",
            "class_name_metadata_request": (
                f"{name_field:x}:{OSSTRING_METADATA_OFFSET:x}:{OSSTRING_METADATA_BYTES:x}"),
            "vtable_prefix_request": f"{runtime_va:x}:0:{OSMETA_CLASS_VTABLE_PREFIX_BYTES:x}",
        }
    return requests


def donor_storage_metaclass_read_args():
    arguments = []
    for request in donor_storage_metaclass_requests().values():
        arguments += ["--read-memory", request["request"]]
        arguments += ["--read-indirect-optional", request["class_name_metadata_request"]]
        arguments += ["--read-indirect-optional", request["vtable_prefix_request"]]
    arguments += ["--read-kernel-symbol", f"{IO_SERVICE_METACLASS_SYMBOL}:0x{OSMETA_CLASS_OBJECT_BYTES:x}"]
    return arguments


def inspect_indirect_metaclass_reads(report, class_name, requests):
    """Retain bounded raw OSSymbol metadata and nested MetaClass vtable bytes."""
    captured = report.get("requested_indirect_memory", {}) if isinstance(report, dict) else {}
    if not isinstance(captured, dict):
        captured = {}

    def decode(request, expected_bytes):
        value = captured.get(request)
        if not isinstance(value, dict):
            return {"request": request, "available": False,
                    "unavailable": "probe report has no matching indirect read"}
        result = {"request": request, "pointer": value.get("pointer"),
                  "address": value.get("address"), "available": False}
        if value.get("unavailable"):
            result["unavailable"] = value["unavailable"]
            return result
        raw = value.get("bytes")
        try:
            data = bytes.fromhex(raw) if isinstance(raw, str) else b""
        except ValueError:
            data = b""
        if len(data) != expected_bytes:
            result["unavailable"] = "indirect read returned a malformed byte range"
            return result
        result.update({"available": True, "raw_hex": data.hex()})
        if expected_bytes == OSMETA_CLASS_VTABLE_PREFIX_BYTES:
            result["entries"] = [hex(value) for value in struct.unpack("<8Q", data)]
        return result

    request = requests[class_name]
    return {
        "class_name_symbol_metadata": decode(
            request["class_name_metadata_request"], OSSTRING_METADATA_BYTES),
        "metaclass_vtable_prefix": decode(
            request["vtable_prefix_request"], OSMETA_CLASS_VTABLE_PREFIX_BYTES),
        "scope": "raw bounded reads only; OSSymbol string pointer is not authenticated or dereferenced",
    }


def inspect_donor_storage_metaclasses(report, native_kernel):
    """Decode live donor objects; retain optional raw name metadata and vtable bytes."""
    memory = report.get("memory", {}) if isinstance(report, dict) else {}
    requested_symbols = report.get("requested_kernel_symbols", {}) if isinstance(report, dict) else {}
    if not isinstance(memory, dict):
        memory = {}
    if not isinstance(requested_symbols, dict):
        requested_symbols = {}
    requests = donor_storage_metaclass_requests()
    errors = []
    service_raw = requested_symbols.get(IO_SERVICE_METACLASS_SYMBOL)
    try:
        service_data = bytes.fromhex(service_raw) if isinstance(service_raw, str) else b""
    except ValueError:
        service_data = b""
    if len(service_data) != OSMETA_CLASS_OBJECT_BYTES:
        errors.append("IOService: missing or malformed source-kernel superclass metaclass")

    names = subprocess.check_output(["llvm-nm", "--defined-only", native_kernel], text=True)
    symbol_addresses = {}
    for line in names.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] == IO_SERVICE_METACLASS_SYMBOL:
            try:
                symbol_addresses[IO_SERVICE_METACLASS_SYMBOL] = int(fields[0], 16)
            except ValueError:
                pass
    expected_service_address = symbol_addresses.get(IO_SERVICE_METACLASS_SYMBOL, 0)
    if not expected_service_address:
        errors.append("IOService: exact native XNU metaclass symbol is missing")

    classes = {}
    for class_name, _, expected_size in SOURCE_STORAGE_METACLASSES:
        request = requests[class_name]
        raw = memory.get(request["request"])
        try:
            data = bytes.fromhex(raw) if isinstance(raw, str) else b""
        except ValueError:
            data = b""
        record = {
            "symbol": request["symbol"],
            "donor_link_address": hex(request["donor_va"]),
            "runtime_address": hex(request["runtime_va"]),
            "read_request": request["request"],
            "expected_class_size_bytes": expected_size,
            "expected_superclass_symbol": DONOR_STORAGE_PARENTS[class_name],
            "available": False,
        }
        if len(data) != OSMETA_CLASS_OBJECT_BYTES:
            errors.append(f"{class_name}: missing or malformed donor runtime metaclass object")
        else:
            vtable = struct.unpack_from("<Q", data, 0)[0]
            superclass = struct.unpack_from("<Q", data, 0x10)[0]
            class_name_symbol = struct.unpack_from("<Q", data, 0x18)[0]
            class_size = struct.unpack_from("<I", data, 0x20)[0]
            if DONOR_STORAGE_PARENTS[class_name] == IO_SERVICE_METACLASS_SYMBOL:
                expected_superclass = expected_service_address
            else:
                parent = requests["IOStorage"]
                expected_superclass = parent["runtime_va"]
            record.update({
                "available": True,
                "raw_hex": data.hex(),
                "vtable_pointer": hex(vtable),
                "superclass_metaclass_pointer": hex(superclass),
                "class_name_symbol_pointer": hex(class_name_symbol),
                "class_size_bytes": class_size,
                "class_size_nonzero": class_size > 0,
                "class_size_matches_expected": class_size == expected_size,
                "required_pointers_nonzero": bool(vtable and superclass and class_name_symbol),
                "superclass_matches_expected": bool(expected_superclass and superclass == expected_superclass),
            })
            record.update(inspect_indirect_metaclass_reads(report, class_name, requests))
            if class_size != expected_size:
                errors.append(f"{class_name}: donor runtime class size differs from pinned source layout")
            if not class_size:
                errors.append(f"{class_name}: donor runtime class size was not initialized")
            if not (vtable and superclass and class_name_symbol):
                errors.append(f"{class_name}: donor runtime metaclass has a null required pointer")
            if not record["superclass_matches_expected"]:
                errors.append(f"{class_name}: donor runtime superclass pointer is unexpected")
        classes[class_name] = record

    complete = all(record["available"] for record in classes.values())
    return {
        "schema": "dali-donor-runtime-metaclass-observation-v1",
        "object_bytes": OSMETA_CLASS_OBJECT_BYTES,
        "classes": classes,
        "capture_complete": complete,
        "all_class_sizes_nonzero": complete and all(
            record.get("class_size_nonzero") is True for record in classes.values()),
        "all_class_sizes_match_expected": complete and all(
            record.get("class_size_matches_expected") is True for record in classes.values()),
        "all_required_pointers_nonzero": complete and all(
            record.get("required_pointers_nonzero") is True for record in classes.values()),
        "all_superclasses_match_expected": complete and all(
            record.get("superclass_matches_expected") is True for record in classes.values()),
        "all_class_name_metadata_reads_available": complete and all(
            record.get("class_name_symbol_metadata", {}).get("available") is True
            for record in classes.values()),
        "all_metaclass_vtable_prefix_reads_available": complete and all(
            record.get("metaclass_vtable_prefix", {}).get("available") is True
            for record in classes.values()),
        "source_io_service_metaclass_matches_native_symbol": bool(
            expected_service_address and len(service_data) == OSMETA_CLASS_OBJECT_BYTES),
        "errors": errors,
        "donor_runtime_comparison_available": True,
        "complete_abi_qualified": False,
        "abi_qualified": False,
        "loadable": False,
    }


def inspect_source_storage_metaclasses(report, kernel):
    """Decode source-XNU OSMetaClass objects plus optional bounded raw reads."""
    requested = report.get("requested_kernel_symbols") if isinstance(report, dict) else None
    requested = requested if isinstance(requested, dict) else {}
    requests = source_storage_metaclass_requests(kernel)
    classes = {}
    errors = []
    for class_name, symbol, expected_size in SOURCE_STORAGE_METACLASSES:
        record = {"symbol": symbol, "expected_class_size_bytes": expected_size, "available": False}
        raw = requested.get(symbol)
        try:
            data = bytes.fromhex(raw) if isinstance(raw, str) else b""
        except ValueError:
            data = b""
        if len(data) != OSMETA_CLASS_OBJECT_BYTES:
            errors.append(f"{class_name}: missing or malformed {OSMETA_CLASS_OBJECT_BYTES}-byte object")
        else:
            vtable = struct.unpack_from("<Q", data, 0)[0]
            superclass = struct.unpack_from("<Q", data, 0x10)[0]
            class_name_symbol = struct.unpack_from("<Q", data, 0x18)[0]
            class_size = struct.unpack_from("<I", data, 0x20)[0]
            record.update({
                "available": True,
                "vtable_pointer": hex(vtable),
                "superclass_metaclass_pointer": hex(superclass),
                "class_name_symbol_pointer": hex(class_name_symbol),
                "class_size_bytes": class_size,
                "class_size_matches_source_layout": class_size == expected_size,
                "required_pointers_nonzero": bool(vtable and superclass and class_name_symbol),
            })
            record.update(inspect_indirect_metaclass_reads(report, class_name, requests))
        classes[class_name] = record
    complete = all(record["available"] for record in classes.values())
    return {
        "schema": "dali-source-storage-metaclass-observation-v1",
        "object_bytes": OSMETA_CLASS_OBJECT_BYTES,
        "classes": classes,
        "capture_complete": complete,
        "all_class_sizes_match_source_layout": complete and all(
            record.get("class_size_matches_source_layout") is True for record in classes.values()),
        "all_required_pointers_nonzero": complete and all(
            record.get("required_pointers_nonzero") is True for record in classes.values()),
        "all_class_name_metadata_reads_available": complete and all(
            record.get("class_name_symbol_metadata", {}).get("available") is True
            for record in classes.values()),
        "all_metaclass_vtable_prefix_reads_available": complete and all(
            record.get("metaclass_vtable_prefix", {}).get("available") is True
            for record in classes.values()),
        "errors": errors,
        "donor_runtime_comparison_available": False,
        "abi_qualified": False,
        "loadable": False,
    }


def validate_trust_cache_runtime(report, expected_sha256):
    """Require the target XNU loader to consume the exact DarwinBoot ADT cache."""
    if not isinstance(report, dict) or report.get("trust_cache_bundle_sha256") != expected_sha256:
        raise SystemExit("Trust-cache runtime checkpoint did not bind the pinned sidecar")
    symbols = report.get("requested_kernel_symbols", {})
    if not isinstance(symbols, dict):
        raise SystemExit("Trust-cache runtime checkpoint has malformed symbol evidence")
    raw_loaded = symbols.get("_trust_cache_static_loaded")
    if not isinstance(raw_loaded, str) or len(raw_loaded) != 2:
        raise SystemExit("Trust-cache runtime checkpoint omitted the actual static-loaded flag")
    try:
        loaded = bytes.fromhex(raw_loaded)
    except ValueError:
        raise SystemExit("Trust-cache runtime checkpoint returned malformed flag bytes") from None
    if loaded != b"\x01":
        raise SystemExit("XNU returned from the static trust-cache loader without loading an ADT cache")
    raw_events = report.get("trace_events", [])
    if not isinstance(raw_events, list):
        raise SystemExit("Trust-cache runtime checkpoint has malformed trace events")
    events = [item.get("symbol") for item in raw_events if isinstance(item, dict)]
    if "_trust_cache_runtime_init" not in events:
        raise SystemExit("QEMU did not observe trust-cache runtime initialization")
    if report.get("bounded_timeout_interrupted") is not False:
        raise SystemExit("Trust-cache loader return was interrupted or timed out")
    if report.get("stop_symbol") != "_load_static_trust_cache":
        raise SystemExit("QEMU did not stop at the static trust-cache loader return")
    if report.get("function_return_stack_matched") is not True:
        raise SystemExit("Trust-cache loader return did not match the entered function stack")
    entry = report.get("finished_function_entry")
    return_target = report.get("function_return_target")
    pc = report.get("pc")
    if not isinstance(entry, str) or not entry.startswith("0x") or \
            not isinstance(return_target, str) or return_target != pc:
        raise SystemExit("Trust-cache loader return lacks a matching entry/return checkpoint")
    return {
        "bundle_sha256": expected_sha256,
        "runtime_init_observed": True,
        "adt_static_loader_returned": True,
        "static_loaded_flag": 1,
        "adt_handoff_verified": True,
        "runtime_consumed": True,
    }


def validate_source_xnu_config(path, require_trust_cache_runtime):
    """Bind runtime-loader expectations to the effective source-XNU config."""
    data = path.read_bytes()
    text = data.decode("utf-8")
    enabled = re.search(r"(?m)^CONFIG_XNU_TRUST_CACHE_RUNTIME=y$", text) is not None
    if require_trust_cache_runtime and not enabled:
        raise SystemExit("The iPadOS trust-cache QEMU case requires CONFIG_XNU_TRUST_CACHE_RUNTIME=y")
    return {"sha256": hashlib.sha256(data).hexdigest(),
            "trust_cache_runtime_enabled": enabled}


def amfi_pmgr_trace_args(prelink_report):
    """Bind AMFI probes and gate bytes to exact 21H461 component extents."""
    if not isinstance(prelink_report, dict) or \
            prelink_report.get("original_sha256") != AMFI_PMGRAUDIT_KERNELCACHE_SHA256 or \
            AMFI_PMGRAUDIT_COMPONENT not in prelink_report.get("components", []):
        raise SystemExit("AMFI PMGR trace requires the pinned 21H461 prelink candidate")
    component_text = [extent for extent in prelink_report.get("extents", [])
                      if extent.get("module") == AMFI_PMGRAUDIT_COMPONENT and
                      extent.get("segment") == "__TEXT_EXEC"]
    if len(component_text) != 1:
        raise SystemExit("AMFI PMGR trace requires one exact AMFI executable text extent")
    extent = component_text[0]
    if prelink_report.get("original_executable_sha256", {}).get(AMFI_PMGRAUDIT_COMPONENT) != \
            AMFI_PMGRAUDIT_COMPONENT_SHA256:
        raise SystemExit("AMFI PMGR trace candidate has an unexpected original AMFI executable hash")
    if any(not extent["va"] <= address < extent["va"] + extent["size"]
           for address, _ in AMFI_PMGRAUDIT_ALL_TRACE_POINTS):
        raise SystemExit("Pinned AMFI PMGR trace PC escaped its executable text extent")
    component_data_const = [extent for extent in prelink_report.get("extents", [])
                            if extent.get("module") == AMFI_PMGRAUDIT_COMPONENT and
                            extent.get("segment") == "__DATA_CONST"]
    if len(component_data_const) != 1:
        raise SystemExit("AMFI PMGR gate read requires one exact AMFI __DATA_CONST extent")
    data_extent = component_data_const[0]
    gate_end = AMFI_PMGRAUDIT_GATE_ADDRESS + AMFI_PMGRAUDIT_GATE_LENGTH
    if not data_extent["va"] <= AMFI_PMGRAUDIT_GATE_ADDRESS or \
            gate_end > data_extent["va"] + data_extent["size"]:
        raise SystemExit("Pinned AMFI PMGR gate bytes escaped the __DATA_CONST extent")
    arguments = []
    for address, _ in AMFI_PMGRAUDIT_ALL_TRACE_POINTS:
        arguments += ["--trace-address", hex(address)]
    for address, _ in AMFI_PMGRAUDIT_PROVIDER_READS:
        arguments += ["--trace-read-register-offset", f"{address:x}:x0:88:8"]
    arguments += ["--read-memory", f"{AMFI_PMGRAUDIT_GATE_ADDRESS:x}:{AMFI_PMGRAUDIT_GATE_LENGTH:x}"]
    return arguments


def inspect_amfi_pmgr_runtime(report, prelink_report):
    """Summarize bounded original-AMFI trace events without elevating qualification."""
    amfi_pmgr_trace_args(prelink_report)
    if not isinstance(report, dict) or report.get("kernel_sha256") != prelink_report.get("candidate_sha256"):
        raise SystemExit("AMFI PMGR runtime trace does not match the exact source candidate")
    raw_events = report.get("trace_events", [])
    if not isinstance(raw_events, list):
        raise SystemExit("AMFI PMGR runtime trace has malformed trace events")
    by_pc = {int(event["pc"], 16): event for event in raw_events
             if isinstance(event, dict) and isinstance(event.get("pc"), str)}
    events = {}
    for address, label in AMFI_PMGRAUDIT_ALL_TRACE_POINTS:
        event = by_pc.get(address)
        if event is not None:
            event["symbol"] = f"AMFI PMGR: {label}"
            events[label] = dict(pc=hex(address), x0=event.get("x0"), x1=event.get("x1"),
                                 x8=event.get("x8"),
                                 register_memory_reads=event.get("register_memory_reads", {}))
    gate_branch_events = {
        label: {"pc": hex(address), "x8": by_pc[address].get("x8")}
        for address, label in AMFI_PMGRAUDIT_GATE_TRACE_POINTS if address in by_pc
    }
    entitlement_gate_events = {
        label: {"pc": hex(address), "x0": by_pc[address].get("x0")}
        for address, label in AMFI_PMGRAUDIT_ENTITLEMENT_TRACE_POINTS if address in by_pc
    }
    entitlement_result = entitlement_gate_events.get(
        "developer-mode-entitlement-result", {}).get("x0")
    try:
        entitlement_result_nonzero = bool(int(entitlement_result, 16) & 0xffffffff)
    except (TypeError, ValueError):
        entitlement_result_nonzero = None
    accepted_path_address = AMFI_PMGRAUDIT_ENTITLEMENT_TRACE_POINTS[1][0]
    provider_fields = {}
    for address, label in AMFI_PMGRAUDIT_PROVIDER_READS:
        event = by_pc.get(address, {})
        key = f"{address:x}:x0:88:8"
        capture = event.get("register_memory_reads", {}).get(key)
        record = dict(capture) if isinstance(capture, dict) else {"unavailable": "trace read was not captured"}
        raw = record.get("raw_hex")
        if isinstance(raw, str) and len(raw) == 16:
            record["provider_pointer"] = hex(int.from_bytes(bytes.fromhex(raw), "little"))
        provider_fields[label] = record
    safe_cast = events.get("safeMetaCast-return", {}).get("x0")
    missing_trace_points = [
        {"pc": hex(address), "event": label}
        for address, label in AMFI_PMGRAUDIT_ALL_TRACE_POINTS if address not in by_pc
    ]
    memory = report.get("memory", {})
    if not isinstance(memory, dict):
        raise SystemExit("AMFI PMGR gate memory report is malformed")
    gate_request = f"{AMFI_PMGRAUDIT_GATE_ADDRESS:x}:{AMFI_PMGRAUDIT_GATE_LENGTH:x}"
    gate_response = memory.get(gate_request)
    gate_read = {
        "address": hex(AMFI_PMGRAUDIT_GATE_ADDRESS),
        "length_bytes": AMFI_PMGRAUDIT_GATE_LENGTH,
        "available": False,
    }
    if isinstance(gate_response, str):
        if re.fullmatch(r"[0-9a-fA-F]{4}", gate_response):
            gate_bytes = bytes.fromhex(gate_response)
            gate_read.update(available=True,
                             raw_hex=gate_response.lower(),
                             bytes=list(gate_bytes))
        else:
            reason = "gdb-read-error" if gate_response.startswith("E") else "short-or-malformed-read"
            gate_read.update(unavailable=reason, raw_response=gate_response[:32])
    else:
        gate_read["unavailable"] = "not-captured"
    return {
        "candidate_sha256": report["kernel_sha256"],
        "original_kernelcache_sha256": prelink_report.get("original_sha256"),
        "original_amfi_component_sha256": AMFI_PMGRAUDIT_COMPONENT_SHA256,
        "events": events,
        "gate_branch_events": gate_branch_events,
        "entitlement_gate": {
            "required_entitlement": AMFI_PMGRAUDIT_ENTITLEMENT,
            "events": entitlement_gate_events,
            "result_nonzero": entitlement_result_nonzero,
            "accepted_path_observed": accepted_path_address in by_pc,
        },
        "missing_trace_points": missing_trace_points,
        "provider_field_reads": provider_fields,
        "control_gate_read": gate_read,
        "safe_meta_cast_succeeded": (int(safe_cast, 16) != 0) if isinstance(safe_cast, str) else None,
        "trace_complete": not missing_trace_points,
        "amfi_modified": False,
        "hardware_qualified": False,
        "abi_qualified": False,
    }


def validate_reset_instructions(kernel, output):
    """Require the real compiled call chain; source/compile success is insufficient."""
    output.mkdir(parents=True, exist_ok=True)
    symbols = subprocess.check_output(['llvm-nm', '--defined-only', kernel], text=True)
    names = [line.split()[-1] for line in symbols.splitlines() if line.split()]
    assembly = {}
    def find(fragment):
        found = [name for name in names if fragment in name and '.cold.' not in name]
        if len(found) != 1:
            raise SystemExit(f'Compiled reset boundary {fragment} missing/ambiguous: {found}')
        name, = found
        assembly[fragment] = subprocess.check_output(
            ['llvm-objdump', '-d', '--disassemble-symbols=' + name, kernel], text=True)
        return name
    try:
        start = find('__ZN11DaliBootLog5startEP9IOService')
        recover = find('recoverToFastboot')
        issue = find('daliIssueSystemReset')
        find('recoverySystemReset')
        for name in ('recoveryPsciFeatures', 'recoveryPsciColdReset', 'recoveryPsciReset2'):
            find(name)
        find('recoveryRead')
        find('recoveryFence')
        def calls(body, callee):
            return bool(re.search(r'\bbl\s+[^\n]*<' + re.escape(callee) + r'>', body))
        if not calls(assembly['__ZN11DaliBootLog5startEP9IOService'], recover) or \
                not calls(assembly['recoverToFastboot'], issue):
            raise SystemExit('Compiled Dali checkpoint cannot call the guarded software-reset sequence')
        validate_psci_abis(assembly)
        if not all(re.search(r'\bdsb\s+sy\b', assembly[key]) for key in ('recoveryRead', 'recoveryFence')):
            raise SystemExit('Compiled MMIO/persistent-record barriers are missing')
    finally:
        (output / 'reset-instructions.log').write_text('\n'.join(assembly.values()))
    (output / 'reset-instructions.json').write_text(json.dumps({
        'compiled_checkpoint_calls_recovery': True,
        'compiled_recovery_calls_guarded_reset': True,
        'psci_features_reset2_smcs_daif_and_barriers_present': True,
        'hardware_recovery_demonstrated': False,
    }, indent=2) + '\n')


def should_run_default_budget_control(prelink_report):
    """Run the fallback-allocation negative control only on source-only XNU."""
    return prelink_report is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unixnu", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--prelink-report", type=Path,
                        help="Exact report for a diagnostic-only, explicitly non-loadable candidate")
    parser.add_argument("--prelink-verification", type=Path,
                        help="Independent structural/pointer-table verification for the exact candidate")
    parser.add_argument("--donor-runtime-audit", action="store_true",
                        help="Capture original IOStorageFamily runtime metaclasses; never qualifies the candidate")
    parser.add_argument("--native-kernel", type=Path,
                        help="Exact source-only XNU input for the donor-runtime audit")
    parser.add_argument("--config", type=Path,
                        help="Effective source-XNU .config used to build this QEMU candidate")
    parser.add_argument("--trust-cache", type=Path,
                        help="Exact board-pinned original iPadOS trust-cache bundle for DarwinBoot handoff")
    parser.add_argument("--uefi-efi", type=Path, required=True)
    parser.add_argument("--qemu-firmware", type=Path,
                        help="Local AArch64 UEFI code FD for QEMU; defaults to the QEMU share directory")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.qemu_firmware and not args.qemu_firmware.is_file():
        parser.error("QEMU firmware code FD does not exist")
    root = args.unixnu.resolve()
    output = args.output_dir.resolve()
    kernel = args.kernel.resolve()
    prelink_report = None
    prelink_report_sha256 = None
    prelink_verification = None
    prelink_verification_sha256 = None
    xnu_config = None
    if args.donor_runtime_audit:
        if not args.prelink_report or not args.prelink_verification:
            parser.error("the donor-runtime audit requires the exact prelink report and structural verification")
        if args.trust_cache:
            parser.error("the donor-runtime audit does not consume or qualify a trust-cache input")
        if not args.native_kernel or not args.native_kernel.is_file() or not args.config or not args.config.is_file():
            parser.error("the donor-runtime audit requires the exact native kernel and selected defconfig")
        xnu_config = validate_source_xnu_config(args.config.resolve(), require_trust_cache_runtime=False)
        prelink_report = validate_donor_runtime_audit_candidate(
            kernel, args.prelink_report.resolve(), args.native_kernel.resolve(), args.config.resolve())
        prelink_report_sha256 = hashlib.sha256(args.prelink_report.read_bytes()).hexdigest()
        verification_path = args.prelink_verification.resolve()
        prelink_verification = validate_prelink_structure_audit(
            kernel, prelink_report, verification_path, expected_map_count=0)
        prelink_verification_sha256 = hashlib.sha256(verification_path.read_bytes()).hexdigest()
    elif args.prelink_report:
        report_path = args.prelink_report.resolve()
        prelink_report = validate_prelink_audit_candidate(kernel, report_path)
        prelink_report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if not args.prelink_verification:
            parser.error("the pinned iPadOS prelink probe requires the independent structural verification")
        verification_path = args.prelink_verification.resolve()
        prelink_verification = validate_prelink_structure_audit(kernel, prelink_report, verification_path)
        prelink_verification_sha256 = hashlib.sha256(verification_path.read_bytes()).hexdigest()
        if not args.trust_cache:
            parser.error("the pinned iPadOS prelink probe requires the board trust-cache bundle")
        if not args.config or not args.config.is_file():
            parser.error("the pinned iPadOS trust-cache QEMU probe requires the effective source-XNU .config")
        xnu_config = validate_source_xnu_config(args.config.resolve(), require_trust_cache_runtime=True)
    elif args.prelink_verification:
        parser.error("an independent prelink verification requires its candidate report")
    elif args.native_kernel or args.config:
        parser.error("--native-kernel and --config are only valid for --donor-runtime-audit")
    board = tomllib.loads((DEVICE_ROOT / "board.toml").read_text())
    trust_cache = (validate_trust_cache(args.trust_cache.resolve(), board["payload"]["trust_cache_sha256"])
                   if args.trust_cache else None)
    validate_reset_instructions(kernel, output)
    probe = root / "scripts/probe-darwinboot-xnu.py"
    board_args = hardware_arguments()
    # Keep only console diagnostics specific to the offline VM. Allocation
    # policy comes from the hardware configuration, never the probe's defaults.
    kernel_args = ' '.join([*board_args, 'serial=3', 'debug=0x14e'])
    common = [sys.executable, probe, "--kernel", kernel, "--uefi-efi", args.uefi_efi.resolve(),
              "--timeout", "90", "--armvirt", "--armcpu", "--nvram-proxy"]
    if args.qemu_firmware:
        common += ["--qemu-firmware", args.qemu_firmware.resolve()]
    if args.prelink_report:
        # This candidate is explicitly non-loadable, so keep the QEMU run
        # diagnostic-only and capture the real security-kext/class-registration
        # path if the pinned target kernel panics during bootstrap.
        common += ["--crypto-hash-method", IMAGE4_CRYPTO_HASH_METHOD,
                   "--amfi-platform-only",
                   "--prelink-report", args.prelink_report.resolve(), "--kext-logs",
                   "--trace-symbol", "_register_and_init_prng",
                   "--trace-symbol", "_OSRuntimeInitializeCPP",
                   "--trace-symbol", "__ZN11OSMetaClass11postModLoadEPv",
                   "--trace-symbol", "__ZN6OSKext5startEb",
                   "--trace-symbol", "__ZN12KLDBootstrap22loadSecurityExtensionsEv"]
    if trust_cache:
        common += ["--trust-cache", args.trust_cache.resolve()]
    cases = {
        "platform": "__ZN13ArmDTPlatform9configureEP9IOService",
        "cpu": "__ZN6ArmCpu5startEP9IOService",
        "timer": "_rtclock_intr",
    }
    if args.donor_runtime_audit:
        cases["donor-runtime"] = "_load_static_trust_cache"
    elif trust_cache and prelink_report:
        cases["trust-cache"] = "_load_static_trust_cache"
    reports = {}
    for name, symbol in cases.items():
        destination = output / name
        command = [*common, "--kernel-base-args", kernel_args, "--require-crypto14", "--finish-function",
                   "--break-symbol", symbol, "--output", destination]
        if name == "trust-cache":
            command += ["--read-kernel-symbol", "_trust_cache_static_loaded:1",
                        "--trace-symbol", "_trust_cache_runtime_init",
                        "--trace-symbol", "_load_static_trust_cache"]
            command += source_storage_metaclass_read_args(kernel)
            command += amfi_pmgr_trace_args(prelink_report)
        if name == "donor-runtime":
            command += donor_storage_metaclass_read_args()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=150)
        (output / f"{name}.log").parent.mkdir(parents=True, exist_ok=True)
        (output / f"{name}.log").write_text(result.stdout)
        report_path = destination / "gdb-stop.json"
        probe_report = None
        metaclass_observation = None
        donor_runtime_observation = None
        amfi_pmgr_observation = None
        expected_donor_stop = False
        if report_path.is_file():
            probe_report = json.loads(report_path.read_text())
            if name == "trust-cache" and prelink_report:
                amfi_pmgr_observation = inspect_amfi_pmgr_runtime(probe_report, prelink_report)
                probe_report["amfi_pmgr_runtime_observation"] = amfi_pmgr_observation
                metaclass_observation = inspect_source_storage_metaclasses(probe_report, kernel)
                probe_report["source_storage_metaclass_observation"] = metaclass_observation
                report_path.write_text(json.dumps(probe_report, indent=2) + "\n")
            if name == "donor-runtime" and prelink_report:
                donor_runtime_observation = inspect_donor_storage_metaclasses(probe_report, args.native_kernel.resolve())
                probe_report["donor_storage_metaclass_observation"] = donor_runtime_observation
                report_path.write_text(json.dumps(probe_report, indent=2) + "\n")
        if result.returncode:
            expected_donor_stop = (args.donor_runtime_audit and name == "donor-runtime" and
                                   probe_report is not None and
                                   expected_donor_runtime_terminal(probe_report, result.stdout))
            if expected_donor_stop:
                print("QEMU reached the expected AMFI No-PMGR diagnostic stop after the donor runtime capture.")
            else:
                message = format_qemu_probe_failure(name, result.stdout, report_path, output / f"{name}.log")
                if metaclass_observation is not None:
                    message += "\nSource storage metaclass observation: " + json.dumps(
                        metaclass_observation, sort_keys=True)
                if amfi_pmgr_observation is not None:
                    message += "\nAMFI PMGR runtime observation: " + json.dumps(
                        amfi_pmgr_observation, sort_keys=True)
                if donor_runtime_observation is not None:
                    message += "\nDonor storage metaclass observation: " + json.dumps(
                        donor_runtime_observation, sort_keys=True)
                raise SystemExit(message)
        if not report_path.is_file():
            raise SystemExit(f"QEMU probe {name} did not produce {report_path}")
        reports[name] = probe_report
        if name == "donor-runtime":
            if not donor_runtime_observation or \
                    donor_runtime_observation.get("capture_complete") is not True or \
                    donor_runtime_observation.get("all_class_sizes_nonzero") is not True or \
                    donor_runtime_observation.get("all_required_pointers_nonzero") is not True:
                raise SystemExit("QEMU did not capture five initialized original donor runtime metaclass objects: " +
                                 json.dumps(donor_runtime_observation, sort_keys=True))
            if result.returncode and not expected_donor_stop:
                raise SystemExit("Donor runtime probe failed outside the expected AMFI diagnostic stop")
        if reports[name].get('kernel_arguments') != kernel_args:
            raise SystemExit(f'{name}: QEMU did not use the hardware allocation arguments')
        shutil.rmtree(destination / "esp", ignore_errors=True)

    hashes = {report.get("kernel_sha256") for report in reports.values()}
    if len(hashes) != 1 or None in hashes:
        raise SystemExit("all checkpoints must use the same source XNU Mach-O")
    for name, report in reports.items():
        if report.get("bounded_timeout_interrupted") or not report.get("native_4k_mmu_observed"):
            raise SystemExit(f"{name}: native XNU initialization or bounded handoff failed")
        validate_corecrypto_state(report, prelink_report is not None)
        if name == "donor-runtime" and expected_donor_stop:
            if not expected_donor_runtime_terminal(report, (output / "donor-runtime.log").read_text()):
                raise SystemExit("Donor runtime probe did not stop at the expected AMFI No-PMGR checkpoint")
        elif report.get("pc") != report.get("function_return_target"):
            raise SystemExit(f"{name}: QEMU stopped before returning from the selected XNU checkpoint")
        pthread = report.get("pthread_globals", {})
        if int(pthread.get("_source_pthread_status", -1)) != 0 or \
                int(pthread.get("_pthread_functions", 0)) == 0 or \
                int(pthread.get("_pthread519_pthread_kern", 0)) == 0:
            raise SystemExit(f"{name}: the real source pthread provider was not registered before this checkpoint")
        if trust_cache and report.get("trust_cache_bundle_sha256") != trust_cache["sha256"]:
            raise SystemExit(f"{name}: DarwinBoot QEMU probe did not report the exact pinned trust-cache sidecar")

    platform = reports["platform"]
    if int(platform.get("x0", "0"), 16) != 1:
        raise SystemExit("ArmDTPlatform::configure did not return success")
    expected_platform = {
        "_armdt_platform_configured": 1,
        "_armdt_platform_cpu_count": 1,
        "_armdt_platform_bus_count": 1,
        "_armdt_platform_defaults_count": 1,
        "_armdt_platform_nvram_proxy": 1,
    }
    for key, value in expected_platform.items():
        if platform.get("armdt_globals", {}).get(key) != value:
            raise SystemExit(f"QEMU platform checkpoint {key} did not reach {value}")

    cpu = reports["cpu"]
    if int(cpu.get("x0", "0"), 16) != 1:
        raise SystemExit("ArmCpu::start did not register the single boot CPU")
    timer = reports["timer"].get("armcpu_globals", {})
    if any(timer.get(key) != 1 for key in ("_armcpu_registered", "_armcpu_gic_ready")) or \
            timer.get("_armcpu_timer_interrupts", 0) < 1:
        raise SystemExit("QEMU did not deliver a real architectural timer interrupt through ArmCpu")

    trust_cache_runtime = (validate_trust_cache_runtime(reports["trust-cache"], trust_cache["sha256"])
                           if trust_cache and prelink_report else None)

    budget_control = {
        "status": "not-run",
        "assertion_reproduced": None,
        "reason": "non-loadable prelink audit candidate cannot qualify source allocator fallback",
    }
    if should_run_default_budget_control(prelink_report):
        # Reproduce the hardware failure with this exact source-only kernel by
        # omitting only the explicit typed-zone budget. Stop at the real fatal
        # Assert, before panic enters the debugger; no guest state or assertion
        # is modified.
        negative_args = ' '.join([arg for arg in board_args if not arg.startswith('kt_fixed_zones=')]
                                 + ['serial=3', 'debug=0x14e'])
        destination = output / 'default-budget-refusal'
        result = subprocess.run([*common, '--kernel-base-args', negative_args, '--break-symbol', '_Assert',
                                 '--read-entry-region', '0:0:40', '--read-entry-region', '2:0:60',
                                 '--output', destination], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=150)
        (output / 'default-budget-refusal.log').write_text(result.stdout)
        if result.returncode:
            raise SystemExit(f'Default kalloc budget control failed:\n{result.stdout[-5000:]}')
        refusal = json.loads((destination / 'gdb-stop.json').read_text())
        validate_budget_refusal(refusal, reports['platform']['kernel_sha256'], negative_args)
        shutil.rmtree(destination / "esp", ignore_errors=True)
        budget_control = {"status": "passed", "assertion_reproduced": True,
                          "reason": "source-only kernel stopped at the expected fatal Assert"}

    summary = {
        "kernel_sha256": hashes.pop(),
        "kernel_kind": "prelink-audit-candidate" if prelink_report else "source-xnu",
        "prelink_report_sha256": prelink_report_sha256,
        "prelink_verification_sha256": prelink_verification_sha256,
        "xnu_config_sha256": xnu_config["sha256"] if xnu_config else None,
        "trust_cache_runtime_enabled": xnu_config["trust_cache_runtime_enabled"] if xnu_config else False,
        "prelink_structure_verified": bool(prelink_verification),
        "candidate_loadable": prelink_report.get("loadable") if prelink_report else None,
        "diagnostic_only": bool(prelink_report),
        "image4_crypto_hash_method": IMAGE4_CRYPTO_HASH_METHOD if prelink_report else None,
        "amfi_only_platform_code": AMFI_ONLY_PLATFORM_CODE if prelink_report else None,
        "trust_cache_bundle_sha256": trust_cache["sha256"] if trust_cache else None,
        "trust_cache_module_count": len(trust_cache["modules"]) if trust_cache else None,
        "trust_cache_adt_handoff_verified": bool(trust_cache_runtime),
        "trust_cache_runtime_consumed": bool(trust_cache_runtime),
        "trust_cache_runtime_checkpoint": trust_cache_runtime,
        "source_storage_metaclass_observation": (
            reports.get("trust-cache", {}).get("source_storage_metaclass_observation")
            if trust_cache and prelink_report else None),
        "amfi_pmgr_runtime_observation": (
            reports.get("trust-cache", {}).get("amfi_pmgr_runtime_observation")
            if trust_cache and prelink_report else None),
        "donor_runtime_metaclass_observation": (
            reports.get("donor-runtime", {}).get("donor_storage_metaclass_observation")
            if args.donor_runtime_audit else None),
        "donor_runtime_capture_complete": bool(
            reports.get("donor-runtime", {}).get("donor_storage_metaclass_observation", {}).get("capture_complete")
            if args.donor_runtime_audit else False),
        "donor_runtime_source_layout_matches": bool(
            reports.get("donor-runtime", {}).get("donor_storage_metaclass_observation", {}).get(
                "all_class_sizes_match_expected") and
            reports.get("donor-runtime", {}).get("donor_storage_metaclass_observation", {}).get(
                "all_superclasses_match_expected") if args.donor_runtime_audit else False),
        "donor_runtime_expected_amfi_stop": expected_donor_stop if args.donor_runtime_audit else False,
        "donor_runtime_abi_qualified": False,
        "hardware_kernel_arguments": board_args,
        "default_kalloc_budget_assertion_reproduced": budget_control["assertion_reproduced"],
        "default_kalloc_budget_control": budget_control,
        "compiled_reset_instruction_chain_present": True,
        "darwinboot_uefi_to_xnu": True,
        "qemu_cpus": 1,
        "armdt_platform_configured": True,
        "boot_cpu_registered": True,
        "gic_ready": True,
        "timer_interrupts": timer["_armcpu_timer_interrupts"],
        "hardware_boot_demonstrated": False,
        "device_flashable": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if args.donor_runtime_audit:
        print("PASS: donor runtime metaclasses captured; candidate remains ABI-unqualified and not for device")
    else:
        print("PASS: DarwinBoot -> source XNU, one QEMU CPU, Apple DT platform, GIC, timer, and trust-cache checkpoint")


if __name__ == "__main__":
    main()
