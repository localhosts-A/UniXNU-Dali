#!/usr/bin/env python3
"""Run bounded single-CPU DarwinBoot/XNU QEMU checkpoints for the Dali build."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib


DEVICE_ROOT = Path(__file__).resolve().parents[1]


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
        find('recoveryRead')
        find('recoveryFence')
        def calls(body, callee):
            return bool(re.search(r'\bbl\s+[^\n]*<' + re.escape(callee) + r'>', body))
        if not calls(assembly['__ZN11DaliBootLog5startEP9IOService'], recover) or \
                not calls(assembly['recoverToFastboot'], issue):
            raise SystemExit('Compiled Dali checkpoint cannot call the guarded software-reset sequence')
        reset = assembly['recoverySystemReset']
        def function_id(low, high):
            return bool(re.search(r'\bmov\s+w0, #(?:' + hex(low) + '|' + str(low) + r')\b', reset) and
                re.search(r'\bmovk\s+w0, #(?:' + hex(high) + '|' + str(high) + r'), lsl #16\b', reset))
        if not function_id(10, 0x8400) or not function_id(18, 0xC400) or \
                len(re.findall(r'\bsmc\s+#(?:0x0|0)\b', reset)) != 2 or \
                not re.search(r'\bmsr\s+DAIFSet, #(?:0x3|3)\b', reset, re.I) or \
                not re.search(r'\bmsr\s+DAIF,', reset, re.I) or \
                not re.search(r'\bdsb\s+sy\b', reset) or \
                not all(re.search(r'\bdsb\s+sy\b', assembly[key]) for key in ('recoveryRead', 'recoveryFence')):
            raise SystemExit('Compiled PSCI FEATURES/RESET2 SMCs, DAIF restoration or Device-memory barriers are missing')
    finally:
        (output / 'reset-instructions.log').write_text('\n'.join(assembly.values()))
    (output / 'reset-instructions.json').write_text(json.dumps({
        'compiled_checkpoint_calls_recovery': True,
        'compiled_recovery_calls_guarded_reset': True,
        'psci_features_reset2_smcs_daif_and_barriers_present': True,
        'hardware_recovery_demonstrated': False,
    }, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unixnu", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--uefi-efi", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.unixnu.resolve()
    output = args.output_dir.resolve()
    validate_reset_instructions(args.kernel.resolve(), output)
    probe = root / "scripts/probe-darwinboot-xnu.py"
    board_args = hardware_arguments()
    # Keep only console diagnostics specific to the offline VM. Allocation
    # policy comes from the hardware configuration, never the probe's defaults.
    kernel_args = ' '.join([*board_args, 'serial=3', 'debug=0x14e'])
    common = [sys.executable, probe, "--kernel", args.kernel.resolve(), "--uefi-efi", args.uefi_efi.resolve(),
              "--timeout", "90", "--armvirt", "--armcpu", "--nvram-proxy"]
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

    hashes = {report.get("kernel_sha256") for report in reports.values()}
    if len(hashes) != 1 or None in hashes:
        raise SystemExit("all checkpoints must use the same source XNU Mach-O")
    for name, report in reports.items():
        if report.get("bounded_timeout_interrupted") or not report.get("native_4k_mmu_observed") or \
                not report.get("crypto14_initialization_observed"):
            raise SystemExit(f"{name}: native XNU initialization or bounded handoff failed")
        if report.get("pc") != report.get("function_return_target"):
            raise SystemExit(f"{name}: QEMU stopped before returning from the selected XNU checkpoint")
        pthread = report.get("pthread_globals", {})
        if int(pthread.get("_source_pthread_status", -1)) != 0 or \
                int(pthread.get("_pthread_functions", 0)) == 0 or \
                int(pthread.get("_pthread519_pthread_kern", 0)) == 0:
            raise SystemExit(f"{name}: the real source pthread provider was not registered before this checkpoint")

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

    # Reproduce the hardware failure with this exact kernel by omitting only
    # the explicit typed-zone budget. Stop at the real fatal Assert, before
    # panic enters the debugger; no guest state or assertion is modified.
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

    summary = {
        "kernel_sha256": hashes.pop(),
        "hardware_kernel_arguments": board_args,
        "default_kalloc_budget_assertion_reproduced": True,
        "compiled_reset_instruction_chain_present": True,
        "darwinboot_uefi_to_xnu": True,
        "qemu_cpus": 1,
        "armdt_platform_configured": True,
        "boot_cpu_registered": True,
        "gic_ready": True,
        "timer_interrupts": timer["_armcpu_timer_interrupts"],
        "hardware_boot_demonstrated": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("PASS: DarwinBoot -> source XNU, one QEMU CPU, Apple DT platform, GIC and timer interrupt")


if __name__ == "__main__":
    main()
