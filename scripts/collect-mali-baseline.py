#!/usr/bin/env python3
"""Read existing Android Mali metadata/FDT/firmware to a private host directory.

Only fixed getprop/uname/cat commands are sent to ADB. This does not open
/dev/mali0, create contexts, submit work, install software, or change settings.
The output includes device-specific data and must remain outside Git.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import struct
import subprocess


def command(serial, args, limit=1024 * 1024):
    result = subprocess.run(['adb', '-s', serial, *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if len(result.stdout) > limit:
        raise ValueError('Read exceeded its expected size bound')
    return result.stdout


def collect(serial, destination):
    destination = destination.resolve()
    existing = destination
    while not existing.exists():
        existing = existing.parent
    git = subprocess.run(['git', '-C', str(existing), 'rev-parse', '--show-toplevel'],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if git.returncode == 0:
        raise ValueError('Private evidence must be outside every Git checkout')
    if destination.exists():
        raise ValueError('Evidence directory already exists; preserve it and use a fresh directory')
    if command(serial, ['get-serialno']).decode().strip() != serial:
        raise ValueError('ADB serial identity differs')
    identity = {}
    for name in ('ro.product.model', 'ro.soc.model', 'ro.board.platform',
                 'ro.build.version.release', 'ro.build.version.sdk', 'ro.build.fingerprint'):
        identity[name] = command(serial, ['shell', 'getprop', name], 4096).decode().strip()
    if identity['ro.soc.model'] != 'MT6991' or identity['ro.product.model'] != '25060RK16C':
        raise ValueError('This collector is pinned to the Dali MT6991 device')
    destination.mkdir(parents=True, mode=0o700)
    files = {}
    reads = {
        'gpuinfo.txt': '/sys/class/misc/mali0/device/gpuinfo',
        'modules.txt': '/proc/modules',
        'runtime.dtb': '/sys/firmware/fdt',
        'mali_csffw.bin': '/vendor/firmware/mali_csffw.bin',
    }
    try:
        for name, source in reads.items():
            data = command(serial, ['exec-out', 'su', '-c', 'cat ' + source])
            if not data:
                raise ValueError(f'Empty read: {source}')
            if name == 'runtime.dtb':
                if len(data) < 40 or data[:4] != b'\xd0\x0d\xfe\xed':
                    raise ValueError('Runtime FDT header differs')
                if struct.unpack_from('>I', data, 4)[0] != len(data):
                    raise ValueError('Runtime FDT total size differs')
            if name == 'gpuinfo.txt' and b'Mali-G925-Immortalis 12 cores' not in data:
                raise ValueError('GPU identity differs')
            (destination / name).write_bytes(data)
            files[name] = {'source': source, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        kernel = command(serial, ['shell', 'uname', '-a'], 4096).decode().strip()
        report = {'schema': 1, 'serial': serial, 'collected_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'identity': identity, 'kernel': kernel, 'files': files,
                  'device_reads_only': True, 'gpu_context_opened': False, 'gpu_exercised': False}
        (destination / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
        return report
    except Exception as error:
        (destination / 'failure.json').write_text(json.dumps({'files': files, 'error': str(error)}) + '\n')
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.serial, args.output)
    print(f"Captured {len(report['files'])} existing files to private host evidence; GPU not exercised")


if __name__ == '__main__':
    main()
