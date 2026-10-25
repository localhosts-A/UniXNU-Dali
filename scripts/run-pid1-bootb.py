#!/usr/bin/env python3
"""One boot_b-only owned PID1 experiment; dry-run works from the verified backup.

No automatic-fastboot claim is made. If fastboot does not return, the host leaves
an explicit pending-restore record and requires physical recovery. Storage writes
remain limited to the existing literal boot_b flasher/restorer.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys

DEVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootb_cycles', DEVICE / 'scripts/run-bootb-cycles.py')
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


def verify_pid1_artifact(directory, manifest):
    record = manifest.get('pid1', {})
    if record.get('root_file') != 'pid1.macho' or record.get('trust_cache_file') != 'pid1.trustcache' or \
            record.get('minimum_heartbeat_seconds') != 30 or record.get('test_instrument_only') is not True or \
            record.get('apple_userland_included') is not False or \
            record.get('runtime_trust_cache_provider') is not False or \
            record.get('entitlement_free_development_profile') is not True or \
            record.get('entitlements_supported') is not False or \
            record.get('apple_trust_chain_verified') is not False:
        raise cycle.CycleError('artifact lacks the owned PID1 identity and explicit trust-runtime limits')
    sources = []
    for prefix in ('root', 'trust_cache'):
        source = directory / record[prefix + '_file']
        if not source.is_file() or source.stat().st_size != record.get(prefix + '_bytes') or \
                cycle.sha256(source.read_bytes()) != record.get(prefix + '_sha256'):
            raise cycle.CycleError('PID1 root or trust-cache size/SHA256 differs from its artifact')
        sources.append(source)
    return tuple(sources)


def validate_pid1_log(data, token):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise cycle.CycleError('invalid PID1 evidence token')
    lines = data.decode('ascii', errors='replace').splitlines()
    prefix = 'DALI-PID1: run=' + token + ' '
    selected = [line for line in lines if line.startswith(prefix)]
    pattern = re.compile(re.escape(prefix) + r'stage=(heartbeat|PASS) pid=1 seq=(\d+) '
                         r'elapsed-ns=(\d+) root=mockfs readonly=1 pagesize=4096 backend=ramoops')
    messages = []
    for line in selected:
        match = pattern.fullmatch(line)
        if match:
            messages.append((match[1], int(match[2]), int(match[3])))
        elif not line.startswith(prefix + 'stage=end-reset-'):
            raise cycle.CycleError('current-token PID1 refusal or malformed evidence')
    if len(messages) != 31:
        raise cycle.CycleError('need exactly 31 complete current-token ramoops PID1 messages')
    for index, (stage, sequence, elapsed) in enumerate(messages):
        if sequence != index or stage != ('PASS' if index == 30 else 'heartbeat') or \
                (index == 0 and elapsed != 0) or \
                (index > 0 and elapsed - messages[index - 1][2] < 900_000_000):
            raise cycle.CycleError('PID1 sequence, stage or kernel clock evidence disagrees')
    if messages[-1][2] < 30_000_000_000:
        raise cycle.CycleError('PID1 did not prove 30 seconds of kernel monotonic time')
    return {'pid': 1, 'heartbeats': 31, 'kernel_elapsed_ns': messages[-1][2],
            'readonly_mockfs': True, 'page_bytes': 4096, 'backend': 'ramoops', 'run_token': token,
            'runtime_trust_cache_provider': False, 'apple_trust_chain_verified': False}


def collect_result(a, run, token, records):
    android = cycle.current_android(a.serial)
    hashes = cycle.compare_partition_hashes(a.serial, records)
    cycle.write_json(run / 'recovery-verification.json', {'android': android, 'partition_hashes': hashes,
                     'all_partition_hashes_match': True, 'automatic_fastboot': None})
    cycle.run_logged([sys.executable, a.uefi / 'tools/dali/collect.py', '--serial', a.serial,
                      '--output', run / 'collect', '--require-xnu', '--expected-run-token', token],
                     run / 'collect.log', timeout=120)
    logs = list((run / 'collect').glob('console-ramoops-*'))
    if len(logs) != 1:
        raise cycle.CycleError('one retained ramoops console is required for PID1 qualification')
    result = validate_pid1_log(logs[0].read_bytes(), token)
    result.update({'android_restored': True, 'all_partition_hashes_match': True,
                   'automatic_fastboot': None, 'evidence': str(run)})
    cycle.write_json(run / 'pid1-result.json', result)
    print(f'PASS: actual retained PID1/readonly-root/30s heartbeat and original Android restored. Evidence: {run}')


def restore_pending(a):
    run = a.restore_run.resolve()
    state_path = run / 'restore-state.json'
    state = json.loads(state_path.read_text())
    backup = a.backup.resolve()
    if state.get('serial') != a.serial or state.get('fastboot_serial') != a.fastboot_serial or \
            state.get('backup') != str(backup) or not re.fullmatch('[0-9a-f]{32}', state.get('token', '')):
        raise cycle.CycleError('pending restore identity/backup/token differs from the explicit command')
    _, records = cycle.verify_backup(backup, a.serial)
    if state.get('original_boot_b_sha256') != records['boot_b']['sha256']:
        raise cycle.CycleError('pending restore record differs from the verified original boot_b')
    if cycle.adb_online(a.serial):
        cycle.current_android(a.serial)
        cycle.compare_partition_hashes(a.serial, records)
    else:
        print(f'RESTORE ONLY: waiting up to {a.fastboot_timeout}s for {a.fastboot_serial}; no candidate flash.', flush=True)
        if not cycle.wait_fastboot(a.fastboot_serial, a.fastboot_timeout):
            raise cycle.CycleError('fastboot absent; original boot_b restoration is still pending')
        cycle.restore_android_safely(a.serial, a.fastboot_serial, backup, records, a.fastboot_timeout)
    state['boot_b_may_be_modified'] = False
    cycle.write_json(state_path, state)
    collect_result(a, run, state['token'], records)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('backup', 'evidence-root', 'uefi', 'unixnu', 'artifact-dir'):
        p.add_argument('--' + name, type=Path, required=name in ('backup', 'uefi'))
    p.add_argument('--serial', required=True)
    p.add_argument('--fastboot-serial')
    p.add_argument('--dry-run', action='store_true', help='package using the saved FDT without contacting a device')
    p.add_argument('--restore-run', type=Path, help='only restore and collect a previously recorded PID1 experiment')
    p.add_argument('--fastboot-timeout', type=int, default=120)
    a = p.parse_args()
    a.fastboot_serial = a.fastboot_serial or a.serial
    if not 90 <= a.fastboot_timeout <= 600:
        p.error('fastboot observation deadline must be within 90..600 seconds')
    a.uefi = a.uefi.resolve()
    if a.restore_run:
        if a.dry_run:
            p.error('--restore-run is separate from --dry-run')
        return restore_pending(a)
    if any(getattr(a, name) is None for name in ('unixnu', 'artifact_dir', 'evidence_root')):
        p.error('a new experiment requires --unixnu, --artifact-dir and --evidence-root')
    a.uefi, a.unixnu, a.artifact_dir = (path.resolve() for path in (a.uefi, a.unixnu, a.artifact_dir))
    repositories = (DEVICE, a.uefi, a.unixnu)
    backup = cycle.require_private_path(a.backup, repositories, 'backup')
    evidence = cycle.require_private_path(a.evidence_root, repositories, 'evidence')
    for repository in (DEVICE, a.unixnu):
        cycle.verify_clean_repository(repository)
    cycle.verify_clean_repository(a.uefi, allow_mu_patches=True)
    _, records = cycle.verify_backup(backup, a.serial)
    kernel, artifact = cycle.verify_kernel_artifact(a.artifact_dir, a.unixnu, DEVICE, pid1=True)
    if cycle.run_capture(['git', 'rev-parse', 'HEAD'], cwd=a.uefi).strip() != artifact['uefi_commit']:
        raise cycle.CycleError('UEFI checkout differs from the tested artifact pin')
    inputs = verify_pid1_artifact(a.artifact_dir, artifact)
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evidence, 0o700)
    run = evidence / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    run.mkdir(mode=0o700)
    if a.dry_run:
        fdt = (backup / 'runtime.dtb').read_bytes()
    else:
        cycle.current_android(a.serial)
        cycle.compare_partition_hashes(a.serial, records)
        fdt = cycle.adb_output(a.serial, 'cat /sys/firmware/fdt')
    cycle.verify_live_layout(fdt, backup, a.uefi)
    fdt_path = run / 'runtime.dtb'
    fdt_path.write_bytes(fdt)
    os.chmod(fdt_path, 0o600)
    adt = run / 'dali.adt'
    cycle.run_logged([sys.executable, DEVICE / 'scripts/make-dali-adt.py', '--fdt', fdt_path,
                      '--output', adt, '--dram-size', '0x300000000', '--ramdisk', '--trust-cache'],
                     run / 'adt.log', timeout=60)
    token = secrets.token_hex(16)
    # The artifact and this continuation profile both disable the short watchdog.
    recovery = {'auto_fastboot': False, 'device_watchdog_timeout_seconds': 31}
    image, manifest = cycle.build_candidate(a, backup, kernel, adt, recovery, token, run, pid1_inputs=inputs)
    if image.stat().st_size > records['boot_b']['bytes']:
        raise cycle.CycleError('PID1 candidate exceeds the protected boot_b partition size')
    cycle.write_json(run / 'candidate.json', manifest)
    cycle.write_json(run / 'experiment.json', {'token': token, 'dry_run': a.dry_run,
                     'firmware_watchdog_enabled': False, 'automatic_fastboot': None,
                     'artifact': artifact, 'candidate_sha256': manifest['sha256']})
    if a.dry_run:
        print(f'DRY RUN PASS: RAM-root image packaged and read back; no device contacted. Evidence: {run}')
        return 0
    cycle.current_android(a.serial)
    cycle.compare_partition_hashes(a.serial, records)
    cycle.request_fastboot_from_android(a.serial, a.fastboot_serial, a.fastboot_timeout)
    cycle.verify_fastboot(a.fastboot_serial, require_slot_b=True)
    restore_command = [sys.executable, str(Path(__file__).resolve()), '--backup', str(backup),
                       '--uefi', str(a.uefi), '--serial', a.serial, '--fastboot-serial', a.fastboot_serial,
                       '--restore-run', str(run), '--fastboot-timeout', '600']
    restore_state = {'boot_b_may_be_modified': True, 'token': token, 'serial': a.serial,
                     'fastboot_serial': a.fastboot_serial, 'backup': str(backup),
                     'original_boot_b_sha256': records['boot_b']['sha256'], 'restore_command': restore_command}
    try:
        cycle.verify_original_boot_b(backup / 'boot_b.img', records['boot_b'])
        if cycle.sha256(image.read_bytes()) != manifest['sha256']:
            raise cycle.CycleError('candidate changed after packaging')
        cycle.write_json(run / 'restore-state.json', restore_state)
        cycle.flash_boot_b(a.fastboot_serial, image)
        cycle.fastboot_command(a.fastboot_serial, 'reboot', timeout=20)
        if not cycle.wait_fastboot_disconnect(a.fastboot_serial, 15) or \
                not cycle.wait_fastboot(a.fastboot_serial, a.fastboot_timeout):
            raise cycle.CycleError('PID1 observation ended without fastboot; physical recovery is required')
    except Exception:
        if cycle.fastboot_online(a.fastboot_serial):
            cycle.restore_android_safely(a.serial, a.fastboot_serial, backup, records, a.fastboot_timeout)
            restore_state['boot_b_may_be_modified'] = False
            cycle.write_json(run / 'restore-state.json', restore_state)
        else:
            print('RESTORE PENDING: enter fastboot, then run ' + shlex.join(restore_command), flush=True)
        raise
    cycle.restore_android_safely(a.serial, a.fastboot_serial, backup, records, a.fastboot_timeout)
    restore_state['boot_b_may_be_modified'] = False
    cycle.write_json(run / 'restore-state.json', restore_state)
    collect_result(a, run, token, records)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except cycle.CycleError as error:
        print(f'run-pid1-bootb: {error}', file=sys.stderr)
        raise SystemExit(1)
