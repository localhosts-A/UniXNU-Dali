#!/usr/bin/env python3
"""Build a hash-pinned 21H461 APFS Data seed candidate for the Dali payload."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib


DEVICE = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
MAX_DATA_LOGICAL_BYTES = 32 * MIB


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(8 * MIB):
            digest.update(chunk)
    return digest.hexdigest()


def validate_os_dmg(path, expected_sha256):
    if not path.is_file():
        raise SystemExit(f'Missing pinned iPadOS OS DMG: {path}')
    actual = sha256(path)
    if actual != expected_sha256:
        raise SystemExit(f'OS DMG SHA-256 mismatch: expected {expected_sha256}, got {actual}')
    return actual


def inspect_packed_seed(path, packed_ramdisk, expected_logical_bytes, max_encoded_bytes):
    metadata, _ = packed_ramdisk.inspect(path)
    if not metadata.get('packed') or metadata.get('bytes') != expected_logical_bytes:
        raise ValueError('Data seed must be the expected packed DBRDISK1 APFS image')
    if path.stat().st_size > max_encoded_bytes:
        raise ValueError(f'packed Data seed exceeds the payload budget ({path.stat().st_size} > {max_encoded_bytes})')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os-root', required=True, type=Path, help='read-only mount of the pinned 21H461 OS volume')
    parser.add_argument('--os-dmg', required=True, type=Path, help='source OS DMG whose hash is pinned in board.toml')
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--unixnu', type=Path, default=DEVICE.parent / 'UniXNU')
    args = parser.parse_args()
    unixnu = args.unixnu.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise SystemExit(f'Refuse to overwrite Data seed output: {output}')
    board = tomllib.loads((DEVICE / 'board.toml').read_text())
    ipsw = board['ipsw']
    if (ipsw.get('product'), ipsw.get('version'), ipsw.get('build')) != ('iPad7,1', '17.7.11', '21H461'):
        raise SystemExit('Dali board no longer selects the pinned iPad7,1 / 21H461 donor')
    os_name = '044-76040-038.dmg'
    os_digest = validate_os_dmg(args.os_dmg.resolve(), ipsw['sha256'][os_name])
    prepare = unixnu / 'scripts/prepare-owner-data-fixture.py'
    skip_setup = unixnu / 'scripts/seed-data-skip-setup.py'
    ramdisk_module = unixnu / 'drivers/DarwinBoot/Test'
    for path in (prepare, skip_setup, ramdisk_module / 'packed_ramdisk.py'):
        if not path.is_file():
            raise SystemExit(f'Missing pinned UniXNU Data seed input: {path}')
    sys.path.insert(0, str(ramdisk_module))
    import packed_ramdisk

    output.mkdir(parents=True)
    fixture = output / 'base'
    subprocess.run([sys.executable, prepare, '--os-root', args.os_root.resolve(), '--output', fixture], check=True)
    raw = fixture / 'owner-data-32.raw'
    fixture_report_path = fixture / 'seed.json'
    fixture_report = json.loads(fixture_report_path.read_text())
    logical_bytes = fixture_report.get('bytes')
    if (not isinstance(logical_bytes, int) or isinstance(logical_bytes, bool) or logical_bytes <= 0 or
            logical_bytes > MAX_DATA_LOGICAL_BYTES or logical_bytes % 4096 or raw.stat().st_size != logical_bytes or
            fixture_report.get('production_storage_qualified') is not False or
            fixture_report.get('numeric_owner_mode_xattr_copy_verified') is not True or
            fixture_report.get('acl_copy_requested') is not True):
        raise SystemExit('Base APFS fixture is not the expected metadata-verified 21H461 candidate')

    base = output / 'data-seed-base.dbr'
    final = output / 'data-seed-skip-setup.dbr'
    base_metadata = packed_ramdisk.pack(raw, base)
    max_bytes = int(board['payload']['size_mib'] * MIB * 0.95)
    inspect_packed_seed(base, packed_ramdisk, logical_bytes, max_bytes)
    subprocess.run([sys.executable, skip_setup, base, final, '--no-auto-dim'], check=True)
    setup_report = json.loads(final.with_suffix('.json').read_text())
    final_metadata = inspect_packed_seed(final, packed_ramdisk, logical_bytes, max_bytes)
    if setup_report.get('build') != '21H461' or setup_report.get('no_auto_dim') is not True:
        raise SystemExit('Skip-setup Data seed is not bound to 21H461 and no-auto-dim')

    report = {
        'target': 'iPad7,1 / iPadOS 17.7.11 / 21H461',
        'physical_device': 'REDMI K80 Ultra / Dali / MT6991',
        'os_dmg': {'name': os_name, 'sha256': os_digest},
        'os_root_launchd_sha256': fixture_report['os_root_launchd_sha256'],
        'os_root_dyld_sha256': fixture_report['os_root_dyld_sha256'],
        'volume_uuid': fixture_report['volume_uuid'],
        'raw_bytes': logical_bytes,
        'raw_sha256': fixture_report['sha256'],
        'raw_report': 'base/seed.json',
        'base_packed': base_metadata,
        'skip_setup_packed': final_metadata,
        'skip_setup_report': final.with_suffix('.json').name,
        'no_auto_dim': True,
        'payload_size_mib': board['payload']['size_mib'],
        'payload_seed_limit_bytes': max_bytes,
        'owner_keybag_created': False,
        'initial_seed_signature_verified': False,
        'production_storage_qualified': False,
        'payload_fit_verified': False,
        'device_tested': False,
    }
    (output / 'seed-candidate.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
