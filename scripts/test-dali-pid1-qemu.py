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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('unixnu', 'kernel', 'uefi-efi', 'fixture', 'output-dir'):
        p.add_argument('--' + name, required=True, type=Path)
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
               '--read-user-symbol', '_pid1_failure:8', '--read-user-symbol', '_readonly_result:16',
               '--native-user-image', str(process), '--trust-cache', str(trust)]
    with (a.output_dir / 'probe.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    if result.returncode:
        raise SystemExit(f'PID1 QEMU probe failed; see {a.output_dir / "probe.log"}')
    report = json.loads((a.output_dir / 'probe/gdb-stop.json').read_text())
    summary = validate_report(report, *hashes)
    if report.get('kernel_arguments') != 'rd=md0 ' + base:
        raise SystemExit('PID1 QEMU used different kernel arguments')
    summary['run_token'] = token
    (a.output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print('PASS: real PID1, getpid/sysctl/select, readonly mockfs/EROFS and >=30 seconds of heartbeats')


if __name__ == '__main__':
    main()
