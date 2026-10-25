#!/usr/bin/env python3
"""One manual source-XNU UFS read/sync trial with durable original-only recovery."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

DEVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEVICE / 'scripts'))
from ufs_xnu_evidence import score_xnu_readonly

probe_spec = importlib.util.spec_from_file_location('dali_xnu_ufs_probe_helpers', DEVICE / 'scripts/run-ufs-probe.py')
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)
cycle = probe.cycle
KIND = 'dali-source-xnu-ufs-read-only'
MODE = 'xnu-ufs-read-only'


def copy_private(source, destination):
    data = Path(source).read_bytes()
    with Path(destination).open('xb') as target:
        os.chmod(destination, 0o400)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    return hashlib.sha256(data).hexdigest()


def paths_overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def candidate_binding(manifest, token, kernel_manifest, live_fdt):
    payload = manifest.get('payload', {})
    profile = manifest.get('ufs_xnu', {})
    firmware = manifest.get('ufs_host', {})
    xnu_payload = payload.get('xnu_ufs_read_only', {}) if isinstance(payload, dict) else {}
    recovery = payload.get('recovery', {}) if isinstance(payload, dict) else {}
    if (manifest.get('target') != 'dali' or not isinstance(payload, dict) or
            not isinstance(profile, dict) or not isinstance(firmware, dict) or
            not isinstance(xnu_payload, dict) or not isinstance(recovery, dict) or
            profile.get('enabled') is not True or profile.get('run_token') != token or
            profile.get('write_authorized') is not False or profile.get('physical_ufs_qualified') is not False or
            firmware.get('enabled') is not True or firmware.get('mode') != 'read-only-blockio' or
            firmware.get('run_token') != token or firmware.get('media_writes') is not False or
            firmware.get('write_authorized') is not False or
            xnu_payload.get('enabled') is not True or
            xnu_payload.get('run_token') != token or
            xnu_payload.get('write_authorized') is not False or
            payload.get('xnu_run_token') != token or
            payload.get('xnu_kernel_sha256') != kernel_manifest.get('kernel_sha256') or
            profile.get('live_fdt_sha256') != hashlib.sha256(Path(live_fdt).read_bytes()).hexdigest() or
            profile.get('live_fdt_sha256') != payload.get('xnu_ufs_read_only', {}).get('live_fdt_sha256') or
            payload.get('xnu_ramdisk_sha256') is not None or payload.get('xnu_trust_cache_sha256') is not None or
            recovery.get('auto_fastboot') is not False or
            recovery.get('reset_method') is not None):
        raise cycle.CycleError('candidate does not bind source XNU, live FDT, firmware handoff and one read-only token')


def private_candidate(args, run, kernel, kernel_manifest, adt, live_fdt, token, records):
    packed = run / 'candidate-build'
    packed.mkdir(mode=0o700)
    image, manifest = cycle.build_candidate(args, args.backup, kernel, adt, kernel_manifest['recovery'],
                                            token, packed, xnu_ufs_readonly=True, live_fdt=live_fdt)
    if image.stat().st_size > records['boot_b']['bytes']:
        raise cycle.CycleError('source-XNU UFS candidate exceeds original boot_b')
    candidate_binding(manifest, token, kernel_manifest, live_fdt)

    sealed = run / 'candidate'
    sealed.mkdir(mode=0o700)
    copies = {
        'UniXNU-dali-uefi-boot.img': image,
        'SILICIUM_UEFI.fd': args.uefi / 'Build/daliPkg/DEBUG_CLANGPDB/FV/SILICIUM_UEFI.fd',
        'payload.img': args.uefi / 'out/payload.img',
        'runtime-live.dtb': live_fdt,
        'source-kernel.macho': kernel,
        'kernel.macho.adt': adt,
    }
    for name, source in copies.items():
        copy_private(source, sealed / name)
    (sealed / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    os.chmod(sealed / 'manifest.json', 0o400)
    checked_image, checked_manifest = probe.verify_candidate(sealed, args.uefi, args.unixnu, args.backup, MODE)
    if (cycle.sha256(checked_image.read_bytes()) != manifest['sha256'] or
            checked_manifest != manifest or cycle.sha256((sealed / 'source-kernel.macho').read_bytes()) != kernel_manifest['kernel_sha256'] or
            cycle.sha256((sealed / 'kernel.macho.adt').read_bytes()) != manifest['payload']['xnu_adt_sha256']):
        raise cycle.CycleError('sealed source-XNU candidate differs after independent profile verification')
    return sealed, manifest


def xnu_scorer(args, run, state):
    def score(text, token):
        failure = {'run_token': token, 'observation_complete': False,
                   'device_identity_verified_by_scorer': False, 'xnu_ufs_qualified': False,
                   'firmware_xnu_handoff_qualified': False, 'media_writes_qualified': False,
                   'persistence_qualified': False, 'physical_ufs_qualified': False}
        try:
            if (state.get('kind') != KIND or state.get('serial') != args.serial or
                    state.get('fastboot_serial') != args.fastboot_serial or state.get('backup') != str(args.backup) or
                    state.get('token') != token or state.get('dry_run') is not False or
                    state.get('candidate_verified_before_flash') is not True or
                    state.get('device_verified_before_flash') is not True or
                    state.get('baseline_folder') != 'live-baseline'):
                raise ValueError('source-XNU trial identity/baseline binding mismatch')
            baseline = probe.readonly.load_baseline(run / state['baseline_folder'], serial=args.serial,
                                                     original_boot_b=args.backup / 'boot_b.img')
            if baseline['baseline_sha256'] != state.get('baseline_sha256'):
                raise ValueError('source-XNU live baseline changed')
            candidate_record = (run / 'candidate/manifest.json').read_bytes()
            manifest = json.loads(candidate_record)
            candidate = (run / 'candidate/UniXNU-dali-uefi-boot.img').read_bytes()
            if (cycle.sha256(candidate_record) != state.get('candidate_record_sha256') or
                    cycle.sha256(candidate) != state.get('candidate_sha256') or
                    manifest.get('payload', {}).get('xnu_run_token') != token or
                    manifest.get('ufs_xnu', {}).get('run_token') != token):
                raise ValueError('sealed source-XNU candidate/token changed')
            uefi = probe.readonly.score_readonly(text, token, baseline, candidate)
            firmware_luns = uefi.get('observed_roots', {}).keys() if uefi.get('uefi_read_only_pass') else None
            result = score_xnu_readonly(text, token, baseline, candidate, firmware_luns)
            restored_bound = (state.get('restore_verified') is True and
                              state.get('all_partition_hashes_match') is True and
                              state.get('android_root_verified') is True and
                              state.get('original_boot_b_restored') is True)
            xnu_bound = restored_bound and result.get('xnu_read_only_pass') is True
            uefi_bound = restored_bound and uefi.get('uefi_read_only_pass') is True
            handoff_bound = (restored_bound and uefi.get('uefi_read_only_pass') is True and
                             result.get('xnu_read_only_pass') is True and
                             result.get('firmware_xnu_handoff_qualified') is True)
            result.update(uefi_read_only_pass=bool(uefi.get('uefi_read_only_pass')),
                          uefi_sync_command_pass=bool(uefi.get('uefi_sync_command_pass')),
                          uefi_diagnostic_stop_pass=bool(uefi.get('diagnostic_stop_pass')),
                          uefi_read_only_qualified=bool(uefi_bound),
                          observation_complete=bool(uefi.get('uefi_read_only_pass') and result.get('xnu_read_only_pass')),
                          device_identity_bound_to_run=restored_bound,
                          candidate_bound_to_run=restored_bound,
                          xnu_ufs_read_only_qualified=bool(xnu_bound),
                          firmware_xnu_handoff_qualified=bool(handoff_bound),
                          # The complete plan still requires the bounded write and persistence phases.
                          xnu_ufs_qualified=False, physical_ufs_qualified=False,
                          media_writes_qualified=False, persistence_qualified=False,
                          evidence_exported_before_original_restore=False)
            return result
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            return dict(failure, binding_error=str(error))
    return score


def collect_evidence(args, run, state):
    return probe.collect_now(args.serial, run, state['token'], xnu_scorer(args, run, state))


def finish_restore(args, run, state, records, lease):
    # Ramoops is exported at the first Android callback after the original-only
    # boot_b restore. A future write/persistence cycle must add pre-restore export.
    callback = lambda: collect_evidence(args, run, state)
    cycle.restore_android_safely(args.serial, args.fastboot_serial, args.backup, records,
                                 args.wait_seconds, post_boot=callback)
    android = cycle.current_android(args.serial)
    hashes = cycle.compare_partition_hashes(args.serial, records)
    if hashes.get('boot_b') != records['boot_b']['sha256']:
        raise cycle.CycleError('source-XNU original boot_b recovery hash mismatch')
    state.update(restore_verified=True, original_boot_b_restored=True,
                 all_partition_hashes_match=True, partition_hashes=hashes,
                 android_root_verified=True, android=android)
    probe.durable_json(run / 'recovery-verification.json', {
        'android': android, 'partition_hashes': hashes, 'original_boot_b_restored': True,
        'all_partition_hashes_match': True, 'all_18_original_hashes_match': True,
        'evidence_exported_before_original_restore': False})
    lease.restored(run, state)
    evidence = collect_evidence(args, run, state)
    result = dict(evidence, android_restored=True, original_boot_b_restored=True,
                  all_partition_hashes_match=True, all_18_original_hashes_match=True,
                  manual_keys_allowed=True, evidence_exported_before_original_restore=False,
                  complete_ufs_read_write_pass=False)
    probe.durable_json(run / 'xnu-ufs-result.json', result)
    return result


def restore_pending(args, lease):
    run = args.restore_run.resolve()
    state = json.loads((run / 'restore-state.json').read_text())
    if (not isinstance(state, dict) or state.get('kind') != KIND or state.get('dry_run') is not False or
            state.get('serial') != args.serial or state.get('fastboot_serial') != args.fastboot_serial or
            state.get('backup') != str(args.backup) or not probe.TOKEN.fullmatch(state.get('token', '')) or
            lease.data['pending_run'] not in (None, str(run))):
        raise cycle.CycleError('source-XNU UFS restore-only identity/token mismatch')
    _, records = cycle.verify_backup(args.backup, args.serial)
    if state.get('original_boot_b_sha256') != records['boot_b']['sha256']:
        raise cycle.CycleError('source-XNU restore-only original boot_b hash mismatch')
    if cycle.adb_online(args.serial):
        android = cycle.current_android(args.serial)
        try:
            hashes = cycle.compare_partition_hashes(args.serial, records)
        except cycle.CycleError:
            cycle.request_fastboot_from_android(args.serial, args.fastboot_serial, args.wait_seconds)
        else:
            if hashes['boot_b'] == records['boot_b']['sha256']:
                state.update(restore_verified=True, original_boot_b_restored=True,
                             all_partition_hashes_match=True, partition_hashes=hashes,
                             android_root_verified=True, android=android)
                evidence = collect_evidence(args, run, state)
                probe.durable_json(run / 'recovery-verification.json', {
                    'android': android, 'partition_hashes': hashes, 'original_boot_b_restored': True,
                    'all_partition_hashes_match': True, 'all_18_original_hashes_match': True,
                    'evidence_exported_before_original_restore': False})
                lease.restored(run, state)
                probe.durable_json(run / 'xnu-ufs-result.json', dict(
                    evidence, android_restored=True, original_boot_b_restored=True,
                    all_partition_hashes_match=True, all_18_original_hashes_match=True,
                    manual_keys_allowed=True, evidence_exported_before_original_restore=False,
                    complete_ufs_read_write_pass=False))
                return
    if not cycle.wait_fastboot(args.fastboot_serial, args.wait_seconds):
        raise cycle.CycleError('fastboot absent; original-only source-XNU restore remains pending')
    finish_restore(args, run, state, records, lease)


def run_trial(args, lease):
    for repo in (DEVICE, args.unixnu):
        cycle.verify_clean_repository(repo)
    cycle.verify_clean_repository(args.uefi, allow_mu_patches=True)
    _, records = cycle.verify_backup(args.backup, args.serial)
    expected = probe.readonly.load_baseline(args.baseline_dir, serial=args.serial,
                                             original_boot_b=args.backup / 'boot_b.img')
    kernel, kernel_manifest = cycle.verify_kernel_artifact(args.artifact_dir, args.unixnu, DEVICE,
                                                            reset_qualification=True)
    if kernel_manifest.get('device_commit') != cycle.run_capture(['git', 'rev-parse', 'HEAD'], cwd=DEVICE).strip():
        raise cycle.CycleError('XNU source artifact does not bind this read-only profile source revision')
    token = secrets.token_hex(16)
    lease.require_new(token)
    if not args.dry_run:
        cycle.current_android(args.serial)
        cycle.compare_partition_hashes(args.serial, records)

    args.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.evidence_root, 0o700)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run = args.evidence_root / ('run-' + timestamp + '-' + token[:8])
    run.mkdir(mode=0o700)
    probe.prepare_readonly_baseline(argparse.Namespace(baseline_dir=args.baseline_dir, serial=args.serial,
        backup=args.backup), run)
    if args.dry_run:
        live_fdt = args.backup / 'runtime.dtb'
    else:
        live_result = probe.collect_live_readonly_baseline(
            argparse.Namespace(serial=args.serial, backup=args.backup), run, expected)
        if live_result['baseline_sha256'] != expected['baseline_sha256']:
            raise cycle.CycleError('live device baseline differs from the preverified Android baseline')
        fdt = cycle.adb_output(args.serial, 'cat /sys/firmware/fdt', timeout=30)
        if not fdt:
            raise cycle.CycleError('live UFS trial FDT is empty')
        cycle.verify_live_layout(fdt, args.backup, args.uefi)
        live_fdt = run / 'runtime-live.dtb'
        with live_fdt.open('xb') as target:
            os.chmod(target.name, 0o600); target.write(fdt); target.flush(); os.fsync(target.fileno())
    if args.dry_run:
        fdt = live_fdt.read_bytes()
        cycle.verify_live_layout(fdt, args.backup, args.uefi)
        # Preserve the complete recovery/input bundle before producing the image.
        with (run / 'runtime-live.dtb').open('xb') as target:
            os.chmod(target.name, 0o600); target.write(fdt)
        live_fdt = run / 'runtime-live.dtb'
    adt = run / 'dali.adt'
    cycle.run_logged([sys.executable, DEVICE / 'scripts/make-dali-adt.py', '--fdt', live_fdt,
                      '--output', adt, '--dram-size', '0x300000000', '--cpu-regs', '0',
                      '--psci-method', 'smc', '--ufs-readonly'], run / 'adt.log', timeout=60)
    kernel_copy = run / 'source-kernel.macho'
    artifact_copy = run / 'source-kernel-manifest.json'
    copy_private(kernel, kernel_copy); copy_private(args.artifact_dir / 'manifest.json', artifact_copy)
    private_dir, manifest = private_candidate(
        argparse.Namespace(backup=args.backup, uefi=args.uefi, unixnu=args.unixnu),
        run, kernel_copy, kernel_manifest, adt, live_fdt, token, records)
    source_kernel_sha = kernel_manifest['kernel_sha256']
    state = {'kind': KIND, 'dry_run': args.dry_run, 'token': token,
             'serial': args.serial, 'fastboot_serial': args.fastboot_serial, 'backup': str(args.backup),
             'original_boot_b_sha256': records['boot_b']['sha256'], 'boot_b_may_be_modified': False,
             'kernel_artifact_sha256': source_kernel_sha, 'device_commit': kernel_manifest['device_commit'],
             'unixnu_commit': kernel_manifest['unixnu_commit'], 'xnu_commit': kernel_manifest['xnu_commit'],
             'uefi_commit': kernel_manifest['uefi_commit'], 'live_fdt_sha256': cycle.sha256(live_fdt.read_bytes()),
             'candidate_sha256': manifest['sha256'],
             'candidate_record_sha256': cycle.sha256((private_dir / 'manifest.json').read_bytes()),
             'candidate_verified_before_flash': True,
             'baseline_folder': 'baseline' if args.dry_run else 'live-baseline',
             'baseline_sha256': probe.readonly.load_baseline(run / ('baseline' if args.dry_run else 'live-baseline'), serial=args.serial,
                                                             original_boot_b=args.backup / 'boot_b.img')['baseline_sha256'],
             'restore_command': [sys.executable, str(Path(__file__).resolve()), '--serial', args.serial,
                                 '--fastboot-serial', args.fastboot_serial, '--backup', str(args.backup),
                                 '--restore-run', str(run), '--wait-seconds', '600']}
    probe.durable_json(run / 'restore-state.json', state)
    if args.dry_run:
        cycle.write_json(run / 'candidate.json', manifest)
        print('DRY RUN PASS: source-XNU UFS candidate, payload RAW section, FDT, pins and recovery verified; no device contacted.')
        print('Private evidence: ' + str(run), flush=True)
        return 0
    # Recheck rooted Android, all 18 protected hashes and the live GPT immediately before boot_b may change.
    cycle.current_android(args.serial)
    cycle.compare_partition_hashes(args.serial, records)
    cycle.request_fastboot_from_android(args.serial, args.fastboot_serial, args.wait_seconds)
    cycle.verify_fastboot(args.fastboot_serial, require_slot_b=True)
    cycle.verify_original_boot_b(args.backup / 'boot_b.img', records['boot_b'])
    if cycle.sha256((private_dir / 'UniXNU-dali-uefi-boot.img').read_bytes()) != manifest['sha256']:
        raise cycle.CycleError('sealed XNU UFS candidate changed before flashing')
    state['device_verified_before_flash'] = True
    lease.arm(run, state)
    cycle.write_json(run / 'candidate.json', manifest)
    print('ORIGINAL-ONLY RECOVERY: ' + shlex.join(state['restore_command']), flush=True)
    print('XNU UFS candidate is running. Hold Volume Up until the deliberate recovery request; immediately hold Volume Down during the 2-second LK watchdog window to enter fastboot. The runner will restore the complete original Android boot_b.', flush=True)
    try:
        cycle.flash_boot_b(args.fastboot_serial, private_dir / 'UniXNU-dali-uefi-boot.img')
        cycle.fastboot_command(args.fastboot_serial, 'reboot', timeout=20)
        disconnected = cycle.wait_fastboot_disconnect(args.fastboot_serial, 15)
        if not disconnected or not cycle.wait_fastboot(args.fastboot_serial, args.wait_seconds):
            raise cycle.CycleError('manual XNU recovery expired; original boot_b restore remains pending')
    except BaseException:
        try:
            if cycle.fastboot_online(args.fastboot_serial):
                finish_restore(args, run, state, records, lease)
        finally:
            if state['boot_b_may_be_modified']:
                print('RESTORE PENDING: ' + shlex.join(state['restore_command']), flush=True)
        raise
    try:
        result = finish_restore(args, run, state, records, lease)
    except BaseException:
        if state['boot_b_may_be_modified']:
            print('RESTORE PENDING: ' + shlex.join(state['restore_command']), flush=True)
        raise
    print(json.dumps(result, indent=2))
    return 0 if result.get('xnu_ufs_read_only_qualified') else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--fastboot-serial')
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--uefi', type=Path)
    parser.add_argument('--unixnu', type=Path)
    parser.add_argument('--artifact-dir', type=Path)
    parser.add_argument('--evidence-root', type=Path)
    parser.add_argument('--restore-run', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--wait-seconds', type=int, default=120)
    args = parser.parse_args()
    args.fastboot_serial = args.fastboot_serial or args.serial
    if args.fastboot_serial != args.serial:
        parser.error('source-XNU UFS recovery requires one verified ADB/fastboot serial')
    args.backup = args.backup.expanduser().resolve()
    if args.restore_run:
        if args.dry_run or any(getattr(args, key) for key in ('baseline_dir', 'uefi', 'unixnu', 'artifact_dir', 'evidence_root')) or not 120 <= args.wait_seconds <= 600:
            parser.error('restore-only uses no candidate/source paths and has a bounded 120..600 second timeout')
        args.backup = cycle.require_private_path(args.backup, (DEVICE, DEVICE.parent / 'UEFI-Dali', DEVICE.parent / 'UniXNU'), 'backup')
        args.restore_run = cycle.require_private_path(args.restore_run, (DEVICE, DEVICE.parent / 'UEFI-Dali', DEVICE.parent / 'UniXNU'), 'restore run')
        if any(paths_overlap(args.restore_run, protected) for protected in (args.backup, probe.STATE_ROOT)):
            parser.error('restore run must be separate from the verified backup and device-state ledger')
    else:
        if args.wait_seconds != 120 or any(getattr(args, key) is None for key in ('baseline_dir', 'uefi', 'unixnu', 'artifact_dir', 'evidence_root')):
            parser.error('new trial needs baseline, source XNU artifact, both source trees, output and the fixed manual fastboot deadline')
        args.uefi, args.unixnu, args.artifact_dir = args.uefi.resolve(), args.unixnu.resolve(), args.artifact_dir.resolve()
        repos=(DEVICE,args.uefi,args.unixnu)
        args.backup=cycle.require_private_path(args.backup,repos,'backup')
        args.baseline_dir=cycle.require_private_path(args.baseline_dir,repos,'baseline')
        args.artifact_dir=cycle.require_private_path(args.artifact_dir,repos,'XNU artifact')
        args.evidence_root=cycle.require_private_path(args.evidence_root,repos,'evidence root')
        protected = (args.backup, args.baseline_dir, args.artifact_dir, probe.STATE_ROOT,
                     *(p.resolve() for p in repos))
        if any(paths_overlap(args.evidence_root, p) for p in protected):
            parser.error('evidence output must be separate from repositories, device-state ledger, backup, baseline and source artifact')
    with probe.DeviceLease(args.serial) as lease:
        return restore_pending(args, lease) if args.restore_run else run_trial(args, lease)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (cycle.CycleError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('run-xnu-ufs-readonly: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
