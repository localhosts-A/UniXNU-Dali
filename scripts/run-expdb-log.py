#!/usr/bin/env python3
"""Build one manual UEFI expdb log trial; keep ramoops and original-only recovery."""
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

SCRIPTS=Path(__file__).resolve().parent
DEVICE=SCRIPTS.parent
sys.path.insert(0,str(SCRIPTS))
from ufs_expdb import MIB, _read
from ufs_expdb_restore import AndroidExpdb, prepare_restore
from ufs_identity import validate_captured_identity
from ufs_scratch_authorization import build_xnu_scratch_grant

spec=importlib.util.spec_from_file_location('expdb_trial_recovery',SCRIPTS/'restore-expdb-log.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
probe,cycle=recovery.probe,recovery.cycle


def copy_bytes(data,path):
    with path.open('xb') as target:
        os.chmod(path,0o400);target.write(data);target.flush();os.fsync(target.fileno())


def overlap(a,b):
    return a==b or a in b.parents or b in a.parents


def snapshot_baselines(args,run):
    baseline=probe.readonly.load_baseline(args.baseline_dir,serial=args.serial,
                                         original_boot_b=args.backup/'boot_b.img')
    validate_captured_identity(baseline,args.baseline_dir)
    raw=(args.baseline_dir/'baseline.json').read_bytes()
    if cycle.sha256(raw)!=baseline['baseline_sha256']:
        raise cycle.CycleError('UFS baseline changed before snapshot')
    original_report=json.loads(raw)
    folder=run/'ufs-baseline';folder.mkdir(mode=0o700)
    for capture in original_report['captures'].values():
        filename=capture.get('file')
        if not isinstance(filename,str) or Path(filename).name!=filename:
            raise cycle.CycleError('UFS capture is not a fixed single-component file')
        data=_read(args.baseline_dir,filename,capture['bytes'])
        if cycle.sha256(data)!=capture['sha256']:
            raise cycle.CycleError('UFS capture changed before snapshot')
        copy_bytes(data,folder/filename)
    copy_bytes(raw,folder/'baseline.json')
    expdb_raw=(args.expdb_dir/'baseline.json').read_bytes();expdb=json.loads(expdb_raw)
    image=prepare_restore(serial=args.serial,baseline=baseline,baseline_dir=folder,
                           expdb_baseline=expdb,expdb_dir=args.expdb_dir)
    expdb_folder=run/'expdb-baseline';expdb_folder.mkdir(mode=0o700)
    for filename,record in expdb['captures'].items():
        if filename not in ('primary-header.bin','primary-entries.bin','backup-header.bin','backup-entries.bin'):
            raise cycle.CycleError('unknown expdb baseline capture')
        data=_read(args.expdb_dir,filename,record['bytes'])
        if cycle.sha256(data)!=record['sha256']:
            raise cycle.CycleError('expdb capture changed before snapshot')
        copy_bytes(data,expdb_folder/filename)
    copy_bytes(image['original'],expdb_folder/'expdb.img')
    copy_bytes(expdb_raw,expdb_folder/'baseline.json')
    # Verify the copies, not just the source paths used to create them.
    baseline=probe.readonly.load_baseline(folder,serial=args.serial,original_boot_b=args.backup/'boot_b.img')
    image=prepare_restore(serial=args.serial,baseline=baseline,baseline_dir=folder,
                          expdb_baseline=json.loads((expdb_folder/'baseline.json').read_bytes()),expdb_dir=expdb_folder)
    return baseline,expdb,image


def verify_live_originals(args,records,image):
    io=AndroidExpdb(cycle,args.serial,records);io.observe(image)
    digest=hashlib.sha256()
    for offset in range(0,image['partition_bytes'],MIB):
        digest.update(io.read(offset,min(MIB,image['partition_bytes']-offset)))
    if digest.hexdigest()!=image['original_partition_sha256']:
        raise cycle.CycleError('current complete expdb differs from the backed-up original; no trial armed')
    return dict(serial=args.serial,node=io.node,lun=image['lun'],block_bytes=io.block,
                partition_guid=image['partition']['guid'],full_expdb_sha256=digest.hexdigest(),
                original_android_and_18_hashes_verified=True,media_writes_performed=False)


def build_trial(args,lease):
    for repo in (DEVICE,args.unixnu):cycle.verify_clean_repository(repo)
    cycle.verify_clean_repository(args.uefi,allow_mu_patches=True)
    _,records=cycle.verify_backup(args.backup,args.serial)
    kernel,source=cycle.verify_kernel_artifact(args.artifact_dir,args.unixnu,DEVICE,reset_qualification=True)
    if source.get('device_commit')!=cycle.run_capture(['git','rev-parse','HEAD'],cwd=DEVICE).strip():
        raise cycle.CycleError('expdb log needs a kernel built for this exact device revision')
    token=secrets.token_hex(16);lease.require_new(token)
    args.evidence_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    run=args.evidence_root/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+token[:8])
    run.mkdir(mode=0o700)
    baseline,expdb,image=snapshot_baselines(args,run)
    if not args.dry_run:
        probe.durable_json(run/'pre-build-live-originals.json',verify_live_originals(args,records,image))
    live_fdt=run/'runtime-live.dtb'
    if args.dry_run:
        raw_fdt=_read(run/'ufs-baseline',baseline['captures']['live_fdt']['file'],baseline['captures']['live_fdt']['bytes'])
    else:
        raw_fdt=cycle.adb_output(args.serial,'cat /sys/firmware/fdt')
    cycle.verify_live_layout(raw_fdt,args.backup,args.uefi)
    copy_bytes(raw_fdt,live_fdt)
    adt=run/'dali.adt'
    cycle.run_logged([sys.executable,DEVICE/'scripts/make-dali-adt.py','--fdt',live_fdt,'--output',adt,
                      '--dram-size','0x300000000','--cpu-regs','0','--psci-method','smc','--ufs-readonly'],run/'adt.log',timeout=60)
    candidate=run/'candidate';candidate.mkdir(mode=0o700)
    kernel_bytes=kernel.read_bytes();copy_bytes(kernel_bytes,candidate/'source-kernel.macho')
    copy_bytes((args.artifact_dir/'manifest.json').read_bytes(),candidate/'source-kernel-manifest.json')
    inputs=dict(serial=args.serial,token=token,sequence=1,baseline=baseline,baseline_dir=run/'ufs-baseline',
                source_manifest=source,kernel_bytes=kernel_bytes,original_boot_b=args.backup/'boot_b.img',
                target_partition='expdb',expdb_baseline=expdb,expdb_dir=run/'expdb-baseline')
    # The wire binds the kernel, not its enclosing boot image. First shape it
    # using a verified boot-v4 container; after packaging, recompute against
    # the actual candidate and require byte-identical wire/grant.
    grant,wire,_=build_xnu_scratch_grant(**inputs,candidate_image=(args.backup/'boot_b.img').read_bytes())
    copy_bytes(wire,candidate/'grant.bin')
    packed=run/'candidate-build';packed.mkdir(mode=0o700)
    boot,manifest=cycle.build_candidate(args,args.backup,candidate/'source-kernel.macho',adt,
        source['recovery'],token,packed,xnu_ufs_readonly=True,live_fdt=live_fdt,expdb_log_wire=candidate/'grant.bin')
    final_grant,final_wire,proposal=build_xnu_scratch_grant(**inputs,candidate_image=boot.read_bytes())
    if final_wire!=wire or final_grant!=grant:
        raise cycle.CycleError('final expdb candidate changed the proposed grant')
    copy_bytes(json.dumps(grant,indent=2).encode()+b'\n',candidate/'grant.json')
    copy_bytes(json.dumps(proposal,indent=2).encode()+b'\n',candidate/'proposal.json')
    for name,path in {
        'UniXNU-dali-uefi-boot.img':boot,
        'SILICIUM_UEFI.fd':args.uefi/'Build/daliPkg/DEBUG_CLANGPDB/FV/SILICIUM_UEFI.fd',
        'payload.img':args.uefi/'out/payload.img','runtime-live.dtb':live_fdt,'kernel.macho.adt':adt,
    }.items():copy_bytes(path.read_bytes(),candidate/name)
    copy_bytes(json.dumps(manifest,indent=2).encode()+b'\n',candidate/'manifest.json')
    checked,checked_manifest=probe.verify_candidate(candidate,args.uefi,args.unixnu,args.backup,'expdb-log')
    if checked_manifest!=manifest or cycle.sha256(checked.read_bytes())!=manifest['sha256']:
        raise cycle.CycleError('sealed expdb candidate changed after independent verification')
    expected=dict(token=token,device_binding=grant['device_binding_sha256'],candidate_sha256=grant['candidate_sha256'],
                  source_sha256=grant['source_sha256'],block_bytes=grant['block_bytes'],owner=1,sequence=grant['sequence'])
    state=dict(schema=1,kind=recovery.KIND,dry_run=args.dry_run,token=token,serial=args.serial,
               fastboot_serial=args.fastboot_serial,backup=str(args.backup),
               original_boot_b_sha256=records['boot_b']['sha256'],expdb_original_sha256=image['original_partition_sha256'],
               candidate_sha256=manifest['sha256'],candidate_record_sha256=cycle.sha256((candidate/'manifest.json').read_bytes()),
               candidate_verified_before_flash=True,device_verified_before_flash=False,
               boot_b_may_be_modified=False,expdb_may_be_modified=False,expdb_restore_verified=False,
               baseline_sha256=baseline['baseline_sha256'],expdb_baseline_sha256=cycle.sha256((run/'expdb-baseline/baseline.json').read_bytes()),
               log_binding=expected,
               restore_command=[sys.executable,str(SCRIPTS/'restore-expdb-log.py'),'--serial',args.serial,
                   '--fastboot-serial',args.fastboot_serial,'--backup',str(args.backup),'--restore-run',str(run),'--wait-seconds','600'])
    probe.durable_json(run/'restore-state.json',state)
    return run,state,records,image


def execute_trial(args,lease,run,state,records,image):
    probe.durable_json(run/'pre-flash-live-originals.json',verify_live_originals(args,records,image))
    cycle.request_fastboot_from_android(args.serial,args.fastboot_serial,args.wait_seconds)
    cycle.verify_fastboot(args.fastboot_serial,require_slot_b=True)
    cycle.verify_original_boot_b(args.backup/'boot_b.img',records['boot_b'])
    candidate=run/'candidate/UniXNU-dali-uefi-boot.img'
    if cycle.sha256(candidate.read_bytes())!=state['candidate_sha256']:
        raise cycle.CycleError('expdb candidate changed immediately before flashing')
    state.update(device_verified_before_flash=True,expdb_may_be_modified=True)
    lease.arm(run,state)
    print('ORIGINAL-ONLY RECOVERY: '+shlex.join(state['restore_command']),flush=True)
    print('First firmware log menu: Volume Up selects WRITE; Power confirms. After it finishes, reboot the SAME candidate without reflashing. Second firmware log menu: keep READBACK (or Volume Down) and confirm with Power. Then choose Return to firmware diagnostics in the normal menu; confirm with Power and hold only Volume Down during the 10-second recovery window. Ramoops remains enabled. The runner waits for manual fastboot and restores original boot_b and the original expdb window.',flush=True)
    try:
        cycle.flash_boot_b(args.fastboot_serial,candidate)
        cycle.fastboot_command(args.fastboot_serial,'reboot',timeout=20)
        if not cycle.wait_fastboot_disconnect(args.fastboot_serial,15) or not cycle.wait_fastboot(args.fastboot_serial,args.wait_seconds):
            raise cycle.CycleError('manual paired log trial timed out; original recovery remains pending')
    except BaseException:
        try:
            if cycle.fastboot_online(args.fastboot_serial):
                recovery.restore(argparse.Namespace(**vars(args),restore_run=run),lease)
        finally:
            if lease.data['pending_run']==str(run):
                print('RESTORE PENDING: '+shlex.join(state['restore_command']),flush=True)
        raise
    result=recovery.restore(argparse.Namespace(**vars(args),restore_run=run),lease)
    print(json.dumps(result,indent=2))
    # Integrity/restore success alone cannot qualify two distinct boots. Keep
    # runtime command and reboot-boundary review separate from restoration.
    return 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--fastboot-serial')
    for name in ('backup','baseline-dir','expdb-dir','uefi','unixnu','artifact-dir','evidence-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--wait-seconds',type=int,default=600)
    args=parser.parse_args();args.fastboot_serial=args.fastboot_serial or args.serial
    if args.fastboot_serial!=args.serial or not 120<=args.wait_seconds<=600:
        parser.error('one ADB/fastboot identity and a bounded 120..600 second manual recovery deadline are required')
    for key in ('backup','baseline_dir','expdb_dir','uefi','unixnu','artifact_dir','evidence_root'):
        setattr(args,key,getattr(args,key).expanduser().resolve())
    repos=(DEVICE,args.uefi,args.unixnu);state_root=probe.device_state_root(args.backup)
    for key in ('backup','baseline_dir','expdb_dir','artifact_dir','evidence_root'):
        setattr(args,key,cycle.require_private_path(getattr(args,key),repos,key))
    if any(overlap(args.evidence_root,path) for path in (*repos,state_root,args.backup,args.baseline_dir,args.expdb_dir,args.artifact_dir)):
        parser.error('evidence output must be separate from source, ledger, baselines, backup and kernel artifacts')
    with probe.DeviceLease(args.serial,state_root) as lease:
        run,state,records,image=build_trial(args,lease)
        if args.dry_run:
            print('DRY RUN PASS: sealed expdb log candidate and recovery inputs verified; no device contacted. '+str(run))
            return 0
        return execute_trial(args,lease,run,state,records,image)


if __name__=='__main__':
    try:raise SystemExit(main())
    except (cycle.CycleError,OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
        print('run-expdb-log: '+str(error),file=sys.stderr);raise SystemExit(1)
