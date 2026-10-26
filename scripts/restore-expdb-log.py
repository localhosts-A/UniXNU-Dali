#!/usr/bin/env python3
"""Resume a leased expdb log trial's original-only recovery. Never start a trial."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from dali_ufs_resources import encode_scratch_grant
from ufs_expdb_restore import AndroidExpdb, prepare_restore, recover_window
from ufs_scratch_authorization import source_binding_sha256

spec = importlib.util.spec_from_file_location('expdb_recovery_probe', SCRIPTS/'run-ufs-probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
cycle = probe.cycle
KIND = 'dali-expdb-log-trial'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_run(args, lease):
    run = args.restore_run.resolve(strict=True)
    state = json.loads((run/'restore-state.json').read_bytes())
    if (lease.data.get('pending_run') != str(run) or lease.data.get('pending_expdb') is not True or
            type(state.get('schema')) is not int or state['schema'] != 1 or
            state.get('kind') != KIND or state.get('dry_run') is not False or
            state.get('serial') != args.serial or state.get('fastboot_serial') != args.fastboot_serial or
            state.get('backup') != str(args.backup) or not probe.TOKEN.fullmatch(state.get('token','')) or
            state.get('candidate_verified_before_flash') is not True or
            state.get('device_verified_before_flash') is not True):
        raise cycle.CycleError('expdb recovery does not match the canonical pending trial')
    _, records = cycle.verify_backup(args.backup, args.serial)
    if records['boot_b']['sha256'] != state.get('original_boot_b_sha256'):
        raise cycle.CycleError('expdb recovery original boot_b backup differs')
    baseline = probe.readonly.load_baseline(run/'ufs-baseline', serial=args.serial,
                                           original_boot_b=args.backup/'boot_b.img')
    expdb_raw = (run/'expdb-baseline/baseline.json').read_bytes()
    if (baseline['baseline_sha256'] != state.get('baseline_sha256') or
            digest(expdb_raw) != state.get('expdb_baseline_sha256')):
        raise cycle.CycleError('expdb recovery baseline record changed')
    image = prepare_restore(serial=args.serial, baseline=baseline, baseline_dir=run/'ufs-baseline',
                            expdb_baseline=json.loads(expdb_raw), expdb_dir=run/'expdb-baseline')
    if image['original_partition_sha256'] != state.get('expdb_original_sha256'):
        raise cycle.CycleError('expdb recovery original full backup differs')
    candidate = run/'candidate'
    image_raw = (candidate/'UniXNU-dali-uefi-boot.img').read_bytes()
    manifest_raw = (candidate/'manifest.json').read_bytes()
    kernel = (candidate/'source-kernel.macho').read_bytes()
    source = json.loads((candidate/'source-kernel-manifest.json').read_bytes())
    grant = json.loads((candidate/'grant.json').read_bytes())
    wire = (candidate/'grant.bin').read_bytes()
    expected = state.get('log_binding')
    if (not isinstance(expected,dict) or set(expected) != {'token','device_binding','candidate_sha256',
            'source_sha256','block_bytes','owner','sequence'} or
            digest(image_raw) != state.get('candidate_sha256') or
            digest(manifest_raw) != state.get('candidate_record_sha256') or
            expected.get('token') != state['token'] or expected.get('device_binding') != image['device_binding'] or
            expected.get('candidate_sha256') != digest(kernel) or source.get('kernel_sha256') != digest(kernel) or
            expected.get('source_sha256') != source_binding_sha256(source) or
            expected.get('block_bytes') != image['block_bytes'] or type(expected.get('owner')) is not int or
            expected['owner'] not in (1,2) or
            type(expected.get('sequence')) is not int or expected['sequence'] <= 0 or
            grant.get('target_partition') != 'expdb' or grant.get('profile') != 'expdb-scratch-write-test-v1' or
            wire != encode_scratch_grant(grant) or len(wire) != 236):
        raise cycle.CycleError('expdb recovery candidate/source/grant/log identity differs')
    for field in ('token','candidate_sha256','source_sha256','block_bytes','sequence'):
        if grant.get(field) != expected[field]:
            raise cycle.CycleError('expdb log binding differs from sealed grant: '+field)
    if grant.get('device_binding_sha256') != expected['device_binding']:
        raise cycle.CycleError('expdb grant device binding differs')
    # The original backup is the authority for every permitted restoration byte.
    if (grant.get('partition_guid') != image['partition']['guid'] or
            grant.get('lun') != image['lun'] or grant.get('capacity_blocks') != image['capacity_bytes']//image['block_bytes'] or
            grant.get('boot_start_lba') != image['partition']['first_lba'] or
            grant.get('boot_blocks') != image['partition']['blocks'] or
            grant.get('original_partition_sha256') != image['original_partition_sha256'] or
            grant.get('original_scratch_sha256') != image['original_scratch_sha256']):
        raise cycle.CycleError('expdb restoration extent differs from original backup')
    return run, state, records, image, expected


def raw_scorer(text, token):
    return dict(run_token=token, raw_characters=len(text), token_seen=token in text,
                snapshot_pass=False, physical_ufs_qualified=False,
                media_writes_qualified=False, persistence_qualified=False)


def restore(args, lease):
    run, state, records, image, expected = validate_run(args, lease)
    callback = lambda: probe.collect_now(args.serial, run, state['token'], raw_scorer)
    original_android = False
    if cycle.adb_online(args.serial):
        try:
            cycle.current_android(args.serial)
            cycle.compare_partition_hashes(args.serial, records)
            original_android = True
        except cycle.CycleError:
            cycle.request_fastboot_from_android(args.serial, args.fastboot_serial, args.wait_seconds)
    if not original_android:
        if not cycle.wait_fastboot(args.fastboot_serial, args.wait_seconds):
            raise cycle.CycleError('fastboot absent; boot_b and expdb restoration remain pending')
        # The callback only captures ramoops. The helper's bounded boot_b retry
        # can never repeat a window write, which happens strictly after return.
        cycle.restore_android_safely(args.serial, args.fastboot_serial, args.backup, records,
                                     args.wait_seconds, post_boot=callback)
    ramoops = callback()
    if (not isinstance(ramoops,dict) or
            any(key in ramoops for key in ('collection_error','collection_store_error'))):
        state['ramoops_collection_failure'] = ramoops
        probe.durable_json(run/'restore-state.json',state)
        raise cycle.CycleError('ramoops collection was not stored; preserve pending expdb recovery')
    attempt = 'expdb-recovery-' + secrets.token_hex(8)
    state['latest_expdb_recovery'] = attempt
    probe.durable_json(run/'restore-state.json', state)
    report = recover_window(image, AndroidExpdb(cycle,args.serial,records), run/attempt,
                            expected_log=expected)
    android = cycle.current_android(args.serial)
    hashes = cycle.compare_partition_hashes(args.serial, records)
    state.update(expdb_may_be_modified=False, expdb_restore_verified=True,
                 restore_verified=True, original_boot_b_restored=True,
                 all_partition_hashes_match=True, android_root_verified=True,
                 android=android, partition_hashes=hashes)
    result = dict(android=android, partition_hashes=hashes, ramoops=ramoops,
                  expdb_recovery=report, expdb_recovery_folder=attempt,
                  original_boot_b_restored=True, expdb_restore_verified=True,
                  all_18_original_hashes_match=True, complete_ufs_read_write_pass=False,
                  media_writes_qualified=False, persistence_qualified=False)
    probe.durable_json(run/'recovery-verification.json', result)
    lease.restored(run,state)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--fastboot-serial')
    parser.add_argument('--backup',type=Path,required=True)
    parser.add_argument('--restore-run',type=Path,required=True)
    parser.add_argument('--wait-seconds',type=int,default=600)
    args=parser.parse_args()
    args.fastboot_serial=args.fastboot_serial or args.serial
    args.backup=args.backup.resolve(strict=True)
    if not 120 <= args.wait_seconds <= 600:
        parser.error('wait-seconds must be between 120 and 600')
    try:
        with probe.DeviceLease(args.serial,probe.device_state_root(args.backup)) as lease:
            result=restore(args,lease)
    except (cycle.CycleError,OSError,ValueError,TypeError,KeyError) as error:
        parser.exit(1,'expdb recovery remains pending: '+str(error)+'\n')
    print('Original boot_b/expdb/Android B/root/18 hashes restored; log integrity='+
          str(result['expdb_recovery']['record_integrity_verified']))


if __name__=='__main__':main()
