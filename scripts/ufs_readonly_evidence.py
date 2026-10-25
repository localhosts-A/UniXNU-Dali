#!/usr/bin/env python3
"""Offline evidence checks for the explicit physical UFS read-only diagnostic.

No device I/O or write authorization lives here. Successful scoring establishes
only the named baseline LUN's reads and one device sync command per observed
root, followed by this diagnostic's stop. XNU handoff and durability are separate.
The caller must bind the real connected device and sealed candidate to this
fresh token; firmware console text alone cannot identify a cloned device.
"""
import hashlib
import json
from pathlib import Path
import re
import uuid

import ufs_geometry as geometry

TOKEN = re.compile(r'[0-9a-f]{32}')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_baseline(directory, *, serial, original_boot_b):
    """Recheck raw captures, GPT CRCs and original-image/device binding."""
    directory = Path(directory).resolve()
    baseline_bytes = (directory / 'baseline.json').read_bytes()
    report = json.loads(baseline_bytes)
    original = Path(original_boot_b).read_bytes()
    if (not isinstance(report, dict) or report.get('schema') != 1 or report.get('kind') != 'dali-ufs-android-read-only-baseline' or
            report.get('serial') != serial or report.get('original_boot_b_sha256') != sha256(original) or
            report.get('all_partition_hashes_match') is not True or
            report.get('known_block_matches_backup') is not True or
            report.get('device_writes_performed') is not False or
            report.get('device_reboot_performed') is not False):
        raise ValueError('baseline identity/original/read-only evidence mismatch')
    raw = {}
    for name in ('primary_header', 'primary_entries', 'backup_header', 'backup_entries',
                 'boot_b_first_block', 'live_fdt'):
        record = report['captures'][name]
        path = (directory / record['file']).resolve()
        if directory not in path.parents or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('baseline capture escaped its bounded directory')
        with path.open('rb') as source:
            data = source.read(4 * 1024 * 1024 + 1)
        if len(data) != record['bytes'] or sha256(data) != record['sha256']:
            raise ValueError('baseline raw capture hash/length mismatch: ' + name)
        raw[name] = data
    geo = report['geometry']
    block = geometry.block_size(geo['logical_block_bytes'])
    capacity = geometry.natural(geo['capacity_bytes'], 'baseline capacity', positive=True)
    lun = geometry.natural(geo['lun'], 'baseline LUN')
    count = geometry.natural(geo['capacity_blocks'], 'baseline block count', positive=True)
    if lun >= 8 or geo.get('geometry_verified') is not True or count * block != capacity:
        raise ValueError('unsupported/unverified baseline geometry')
    gpt = geometry.validate_gpt(*(raw[name] for name in
        ('primary_header', 'primary_entries', 'backup_header', 'backup_entries')), block, capacity)
    if gpt != report['gpt']:
        raise ValueError('baseline GPT metadata differs from raw captures')
    parts = [part for part in gpt['partitions'] if part['name'] == 'boot_b']
    if len(parts) != 1:
        raise ValueError('baseline must identify exactly one boot_b')
    part = parts[0]
    if (geo['boot_b'] != dict(part, bytes=part['blocks'] * block) or
            len(original) != part['blocks'] * block or raw['boot_b_first_block'] != original[:block]):
        raise ValueError('baseline boot_b range/sample differs from original image')
    return {'lun': lun, 'block_size': block, 'last_lba': capacity // block - 1,
            'boot_b_lba': part['first_lba'], 'boot_b_blocks': part['blocks'],
            'partitions': {p['number']: p for p in gpt['partitions']},
            'header_hashes': {1: sha256(raw['primary_header']), capacity // block - 1: sha256(raw['backup_header'])},
            'baseline_sha256': sha256(baseline_bytes),
            'original_boot_b_sha256': sha256(original), 'serial': serial}


def score_readonly(text, token, baseline, candidate):
    """Require one ordered current-token trace; never promote it to write PASS."""
    result = {'run_token': token, 'uefi_read_only_pass': False, 'uefi_sync_command_pass': False,
              'diagnostic_stop_pass': False, 'observation_complete': False,
              'device_identity_verified_by_scorer': False, 'physical_ufs_qualified': False,
              'xnu_ufs_qualified': False, 'media_writes_qualified': False,
              'persistence_qualified': False, 'firmware_xnu_handoff_qualified': False}
    try:
        if not isinstance(token, str) or not TOKEN.fullmatch(token) or len(text) > 16 * 1024 * 1024:
            raise ValueError('invalid token or oversized console')
        candidate = bytes(candidate)
        if not baseline['block_size'] <= len(candidate) <= baseline['boot_b_blocks'] * baseline['block_size']:
            raise ValueError('candidate size differs from baseline boot_b bounds')
        ro_prefix, host_prefix = 'DALI-UFS-RO: run=' + token + ' ', 'DALI-UFS-HOST: run=' + token + ' '
        lines = [(line[len(ro_prefix):] if line.startswith(ro_prefix) else '@' + line[len(host_prefix):])
                 for line in text.splitlines() if line.startswith((ro_prefix, host_prefix))]
        position = 0

        def take(pattern):
            nonlocal position
            if position >= len(lines):
                raise ValueError('truncated diagnostic trace')
            match = re.fullmatch(pattern, lines[position])
            if not match:
                raise ValueError('unexpected/out-of-order diagnostic record: ' + lines[position][:160])
            position += 1
            return match

        counters = {}

        def sync_completion():
            m = take(r'@SYNC_COMPLETE lun=([0-7]) tag=0 opcode=(35|91) count=(\d+)')
            lun, count = int(m[1]), int(m[3])
            geometry.natural(count, 'SYNC count', positive=True)
            if count != counters.get(lun, 0) + 1:
                raise ValueError('device SYNC count is not the next validated completion')
            counters[lun] = count
            return lun

        take(r'@link=7 stage=7 fault=0+/0+')
        # Driver connection may synchronize media before publishing BlockIo.
        # Retain that history; the diagnostic's own sync must advance it once.
        while position < len(lines) and lines[position].startswith('@SYNC_COMPLETE '):
            sync_completion()
        prior_syncs = dict(counters)
        take(r'@connect=Success')
        take(r'BEGIN version=1')
        roots, partitions, samples, syncs = {}, {}, {}, []
        while position < len(lines) and lines[position].startswith('MEDIA '):
            m = take(r'MEDIA lun=([0-7]) partition=([01]) readonly=1 block_size=(\d+) last_lba=(\d+)')
            lun, partition, block, last = map(int, m.groups())
            geometry.block_size(block)
            geometry.natural(last, 'media last LBA')
            if last == geometry.UINT64_MAX:
                raise ValueError('media capacity overflows block count')
            if partition:
                p = take(r'PARTITION lun=([0-7]) number=(\d+) start_lba=(\d+) blocks=(\d+) signature_le=([0-9a-f]{32})')
                plun, number, start, count = map(int, p.groups()[:4])
                if plun != lun or not number or not count or count != last + 1 or (lun, number) in partitions:
                    raise ValueError('duplicate/inconsistent partition media/path')
                geometry.natural(start, 'partition start')
                geometry.natural(count, 'partition blocks', positive=True)
                partitions[lun, number] = {'first_lba': start, 'blocks': count, 'block_size': block,
                                         'guid': str(uuid.UUID(bytes_le=bytes.fromhex(p[5])))}
                continue
            if lun in roots or last < 1:
                raise ValueError('duplicate or invalid root media')
            roots[lun] = {'block_size': block, 'last_lba': last}
            expected_lbas = [0, 1, last]
            if lun == baseline['lun']:
                if roots[lun] != {k: baseline[k] for k in ('block_size', 'last_lba')}:
                    raise ValueError('baseline LUN geometry differs')
                expected_lbas.append(baseline['boot_b_lba'])
            for lba in expected_lbas:
                s = take(r'SAMPLE lun=([0-7]) lba=(\d+) bytes=(\d+) sha256=([0-9a-f]{64})')
                if tuple(map(int, s.groups()[:3])) != (lun, lba, block) or (lun, lba) in samples:
                    raise ValueError('sample location/size/uniqueness mismatch')
                samples[lun, lba] = s[4]
                if lun == baseline['lun']:
                    expected = (sha256(candidate[:block]) if lba == baseline['boot_b_lba'] else
                                baseline['header_hashes'].get(lba))
                    if expected is not None and s[4] != expected:
                        raise ValueError('known-block SHA-256 differs from baseline/current candidate')
            if sync_completion() != lun:
                raise ValueError('device SYNC completed on another LUN')
            s = take(r'SYNC lun=([0-7]) status=0+')
            if int(s[1]) != lun:
                raise ValueError('BlockIo sync LUN differs')
            syncs.append(lun)
        end = take(r'END status=0+ roots=(\d+) partitions=(\d+) samples=(\d+)')
        result['observation_complete'] = True
        if tuple(map(int, end.groups())) != (len(roots), len(partitions), len(samples)):
            raise ValueError('END counts differ from actual diagnostic records')
        take(r'@quiesce=Success hce=0+')
        if position != len(lines) or baseline['lun'] not in roots:
            raise ValueError('duplicate/trailing trace or missing baseline root')
        expected_parts = baseline['partitions']
        observed_parts = {number: p for (lun, number), p in partitions.items() if lun == baseline['lun']}
        if set(observed_parts) != set(expected_parts):
            raise ValueError('baseline LUN partition set differs from GPT')
        for (lun, number), part in partitions.items():
            root = roots.get(lun)
            if (root is None or root['block_size'] != part['block_size'] or
                    part['first_lba'] > root['last_lba'] or
                    part['blocks'] > root['last_lba'] + 1 - part['first_lba']):
                raise ValueError('partition geometry exceeds its actual root')
            if lun == baseline['lun'] and any(part[k] != expected_parts[number][k] for k in ('first_lba', 'blocks', 'guid')):
                raise ValueError('partition path differs from baseline GPT')
        return dict(result, uefi_read_only_pass=True, uefi_sync_command_pass=True, diagnostic_stop_pass=True,
                    verified_content_luns=[baseline['lun']], observed_roots=roots,
                    observed_partitions=len(partitions), samples=len(samples), synchronized_luns=syncs,
                    pre_diagnostic_syncs=prior_syncs,
                    candidate_sha256=sha256(candidate), baseline_sha256=baseline['baseline_sha256'], records=lines)
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        return dict(result, failure=str(error))
