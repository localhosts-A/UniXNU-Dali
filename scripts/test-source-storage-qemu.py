#!/usr/bin/env python3
"""Exercise the real source IOStorageFamily through volatile QEMU test nubs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def validate(report, expected_hash, arguments, mode):
    expected = (0x31534600 << 32) | (mode << 16) | (0x7f if mode == 1 else 0xf)
    if (report.get('kernel_sha256') != expected_hash or report.get('kernel_arguments') != arguments or
            report.get('bounded_timeout_interrupted') or not report.get('native_4k_mmu_observed') or
            not report.get('crypto14_initialization_observed') or
            report.get('stop_symbol') != '_source_storage_test_checkpoint' or
            not report.get('function_return_target') or
            report.get('pc') != report.get('function_return_target') or
            int(report.get('x0', '0'), 16) != expected):
        raise ValueError('Real IOStorageFamily runtime checkpoint failed or is not bound to this kernel/case')


def validate_msdosfs_mount(report, expected_hash, arguments):
    expected = (0x4d534446 << 32) | 0x3f
    if (report.get('kernel_sha256') != expected_hash or report.get('kernel_arguments') != arguments or
            report.get('bounded_timeout_interrupted') or not report.get('native_4k_mmu_observed') or
            not report.get('crypto14_initialization_observed') or
            report.get('stop_symbol') != '_source_msdosfs_test_checkpoint' or
            not report.get('function_return_target') or
            report.get('pc') != report.get('function_return_target') or
            int(report.get('x0', '0'), 16) != expected):
        raise ValueError('Production source msdosfs mount/read checkpoint failed or is not bound to this kernel/case')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unixnu', type=Path, required=True)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--uefi-efi', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--msdosfs-mount-test', action='store_true',
                        help='Also mount/read the RAM FAT32 medium through production XNU VFS/msdosfs')
    args = parser.parse_args()
    root, kernel, output = args.unixnu.resolve(), args.kernel.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    device = Path(__file__).resolve().parents[1]
    board = tomllib.loads((device / 'board.toml').read_text())
    base = [*board['payload']['kernel_args'], 'serial=3', 'debug=0x14e']
    sha = hashlib.sha256(kernel.read_bytes()).hexdigest()
    reports = {}
    for mode, name in ((1, 'rw4096'), (2, 'readonly512')):
        arguments = ' '.join([*base, f'source-storage-test={mode}'])
        destination = output / name
        command = [sys.executable, root / 'scripts/probe-darwinboot-xnu.py', '--kernel', kernel,
                   '--uefi-efi', args.uefi_efi.resolve(), '--timeout', '120', '--armvirt', '--armcpu',
                   '--nvram-proxy', '--kernel-base-args', arguments, '--require-crypto14',
                   '--test-service', 'source-storage:unixnu,source-storage-fixture',
                   '--finish-function', '--break-symbol', '_source_storage_test_checkpoint',
                   '--output', destination]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
        (output / (name + '.log')).write_text(result.stdout)
        if result.returncode:
            raise SystemExit(f'{name}: QEMU probe failed\n' + result.stdout[-6000:])
        report = json.loads((destination / 'gdb-stop.json').read_text())
        validate(report, sha, arguments, mode)
        reports[name] = {'kernel_sha256': sha, 'actual_checkpoint_return': report['x0'],
                         'logical_block_bytes': 4096 if mode == 1 else 512, 'pass': True}
    if args.msdosfs_mount_test:
        native_user = output / 'native-user'
        build = subprocess.run([sys.executable, root / 'scripts/build-native-user-fixture.py',
                                '--output', native_user], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=180)
        (output / 'native-user-build.log').write_text(build.stdout)
        if build.returncode:
            raise SystemExit('native-user QEMU root fixture build failed\n' + build.stdout[-6000:])
        arguments = ' '.join([*base, 'source-storage-test=2', 'source-msdosfs-test=1'])
        expected_arguments = 'rd=md0 ' + arguments
        destination = output / 'msdosfs-readonly'
        command = [sys.executable, root / 'scripts/probe-darwinboot-xnu.py', '--kernel', kernel,
                   '--uefi-efi', args.uefi_efi.resolve(), '--timeout', '120', '--armvirt', '--armcpu',
                   '--nvram-proxy', '--native-user', '--native-user-image', native_user / 'launchd.macho',
                   '--kernel-base-args', arguments, '--require-crypto14',
                   '--test-service', 'source-storage:unixnu,source-storage-fixture',
                   '--finish-function', '--break-symbol', '_source_msdosfs_test_checkpoint',
                   '--output', destination]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
        (output / 'msdosfs-readonly.log').write_text(result.stdout)
        if result.returncode:
            raise SystemExit('msdosfs-readonly: QEMU probe failed\n' + result.stdout[-6000:])
        report = json.loads((destination / 'gdb-stop.json').read_text())
        validate_msdosfs_mount(report, sha, expected_arguments)
        reports['msdosfs-readonly'] = {'kernel_sha256': sha, 'actual_checkpoint_return': report['x0'],
                                       'filesystem': 'msdos', 'mount_readonly': True,
                                       'volume_format': 'FAT32', 'media_source': 'volatile SourceStorageFixture RAM',
                                       'fat32_file': '/mnt/README.TXT',
                                       'native_user_image_sha256': report['native_user_image_sha256'],
                                       'pass': True}
    report = {'cases': reports, 'source_storage_runtime_pass': True, 'volatile_ram_only': True,
              'source_msdosfs_mount_read_runtime_pass': args.msdosfs_mount_test,
              'production_msdosfs_fat32_ram_runtime_pass': args.msdosfs_mount_test,
              'usb_host_storage_path_qualified': False,
              'physical_ufs_qualified': False, 'dma_qualified': False, 'persistence_qualified': False}
    (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: source IOStorageFamily and volatile RAM checks passed in QEMU')


if __name__ == '__main__':
    main()
