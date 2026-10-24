#!/usr/bin/env python3
"""One opt-in boot_b reset/ramoops trial, with a separate restore-only entry.

Only fresh logs and host enumeration are scored here. The owner's no-key
observation is recorded separately before any unattended qualification claim.
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
import shutil
import sys
import time

DEVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reset_cycles', DEVICE / 'scripts/run-bootb-cycles.py')
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)
METHODS = ('off', 'psci-cold', 'psci-warm', 'aee')


def collect_now(a, run, token, method):
    output = run / 'collect'
    if output.exists():
        return
    command = [sys.executable, a.uefi / 'tools/dali/collect.py', '--serial', a.serial,
               '--output', output, '--require-xnu', '--expected-run-token', token]
    if method != 'off':
        command += ['--expected-reset-method', method, '--require-software-reset']
    try:
        cycle.run_logged(command, run / 'collect.log', timeout=120)
    except Exception as error:
        # Missing logs are an expected trial outcome, never a reason to skip
        # original-image restoration or its partition verification.
        cycle.write_json(run / 'collection-error.json', {'error': str(error)})


def score_result(run, token, method, returned_fastboot):
    path = run / 'collect/result.json'
    logs = json.loads(path.read_text()) if path.is_file() else {}
    qualified = logs.get('recovery', {}).get('qualification', {})
    no_keys_logged = not logs.get('io', {}).get('keypad_events') and not logs.get('io', {}).get('input_events')
    technical_pass = bool(returned_fastboot and logs.get('xnu', {}).get('pass') and
                          qualified.get('pass') and qualified.get('method') == method and
                          logs.get('xnu', {}).get('run_token') == token and not logs.get('failures') and no_keys_logged)
    result = {'run_token': token, 'reset_method': method, 'returned_to_fastboot_in_deadline': returned_fastboot,
              'technical_evidence_pass': technical_pass, 'ramoops_retained': bool(logs.get('files')),
              'xnu_pass': logs.get('xnu', {}).get('pass', False), 'android_restored': True,
              'all_partition_hashes_match': True, 'unattended_observation': 'pending',
              'complete_unattended_pass': False, 'automatic_fastboot': None,
              'loss_stage': None if technical_pass else 'undetermined across full reset/restore chain'}
    cycle.write_json(run / 'reset-result.json', result)
    return result


def finish_restore(a, run, state, records):
    callback = lambda: collect_now(a, run, state['token'], state['reset_method'])
    cycle.restore_android_safely(a.serial, a.fastboot_serial, a.backup, records, a.fastboot_timeout,
                               post_boot=callback)
    state['boot_b_may_be_modified'] = False
    cycle.write_json(run / 'restore-state.json', state)
    hashes = cycle.compare_partition_hashes(a.serial, records)
    cycle.write_json(run / 'recovery-verification.json', {'android': cycle.current_android(a.serial),
                     'all_partition_hashes_match': True, 'partition_hashes': hashes})


def restore_pending(a):
    run = a.restore_run.resolve()
    state = json.loads((run / 'restore-state.json').read_text())
    if state.get('serial') != a.serial or state.get('fastboot_serial') != a.fastboot_serial or \
            state.get('backup') != str(a.backup) or not re.fullmatch('[0-9a-f]{32}', state.get('token', '')) or \
            state.get('reset_method') not in METHODS:
        raise cycle.CycleError('pending restore identity/token/method mismatch')
    _, records = cycle.verify_backup(a.backup, a.serial)
    if state.get('original_boot_b_sha256') != records['boot_b']['sha256']:
        raise cycle.CycleError('pending restore differs from original boot_b SHA-256')
    if cycle.adb_online(a.serial):
        cycle.current_android(a.serial)
        collect_now(a, run, state['token'], state['reset_method'])
        try:
            hashes = cycle.compare_partition_hashes(a.serial, records)
        except cycle.CycleError:
            cycle.request_fastboot_from_android(a.serial, a.fastboot_serial, a.fastboot_timeout)
            finish_restore(a, run, state, records)
        else:
            state['boot_b_may_be_modified'] = False
            cycle.write_json(run / 'restore-state.json', state)
            cycle.write_json(run / 'recovery-verification.json', {'android': cycle.current_android(a.serial),
                             'all_partition_hashes_match': True, 'partition_hashes': hashes})
    else:
        if not cycle.wait_fastboot(a.fastboot_serial, a.fastboot_timeout):
            raise cycle.CycleError('fastboot absent; original boot_b restore remains pending')
        finish_restore(a, run, state, records)
    score_result(run, state['token'], state['reset_method'], False)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('backup', 'evidence-root', 'uefi', 'unixnu', 'artifact-dir'):
        p.add_argument('--' + name, type=Path, required=name in ('backup', 'uefi'))
    p.add_argument('--serial', required=True)
    p.add_argument('--fastboot-serial')
    p.add_argument('--reset-method', choices=METHODS)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--restore-run', type=Path)
    p.add_argument('--fastboot-timeout', type=int, default=120)
    a = p.parse_args()
    a.fastboot_serial = a.fastboot_serial or a.serial
    a.uefi, a.backup = a.uefi.resolve(), a.backup.resolve()
    if a.restore_run:
        if a.dry_run or a.reset_method is not None or not 120 <= a.fastboot_timeout <= 600:
            p.error('restore-only takes no method/dry-run and a 120..600 second deadline')
        return restore_pending(a)
    if a.fastboot_timeout != 120 or a.reset_method is None or any(
            getattr(a, name) is None for name in ('unixnu', 'artifact_dir', 'evidence_root')):
        p.error('a new trial needs a method, checkout/artifact/evidence paths and exactly 120 seconds')
    a.unixnu, a.artifact_dir = a.unixnu.resolve(), a.artifact_dir.resolve()
    repositories = (DEVICE, a.unixnu, a.uefi)
    a.backup = cycle.require_private_path(a.backup, repositories, 'backup')
    evidence = cycle.require_private_path(a.evidence_root, repositories, 'evidence')
    for repository in (DEVICE, a.unixnu):
        cycle.verify_clean_repository(repository)
    cycle.verify_clean_repository(a.uefi, allow_mu_patches=True)
    _, records = cycle.verify_backup(a.backup, a.serial)
    kernel, artifact = cycle.verify_kernel_artifact(a.artifact_dir, a.unixnu, DEVICE, reset_qualification=True)
    if cycle.run_capture(['git', 'rev-parse', 'HEAD'], cwd=a.uefi).strip() != artifact['uefi_commit']:
        raise cycle.CycleError('UEFI checkout differs from the artifact pin')
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = evidence / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + a.reset_method)
    run.mkdir(mode=0o700)
    if a.dry_run:
        fdt = (a.backup / 'runtime.dtb').read_bytes()
    else:
        cycle.current_android(a.serial)
        cycle.compare_partition_hashes(a.serial, records)
        fdt = cycle.adb_output(a.serial, 'cat /sys/firmware/fdt')
    cycle.verify_live_layout(fdt, a.backup, a.uefi)
    fdt_path, adt = run / 'runtime.dtb', run / 'dali.adt'
    fdt_path.write_bytes(fdt)
    os.chmod(fdt_path, 0o600)
    cycle.run_logged([sys.executable, DEVICE / 'scripts/make-dali-adt.py', '--fdt', fdt_path,
                      '--output', adt, '--dram-size', '0x300000000'], run / 'adt.log', timeout=60)
    token = secrets.token_hex(16)
    recovery = {'auto_fastboot': False, 'device_watchdog_timeout_seconds': 31}
    image, manifest = cycle.build_candidate(a, a.backup, kernel, adt, recovery, token, run,
                                            reset_method=a.reset_method)
    if image.stat().st_size > records['boot_b']['bytes']:
        raise cycle.CycleError('candidate exceeds boot_b')
    private_image = run / 'candidate-boot.img'
    shutil.copyfile(image, private_image)
    os.chmod(private_image, 0o600)
    image = private_image
    cycle.write_json(run / 'candidate.json', manifest)
    cycle.write_json(run / 'experiment.json', {'token': token, 'reset_method': a.reset_method,
                     'dry_run': a.dry_run, 'firmware_watchdogs_enabled': False, 'artifact': artifact})
    if a.dry_run:
        print(f'DRY RUN PASS: {a.reset_method} packaged; no device contacted. Evidence: {run}')
        return 0
    cycle.current_android(a.serial)
    cycle.compare_partition_hashes(a.serial, records)
    cycle.request_fastboot_from_android(a.serial, a.fastboot_serial, 120)
    cycle.verify_fastboot(a.fastboot_serial, require_slot_b=True)
    restore_command = [sys.executable, str(Path(__file__).resolve()), '--backup', str(a.backup),
                       '--uefi', str(a.uefi), '--serial', a.serial, '--fastboot-serial', a.fastboot_serial,
                       '--restore-run', str(run), '--fastboot-timeout', '600']
    state = {'boot_b_may_be_modified': True, 'token': token, 'reset_method': a.reset_method,
             'serial': a.serial, 'fastboot_serial': a.fastboot_serial, 'backup': str(a.backup),
             'original_boot_b_sha256': records['boot_b']['sha256'], 'restore_command': restore_command}
    cycle.write_json(run / 'restore-state.json', state)
    try:
        cycle.verify_original_boot_b(a.backup / 'boot_b.img', records['boot_b'])
        if cycle.sha256(image.read_bytes()) != manifest['sha256']:
            raise cycle.CycleError('candidate changed after packaging')
        cycle.flash_boot_b(a.fastboot_serial, image)
        deadline = time.monotonic() + 120
        cycle.fastboot_command(a.fastboot_serial, 'reboot', timeout=20)
        returned = cycle.wait_fastboot_disconnect(a.fastboot_serial, 15) and \
            cycle.wait_fastboot(a.fastboot_serial, max(0, deadline - time.monotonic()))
        if not returned:
            raise cycle.CycleError('120-second reset observation failed; physical fastboot entry is required')
    except Exception:
        if cycle.fastboot_online(a.fastboot_serial):
            finish_restore(a, run, state, records)
            score_result(run, token, a.reset_method, False)
        else:
            print('RESTORE PENDING: enter fastboot, then run ' + shlex.join(restore_command), flush=True)
        raise
    finish_restore(a, run, state, records)
    result = score_result(run, token, a.reset_method, True)
    print(json.dumps(result, indent=2))
    return 0 if result['technical_evidence_pass'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except cycle.CycleError as error:
        print('run-reset-qualification: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
