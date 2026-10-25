#!/usr/bin/env python3
"""One explicitly selected UEFI UFS diagnostic, with original-boot_b restoration.

The default register mode contains no physical storage commands. The separate
read-only-blockio mode submits bounded reads/sync, never media writes. Neither
candidate contains XNU. Manual menu/fastboot keys are allowed. Register snapshot
success never qualifies DMA/media; read-only success never qualifies persistence.
Dry-run validates saved inputs without ADB/fastboot. Restore-only never loads a
candidate and needs neither its source revision nor a new build.
"""
import argparse
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

DEVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEVICE / 'scripts'))
import ufs_readonly_evidence as readonly
spec = importlib.util.spec_from_file_location('ufs_probe_cycles', DEVICE / 'scripts/run-bootb-cycles.py')
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)
STATE_ROOT = DEVICE.parent / 'device-backups/.ufs-device-state'
TOKEN = re.compile(r'[0-9a-f]{32}')
KINDS = {'registers': 'dali-ufs-register-probe', 'read-only-blockio': 'dali-ufs-read-only-blockio'}


def selected_mode(a):
    mode = getattr(a, 'mode', 'registers')
    if mode not in KINDS:
        raise cycle.CycleError('unknown UFS diagnostic mode')
    return mode


def durable_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + '.partial')
    with temp.open('w') as target:
        os.chmod(temp, 0o600)
        json.dump(value, target, indent=2, sort_keys=True)
        target.write('\n')
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class DeviceLease(AbstractContextManager):
    """Live advisory lock plus durable pending-run/token ledger for UFS trials."""
    def __init__(self, serial, root=None):
        if not serial or len(serial) > 128:
            raise cycle.CycleError('invalid device identity for UFS lease')
        root = STATE_ROOT if root is None else root
        self.folder = root / hashlib.sha256(serial.encode()).hexdigest()
        self.fd = None

    def __enter__(self):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.folder / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise cycle.CycleError('another live UFS trial/restore holds the device lease') from None
        try:
            self.path = self.folder / 'state.json'
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {'pending_run': None, 'used_tokens': []}
            if (not isinstance(self.data, dict) or 'pending_run' not in self.data or not isinstance(self.data.get('used_tokens'), list) or
                    any(not isinstance(t, str) or not TOKEN.fullmatch(t) for t in self.data['used_tokens']) or
                    self.data.get('pending_run') is not None and not isinstance(self.data['pending_run'], str)):
                raise cycle.CycleError('invalid UFS pending-run/token ledger')
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def require_new(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise cycle.CycleError('invalid new probe token')
        if self.data['pending_run'] is not None:
            raise cycle.CycleError('original boot_b restore is pending: ' + self.data['pending_run'])
        if token in self.data['used_tokens']:
            raise cycle.CycleError('probe token was already used; rebuild with a fresh token')

    def arm(self, run, state):
        self.require_new(state['token'])
        state['boot_b_may_be_modified'] = True
        durable_json(run / 'restore-state.json', state)
        self.data['used_tokens'].append(state['token'])
        self.data['pending_run'] = str(run)
        durable_json(self.path, self.data)

    def restored(self, run, state):
        if self.data['pending_run'] not in (None, str(run)):
            raise cycle.CycleError('a different UFS run owns pending restoration')
        state['boot_b_may_be_modified'] = False
        durable_json(run / 'restore-state.json', state)
        self.data['pending_run'] = None
        durable_json(self.path, self.data)


def verify_candidate(directory, uefi, unixnu, backup, mode='registers'):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if not isinstance(manifest, dict):
        raise cycle.CycleError('invalid probe manifest')
    image = directory / 'UniXNU-dali-uefi-boot.img'
    fd = (directory / 'SILICIUM_UEFI.fd').read_bytes()
    probe = manifest.get('ufs_probe', {})
    if not isinstance(probe, dict):
        raise cycle.CycleError('invalid probe metadata')
    xnu_ufs_mode = mode == 'xnu-ufs-read-only'
    if mode in ('read-only-blockio', 'xnu-ufs-read-only'):
        host = manifest.get('ufs_host', {})
        if not isinstance(host, dict):
            raise cycle.CycleError('invalid physical UFS host metadata')
        token = host.get('run_token', '')
        if (host.get('enabled') is not True or host.get('mode') != 'read-only-blockio' or host.get('log_version') != 1 or
                host.get('controller_commands') is not True or host.get('mmio_writes') is not True or
                host.get('media_writes') is not False or host.get('write_authorized') is not False or
                host.get('physical_storage_qualified') is not False or host.get('host_verified_in_final_fd') is not True or
                probe.get('enabled') is not False or probe.get('mode') != 'off' or probe.get('run_token') is not None):
            raise cycle.CycleError('candidate is not an explicit physical read-only diagnostic')
        if xnu_ufs_mode:
            xnu_profile = manifest.get('ufs_xnu', {})
            if (not isinstance(xnu_profile, dict) or xnu_profile.get('enabled') is not True or
                    xnu_profile.get('run_token') != token or xnu_profile.get('write_authorized') is not False or
                    xnu_profile.get('physical_ufs_qualified') is not False):
                raise cycle.CycleError('candidate lacks its source-XNU read-only UFS binding')
    elif mode == 'registers':
        token = probe.get('run_token', '')
        if (probe.get('enabled') is not True or probe.get('mode') != 'read-only-registers' or
                probe.get('controller_commands') is not False or probe.get('mmio_writes') is not False):
            raise cycle.CycleError('candidate is not a bound UEFI register-only probe')
    else:
        raise cycle.CycleError('unknown candidate diagnostic mode')
    if (not isinstance(token, str) or not TOKEN.fullmatch(token) or
            probe.get('physical_storage_qualified') is not False or probe.get('bds_verified_in_final_fd') is not True or
            manifest.get('target') != 'dali'):
        raise cycle.CycleError('invalid diagnostic identity/token/evidence metadata')
    payload = manifest.get('payload', {})
    if not isinstance(payload, dict) or not isinstance(payload.get('recovery'), dict):
        raise cycle.CycleError('invalid probe payload/recovery metadata')
    if ((not xnu_ufs_mode and payload.get('xnu_kernel_sha256') is not None) or
            payload.get('xnu_ramdisk_sha256') is not None or payload.get('xnu_trust_cache_sha256') is not None or
            payload.get('recovery', {}).get('auto_fastboot') is not False or
            payload.get('recovery', {}).get('reset_method') is not None):
        raise cycle.CycleError('probe candidate contains PID1 or competing reset policy')
    if xnu_ufs_mode and (payload.get('xnu_kernel_sha256') is None or payload.get('xnu_adt_sha256') is None or
                         payload.get('xnu_run_token') != token):
        raise cycle.CycleError('combined UFS candidate lacks source XNU/ADT bound to the token')
    # The packager's historic --mu argument names this firmware workspace;
    # Mu_Basecore itself is separately checked against its pinned gitlink/patches.
    for key, path in (('uefi_commit', uefi), ('unixnu_commit', unixnu), ('device_commit', DEVICE),
                      ('mu_commit', uefi), ('upstream_commit', uefi)):
        if manifest.get(key) != cycle.run_capture(['git', 'rev-parse', 'HEAD'], cwd=path).strip():
            raise cycle.CycleError('probe source revision mismatch: ' + key)
    pins = tomllib.loads((DEVICE / 'board.toml').read_text())['firmware']
    if any(manifest.get(key) != pins.get(key) for key in ('uefi_commit', 'unixnu_commit')):
        raise cycle.CycleError('probe firmware/generic revision differs from board pins')
    data = image.read_bytes()
    import struct
    if (len(data) != manifest.get('bytes') or cycle.sha256(data) != manifest.get('sha256') or
            len(data) < 4096 or data[:8] != b'ANDROID!' or struct.unpack_from('<I', data, 40)[0] != 4 or
            struct.unpack_from('<I', data, 12)[0] or struct.unpack_from('<I', data, 1580)[0]):
        raise cycle.CycleError('probe image format/size/hash mismatch')
    length = struct.unpack_from('<I', data, 8)[0]
    if not 4 <= length <= len(data) - 4096 or data[4096:4100] != bytes.fromhex('02214c18'):
        raise cycle.CycleError('probe kernel is not bounded legacy LZ4')
    if any(data[4096 + length:]):
        raise cycle.CycleError('unknown probe image tail metadata')
    raw = subprocess.check_output(['lz4', '-d', '-c'], input=data[4096:4096 + length], timeout=30)
    if (len(fd) != 0x4000000 or cycle.sha256(fd) != manifest.get('fd_sha256') or
            len(raw) != 4096 + len(fd) or raw[4096:] != fd):
        raise cycle.CycleError('candidate does not embed the exact verified FD')
    original_header = bytearray((backup / 'boot_b.img').read_bytes()[:4096])
    struct.pack_into('<I', original_header, 8, length)
    struct.pack_into('<I', original_header, 12, 0)
    struct.pack_into('<I', original_header, 1580, 0)
    if data[:4096] != original_header:
        raise cycle.CycleError('probe boot header differs from bound original backup')
    if manifest.get('mode') != 'DEBUG':
        raise cycle.CycleError('unsupported local probe build mode')
    build = uefi / 'Build/daliPkg/DEBUG_CLANGPDB'
    if fd != (build / 'FV/SILICIUM_UEFI.fd').read_bytes():
        raise cycle.CycleError('candidate FD differs from the current local pinned build')
    sys.path.insert(0, str(uefi / 'tools/dali'))
    from firmware_volume import extract_bds
    package_spec = importlib.util.spec_from_file_location('ufs_probe_package', uefi / 'tools/dali/package.py')
    package = importlib.util.module_from_spec(package_spec)
    package_spec.loader.exec_module(package)
    if mode in ('read-only-blockio', 'xnu-ufs-read-only'):
        expected_probe, expected_host = package.ufs_read_only_metadata(build / 'AARCH64', token, fd=fd)
    else:
        expected_probe = package.ufs_probe_metadata(build / 'AARCH64', token, fd=fd)
        expected_host = package.ufs_host_off_metadata(build / 'AARCH64', fd=fd)
    if expected_probe != probe or expected_host != manifest.get('ufs_host'):
        raise cycle.CycleError('diagnostic differs from local compiled PCD/modules/FD inputs')
    module = extract_bds(fd)
    if cycle.sha256(module) != probe.get('bds_sha256') or (mode == 'registers' and token.encode() + b'\0' not in module):
        raise cycle.CycleError('final candidate BDS/token does not match manifest')
    if xnu_ufs_mode:
        runtime_fdt = directory / 'runtime-live.dtb'
        payload_image = directory / 'payload.img'
        profile = payload.get('xnu_ufs_read_only', {})
        xnu_profile = manifest.get('ufs_xnu', {})
        if not isinstance(profile, dict) or not isinstance(xnu_profile, dict):
            raise cycle.CycleError('candidate source-XNU UFS profile metadata is malformed')
        if (not runtime_fdt.is_file() or not payload_image.is_file() or
                cycle.sha256(runtime_fdt.read_bytes()) != profile.get('live_fdt_sha256') or
                profile.get('live_fdt_sha256') != xnu_profile.get('live_fdt_sha256')):
            raise cycle.CycleError('candidate live FDT differs from its source-XNU UFS profile')
        raw_payload = package.extract_module(fd, package.XNU_UFS_PAYLOAD_GUID,
                                             'XNU UFS payload', section_type=0x19, expected_file_type=0x02)
        staged_payload = payload_image.read_bytes()
        if (raw_payload != staged_payload or len(raw_payload) != payload.get('bytes') or
                cycle.sha256(raw_payload) != payload.get('sha256')):
            raise cycle.CycleError('actual final-FD RAW payload differs from the bound candidate payload')
        try:
            payload_spec = importlib.util.spec_from_file_location('xnu_ufs_payload_tools', uefi / 'tools/make_payload.py')
            payload_tools = importlib.util.module_from_spec(payload_spec)
            payload_spec.loader.exec_module(payload_tools)
            config = subprocess.check_output(['mtype', '-i', str(payload_image), '::/EFI/UniXNU/unixnu.toml'])
            recovery = subprocess.check_output(['mtype', '-i', str(payload_image), '::/EFI/UniXNU/dali-boot.conf'])
            adt = subprocess.check_output(['mtype', '-i', str(payload_image), '::/EFI/UniXNU/Payloads/kernel.macho.adt'])
            kernel = subprocess.check_output(['mtype', '-i', str(payload_image), '::/EFI/UniXNU/Payloads/kernel.macho'])
            payload_tools.validate_xnu_ufs_bundle(payload, token, adt, runtime_fdt.read_bytes(),
                                                  config, recovery, kernel)
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
            raise cycle.CycleError('source-XNU UFS payload validation failed: ' + str(error)) from None
    # Rebuild the small entry shim from the clean pinned source, independently
    # of both the supplied candidate and its self-reported manifest hashes.
    with tempfile.TemporaryDirectory(prefix='dali-ufs-shim-') as temp:
        temp = Path(temp)
        commands = [
            ['clang', '--target=aarch64-none-elf', '-c', str(uefi / 'BootShim/DaliShim.S'),
             '-o', str(temp / 'shim.o'), '-DFD_BASE=0xc1000000', '-DFD_SIZE=0x4000000'],
            ['ld.lld', '-m', 'aarch64elf', '--image-base=0', '-Ttext=0', '-e', '_start',
             str(temp / 'shim.o'), '-o', str(temp / 'shim.elf')],
            ['llvm-objcopy', '-O', 'binary', str(temp / 'shim.elf'), str(temp / 'shim.bin')]]
        for command in commands:
            subprocess.run(command, check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if raw[:4096] != (temp / 'shim.bin').read_bytes():
            raise cycle.CycleError('candidate entry shim differs from rebuilt pinned source')
    return image, manifest


def prepare_readonly_baseline(a, run):
    """Snapshot only verified raw captures; never copy arbitrary bundle contents."""
    expected = readonly.load_baseline(a.baseline_dir, serial=a.serial, original_boot_b=a.backup / 'boot_b.img')
    report_bytes = (a.baseline_dir / 'baseline.json').read_bytes()
    if cycle.sha256(report_bytes) != expected['baseline_sha256']:
        raise cycle.CycleError('baseline changed during verification')
    report = json.loads(report_bytes)
    output = run / 'baseline'
    output.mkdir(mode=0o700)
    for label in ('primary_header', 'primary_entries', 'backup_header', 'backup_entries', 'boot_b_first_block', 'live_fdt'):
        record = report['captures'][label]
        source = (a.baseline_dir / record['file']).resolve()
        if a.baseline_dir.resolve() not in source.parents:
            raise cycle.CycleError('baseline capture escaped during copy')
        with source.open('rb') as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) != record['bytes'] or cycle.sha256(data) != record['sha256']:
            raise cycle.CycleError('baseline capture changed during copy')
        name = label + '.bin'
        with (output / name).open('xb') as target:
            os.chmod(target.name, 0o600)
            target.write(data)
        record['file'] = name
    durable_json(output / 'baseline.json', report)
    return readonly.load_baseline(output, serial=a.serial, original_boot_b=a.backup / 'boot_b.img')


def collect_live_readonly_baseline(a, run, expected):
    baseline_spec = importlib.util.spec_from_file_location('ufs_live_baseline', DEVICE / 'scripts/collect-ufs-baseline.py')
    collector = importlib.util.module_from_spec(baseline_spec)
    baseline_spec.loader.exec_module(collector)
    collector.collect(a.serial, a.backup, run / 'live-baseline')
    live = readonly.load_baseline(run / 'live-baseline', serial=a.serial, original_boot_b=a.backup / 'boot_b.img')
    for key in ('lun', 'block_size', 'last_lba', 'boot_b_lba', 'boot_b_blocks', 'partitions', 'header_hashes', 'original_boot_b_sha256'):
        if live[key] != expected[key]:
            raise cycle.CycleError('live Android UFS baseline changed: ' + key)
    return live


def trial_scorer(a, run, state):
    if state.get('kind', KINDS['registers']) == KINDS['registers']:
        return score_snapshot

    def score(text, token):
        failure = {'run_token': token, 'uefi_read_only_pass': False, 'observation_complete': False,
                   'physical_ufs_qualified': False, 'media_writes_qualified': False,
                   'uefi_read_only_qualified': False, 'persistence_qualified': False}
        try:
            if (state.get('kind') != KINDS['read-only-blockio'] or state.get('serial') != a.serial or
                    state.get('backup') != str(a.backup) or state.get('token') != token or
                    state.get('baseline_folder') not in ('baseline', 'live-baseline')):
                raise ValueError('read-only run identity/baseline mismatch')
            baseline = readonly.load_baseline(run / state['baseline_folder'], serial=a.serial,
                                              original_boot_b=a.backup / 'boot_b.img')
            if (baseline['baseline_sha256'] != state.get('baseline_sha256') or
                    baseline['original_boot_b_sha256'] != state.get('original_boot_b_sha256')):
                raise ValueError('read-only baseline/original binding changed')
            record_bytes = (run / 'candidate.json').read_bytes()
            manifest = json.loads(record_bytes)
            candidate = (run / 'candidate-boot.img').read_bytes()
            if (cycle.sha256(record_bytes) != state.get('candidate_record_sha256') or
                    manifest.get('sha256') != state.get('candidate_sha256') or
                    cycle.sha256(candidate) != state.get('candidate_sha256') or
                    manifest.get('ufs_host', {}).get('run_token') != token):
                raise ValueError('read-only candidate/token binding changed')
            result = readonly.score_readonly(text, token, baseline, candidate)
            bound = (state.get('dry_run') is False and state.get('device_verified_before_flash') is True and
                     state.get('candidate_verified_before_flash') is True and state['baseline_folder'] == 'live-baseline')
            result.update(device_identity_bound_to_run=bound, candidate_bound_to_run=bound,
                          uefi_read_only_qualified=bool(bound and result['uefi_read_only_pass']),
                          uefi_sync_command_qualified=bool(bound and result.get('uefi_sync_command_pass')),
                          diagnostic_stop_qualified=bool(bound and result.get('diagnostic_stop_pass')))
            return result
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            return dict(failure, binding_error=str(error))
    return score


def score_snapshot(text, token):
    """Require one complete current-token sequence and the production read list."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise cycle.CycleError('invalid snapshot token')
    lines = [line for line in text.splitlines() if line.startswith('DALI-UFS: run=' + token + ' ')]
    prefix = 'DALI-UFS: run=' + token + ' '
    begin = [(i, re.fullmatch(re.escape(prefix) + r'begin version=([12]) mode=read-only writes=0', line))
             for i, line in enumerate(lines) if line.startswith(prefix + 'begin ')]
    end = [(i, re.fullmatch(re.escape(prefix) + r'end result=([0-4]) dma-qualified=0 media-qualified=0', line))
           for i, line in enumerate(lines) if line.startswith(prefix + 'end ')]
    result = {'run_token': token, 'snapshot_pass': False, 'observation_complete': False,
              'dma_qualified': False, 'physical_ufs_qualified': False, 'media_writes_qualified': False}
    if len(begin) != 1 or not begin[0][1] or len(end) != 1 or not end[0][1] or begin[0][0] >= end[0][0]:
        return dict(result, failure='missing/duplicate/truncated current-token begin/end')
    start, schema = begin[0][0], int(begin[0][1][1])
    code = int(end[0][1][1])
    result.update(observation_complete=True, probe_result=code, probe_log_version=schema)
    if code != 4:
        return dict(result, failure='probe refused or clocks/registers unavailable', records=lines)
    patterns = [r'gates=([0-9a-f]{8}),([0-9a-f]{8}) iommu-property-bytes=(\d+) result=4',
                r'cap=([0-9a-f]{8}) ver=([0-9a-f]{8}) hce=([0-9a-f]{8}) hcs=([0-9a-f]{8}) is=([0-9a-f]{8}) ie=([0-9a-f]{8}) ahit=([0-9a-f]{8})',
                r'utrl=([0-9a-f]{16}) db=([0-9a-f]{8}) rs=([0-9a-f]{8}) utmrl=([0-9a-f]{16}) db=([0-9a-f]{8}) rs=([0-9a-f]{8})',
                r'mcq-read=([01]) mcqcap=([0-9a-f]{8}) memcfg=([0-9a-f]{8}) mcqcfg=([0-9a-f]{8})']
    if schema == 2:
        patterns.append(r'mtk-ip=([0-9a-f]{8}) refclk=([0-9a-f]{8}) xoufs=([0-9a-f]{8}) opt=([0-9a-f]{8}) otsd=([0-9a-f]{8}) mphy-ver=1')
    matches, positions = [], []
    for pattern in patterns:
        found = [(i, re.fullmatch(re.escape(prefix) + pattern, line, re.I)) for i, line in enumerate(lines)]
        found = [(i, match) for i, match in found if match]
        if len(found) != 1:
            return dict(result, failure='missing or duplicate register snapshot record')
        positions.append(found[0][0])
        matches.append(found[0][1])
    order = [start, *positions, end[0][0]]
    if not all(a < b for a, b in zip(order, order[1:])):
        return dict(result, failure='snapshot records out of order')
    gates = [int(matches[0][i], 16) for i in (1, 2)]
    cap, version = int(matches[1][1], 16), int(matches[1][2], 16)
    mcq = bool(cap & 0x40000000 and version >= 0x400)
    reads, read_positions = [], []
    for i, line in enumerate(lines):
        match = re.fullmatch(re.escape(prefix) + r'read32=([0-9a-f]+)', line, re.I)
        if match:
            reads.append(int(match[1], 16))
            read_positions.append(i)
    offsets = [0, 8, 0x34, 0x30, 0x20, 0x24, 0x18, 0x50, 0x54, 0x58, 0x60, 0x70, 0x74, 0x78, 0x80]
    if mcq:
        offsets += [4, 0x300, 0x380]
    if schema == 2:
        offsets += [0x2240, 0x144, 0x140, 0x160, 0x14c]
    expected = [0x168a0104, 0x168a0004] + [0x16810000 + at for at in offsets]
    if (gates[0] & 1 or gates[1] & 0x18 or 0xffffffff in gates or cap == 0xffffffff or
            version in (0, 0xffffffff) or bool(int(matches[3][1])) != mcq or reads != expected or
            len(lines) != len(expected) + len(patterns) + 2 or start != 0 or end[0][0] != len(lines) - 1 or
            not read_positions or read_positions[0] <= start or read_positions[-1] >= positions[0]):
        return dict(result, failure='register gate/read sequence mismatch')
    if schema == 2:
        result.update(mtk_ip_version=int(matches[4][1], 16), reference_clock=int(matches[4][2], 16),
                      xoufs_control=int(matches[4][3], 16), options_control=int(matches[4][4], 16),
                      outstanding_control=int(matches[4][5], 16), mphy_version=1)
    return dict(result, snapshot_pass=True, cap=cap, version=version,
                hce=int(matches[1][3], 16), hcs=int(matches[1][4], 16),
                utrl_address=int(matches[2][1], 16), utrl_doorbell=int(matches[2][2], 16),
                utrl_run=int(matches[2][3], 16), utmrl_address=int(matches[2][4], 16),
                utmrl_doorbell=int(matches[2][5], 16), utmrl_run=int(matches[2][6], 16),
                mcq_cap=int(matches[3][2], 16), memcfg=int(matches[3][3], 16),
                mcqcfg=int(matches[3][4], 16), gate_status=gates,
                iommu_property_bytes=int(matches[0][3]), records=lines)


def cached_snapshot(output, token, scorer=score_snapshot):
    old = json.loads((output / 'result.json').read_text())
    if not isinstance(old, dict) or old.get('run_token') != token or not isinstance(old.get('files'), list):
        raise ValueError('invalid cached probe collection')
    chunks, files = [], old['files']
    for record in files:
        name = record.get('name', '')
        if not re.fullmatch(r'capture-[A-Za-z0-9_-]+/console-ramoops-[0-9]+', name):
            raise ValueError('invalid cached raw evidence path')
        path = (output / name).resolve()
        if output.resolve() not in path.parents:
            raise ValueError('cached raw evidence escaped collection')
        data = path.read_bytes()
        if len(data) != record.get('bytes') or cycle.sha256(data) != record.get('sha256'):
            raise ValueError('cached raw evidence changed')
        chunks.append(data.decode('utf-8', errors='replace'))
    result = scorer('\n'.join(chunks), token)
    if not result['observation_complete']:
        raise ValueError('cached collection is incomplete; retry raw capture')
    return dict(result, files=files)


def _collect_now(serial, run, token, scorer=score_snapshot):
    output = run / 'collect'
    output.mkdir(exist_ok=True, mode=0o700)
    existing = output / 'result.json'
    if existing.exists():
        try:
            return cached_snapshot(output, token, scorer)
        except (OSError, ValueError, AttributeError, TypeError, KeyError):
            pass  # Keep partial raw captures and retry into a new private folder.
    chunks, files = [], []
    try:
        capture = Path(tempfile.mkdtemp(prefix='capture-', dir=output))
        listing = cycle.adb_output(serial, 'ls /sys/fs/pstore').decode().split()
        for name in listing:
            if not re.fullmatch(r'console-ramoops-[0-9]+', name):
                continue
            data = cycle.adb_output(serial, 'cat ' + shlex.quote('/sys/fs/pstore/' + name))
            path = capture / name
            with path.open('xb') as target:
                os.chmod(path, 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            files.append({'name': str(path.relative_to(output)), 'bytes': len(data), 'sha256': cycle.sha256(data)})
            chunks.append(data.decode('utf-8', errors='replace'))
        result = scorer('\n'.join(chunks), token)
    except Exception as error:
        result = {'run_token': token, 'snapshot_pass': False, 'collection_error': str(error),
                  'dma_qualified': False, 'physical_ufs_qualified': False, 'media_writes_qualified': False,
                  'uefi_read_only_pass': False, 'uefi_read_only_qualified': False}
    result['files'] = files
    durable_json(existing, result)
    return result


def collect_now(serial, run, token, scorer=score_snapshot):
    # Diagnostics are best effort during original-only recovery. In particular,
    # mkdir/cache/result-write failures must not prevent the restore operation.
    try:
        return _collect_now(serial, run, token, scorer)
    except Exception as error:
        return {'run_token': token, 'snapshot_pass': False, 'observation_complete': False,
                'collection_store_error': str(error), 'files': [], 'dma_qualified': False,
                'physical_ufs_qualified': False, 'media_writes_qualified': False,
                'uefi_read_only_pass': False, 'uefi_read_only_qualified': False,
                'persistence_qualified': False}


def finish_restore(a, run, state, records, lease):
    scorer = trial_scorer(a, run, state)
    callback = lambda: collect_now(a.serial, run, state['token'], scorer)
    cycle.restore_android_safely(a.serial, a.fastboot_serial, a.backup, records, a.wait_seconds, post_boot=callback)
    hashes = cycle.compare_partition_hashes(a.serial, records)
    android = cycle.current_android(a.serial)
    return record_restored_result(a, run, state, hashes, android, lease)


def record_restored_result(a, run, state, hashes, android, lease):
    durable_json(run / 'recovery-verification.json', {'android': android, 'partition_hashes': hashes,
                 'all_partition_hashes_match': True, 'original_boot_b_restored': True})
    lease.restored(run, state)
    snapshot = collect_now(a.serial, run, state['token'], trial_scorer(a, run, state))
    result = dict(snapshot, android_restored=True, all_partition_hashes_match=True,
                  complete_ufs_read_write_pass=False, manual_keys_allowed=True)
    durable_json(run / 'probe-result.json', result)
    return result


def restore_pending(a, lease):
    run = a.restore_run.resolve()
    state = json.loads((run / 'restore-state.json').read_text())
    if (not isinstance(state, dict) or state.get('kind') != KINDS[selected_mode(a)] or state.get('dry_run') is not False or
            state.get('serial') != a.serial or state.get('fastboot_serial') != a.fastboot_serial or
            state.get('backup') != str(a.backup) or not isinstance(state.get('token'), str) or
            not TOKEN.fullmatch(state['token']) or
            lease.data['pending_run'] not in (None, str(run))):
        raise cycle.CycleError('restore-only run identity/token/kind mismatch')
    _, records = cycle.verify_backup(a.backup, a.serial)
    if state.get('original_boot_b_sha256') != records['boot_b']['sha256']:
        raise cycle.CycleError('restore-only original backup hash mismatch')
    if cycle.adb_online(a.serial):
        cycle.current_android(a.serial)
        collect_now(a.serial, run, state['token'], trial_scorer(a, run, state))
        try:
            hashes = cycle.compare_partition_hashes(a.serial, records)
        except cycle.CycleError:
            cycle.request_fastboot_from_android(a.serial, a.fastboot_serial, a.wait_seconds)
        else:
            result = record_restored_result(a, run, state, hashes, cycle.current_android(a.serial), lease)
            print(json.dumps(result, indent=2))
            return
    if not cycle.wait_fastboot(a.fastboot_serial, a.wait_seconds):
        raise cycle.CycleError('fastboot absent; original-only restore remains pending')
    finish_restore(a, run, state, records, lease)


def run_trial(a, lease):
    mode = selected_mode(a)
    for repo in (DEVICE, a.unixnu):
        cycle.verify_clean_repository(repo)
    cycle.verify_clean_repository(a.uefi, allow_mu_patches=True)
    _, records = cycle.verify_backup(a.backup, a.serial)
    image, manifest = verify_candidate(a.candidate_dir, a.uefi, a.unixnu, a.backup, mode)
    token = manifest['ufs_host' if mode == 'read-only-blockio' else 'ufs_probe']['run_token']
    lease.require_new(token)
    if image.stat().st_size > records['boot_b']['bytes']:
        raise cycle.CycleError('probe candidate exceeds boot_b')
    a.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = a.evidence_root / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + token[:8])
    run.mkdir(mode=0o700)
    private_image = run / 'candidate-boot.img'
    shutil.copyfile(image, private_image)
    os.chmod(private_image, 0o400)
    durable_json(run / 'candidate.json', manifest)
    restore_command = [sys.executable, str(Path(__file__).resolve()), '--serial', a.serial,
                       '--fastboot-serial', a.fastboot_serial, '--backup', str(a.backup),
                       '--restore-run', str(run), '--wait-seconds', '600']
    if mode != 'registers':
        restore_command += ['--mode', mode]
    state = {'kind': KINDS[mode], 'dry_run': a.dry_run, 'token': token,
             'serial': a.serial, 'fastboot_serial': a.fastboot_serial, 'backup': str(a.backup),
             'original_boot_b_sha256': records['boot_b']['sha256'], 'boot_b_may_be_modified': False,
             'restore_command': restore_command}
    if mode in ('read-only-blockio', 'xnu-ufs-read-only'):
        baseline = prepare_readonly_baseline(a, run)
        state.update(candidate_sha256=manifest['sha256'], candidate_verified_before_flash=True,
                     candidate_record_sha256=cycle.sha256((run / 'candidate.json').read_bytes()),
                     baseline_folder='baseline', baseline_sha256=baseline['baseline_sha256'])
    durable_json(run / 'restore-state.json', state)
    if not a.dry_run:
        cycle.current_android(a.serial)
        cycle.compare_partition_hashes(a.serial, records)
        if mode == 'read-only-blockio':
            baseline = collect_live_readonly_baseline(a, run, baseline)
            state.update(baseline_folder='live-baseline', baseline_sha256=baseline['baseline_sha256'])
    fdt = (a.backup / 'runtime.dtb').read_bytes() if a.dry_run else cycle.adb_output(a.serial, 'cat /sys/firmware/fdt')
    cycle.verify_live_layout(fdt, a.backup, a.uefi)
    with (run / 'runtime-checked.dtb').open('xb') as target:
        os.chmod(target.name, 0o600)
        target.write(fdt)
    if a.dry_run:
        print('DRY RUN PASS: ' + mode + ' candidate verified; no device contacted. ' + str(run), flush=True)
        return 0
    cycle.request_fastboot_from_android(a.serial, a.fastboot_serial, a.wait_seconds)
    cycle.verify_fastboot(a.fastboot_serial, require_slot_b=True)
    cycle.verify_original_boot_b(a.backup / 'boot_b.img', records['boot_b'])
    if cycle.sha256(private_image.read_bytes()) != manifest['sha256']:
        raise cycle.CycleError('private probe image changed')
    if mode == 'read-only-blockio':
        state['device_verified_before_flash'] = True
    lease.arm(run, state)  # Durable recovery state and token consumption BEFORE flash.
    print('ORIGINAL-ONLY RECOVERY: ' + shlex.join(restore_command), flush=True)
    try:
        cycle.flash_boot_b(a.fastboot_serial, private_image)
        cycle.fastboot_command(a.fastboot_serial, 'reboot', timeout=20)
        print('PROBE RUNNING: choose Return to firmware diagnostics and confirm with Power, then hold ONLY Volume Down during the 10s recovery window.', flush=True)
        disconnected = cycle.wait_fastboot_disconnect(a.fastboot_serial, 15)
        if not disconnected or not cycle.wait_fastboot(a.fastboot_serial, a.wait_seconds):
            raise cycle.CycleError('manual fastboot observation expired; original restore is pending')
    except BaseException:
        try:
            if cycle.fastboot_online(a.fastboot_serial):
                finish_restore(a, run, state, records, lease)
        finally:
            if state['boot_b_may_be_modified']:
                print('RESTORE PENDING: ' + shlex.join(restore_command), flush=True)
        raise
    try:
        result = finish_restore(a, run, state, records, lease)
    except BaseException:
        if state['boot_b_may_be_modified']:
            print('RESTORE PENDING: ' + shlex.join(restore_command), flush=True)
        raise
    print(json.dumps(result, indent=2))
    return 0 if result.get('uefi_read_only_qualified' if mode == 'read-only-blockio' else 'snapshot_pass') else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial', required=True)
    p.add_argument('--fastboot-serial')
    p.add_argument('--backup', type=Path, required=True)
    p.add_argument('--mode', choices=list(KINDS), default='registers')
    for name in ('uefi', 'unixnu', 'candidate-dir', 'evidence-root', 'restore-run', 'baseline-dir'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--wait-seconds', type=int, default=120)
    a = p.parse_args()
    a.fastboot_serial = a.fastboot_serial or a.serial
    if a.fastboot_serial != a.serial:
        p.error('UFS trials require the same verified Android/fastboot serial')
    a.backup = a.backup.expanduser().resolve()
    if a.restore_run:
        if a.dry_run or any(getattr(a, name) for name in ('uefi', 'unixnu', 'candidate_dir', 'evidence_root', 'baseline_dir')) or not 120 <= a.wait_seconds <= 600:
            p.error('restore-only takes no candidate/build paths and a 120..600 second deadline')
        a.backup = cycle.require_private_path(a.backup, (DEVICE, DEVICE.parent / 'UEFI-Dali', DEVICE.parent / 'UniXNU'), 'backup')
        a.restore_run = cycle.require_private_path(a.restore_run, (DEVICE, DEVICE.parent / 'UEFI-Dali', DEVICE.parent / 'UniXNU'), 'restore run')
        if a.restore_run == a.backup or a.backup in a.restore_run.parents:
            p.error('restore run must stay outside the immutable backup')
    else:
        if a.wait_seconds != 120 or any(getattr(a, name) is None for name in ('uefi', 'unixnu', 'candidate_dir', 'evidence_root')):
            p.error('new trial needs candidate/source/evidence paths and exactly 120 seconds')
        a.uefi, a.unixnu = a.uefi.resolve(), a.unixnu.resolve()
        repos = (DEVICE, a.uefi, a.unixnu)
        a.backup = cycle.require_private_path(a.backup, repos, 'backup')
        a.candidate_dir = cycle.require_private_path(a.candidate_dir, repos, 'candidate')
        a.evidence_root = cycle.require_private_path(a.evidence_root, repos, 'evidence')
        if (a.mode == 'read-only-blockio') != (a.baseline_dir is not None):
            p.error('only read-only-blockio requires --baseline-dir')
        if a.baseline_dir is not None:
            a.baseline_dir = cycle.require_private_path(a.baseline_dir, repos, 'baseline')
        for protected in (a.backup, a.candidate_dir, *([a.baseline_dir] if a.baseline_dir else [])):
            if a.evidence_root == protected or protected in a.evidence_root.parents:
                p.error('evidence must stay outside the immutable backup and candidate')
    with DeviceLease(a.serial) as lease:
        return restore_pending(a, lease) if a.restore_run else run_trial(a, lease)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (cycle.CycleError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('run-ufs-probe: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
