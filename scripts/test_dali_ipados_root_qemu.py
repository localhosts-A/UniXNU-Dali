#!/usr/bin/env python3
"""Observe the read-only 21H461 OS root through real XNU Virtio/APFS owner auth.

This is a QEMU-only diagnostic, stopping before launchd exec. The existing
independent owner policy is enforced; Apple rootauth and AMFI still execute.
No Apple hardware trust, userspace boot, or physical Dali result is asserted.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
OS_UUID = '04769D26-2CA0-4825-B443-E2E5F88300F2'
OS_BYTES = 6077580800
SYSTEMOS_BYTES = 3303014400
OWNER_KEY_BYTES = 395
LAUNCHD_SHA256 = '8cec9a3c44a443b08e977ccc5d577850316ad428c575dc01f09b018dbd5b6a25'
IMG4_IGNITION_BLOB_MIB = '65000000650000007600000064000000'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb', buffering=0) as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def load_device_runner():
    path = ROOT / 'scripts/test-dali-xnu-qemu.py'
    spec = importlib.util.spec_from_file_location('dali_qemu_validation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_qemu_virtio_fdt(qemu, output, workdir):
    if output.exists():
        raise ValueError('QEMU FDT output must be a new file')
    disks = [workdir / 'dtb-probe-os.raw', workdir / 'dtb-probe-systemos.raw']
    for disk in disks:
        with disk.open('xb') as stream:
            stream.truncate(1024 * 1024)
    command = [qemu, '-machine', f'virt,virtualization=off,secure=off,dumpdtb={output},gic-version=3',
               '-cpu', 'cortex-a710', '-accel', 'tcg', '-m', '2048M', '-nodefaults',
               '-display', 'none', '-nic', 'none', '-global', 'virtio-mmio.force-legacy=false',
               '-blockdev', json.dumps({'driver': 'file', 'node-name': 'probe-os-file',
                                        'filename': str(disks[0]), 'read-only': True}),
               '-blockdev', json.dumps({'driver': 'raw', 'node-name': 'probe-os',
                                        'file': 'probe-os-file', 'read-only': True}),
               '-device', 'virtio-blk-device,drive=probe-os',
               '-blockdev', json.dumps({'driver': 'file', 'node-name': 'probe-systemos-file',
                                        'filename': str(disks[1]), 'read-only': True}),
               '-blockdev', json.dumps({'driver': 'raw', 'node-name': 'probe-systemos',
                                        'file': 'probe-systemos-file', 'read-only': True}),
               '-device', 'virtio-blk-device,drive=probe-systemos']
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=60)
    if result.returncode or not output.is_file():
        raise RuntimeError('QEMU failed to generate Virtio FDT: ' + result.stdout[-5000:])
    data = output.read_bytes()
    if len(data) < 40 or struct.unpack_from('>I', data)[0] != 0xd00dfeed:
        raise ValueError('QEMU output is not a flattened device tree')
    return hashlib.sha256(data).hexdigest(), command


def attach_readonly_root_image(image):
    image = Path(image).resolve(strict=True)
    info = plistlib.loads(subprocess.check_output(['hdiutil', 'info', '-plist'], timeout=30))
    if any(Path(item['image-path']).resolve() == image for item in info.get('images', [])):
        raise ValueError('root image is already attached; do not detach another run\'s media')
    attached = subprocess.run(
        ['hdiutil', 'attach', '-nomount', '-readonly', '-noverify', '-noautofsck', '-plist', str(image)],
        check=True, capture_output=True, timeout=120)
    devices = set()
    try:
        result = plistlib.loads(attached.stdout)
        entities = result.get('system-entities', [])
        devices = {entry['dev-entry'] for entry in entities
                   if re.fullmatch(r'/dev/disk[0-9]+', entry.get('dev-entry', ''))}
        matches = [entry.get('dev-entry') for entry in entities
                   if entry.get('content-hint') == 'GUID_partition_scheme']
        if len(matches) != 1 or matches[0] not in devices:
            raise ValueError('hdiutil must expose one GPT whole-disk device')
        device = matches[0]
        info = plistlib.loads(subprocess.check_output(
            ['diskutil', 'info', '-plist', device], timeout=30))
        required = {'BusProtocol': 'Disk Image', 'Writable': False,
                    'DeviceNode': device, 'TotalSize': OS_BYTES}
        observed = {key: info.get(key) for key in required}
        if observed != required:
            raise ValueError(f'root UDIF is not the expected read-only whole disk: {observed}')
        raw = Path('/dev/r' + Path(device).name)
        if not raw.exists() or not stat.S_ISCHR(raw.stat().st_mode):
            raise ValueError('expected the actual read-only whole-disk character device')
        with raw.open('rb', buffering=0) as stream:
            if len(stream.read(4096)) != 4096:
                raise ValueError('short read from attached OS whole disk')
        return device
    except BaseException:
        for device in sorted(devices):
            subprocess.run(['hdiutil', 'detach', device], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        raise


def detach_readonly_root_image(device):
    subprocess.run(['hdiutil', 'detach', device], check=True, timeout=60)


def validate_qemu_root_config(path):
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if line.startswith('# CONFIG_') and line.endswith(' is not set'):
            key, value = line[2:].split(' ', 1)[0], 'n'
        elif line.startswith('CONFIG_') and '=' in line:
            key, value = line.split('=', 1)
        elif not line or line.startswith('#'):
            continue
        else:
            raise ValueError(f'invalid QEMU root config line {number}')
        if key in values:
            raise ValueError(f'duplicate QEMU root config key: {key}')
        values[key] = value.strip('"')
    enabled = {key for key, value in values.items() if value == 'y'}
    disabled = {key for key, value in values.items() if value == 'n'}
    required = {'CONFIG_XNU_BUILD_ARCHIVES', 'CONFIG_XNU_SOURCE_STORAGE_FAMILY',
                'CONFIG_XNU_SOURCE_CORECRYPTO14', 'CONFIG_XNU_NATIVE_VIRTIO_BLOCK',
                'CONFIG_XNU_NATIVE_VIRTIO_QEMU_SOURCE_STORAGE', 'CONFIG_XNU_VERIFIED_ROOT_PROVIDER',
                'CONFIG_XNU_OWNER_ROOT_AUDIT', 'CONFIG_XNU_OWNER_ROOT_TRUST',
                'CONFIG_XNU_TRUST_CACHE_RUNTIME'}
    forbidden = {'CONFIG_XNU_NATIVE_AES', 'CONFIG_XNU_NATIVE_UFS',
                 'CONFIG_XNU_NATIVE_RAM_BLOCK', 'CONFIG_XNU_SOURCE_MOCKFS',
                 'CONFIG_XNU_SOURCE_ENTITLEMENT_FREE'}
    if not required <= enabled:
        raise ValueError(f'QEMU root config lacks required features: {sorted(required - enabled)}')
    if enabled & forbidden:
        raise ValueError(f'QEMU root config enables hardware/fixture features: {sorted(enabled & forbidden)}')
    if not forbidden <= disabled:
        raise ValueError(f'QEMU root config must explicitly disable hardware/fixture features: {sorted(forbidden - disabled)}')
    if values.get('CONFIG_XNU_CRYPTO_BOOTSTRAP') != 'source':
        raise ValueError('QEMU root config requires the source crypto bootstrap')
    archive = values.get('CONFIG_XNU_NATIVE_VIRTIO_BLOCK_ARCHIVE', '')
    if not archive.endswith('/build/native-virtio-block-qemu-source-storage/libNativeVirtioBlock.a'):
        raise ValueError('QEMU root config must select the isolated source-storage Virtio archive')
    extras = values.get('CONFIG_XNU_EXTRA_LD_ARCHIVES', '')
    if 'libDaliUfs.a' in extras or 'libNativeUfs.a' in extras:
        raise ValueError('QEMU root config links the physical Dali UFS driver')
    if 'libNativeVirtioBlock.a' in extras:
        raise ValueError('QEMU root config duplicates the standard Virtio archive force-load')
    if 'libVerifiedRootDali.a' not in extras or 'libDaliBootLog.a' not in extras:
        raise ValueError('QEMU root config must link owner-policy and Dali logging archives')
    return {'required': sorted(required), 'forbidden_disabled': sorted(forbidden),
            'virtio_archive': archive, 'extra_archives': extras}


def validate_inputs(kernel, report_path, verification_path, config_path,
                    trust_cache_path, root_image_path, systemos_image_path,
                    userdata_report_path, root_policy_path):
    runner = load_device_runner()
    board = tomllib.loads((ROOT / 'board.toml').read_text())
    images = {}
    for name, path, pin in (('OS', root_image_path, '044-76040-038.dmg'),
                            ('SystemOS', systemos_image_path, '044-76034-038.dmg')):
        path = Path(path).resolve(strict=True)
        expected = board['ipsw']['sha256'][pin]
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f'read-only {name} input does not match the board SHA-256 pin')
        with path.open('rb', buffering=0) as source:
            first_sector = source.read(512)
        images[name] = {'path': str(path), 'sha256': expected, 'bytes': path.stat().st_size,
                        'first_sector_sha256': hashlib.sha256(first_sector).hexdigest()}
    systemos = Path(systemos_image_path)
    if systemos.stat().st_size != SYSTEMOS_BYTES:
        raise ValueError('SystemOS input does not have its pinned raw geometry')
    with systemos.open('rb', buffering=0) as source:
        source.seek(32)
        if source.read(4) != b'NXSB':
            raise ValueError('SystemOS input lacks the pinned raw APFS superblock')
    prelink = runner.validate_prelink_audit_candidate(kernel, report_path)
    verification = runner.validate_prelink_structure_audit(kernel, prelink, verification_path)
    trust = runner.validate_trust_cache(trust_cache_path, board['payload']['trust_cache_sha256'])
    config = runner.validate_source_xnu_config(config_path, require_trust_cache_runtime=True)
    qemu_config = validate_qemu_root_config(config_path)
    policy = json.loads(Path(root_policy_path).read_bytes())
    image_report = json.loads(Path(userdata_report_path).read_bytes())
    if (image_report.get('image_bytes') != 9382658048 or
            image_report.get('image_sha256') != 'fd43ee7035ce185ce4215ac93fdc006fd784a54b3b7442e21457c226cbe03093' or
            image_report.get('hardware_tested') is not False):
        raise ValueError('userdata report is not the exact pinned assembled image')
    if policy.get('userdata_image_sha256') != image_report.get('image_sha256'):
        raise ValueError('root policies do not bind the exact userdata image report')
    if not re.fullmatch('[0-9a-f]{64}', policy.get('owner_anchor_sha256', '')):
        raise ValueError('root policy owner anchor is missing or malformed')
    expected_windows = {
        'OS': (OS_BYTES, 'e5634d04a8ac35f024845f583394e0a35deb26a04cb0c00286a8674e25a9a4f2', OS_UUID),
        'SystemOS': (SYSTEMOS_BYTES, 'b4908cde41d23e6bc05e6af51a2c9eeb00cf0f863a6a18b1600c96a3fd70414f',
                     '8846FE78-2910-4297-AF97-D59BB6F8FC16'),
    }
    if len(policy.get('policies', [])) != 2:
        raise ValueError('root build report must bind exactly OS and SystemOS')
    for actual, name in zip(policy['policies'], ('OS', 'SystemOS')):
        size, sha, volume_uuid = expected_windows[name]
        window = next((item for item in image_report.get('windows', []) if item.get('name') == name), None)
        if (actual.get('name') != name or actual.get('image_bytes') != size or
                actual.get('image_sha256') != sha or actual.get('volume_uuid') != volume_uuid or
                actual.get('trusted_key_sha256') != policy['owner_anchor_sha256'] or
                not window or window.get('size') != size or window.get('sha256') != sha):
            raise ValueError(f'{name} policy does not bind its exact pinned source window')
    archive = Path(root_policy_path).with_name('libVerifiedRootDali.a')
    if (policy.get('signature_verified_by_openssl') is not True or policy.get('private_key_in_archive') is not False or
            policy.get('boot_root_authenticated') is not False or not archive.is_file() or
            digest(archive) != policy.get('archive_sha256')):
        raise ValueError('root policy archive/report is missing or overstates device authentication')
    return {'kernel_sha256': digest(kernel), 'prelink': prelink, 'verification': verification,
            'trust_cache': trust, 'config': config, 'policy': policy,
            'qemu_config': qemu_config, 'images': images}


def validate_root_report(report, inputs):
    if (report.get('stop_symbol') != '_native_owner_root_accepted_checkpoint' or
            report.get('bounded_timeout_interrupted') is not False or
            report.get('function_return_stack_matched') is not True or
            report.get('functional_ipados_boot_demonstrated') is not False or
            report.get('ipados_userspace_boot_demonstrated') is not False or
            report.get('hardware_boot_demonstrated', False) is not False):
        raise ValueError('root checkpoint report is not the bounded host-only matched return')
    if (report.get('kernel_sha256') != inputs['kernel_sha256'] or
            report.get('offline_qemu_vmm_present_declared') is not True):
        raise ValueError('root checkpoint does not bind the exact kernel and actual QEMU identity')
    try:
        if int(report['pc'], 16) != int(report['function_return_target'], 16):
            raise ValueError('root checkpoint did not reach its actual return address')
    except (KeyError, TypeError) as error:
        raise ValueError('root checkpoint lacks its actual return address') from error
    binding = report.get('owner_root_binding_audit', {})
    device = binding.get('_native_owner_root_mount_device', 0xffffffff)
    if (binding.get('boot_root_accepted') is not True or binding.get('_native_owner_root_accepted') != 1 or
            binding.get('_native_owner_root_decision') != 1 or
            binding.get('_native_owner_root_trust_enabled') != 1 or
            binding.get('_native_owner_root_apple_autherr') != 80 or
            binding.get('_native_owner_root_readonly') != 1 or binding.get('actual_uuid') != OS_UUID or
            not isinstance(device, int) or not 0 < device < 0xffffffff or
            device != binding.get('_native_owner_root_selected_device') or
            device != binding.get('_native_verified_block_root_device') or
            binding.get('_native_verified_block_root_media_matches') != 1 or
            binding.get('_native_verified_block_root_uuid_match') != 1):
        raise ValueError('actual XNU owner-root decision/device/UUID/read-only evidence failed')
    if (report.get('original_amfi_table_owner_observed') is not True or
            report.get('crypto14_initialization_observed') is not True or
            report.get('trust_cache_bundle_sha256') != inputs['trust_cache']['sha256']):
        raise ValueError('original AMFI, initialized crypto or exact trust-cache handoff was not observed')
    try:
        anchor = bytes.fromhex(report['requested_kernel_symbols']['_verified_root_key'])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('actual linked owner public anchor was not captured') from error
    if (len(anchor) != OWNER_KEY_BYTES or not anchor.startswith(b'VROOTKEY') or
            hashlib.sha256(anchor).hexdigest() != inputs['policy']['owner_anchor_sha256']):
        raise ValueError('actual linked owner public anchor differs from the policy input')
    virtio = report.get('native_virtio_globals', {})
    integrity = report.get('native_virtio_integrity', {})
    if (virtio.get('_native_virtio_ready') != 1 or virtio.get('_native_virtio_read_requests', 0) <= 0 or
            virtio.get('_native_virtio_read_failures') != 0 or
            integrity.get('_native_virtio_manifest_ready') != 1 or
            integrity.get('_native_virtio_integrity_failures') != 0 or
            integrity.get('_native_virtio_verified_blocks', 0) <= 0):
        raise ValueError('actual signed Virtio root reads were not observed')
    os_input = report.get('virtio_block_input', {})
    systemos_input = report.get('virtio_extra_block_input', {})
    if (os_input.get('read_only') is not True or os_input.get('device_attached') is not True or
            os_input.get('source_format') != 'UDIF' or os_input.get('bytes') != OS_BYTES or
            os_input.get('host_media_writable') is not False or
            os_input.get('source_sha256') != inputs['images']['OS']['sha256'] or
            systemos_input.get('read_only') is not True or systemos_input.get('device_attached') is not True or
            systemos_input.get('bytes') != SYSTEMOS_BYTES or
            systemos_input.get('first_sector_sha256') != inputs['images']['SystemOS']['first_sector_sha256']):
        raise ValueError('actual QEMU root media were not the exact read-only input pair')
    if not any(event.get('symbol') == '_vfs_mountroot' for event in report.get('trace_events', [])):
        raise ValueError('real BSD root-mount entry was not observed')


def run_root_probe(unixnu, kernel, prelink_report, trust_cache_path,
                   root_image, systemos_image, efi, fdt, output, firmware=None):
    if output.exists():
        raise ValueError('QEMU output directory must be new')
    output.mkdir(parents=True, exist_ok=False)
    probe = Path(unixnu).resolve() / 'scripts/probe-darwinboot-xnu.py'
    command = [sys.executable, '-I', '-B', str(probe),
               '--kernel', str(Path(kernel).resolve()), '--uefi-efi', str(Path(efi).resolve()),
               '--prelink-report', str(Path(prelink_report).resolve()),
               '--trust-cache', str(Path(trust_cache_path).resolve()),
               '--virtio-udif-image', str(Path(root_image).resolve()),
               '--virtio-extra-block-image', str(Path(systemos_image).resolve()),
               '--virtio-mmio-fdt', str(Path(fdt).resolve()),
               '--root-match-volume-uuid', OS_UUID, '--qemu-vmm-present', '--armvirt', '--armcpu',
               '--nvram-proxy', '--crypto-hash-method', 'sha2-384', '--amfi-platform-only',
               '--require-crypto14', '--owner-root-accepted', '--finish-function',
               '--break-symbol', '_panic', '--timeout', '180', '--qemu-memory-mib', '2048', '--kext-logs',
               '--read-kernel-symbol', f'_verified_root_key:{OWNER_KEY_BYTES}',
               '--trace-symbol', '_vfs_mountroot', '--trace-symbol', '_load_init_program',
               '--trace-symbol', '_native_owner_root_checkpoint',
               '--kernel-base-args', '-v dali-earlyboot=1 kt_fixed_zones=120 serial=3 debug=0x14e',
               '--output', str(output)]
    if firmware is not None:
        command += ['--qemu-firmware', str(Path(firmware).resolve())]
    (output / 'probe-command.json').write_text(json.dumps(command, indent=2) + '\n')
    probe_env = os.environ.copy()
    probe_env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(command, env=probe_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=300)
    (output.parent / (output.name + '.log')).write_text(result.stdout)
    report_path = output / 'gdb-stop.json'
    if result.returncode:
        raise RuntimeError('QEMU root probe stopped without acceptance: ' + result.stdout[-8000:])
    if not report_path.is_file():
        raise RuntimeError('QEMU root probe produced no GDB report')
    return json.loads(report_path.read_bytes())


def verify_source_media(inputs):
    for name, image in inputs['images'].items():
        path = Path(image['path'])
        if path.stat().st_size != image['bytes'] or digest(path) != image['sha256']:
            raise ValueError(f'source {name} image changed during the QEMU run')


def root_summary(report, inputs, fdt_sha256):
    validate_root_report(report, inputs)
    return {
        'candidate_sha256': report['kernel_sha256'],
        'effective_config_sha256': inputs['config']['sha256'],
        'trust_cache_sha256': inputs['trust_cache']['sha256'],
        'root_image_sha256': inputs['images']['OS']['sha256'],
        'systemos_image_sha256': inputs['images']['SystemOS']['sha256'],
        'qemu_root_config': inputs['qemu_config'], 'qemu_fdt_sha256': fdt_sha256,
        'owner_anchor_sha256': inputs['policy']['owner_anchor_sha256'],
        'owner_root_accepted': True, 'verified_root_authenticated': True,
        'apple_rootauth_errno': report['owner_root_binding_audit']['_native_owner_root_apple_autherr'],
        'apple_hardware_root_trust_demonstrated': False,
        'actual_virtio_verified_blocks': report['native_virtio_integrity']['_native_virtio_verified_blocks'],
        'init_program_entry_observed': any(event.get('symbol') == '_load_init_program'
                                           for event in report.get('trace_events', [])),
        'launchd_exec_observed': False,
        'apple_userspace_boot_demonstrated': False,
        'springboard_display_demonstrated': False,
        'hardware_boot_demonstrated': False, 'device_flashable': False,
        'scope': 'QEMU owner-policy root checkpoint before launchd; not Apple hardware trust or Dali qualification',
    }


def validate_launchd_target(path):
    path = Path(path).resolve(strict=True)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != LAUNCHD_SHA256:
        raise ValueError('launchd input does not match the pinned original 21H461 executable')
    if len(data) < 32 or struct.unpack_from('<I', data)[0] != 0xfeedfacf:
        raise ValueError('launchd input is not the pinned ARM64 Mach-O')
    ncmds, sizeofcmds = struct.unpack_from('<II', data, 16)
    commands_end = 32 + sizeofcmds
    if commands_end > len(data) or ncmds > sizeofcmds // 8:
        raise ValueError('launchd Mach-O load-command range is invalid')
    offset = 32
    entries = []
    segments = []
    for _ in range(ncmds):
        if offset + 8 > commands_end:
            raise ValueError('launchd Mach-O load command is truncated')
        command, command_size = struct.unpack_from('<II', data, offset)
        if command_size < 8 or offset + command_size > commands_end:
            raise ValueError('launchd Mach-O load command has invalid bounds')
        if command == 0x80000028:
            if command_size < 24:
                raise ValueError('launchd LC_MAIN command is truncated')
            entries.append(struct.unpack_from('<Q', data, offset + 8)[0])
        elif command == 0x19:
            if command_size < 72:
                raise ValueError('launchd LC_SEGMENT_64 command is truncated')
            raw_name = data[offset + 8:offset + 24].split(b'\0', 1)[0]
            try:
                name = raw_name.decode('ascii')
            except UnicodeDecodeError as error:
                raise ValueError('launchd segment name is not ASCII') from error
            vmaddr, vmsize, fileoff, filesize, maxprot, initprot = struct.unpack_from(
                '<QQQQii', data, offset + 24)
            if (not vmsize or filesize > vmsize or fileoff + filesize > len(data) or
                    vmaddr + vmsize > 1 << 64):
                raise ValueError('launchd Mach-O segment bounds are invalid')
            segments.append({'name': name, 'vmaddr': vmaddr, 'vmsize': vmsize,
                             'fileoff': fileoff, 'filesize': filesize,
                             'maxprot': maxprot, 'initprot': initprot})
        offset += command_size
    text = [segment for segment in segments if segment['name'] == '__TEXT' and segment['fileoff'] == 0]
    if (len(entries) != 1 or len(text) != 1 or
            not 0 < entries[0] < text[0]['filesize'] - 16):
        raise ValueError('launchd must have one file-backed __TEXT segment and exactly one backed LC_MAIN entry')
    entry = entries[0]
    return {'path': str(path), 'sha256': LAUNCHD_SHA256, 'bytes': len(data),
            'entry_offset': entry, 'entry_prefix': data[entry:entry + 16].hex(),
            'mach_header': data[:32].hex(), 'segments': segments}


def verify_launchd_target(launchd):
    path = Path(launchd['path'])
    if (not path.is_file() or path.stat().st_size != launchd['bytes'] or
            digest(path) != launchd['sha256']):
        raise ValueError('pinned launchd input changed during the QEMU run')


def map_launchd_syscall_pcs(report, launchd):
    main = report.get('followed_main_entry') or {}
    text = [segment for segment in launchd['segments']
            if segment['name'] == '__TEXT' and segment['fileoff'] == 0]
    if len(text) != 1 or not main.get('mach_header'):
        return {'available': False, 'reason': 'missing unique __TEXT mapping or runtime Mach header'}
    try:
        header = int(main['mach_header'], 0)
        if header & 4095 or main.get('entry_offset') != launchd['entry_offset']:
            raise ValueError
    except (TypeError, ValueError):
        return {'available': False, 'reason': 'invalid runtime Mach header or LC_MAIN offset'}

    data = Path(launchd['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != launchd['sha256']:
        raise ValueError('pinned launchd input changed before syscall-PC mapping')
    slide = header - text[0]['vmaddr']

    def map_address(value):
        try:
            address = int(value, 0)
        except (TypeError, ValueError):
            return {'mapped_to_launchd_executable': False, 'reason': 'invalid address'}
        matches = []
        for segment in launchd['segments']:
            if not segment['initprot'] & 4:
                continue
            start = segment['vmaddr'] + slide
            if start <= address < start + segment['vmsize']:
                offset = segment['fileoff'] + address - start
                matches.append({'segment': segment['name'], 'file_offset': offset,
                                'file_backed': offset < segment['fileoff'] + segment['filesize']})
        if len(matches) != 1:
            return {'mapped_to_launchd_executable': False,
                    'reason': 'outside executable segments' if not matches else 'overlapping executable segments'}
        return {'mapped_to_launchd_executable': True, **matches[0]}

    events = []
    for index, event in enumerate(report.get('syscall_events', [])):
        if not isinstance(event, dict):
            continue
        events.append({'index': index, 'number': event.get('number'),
                       'user_pc': map_address(event.get('user_pc')),
                       'user_lr': map_address(event.get('user_lr'))})
    return {
        'available': True,
        'launchd_sha256': launchd['sha256'],
        'runtime_mach_header': hex(header),
        'image_slide': hex(slide) if slide >= 0 else f'-0x{-slide:x}',
        'lc_main_target': hex(header + launchd['entry_offset']),
        'lc_main_observed': bool(main.get('observed') and main.get('first_instruction_completed')),
        'events': events,
        'scope': 'address-to-file mapping only; does not establish LC_MAIN completion',
    }


APPLEIMAGE4_GATE_WAIT_CALLSITE_OFFSET = 0x8994


def resolve_image4_gate_wait_callsite(kernel, prelink_report_path):
    kernel = Path(kernel).resolve(strict=True)
    report = json.loads(Path(prelink_report_path).read_bytes())
    candidate_sha256 = digest(kernel)
    if report.get('candidate_sha256') != candidate_sha256:
        raise ValueError('Gate-wait trace requires the exact prelink candidate')
    extents = [extent for extent in report.get('extents', [])
               if extent.get('module') == 'com.apple.security.AppleImage4' and
               extent.get('segment') == '__TEXT_EXEC']
    if len(extents) != 1:
        raise ValueError('Exact candidate must have one AppleImage4 executable extent')
    extent = extents[0]
    start, size = extent.get('va'), extent.get('size')
    if (not isinstance(start, int) or isinstance(start, bool) or
            not isinstance(size, int) or isinstance(size, bool) or size <= APPLEIMAGE4_GATE_WAIT_CALLSITE_OFFSET + 4 or
            start < 0 or start + size > 1 << 64):
        raise ValueError('AppleImage4 candidate text extent is invalid')
    callsite = start + APPLEIMAGE4_GATE_WAIT_CALLSITE_OFFSET
    if callsite & 3:
        raise ValueError('AppleImage4 gate-wait callsite is not instruction aligned')

    image_size = kernel.stat().st_size
    segments = []
    with kernel.open('rb') as image:
        header = image.read(32)
        if len(header) != 32:
            raise ValueError('Candidate Mach-O header is truncated')
        magic, cpu, _, filetype, ncmds, sizeofcmds, _, _ = struct.unpack('<IiiIIIII', header)
        commands_end = 32 + sizeofcmds
        if (magic != 0xfeedfacf or cpu != 0x100000c or filetype != 2 or
                commands_end > image_size or ncmds > sizeofcmds // 8):
            raise ValueError('Gate-wait trace requires a complete ARM64 MH_EXECUTE candidate')
        offset = 32
        for _ in range(ncmds):
            if offset + 8 > commands_end:
                raise ValueError('Candidate Mach-O load command is truncated')
            image.seek(offset)
            command_header = image.read(8)
            command, command_size = struct.unpack('<II', command_header)
            if command_size < 8 or offset + command_size > commands_end:
                raise ValueError('Candidate Mach-O load command has invalid bounds')
            if command == 0x19:
                if command_size < 72:
                    raise ValueError('Candidate LC_SEGMENT_64 command is truncated')
                raw_segment = image.read(64)
                name_bytes, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, _, _ = struct.unpack(
                    '<16sQQQQiiII', raw_segment)
                if (filesize > vmsize or fileoff + filesize > image_size or
                        vmaddr + vmsize > 1 << 64):
                    raise ValueError('Candidate Mach-O segment bounds are invalid')
                try:
                    name = name_bytes.split(b'\0', 1)[0].decode('ascii')
                except UnicodeDecodeError as error:
                    raise ValueError('Candidate Mach-O segment name is not ASCII') from error
                if vmsize:
                    segments.append({'name': name, 'vmaddr': vmaddr, 'vmsize': vmsize,
                                     'fileoff': fileoff, 'filesize': filesize,
                                     'maxprot': maxprot, 'initprot': initprot})
            offset += command_size
        if offset != commands_end:
            raise ValueError('Candidate Mach-O load-command size does not match its header')

        def read_candidate_va(address, length, required_protection=0, forbidden_protection=0):
            if (not isinstance(address, int) or not isinstance(length, int) or length <= 0 or
                    address < 0 or address + length > 1 << 64):
                raise ValueError('Invalid candidate VA read')
            matches = [segment for segment in segments
                       if segment['vmaddr'] <= address and
                       address + length <= segment['vmaddr'] + segment['filesize'] and
                       segment['initprot'] & required_protection == required_protection and
                       not segment['initprot'] & forbidden_protection]
            if len(matches) != 1:
                raise ValueError('Candidate VA is not uniquely backed with the required protection')
            segment = matches[0]
            image.seek(segment['fileoff'] + address - segment['vmaddr'])
            raw = image.read(length)
            if len(raw) != length:
                raise ValueError('Candidate VA read is truncated')
            return raw

        call_instruction = int.from_bytes(read_candidate_va(callsite, 4, required_protection=4), 'little')
        if call_instruction & 0xfc000000 != 0x94000000:
            raise ValueError('AppleImage4 gate-wait callsite is not a direct BL in this candidate')
        displacement = call_instruction & 0x03ffffff
        if displacement & (1 << 25):
            displacement -= 1 << 26
        stub = callsite + (displacement << 2)
        if not start <= stub < start + size:
            raise ValueError('AppleImage4 gate-wait import stub left its pinned text extent')
        stub_bytes = read_candidate_va(stub, 12, required_protection=4)
        adrp, ldr, branch = struct.unpack('<3I', stub_bytes)
        if adrp & 0x9f00001f != 0x90000010:
            raise ValueError('AppleImage4 gate-wait stub is not ADRP x16')
        if ldr & 0xffc003ff != 0xf9400210 or branch != 0xd61f0200:
            raise ValueError('AppleImage4 gate-wait stub is not the pinned LDR/BR x16 form')
        page_delta = (((adrp >> 5) & 0x7ffff) << 2) | ((adrp >> 29) & 3)
        if page_delta & (1 << 20):
            page_delta -= 1 << 21
        slot = (stub & ~0xfff) + (page_delta << 12) + (((ldr >> 10) & 0xfff) << 3)
        slot_target = int.from_bytes(read_candidate_va(
            slot, 8, required_protection=1, forbidden_protection=2 | 4), 'little')
        symbols = subprocess.check_output(['xcrun', 'nm', '-U', kernel], text=True)
        matches = re.findall(r'^([0-9a-fA-F]+)\s+\S\s+_lck_mtx_gate_wait$', symbols, re.M)
        if len(matches) != 1 or int(matches[0], 16) != slot_target:
            raise ValueError('AppleImage4 gate-wait slot does not resolve uniquely to _lck_mtx_gate_wait')
        read_candidate_va(slot_target, 4, required_protection=4)

    return dict(candidate_sha256=candidate_sha256, module='com.apple.security.AppleImage4',
                segment='__TEXT_EXEC', module_offset=hex(APPLEIMAGE4_GATE_WAIT_CALLSITE_OFFSET),
                address=hex(callsite), import_stub=hex(stub), slot=hex(slot),
                target_symbol='_lck_mtx_gate_wait', target=hex(slot_target), read_only=True,
                scope='candidate-bound one-shot callsite trace; no guest memory or register writes')


GATE_ILOCK = 1
GATE_WAITERS = 2
GATE_THREAD_MASK = ((1 << 64) - 1) ^ (GATE_ILOCK | GATE_WAITERS)


def gate_holder_thread_diagnostic(snapshots, trace_events, callsite, read_request):
    result = dict(observed=False, read_request=read_request,
                  scope='source-defined gate holder candidate matched only against bounded task-owned thread records; no pointer dereference')
    if not trace_events:
        return result
    result['observed'] = True
    if len(trace_events) != 1:
        result['unavailable'] = 'expected exactly one one-shot gate trace event'
        return result
    if not isinstance(snapshots, dict):
        result['unavailable'] = 'compiler-qualified thread snapshot missing'
        return result
    event = trace_events[0]
    reads = event.get('register_memory_reads')
    read = reads.get(read_request) if isinstance(reads, dict) else None
    if not isinstance(read, dict):
        result['unavailable'] = 'bounded x1 gate-header read missing'
        return result
    raw = read.get('raw_hex')
    try:
        gate_pointer = int(event['x1'], 16)
    except (KeyError, TypeError, ValueError):
        result['unavailable'] = 'gate argument register is unavailable'
        return result
    if (read.get('register') != 'x1' or read.get('base') != hex(gate_pointer) or
            read.get('offset') != '0x0' or read.get('address') != hex(gate_pointer) or
            read.get('length') != 16 or not isinstance(raw, str) or
            not re.fullmatch(r'[0-9a-fA-F]{32}', raw)):
        result['unavailable'] = 'bounded x1 read does not match the source-typed gate argument'
        return result
    header = bytes.fromhex(raw)
    gt_data = int.from_bytes(header[:8], 'little')
    holder = gt_data & GATE_THREAD_MASK
    holder_valid = holder == 0 or (0xfffffff000000000 <= holder < 1 << 64 and holder & 3 == 0)
    result.update(gate_pointer=hex(gate_pointer), gate_header_raw_hex=raw,
                  gt_data=hex(gt_data), gate_interlock=bool(gt_data & GATE_ILOCK),
                  gate_waiters=bool(gt_data & GATE_WAITERS),
                  holder_thread_pointer_candidate=hex(holder) if holder else None,
                  holder_pointer_valid=holder_valid,
                  kernel_task_threads_included=snapshots.get('kernel_task_threads_included') is True)
    if not holder_valid or not holder:
        result['thread_matches'] = []
        return result

    matches = []
    process_rows = snapshots.get('processes')
    if isinstance(process_rows, list):
        for row in process_rows:
            if not isinstance(row, dict):
                continue
            process = row.get('process')
            threads = row.get('threads')
            if not isinstance(process, dict) or not isinstance(threads, list):
                continue
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                try:
                    address = int(thread.get('thread'), 16)
                except (TypeError, ValueError):
                    continue
                if address == holder:
                    matches.append(dict(process_pid=process.get('pid'), process_name=process.get('name'),
                                        process_snapshot_complete=row.get('complete'),
                                        thread_id=thread.get('thread_id'), state=thread.get('state')))
    kernel_rows = [row for row in process_rows if isinstance(row, dict) and
                   isinstance(row.get('process'), dict) and row['process'].get('pid') == 0 and
                   row['process'].get('name') == 'kernel_task'] if isinstance(process_rows, list) else []
    result['kernel_task_snapshot_complete'] = bool(kernel_rows and kernel_rows[0].get('complete') is True)
    result['thread_matches'] = matches
    result['thread_match_count'] = len(matches)
    return result


def vm_page_counters_diagnostic(requested_symbols):
    names = {
        '_vm_page_free_count': 'free_pages',
        '_vm_page_free_target': 'free_page_target',
        '_vm_page_free_min': 'free_page_minimum',
        '_vm_page_free_wanted': 'threads_waiting_for_memory',
        '_vm_page_free_wanted_privileged': 'privileged_threads_waiting_for_memory',
    }
    result = dict(observed=False, counters={},
                  scope='source-typed unsigned-int VM counters from one paused QEMU stop; no guest writes')
    unavailable = []
    if not isinstance(requested_symbols, dict):
        return {**result, 'unavailable': 'kernel symbol snapshot is missing'}
    for symbol, label in names.items():
        raw = requested_symbols.get(symbol)
        if not isinstance(raw, str) or not re.fullmatch(r'[0-9a-fA-F]{8}', raw):
            unavailable.append(symbol)
            continue
        result['counters'][label] = int.from_bytes(bytes.fromhex(raw), 'little')
    result['observed'] = not unavailable
    if unavailable:
        result['unavailable_symbols'] = unavailable
    return result


def launchd_thread_snapshot_diagnostic(snapshots):
    if not isinstance(snapshots, dict):
        return None
    rows = snapshots.get('processes')
    launchd_rows = []
    if isinstance(rows, list):
        for row in rows:
            process = row.get('process', {}) if isinstance(row, dict) else {}
            if not isinstance(process, dict) or process.get('pid') != 1 or process.get('name') != 'launchd':
                continue
            threads = []
            thread_rows = row.get('threads', [])
            if not isinstance(thread_rows, list):
                thread_rows = []
            for thread in thread_rows[:4]:
                if not isinstance(thread, dict):
                    continue
                fields = ('thread_id', 'state', 'continuation', 'saved_state', 'user_pc', 'user_lr',
                          'saved_kernel_context', 'saved_kernel_context_error')
                threads.append({key: thread[key] for key in fields if key in thread})
            launchd_rows.append(dict(complete=row.get('complete'), thread_count=row.get('thread_count'),
                                     error=row.get('error'), threads=threads))
            if len(launchd_rows) == 1:
                break
    result = dict(read_only=snapshots.get('read_only'), native_sha256=snapshots.get('native_sha256'),
                  kernel_stack_size_bytes=snapshots.get('kernel_stack_size_bytes'),
                  kernel_stack_size_error=snapshots.get('kernel_stack_size_error'),
                  launchd_processes=launchd_rows)
    if not launchd_rows:
        result['available'] = False
        result['error'] = snapshots.get('error', 'PID 1 launchd snapshot is absent')
    return result


def validate_launchd_report(report, inputs, launchd):
    if (report.get('stop_symbol') != 'original-launchd-main' or
            report.get('original_launchd_main_executed') is not True or
            report.get('bounded_timeout_interrupted') is not False or
            report.get('functional_ipados_boot_demonstrated') is not False or
            report.get('ipados_userspace_boot_demonstrated') is not False or
            report.get('hardware_boot_demonstrated', False) is not False):
        raise ValueError('launchd report did not prove only the bounded original LC_MAIN execution')
    if (report.get('kernel_sha256') != inputs['kernel_sha256'] or
            report.get('offline_qemu_vmm_present_declared') is not True):
        raise ValueError('launchd report does not bind the exact kernel and QEMU identity')
    main = report.get('followed_main_entry', {})
    main_input = report.get('original_launchd_main_input', {})
    if (main.get('observed') is not True or main.get('first_instruction_completed') is not True or
            main.get('entry_offset') != launchd['entry_offset'] or
            main.get('entry_bytes_after_fault') != launchd['entry_prefix'] or
            main.get('mach_header_bytes') != launchd['mach_header'] or
            main_input.get('sha256') != launchd['sha256'] or
            main_input.get('entry_offset') != launchd['entry_offset'] or
            main_input.get('entry_prefix') != launchd['entry_prefix'] or
            main_input.get('mach_header') != launchd['mach_header']):
        raise ValueError('observed launchd entry is not the exact pinned LC_MAIN and original bytes')
    try:
        header = int(main['mach_header'], 16)
        entry_pc = int(main['pc'], 16)
        completed_pc = int(main['checkpoint_pc'], 16)
        pstate = int(main['pstate'], 16)
        checkpoint_pstate = int(main['checkpoint_pstate'], 16)
        report_pc = int(report['pc'], 16)
        report_pstate = int(report['pstate'], 16)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('launchd LC_MAIN report lacks actual user register state') from error
    if (entry_pc != header + launchd['entry_offset'] or completed_pc != entry_pc + 4 or
            report_pc != completed_pc or pstate & 0x1f != 0 or checkpoint_pstate & 0x1f != 0 or
            report_pstate & 0x1f != 0):
        raise ValueError('launchd main instruction did not complete at the exact original EL0 entry')
    if report.get('direct_user_commpage_shifts') != {'fffff4025:1': 12, 'fffff4037:1': 12}:
        raise ValueError('launchd user commpage does not report native 4K page shifts')
    if report.get('kernel_page_globals') != {
            '_PAGE_SHIFT_CONST': 12, '_page_shift': 12, '_page_shift_user32': 12, '_page_size': 4096}:
        raise ValueError('launchd kernel page globals do not retain native 4K policy')
    binding = report.get('owner_root_binding_audit', {})
    device = binding.get('_native_owner_root_mount_device', 0xffffffff)
    if (binding.get('boot_root_accepted') is not True or binding.get('_native_owner_root_accepted') != 1 or
            binding.get('_native_owner_root_decision') != 1 or binding.get('_native_owner_root_trust_enabled') != 1 or
            binding.get('_native_owner_root_apple_autherr') != 80 or binding.get('_native_owner_root_readonly') != 1 or
            binding.get('actual_uuid') != OS_UUID or not isinstance(device, int) or not 0 < device < 0xffffffff or
            device != binding.get('_native_owner_root_selected_device') or
            device != binding.get('_native_verified_block_root_device') or
            binding.get('_native_verified_block_root_media_matches') != 1 or
            binding.get('_native_verified_block_root_uuid_match') != 1):
        raise ValueError('launchd did not retain the actual read-only owner-root identity')
    root_selection = report.get('root_selection', {})
    if (root_selection.get('volume_uuid') != OS_UUID or
            root_selection.get('root_authentication_bypassed') is not False):
        raise ValueError('launchd root selection does not preserve the original authentication path')
    if (report.get('original_amfi_table_owner_observed') is not True or
            report.get('crypto14_initialization_observed') is not True or
            report.get('original_crypto_table_owner_observed') is not True or
            report.get('original_crypto_initialized_observed') is not True or
            report.get('trust_cache_bundle_sha256') != inputs['trust_cache']['sha256']):
        raise ValueError('launchd report lacks original AMFI/crypto or exact trust-cache input evidence')
    try:
        anchor = bytes.fromhex(report['requested_kernel_symbols']['_verified_root_key'])
        trust_loaded = bytes.fromhex(report['requested_kernel_symbols']['_trust_cache_static_loaded'])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('launchd report lacks linked owner or static trust-cache state') from error
    if (len(anchor) != OWNER_KEY_BYTES or not anchor.startswith(b'VROOTKEY') or
            hashlib.sha256(anchor).hexdigest() != inputs['policy']['owner_anchor_sha256'] or
            len(trust_loaded) != 1 or int.from_bytes(trust_loaded, 'little') != 1):
        raise ValueError('launchd linked owner anchor or runtime-loaded trust-cache state is invalid')
    events = {event.get('symbol') for event in report.get('trace_events', [])}
    if not {'_vfs_mountroot', '_native_owner_root_checkpoint', '_native_owner_root_accepted_checkpoint',
            '_load_static_trust_cache'} <= events:
        raise ValueError('launchd path is missing a root or trust-cache runtime milestone')
    activation = report.get('syscall_start_event', {})
    try:
        activation_pc = int(activation.get('pc', ''), 16)
    except (TypeError, ValueError):
        activation_pc = 0
    if not 0xfffffff000000000 <= activation_pc < 0xfffffff100000000:
        raise ValueError('launchd probe did not enter the _load_init_program syscall-log activation')
    virtio = report.get('native_virtio_globals', {})
    integrity = report.get('native_virtio_integrity', {})
    if (virtio.get('_native_virtio_ready') != 1 or virtio.get('_native_virtio_read_requests', 0) <= 0 or
            virtio.get('_native_virtio_read_failures') != 0 or
            integrity.get('_native_virtio_manifest_ready') != 1 or
            integrity.get('_native_virtio_integrity_failures') != 0 or
            integrity.get('_native_virtio_verified_blocks', 0) <= 0):
        raise ValueError('launchd did not retain authenticated Virtio root reads')
    os_input = report.get('virtio_block_input', {})
    systemos_input = report.get('virtio_extra_block_input', {})
    if (os_input.get('read_only') is not True or os_input.get('device_attached') is not True or
            os_input.get('source_format') != 'UDIF' or os_input.get('bytes') != OS_BYTES or
            os_input.get('host_media_writable') is not False or
            os_input.get('source_sha256') != inputs['images']['OS']['sha256'] or
            systemos_input.get('read_only') is not True or systemos_input.get('device_attached') is not True or
            systemos_input.get('bytes') != SYSTEMOS_BYTES or
            systemos_input.get('first_sector_sha256') != inputs['images']['SystemOS']['first_sector_sha256']):
        raise ValueError('launchd run did not use the exact read-only OS/SystemOS input pair')


def launchd_summary(report, inputs, launchd, fdt_sha256, prelink_report_path, prelink_verification_path):
    validate_launchd_report(report, inputs, launchd)
    binding = report['owner_root_binding_audit']
    entry = report['followed_main_entry']
    return {
        'schema': 'dali-ipados-qemu-launchd-v1',
        'candidate_sha256': report['kernel_sha256'],
        'effective_config_sha256': inputs['config']['sha256'],
        'prelink_report_sha256': digest(prelink_report_path),
        'prelink_verification_sha256': digest(prelink_verification_path),
        'trust_cache_sha256': inputs['trust_cache']['sha256'],
        'trust_cache_static_loaded': True,
        'root_image_sha256': inputs['images']['OS']['sha256'],
        'systemos_image_sha256': inputs['images']['SystemOS']['sha256'],
        'qemu_fdt_sha256': fdt_sha256,
        'owner_anchor_sha256': inputs['policy']['owner_anchor_sha256'],
        'owner_root_accepted': True,
        'verified_root_authenticated': True,
        'owner_root_uuid': binding['actual_uuid'],
        'owner_root_device': binding['_native_owner_root_mount_device'],
        'apple_rootauth_errno': binding['_native_owner_root_apple_autherr'],
        'apple_hardware_root_trust_demonstrated': False,
        'launchd_sha256': launchd['sha256'],
        'launchd_lc_main_offset': launchd['entry_offset'],
        'launchd_entry_pc': entry['pc'],
        'launchd_checkpoint_pc': entry['checkpoint_pc'],
        'launchd_entry_pstate': entry['pstate'],
        'launchd_first_instruction_completed': True,
        'launchd_exec_observed': True,
        'init_program_entry_observed': True,
        'data_volume_attached': False,
        'apple_userspace_boot_demonstrated': False,
        'functional_ipados_boot_demonstrated': False,
        'springboard_display_demonstrated': False,
        'hardware_boot_demonstrated': False,
        'device_flashable': False,
        'scope': 'QEMU-only original launchd LC_MAIN after authenticated owner-root; no Data volume, Apple hardware trust, or Dali qualification',
    }


def run_launchd_probe(unixnu, kernel, prelink_report, trust_cache_path,
                      root_image, systemos_image, efi, fdt, launchd, output, firmware=None):
    if output.exists():
        raise ValueError('QEMU launchd output directory must be new')
    output.mkdir(parents=True, exist_ok=False)
    probe = Path(unixnu).resolve() / 'scripts/probe-darwinboot-xnu.py'
    process_layout_objects = (Path(kernel).resolve().parent.parent /
                              'tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837')
    gate_wait_callsite = resolve_image4_gate_wait_callsite(kernel, prelink_report)
    gate_wait_pc = int(gate_wait_callsite['address'], 16)
    gate_wait_register_reads = [
        f'{gate_wait_pc:x}:x0:0:10',
        f'{gate_wait_pc:x}:x1:0:10',
    ]
    command = [sys.executable, '-I', '-B', str(probe),
               '--kernel', str(Path(kernel).resolve()), '--uefi-efi', str(Path(efi).resolve()),
               '--prelink-report', str(Path(prelink_report).resolve()),
               '--trust-cache', str(Path(trust_cache_path).resolve()),
               '--virtio-udif-image', str(Path(root_image).resolve()),
               '--virtio-extra-block-image', str(Path(systemos_image).resolve()),
               '--virtio-mmio-fdt', str(Path(fdt).resolve()),
               '--root-match-volume-uuid', OS_UUID, '--qemu-vmm-present', '--armvirt', '--armcpu',
               '--nvram-proxy', '--crypto-hash-method', 'sha2-384', '--amfi-platform-only',
               '--require-crypto14', '--follow-target-main', '--target-main-executable', str(Path(launchd).resolve()),
               '--inspect-owner-root', '--inspect-process-list', '--inspect-ui-threads',
               '--include-kernel-task-threads',
               '--process-layout-object-dir', str(process_layout_objects), '--check-commpage',
               '--trace-address', gate_wait_callsite['address'],
               '--trace-read-register-offset', gate_wait_register_reads[0],
               '--trace-read-register-offset', gate_wait_register_reads[1],
               '--break-symbol', '_panic',
               '--timeout', '180', '--qemu-memory-mib', '2048',
               '--read-kernel-symbol', f'_verified_root_key:{OWNER_KEY_BYTES}',
               '--read-kernel-symbol', '_trust_cache_static_loaded:1',
               '--read-kernel-symbol', '_vm_page_free_count:4',
               '--read-kernel-symbol', '_vm_page_free_target:4',
               '--read-kernel-symbol', '_vm_page_free_min:4',
               '--read-kernel-symbol', '_vm_page_free_wanted:4',
               '--read-kernel-symbol', '_vm_page_free_wanted_privileged:4',
               '--trace-symbol', '_vfs_mountroot', '--trace-symbol', '_native_owner_root_checkpoint',
               '--trace-symbol', '_native_owner_root_accepted_checkpoint',
               '--trace-symbol', '_load_static_trust_cache',
               '--log-syscalls-after', '_load_init_program',
               '--log-syscall-return-mib', IMG4_IGNITION_BLOB_MIB,
               '--kernel-base-args', '-v dali-earlyboot=1 kt_fixed_zones=120 serial=3 debug=0x14e',
               '--output', str(output)]
    if firmware is not None:
        command += ['--qemu-firmware', str(Path(firmware).resolve())]
    (output / 'probe-command.json').write_text(json.dumps(command, indent=2) + '\n')
    probe_env = os.environ.copy()
    probe_env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(command, env=probe_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=300)
    (output.parent / (output.name + '.log')).write_text(result.stdout)
    report_path = output / 'gdb-stop.json'
    report = None
    if report_path.is_file():
        report = json.loads(report_path.read_bytes())
        if report.get('syscall_events') and report.get('followed_main_entry'):
            launchd_input = validate_launchd_target(launchd)
            report['launchd_syscall_pc_mapping'] = map_launchd_syscall_pcs(report, launchd_input)
        trace_events = [event for event in report.get('trace_events', [])
                        if isinstance(event, dict) and event.get('pc') == gate_wait_callsite['address']]
        for event in trace_events:
            event['symbol'] = 'AppleImage4 gate-wait callsite'
        report['vm_page_counters'] = vm_page_counters_diagnostic(
            report.get('requested_kernel_symbols'))
        report['launchd_gate_wait_callsite'] = {
            **gate_wait_callsite, 'observed': bool(trace_events),
            'trace_event_count': len(trace_events), 'trace_events': trace_events,
            'trace_read_register_offsets': gate_wait_register_reads,
            'argument_memory_scope': ('optional bounded raw 16-byte reads at source-typed x0 lock and x1 gate '
                                      'arguments; unavailable reads are recorded and fields are not decoded'),
            'gate_holder_thread_snapshot': gate_holder_thread_diagnostic(
                report.get('ui_thread_snapshots'), trace_events, gate_wait_callsite,
                gate_wait_register_reads[1]),
        }
        report_path.write_text(json.dumps(report, indent=2) + '\n')
    if result.returncode:
        diagnostics = {}
        if report and report.get('launchd_syscall_pc_mapping'):
            diagnostics['launchd_syscall_pc_mapping'] = report['launchd_syscall_pc_mapping']
        if report and report.get('syscall_return_checkpoint'):
            diagnostics['syscall_return_checkpoint'] = report['syscall_return_checkpoint']
        if report and report.get('vm_page_counters'):
            diagnostics['vm_page_counters'] = report['vm_page_counters']
        if report and isinstance(report.get('process_list'), list):
            diagnostics['pid1_process'] = next(
                (entry for entry in report['process_list']
                 if isinstance(entry, dict) and entry.get('pid') == 1), None)
        if report and isinstance(report.get('ui_thread_snapshots'), dict):
            diagnostics['launchd_thread_snapshot'] = launchd_thread_snapshot_diagnostic(
                report['ui_thread_snapshots'])
        if report and report.get('launchd_gate_wait_callsite'):
            diagnostics['launchd_gate_wait_callsite'] = report['launchd_gate_wait_callsite']
        suffix = ('\nBounded launchd syscall diagnostics:\n' + json.dumps(diagnostics, indent=2)
                  if diagnostics else '')
        raise RuntimeError('QEMU launchd probe stopped before validated LC_MAIN: ' +
                           result.stdout[-8000:] + suffix)
    if report is None:
        raise RuntimeError('QEMU launchd probe produced no GDB report')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unixnu', type=Path, required=True)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--prelink-report', type=Path, required=True)
    parser.add_argument('--prelink-verification', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--trust-cache', type=Path, required=True)
    parser.add_argument('--root-image', type=Path, required=True)
    parser.add_argument('--systemos-image', type=Path, required=True)
    parser.add_argument('--userdata-report', type=Path, required=True)
    parser.add_argument('--root-policy-report', type=Path, required=True)
    parser.add_argument('--uefi-efi', type=Path, required=True)
    parser.add_argument('--virtio-fdt', type=Path,
                        help='Existing matching QEMU virt FDT; otherwise generate one with two read-only disks')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qemu-firmware', type=Path)
    parser.add_argument('--follow-launchd', action='store_true',
                        help='Run a separate QEMU pass to the exact original launchd LC_MAIN')
    parser.add_argument('--target-launchd', type=Path,
                        help='Unchanged 21H461 launchd executable for --follow-launchd')
    args = parser.parse_args()
    if args.follow_launchd != (args.target_launchd is not None):
        parser.error('--follow-launchd and --target-launchd must be supplied together')
    launchd = validate_launchd_target(args.target_launchd) if args.follow_launchd else None
    if args.output.exists():
        parser.error('QEMU root output must be a new directory')
    inputs = validate_inputs(args.kernel, args.prelink_report, args.prelink_verification,
                             args.config, args.trust_cache, args.root_image, args.systemos_image,
                             args.userdata_report, args.root_policy_report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dali-root-fdt-', dir=args.output.parent) as workdir:
        if args.virtio_fdt:
            fdt = args.virtio_fdt.resolve(strict=True)
            fdt_sha256 = digest(fdt)
        else:
            fdt = Path(workdir) / 'qemu-virtio.dtb'
            qemu = shutil.which('qemu-system-aarch64')
            if not qemu:
                parser.error('qemu-system-aarch64 is required to generate the matching Virtio FDT')
            fdt_sha256, _ = make_qemu_virtio_fdt(qemu, fdt, Path(workdir))
        try:
            device = attach_readonly_root_image(args.root_image)
            try:
                if args.follow_launchd:
                    report = run_launchd_probe(args.unixnu, args.kernel, args.prelink_report, args.trust_cache,
                                               args.root_image, args.systemos_image, args.uefi_efi, fdt,
                                               launchd['path'], args.output, args.qemu_firmware)
                else:
                    report = run_root_probe(args.unixnu, args.kernel, args.prelink_report, args.trust_cache,
                                            args.root_image, args.systemos_image, args.uefi_efi, fdt,
                                            args.output, args.qemu_firmware)
            finally:
                detach_readonly_root_image(device)
        finally:
            if args.output.is_dir():
                shutil.copyfile(fdt, args.output / 'qemu-virtio.dtb')
            verify_source_media(inputs)
            if launchd is not None:
                verify_launchd_target(launchd)
        if args.follow_launchd:
            summary = launchd_summary(report, inputs, launchd, fdt_sha256,
                                      args.prelink_report, args.prelink_verification)
            summary_path = args.output / 'launchd-summary.json'
        else:
            summary = root_summary(report, inputs, fdt_sha256)
            summary['prelink_report_sha256'] = digest(args.prelink_report)
            summary['prelink_verification_sha256'] = digest(args.prelink_verification)
            summary_path = args.output / 'root-checkpoint-summary.json'
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
        print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    main()
