#!/usr/bin/env python3
"""Offline checks of the Dali source-XNU read-only transcript.

The caller must separately bind the actual kernel/ADT/FD, connected device and
fresh token. This parser grants no write permission and performs no device I/O.
It does not infer firmware-provider handoff or durability from XNU log text.
"""
import re
import uuid

from ufs_readonly_evidence import TOKEN, sha256
import ufs_geometry as geometry


def score_xnu_readonly(text, token, baseline, candidate, firmware_luns=None):
    result = dict(run_token=token, xnu_read_only_pass=False, xnu_sync_report_pass=False,
                  xnu_stop_report_pass=False, device_identity_verified_by_scorer=False,
                  physical_ufs_qualified=False, firmware_xnu_handoff_qualified=False,
                  media_writes_qualified=False, persistence_qualified=False)
    try:
        if not isinstance(text, str) or len(text) > 16 * 1024 * 1024 or not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError('invalid token or oversized console')
        candidate = bytes(candidate)
        block = baseline['block_size']
        if not block <= len(candidate) <= baseline['boot_b_blocks'] * block:
            raise ValueError('candidate outside baseline boot_b bounds')
        raw = text.splitlines()
        prefix = 'DALI-XNU-UFS: run=' + token + ' '
        rows = []
        for index, line in enumerate(raw):
            if not line.startswith(prefix):
                continue
            match = re.fullmatch(r'stage=([a-zA-Z0-9_-]+) a=([0-9a-f]{1,16}) b=([0-9a-f]{1,16}) c=([0-9a-f]{1,16})', line[len(prefix):])
            if not match:
                raise ValueError('malformed current-token record')
            rows.append((match[1], *(int(v, 16) for v in match.groups()[1:]), index))
        position = 0

        def take(stage=None):
            nonlocal position
            if position == len(rows):
                raise ValueError('truncated XNU diagnostic')
            row = rows[position]
            if stage is not None and row[0] != stage:
                raise ValueError('unexpected/out-of-order XNU record: ' + row[0])
            position += 1
            return row

        def exact(stage, values):
            row = take(stage)
            if row[1:4] != values:
                raise ValueError('failed XNU stage: ' + stage)
            return row

        def digest():
            row = take()
            if not re.fullmatch('[0-9a-f]{64}', row[0]) or row[1:4] != (0, 0, 0):
                raise ValueError('invalid sample digest')
            return row[0]

        def text_record(kind):
            value = bytearray()
            length = None
            while length is None or len(value) < length:
                row = take('text')
                if row[1] != kind or row[2] != len(value) or not 0 < row[3] <= 128 or (length is not None and row[3] != length):
                    raise ValueError('inconsistent bounded metadata framing')
                length = row[3]
                count = min(32, length - len(value))
                chunk = take()
                if not re.fullmatch('[0-9a-f]{' + str(count * 2) + '}', chunk[0]) or chunk[1:4] != (0, 0, 0):
                    raise ValueError('invalid metadata chunk')
                value.extend(bytes.fromhex(chunk[0]))
            if b'\0' in value:
                raise ValueError('embedded metadata terminator')
            return value.decode('utf-8')

        def sample(lun, lba, base, size, bs, kind, expected=None):
            exact('sample', (lun, lba, bs))
            exact(kind, (base, size, bs))
            observed = digest()
            if expected is not None and observed != expected:
                raise ValueError('known block differs from Android/current candidate')

        start_record = exact('start', (0, 0, 0))
        sha = exact('sha-pass', (0, 0, 0))
        checkpoints = [(i, line) for i, line in enumerate(raw) if line.startswith('DALI-XNU: ') and ' run=' + token + ' ' in line]
        checkpoint = (r'DALI-XNU: darwin=[0-9.]+ build=[0-9.]+ run=' + token +
                      r' bootarg=1 registered=1 platform=1 gic=1 timer=([1-9][0-9]*) mpidr=0x([0-9a-f]{1,16}) checkpoint=PASS')
        platform = re.fullmatch(checkpoint, checkpoints[0][1]) if len(checkpoints) == 1 else None
        if (not platform or checkpoints[0][0] >= start_record[4] or checkpoints[0][0] >= sha[4] or
                int(platform[2], 16) & 0xff00ffffff or int(platform[1]) > geometry.UINT64_MAX):
            raise ValueError('missing successful platform checkpoint before hardware')
        exact('controller', (7, 7, 0))
        dma = take('dma')
        if not dma[1] or dma[1] & 4095 or not 0x22000 <= dma[2] <= 0xffffffff or dma[2] & 4095 or dma[1] + dma[2] > 0x100000000 or dma[3] not in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
            raise ValueError('invalid full DMA span/cache line')
        roots = {}
        while position < len(rows) and rows[position][0] == 'geometry':
            row = take('geometry'); lun, count, bs = row[1:4]
            geometry.block_size(bs)
            if lun >= 8 or lun in roots or count < 2 or count > geometry.UINT64_MAX // bs or (roots and lun <= max(roots)):
                raise ValueError('invalid/duplicate LUN geometry')
            roots[lun] = (count, bs)
        if baseline['lun'] not in roots or roots[baseline['lun']] != (baseline['last_lba'] + 1, block):
            raise ValueError('baseline LUN geometry differs')
        exact('initialized', (1, 0, 0))
        partitions, samples, previous_sync = {}, 0, 0
        boot_seen = False
        for lun, (count, bs) in roots.items():
            size = count * bs
            exact('media', (lun, size, bs))
            for lba in (0, 1, count - 1):
                expected = baseline['header_hashes'].get(lba) if lun == baseline['lun'] else None
                sample(lun, lba, 0, size, bs, 'whole', expected); samples += 1
            while position < len(rows) and rows[position][0] == 'partition':
                row = take('partition'); plun, start, length = row[1:4]
                if plun != lun or not length or start % bs or length % bs or start >= size or length > size - start:
                    raise ValueError('partition exceeds its actual root')
                name, guid = text_record(1), str(uuid.UUID(text_record(2)))
                if (lun, guid) in partitions:
                    raise ValueError('duplicate partition identity')
                partitions[lun, guid] = (name, start // bs, length // bs)
                if name == 'boot_b':
                    if boot_seen or lun != baseline['lun'] or (start // bs, length // bs) != (baseline['boot_b_lba'], baseline['boot_b_blocks']):
                        raise ValueError('ambiguous/wrong boot_b partition')
                    sample(lun, 0, start, length, bs, 'boot_b', sha256(candidate[:bs]))
                    samples += 1; boot_seen = True
            row = take('sync')
            if row[1] != lun or row[2] != 0 or row[3] <= previous_sync:
                raise ValueError('failed or non-advancing device sync report')
            previous_sync = row[3]
        expected = {p['guid']: (p['name'], p['first_lba'], p['blocks']) for p in baseline['partitions'].values()}
        actual = {guid: value for (lun, guid), value in partitions.items() if lun == baseline['lun']}
        if actual != expected or not boot_seen:
            raise ValueError('baseline GPT partition identities/names/geometry differ')
        end = take('diagnostics-end')
        if end[1] != 1 or end[2] < samples or end[3] != previous_sync:
            raise ValueError('failed/inconsistent final diagnostic counters')
        stopped_sync = None
        while position < len(rows):
            row = take('quiesce-sync')
            if row[1] != 0 or row[3] != 0 or row[2] < previous_sync + len(roots) or (stopped_sync is not None and row[2] != stopped_sync):
                raise ValueError('failed/missing/replayed shutdown sync')
            stopped_sync = row[2]
            exact('quiesce', (8, 1, 0))
        if stopped_sync is None:
            raise ValueError('missing shutdown evidence')
        handoff = [(i, line) for i, line in enumerate(raw)
                   if line.startswith('DALI-UFS-HANDOFF: run=' + token + ' ')]
        firmware_stops = [(i, line) for i, line in enumerate(raw)
                          if line.startswith('DALI-UFS-HOST: run=' + token + ' quiesce=')]
        handoff_luns = set(roots) if firmware_luns is None else set(firmware_luns)
        if (not handoff_luns or any(type(lun) is not int or lun < 0 or lun >= 8 for lun in handoff_luns)):
            raise ValueError('invalid firmware-discovered LUN set for ownership handoff')
        expected_mask = sum(1 << lun for lun in handoff_luns)
        if len(handoff) != 1 or len(firmware_stops) != 1:
            raise ValueError('missing/duplicate firmware storage quiesce evidence')
        handoff_match = re.fullmatch(
            r'DALI-UFS-HANDOFF: run=' + token +
            r' closed=1 drained-synced=1 mask=([0-9a-f]+) commands=(\d+) result=Success', handoff[0][1])
        firmware_stop_match = re.fullmatch(
            r'DALI-UFS-HOST: run=' + token + r' quiesce=Success hce=0', firmware_stops[0][1])
        if (not handoff_match or not firmware_stop_match or handoff[0][0] >= firmware_stops[0][0] or
                firmware_stops[0][0] >= checkpoints[0][0] or int(handoff_match[1], 16) != expected_mask or
                int(handoff_match[2]) != len(handoff_luns)):
            raise ValueError('firmware did not sync/stop the exact discovered LUN set before XNU')
        return dict(result, xnu_read_only_pass=True, xnu_sync_report_pass=True, xnu_stop_report_pass=True,
                    firmware_xnu_handoff_qualified=True,
                    verified_content_luns=[baseline['lun']], observed_roots=roots,
                    observed_partitions=len(partitions), samples=samples, shutdown_sync_count=stopped_sync,
                    candidate_sha256=sha256(candidate), baseline_sha256=baseline['baseline_sha256'])
    except (ValueError, KeyError, TypeError, OverflowError, UnicodeError) as error:
        return dict(result, failure=str(error))
