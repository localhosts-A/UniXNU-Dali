#!/usr/bin/env python3
"""Run bounded single-CPU DarwinBoot/XNU QEMU checkpoints for the Dali build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib


DEVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEVICE_ROOT / "scripts"))
from dali_ipados_trust_cache import parse_bundle

IMAGE4_CRYPTO_HASH_METHOD = "sha2-384"
AMFI_ONLY_PLATFORM_CODE = True


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
    parser.add_argument("--trust-cache", type=Path,
                        help="Exact board-pinned original iPadOS trust-cache bundle for DarwinBoot handoff")
    parser.add_argument("--uefi-efi", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.unixnu.resolve()
    output = args.output_dir.resolve()
    kernel = args.kernel.resolve()
    prelink_report = None
    prelink_report_sha256 = None
    if args.prelink_report:
        report_path = args.prelink_report.resolve()
        prelink_report = validate_prelink_audit_candidate(kernel, report_path)
        prelink_report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if not args.trust_cache:
            parser.error("the pinned iPadOS prelink probe requires the board trust-cache bundle")
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
    reports = {}
    for name, symbol in cases.items():
        destination = output / name
        command = [*common, "--kernel-base-args", kernel_args, "--require-crypto14", "--finish-function",
                   "--break-symbol", symbol, "--output", destination]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=150)
        (output / f"{name}.log").parent.mkdir(parents=True, exist_ok=True)
        (output / f"{name}.log").write_text(result.stdout)
        if result.returncode:
            raise SystemExit(f"QEMU probe {name} failed; see {output / f'{name}.log'}\n{result.stdout[-5000:]}")
        report_path = destination / "gdb-stop.json"
        if not report_path.is_file():
            raise SystemExit(f"QEMU probe {name} did not produce {report_path}")
        reports[name] = json.loads(report_path.read_text())
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
        if report.get("pc") != report.get("function_return_target"):
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
        "candidate_loadable": prelink_report.get("loadable") if prelink_report else None,
        "diagnostic_only": bool(prelink_report),
        "image4_crypto_hash_method": IMAGE4_CRYPTO_HASH_METHOD if prelink_report else None,
        "amfi_only_platform_code": AMFI_ONLY_PLATFORM_CODE if prelink_report else None,
        "trust_cache_bundle_sha256": trust_cache["sha256"] if trust_cache else None,
        "trust_cache_module_count": len(trust_cache["modules"]) if trust_cache else None,
        "trust_cache_adt_handoff_verified": False,
        "trust_cache_runtime_consumed": False,
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
    print("PASS: DarwinBoot -> source XNU, one QEMU CPU, Apple DT platform, GIC and timer interrupt")


if __name__ == "__main__":
    main()
