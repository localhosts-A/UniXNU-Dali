#!/usr/bin/env python3
"""Observe the owned PID1 at EL0 after real syscalls and 30 seconds of kernel time."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tomllib

DEVICE = Path(__file__).resolve().parents[1]


def counter(report, symbol):
    raw = bytes.fromhex(report.get('requested_kernel_symbols', {}).get(symbol, ''))
    if len(raw) != 8:
        raise ValueError('Actual source entitlement observation is missing: ' + symbol)
    return int.from_bytes(raw, 'little')


def validate_report(report, kernel_sha, process_sha, trust_sha):
    if report.get('boot_path') != 'uefi-unixnu-darwinboot' or \
            report.get('kernel_sha256') != kernel_sha or report.get('native_user_image_sha256') != process_sha or \
            report.get('trust_cache_bundle_sha256') != trust_sha or report.get('bounded_timeout_interrupted') or \
            not report.get('native_user_el0_observed') or not report.get('native_user_4k_abi_observed'):
        raise ValueError('PID1 did not reach its real EL0 completion instruction with native 4K syscalls')
    syscalls = {'pid': 1, 'sysctl_result': 0, 'page_size': 4096, 'old_length': 4,
                'getpid_carry': 0, 'sysctl_carry': 0}
    if any(report.get('native_user_syscalls', {}).get(k) != v for k, v in syscalls.items()):
        raise ValueError('Actual getpid/HW_PAGESIZE syscall values disagree with the EL0 flags')
    if report.get('amfi_interface_pointer') != 0 or \
            counter(report, '_source_entitlement_free_accepts') < 1 or \
            counter(report, '_source_entitlement_free_query_denials') < 1 or \
            counter(report, '_source_entitlement_free_refusals') != 0 or \
            counter(report, '_source_entitlement_free_entitlement_refusals') != 0:
        raise ValueError('The actual source no-entitlement path did not accept precisely the supported input')
    raw_state = bytes.fromhex(report.get('requested_kernel_symbols', {}).get('_dali_pid1_report', ''))
    if len(raw_state) != 48:
        raise ValueError('Actual compiled kernel PID1 observation is missing')
    state = dict(zip(('active', 'offline', 'pid', 'sequence', 'readonly_root', 'failures',
                      'complete', 'reserved', 'first_ns', 'elapsed_ns'), struct.unpack('<8I2Q', raw_state)))
    expected = {'active': 1, 'offline': 1, 'pid': 1, 'sequence': 30, 'readonly_root': 1,
                'failures': 0, 'complete': 1, 'reserved': 0}
    if any(state.get(k) != v for k, v in expected.items()) or state.get('elapsed_ns', 0) < 30_000_000_000:
        raise ValueError('Actual kernel PID1/root/sequence/time evidence incomplete')
    memory = report.get('native_user_memory', {})
    if int.from_bytes(bytes.fromhex(memory.get('_pid1_failure', '01')), 'little') != 0:
        raise ValueError('Own PID1 reported a syscall failure')
    raw = bytes.fromhex(memory.get('_readonly_result', ''))
    if len(raw) != 16 or int.from_bytes(raw[:8], 'little') != 30 or not (int.from_bytes(raw[8:], 'little') & (1 << 29)):
        raise ValueError('Actual write-open did not return Darwin EROFS')
    # Inputs are recorded by this runner; this result is explicitly offline and
    # cannot satisfy the hardware ramoops/recovery acceptance gate.
    return {'kernel_sha256': kernel_sha, 'process_sha256': process_sha,
            'trust_cache_sha256': trust_sha, 'pid': 1, 'readonly_mockfs': True,
            'write_open_errno': 30, 'kernel_elapsed_ns': state['elapsed_ns'],
            'heartbeats': 31, 'runtime_trust_cache_provider': False,
            'apple_trust_chain_verified': False, 'hardware_demonstrated': False}


def validate_refused_report(denied, returncode, kernel_sha, process_sha, trust_sha):
    if returncode != 1 or denied.get('boot_path') != 'uefi-unixnu-darwinboot' or \
            denied.get('kernel_sha256') != kernel_sha or denied.get('native_user_image_sha256') != process_sha or \
            denied.get('trust_cache_bundle_sha256') != trust_sha or denied.get('kernel_exception') or \
            denied.get('bounded_timeout_interrupted') is not False or denied.get('stop_symbol') != '_panic' or \
            denied.get('native_user_el0_observed') is not False or denied.get('amfi_interface_pointer') != 0 or \
            counter(denied, '_source_entitlement_free_refusals') < 1 or \
            counter(denied, '_source_entitlement_free_entitlement_refusals') < 1 or \
            counter(denied, '_source_entitlement_free_query_denials') != 0 or \
            counter(denied, '_source_entitlement_free_accepts') != 0:
        raise ValueError('Valid ad-hoc entitlement input was not genuinely refused before EL0')
    return {'unsupported_entitlements_refused': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('unixnu', 'kernel', 'uefi-efi', 'fixture', 'output-dir'):
        p.add_argument('--' + name, required=True, type=Path)
    p.add_argument('--entitlement-negative-fixture', required=True, type=Path)
    a = p.parse_args()
    a.output_dir.mkdir(parents=True, exist_ok=True)
    fixture = a.fixture.resolve()
    process = fixture / 'launchd.macho'
    trust = fixture / 'trustcache.bundle'
    hashes = [hashlib.sha256(x.read_bytes()).hexdigest() for x in (a.kernel, process, trust)]
    board = tomllib.loads((DEVICE / 'board.toml').read_text())
    token = secrets.token_hex(16)
    base = ' '.join([*board['payload']['kernel_args'], 'dali-pid1=1', 'dali-pid1-qemu=1',
                     'dali-auto-fastboot=0', 'dali-run=' + token, 'serial=3', 'debug=0x14e'])
    command = [sys.executable, str(a.unixnu / 'scripts/probe-darwinboot-xnu.py'),
               '--kernel', str(a.kernel.resolve()), '--uefi-efi', str(a.uefi_efi.resolve()),
               '--output', str(a.output_dir / 'probe'), '--timeout', '180',
               '--armvirt', '--armcpu', '--nvram-proxy', '--require-crypto14',
               '--kernel-base-args', base, '--native-user',
               '--test-service', 'dali-boot-log:localhosts,dali-boot-log',
               '--read-kernel-symbol', '_dali_pid1_report:48',
               '--read-kernel-symbol', '_source_entitlement_free_accepts:8',
               '--read-kernel-symbol', '_source_entitlement_free_refusals:8',
               '--read-kernel-symbol', '_source_entitlement_free_entitlement_refusals:8',
               '--read-kernel-symbol', '_source_entitlement_free_query_denials:8',
               '--read-user-symbol', '_pid1_failure:8', '--read-user-symbol', '_readonly_result:16',
               '--native-user-image', str(process), '--trust-cache', str(trust)]
    with (a.output_dir / 'probe.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    if result.returncode:
        # Expose the actual bounded guest failure in CI without requiring a
        # full artifact download; the complete raw report is still uploaded.
        report_path = a.output_dir / 'probe/gdb-stop.json'
        if report_path.is_file():
            try:
                failed = json.loads(report_path.read_text())
                print(json.dumps({key: failed.get(key) for key in (
                    'kernel_sha256', 'kernel_exception', 'stop_symbol',
                    'bounded_timeout_interrupted', 'native_user_el0_observed',
                    'amfi_interface_pointer', 'requested_kernel_symbols')}, indent=2), flush=True)
            except (OSError, ValueError) as error:
                print(f'PID1 guest report unavailable: {error}', flush=True)
        raise SystemExit(f'PID1 QEMU probe failed; see {a.output_dir / "probe.log"}')
    report = json.loads((a.output_dir / 'probe/gdb-stop.json').read_text())
    summary = validate_report(report, *hashes)
    if report.get('kernel_arguments') != 'rd=md0 ' + base:
        raise SystemExit('PID1 QEMU used different kernel arguments')
    negative = a.output_dir / 'entitlement-negative'
    negative_command = command.copy()
    for flag, value in (('--output', str(negative)),
                        ('--native-user-image', str(a.entitlement_negative_fixture / 'launchd.macho')),
                        ('--trust-cache', str(a.entitlement_negative_fixture / 'trustcache.bundle'))):
        negative_command[negative_command.index(flag) + 1] = value
    with (a.output_dir / 'entitlement-negative.log').open('w') as log:
        # The generic probe correctly returns failure when exec never reaches EL0.
        result = subprocess.run(negative_command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    denied = json.loads((negative / 'gdb-stop.json').read_text())
    expected_process = hashlib.sha256((a.entitlement_negative_fixture / 'launchd.macho').read_bytes()).hexdigest()
    expected_trust = hashlib.sha256((a.entitlement_negative_fixture / 'trustcache.bundle').read_bytes()).hexdigest()
    summary.update(validate_refused_report(denied, result.returncode, hashes[0], expected_process, expected_trust))
    if denied.get('kernel_arguments') != 'rd=md0 ' + base:
        raise SystemExit('Entitlement negative QEMU used different kernel arguments')
    summary['run_token'] = token
    (a.output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print('PASS: real PID1, getpid/sysctl/select, readonly mockfs/EROFS and >=30 seconds of heartbeats')


if __name__ == '__main__':
    main()
