"""Corrupt, mixed or no-op evidence cannot qualify the physical read-only path."""
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ufs_readonly_evidence as evidence
from test_ufs_geometry import fixture, fix_tables

TOKEN = '1234567890abcdef' * 2
CANDIDATE = b'C' * 8192


def save_fixture(folder, block=4096):
    folder.mkdir()
    raw = fixture(block)
    for table in (raw[1], raw[3]):
        struct.pack_into('<Q', table, 70 * 128 + 40, 527)
    fix_tables(raw)
    gpt = evidence.geometry.validate_gpt(*raw, block, 262144 * block)
    geo = evidence.geometry.geometry(lun=2, logical_bytes=block, capacity_bytes=262144 * block,
                                    start_sectors_512=512 * block // 512, size_sectors_512=16 * block // 512,
                                    partition_number=71, sysfs_read_only=False, gpt=gpt)
    original = b'O' * (16 * block)
    (folder / 'original.img').write_bytes(original)
    captures = {}
    for name, data in zip(('primary_header', 'primary_entries', 'backup_header', 'backup_entries',
                           'boot_b_first_block', 'live_fdt'), [*raw, original[:block], b'fdt']):
        (folder / (name + '.bin')).write_bytes(data)
        captures[name] = {'file': name + '.bin', 'bytes': len(data), 'sha256': evidence.sha256(data)}
    report = {'schema': 1, 'kind': 'dali-ufs-android-read-only-baseline', 'serial': 'test-device',
              'original_boot_b_sha256': evidence.sha256(original), 'all_partition_hashes_match': True,
              'known_block_matches_backup': True, 'device_writes_performed': False,
              'device_reboot_performed': False, 'geometry': geo, 'gpt': gpt, 'captures': captures}
    (folder / 'baseline.json').write_text(json.dumps(report))
    return report


def trace(baseline, token=TOKEN):
    ro, host = 'DALI-UFS-RO: run=' + token + ' ', 'DALI-UFS-HOST: run=' + token + ' '
    block, last = baseline['block_size'], baseline['last_lba']
    part = baseline['partitions'][71]
    lines = [host + 'link=7 stage=7 fault=0/0', host + 'connect=Success', ro + 'BEGIN version=1',
             ro + f'MEDIA lun=2 partition=0 readonly=1 block_size={block} last_lba={last}']
    for lba in (0, 1, last, baseline['boot_b_lba']):
        digest = (evidence.sha256(CANDIDATE[:block]) if lba == baseline['boot_b_lba'] else
                  baseline['header_hashes'].get(lba, '0' * 64))
        lines.append(ro + f'SAMPLE lun=2 lba={lba} bytes={block} sha256={digest}')
    lines += [host + 'SYNC_COMPLETE lun=2 tag=0 opcode=35 count=1', ro + 'SYNC lun=2 status=0',
              ro + f'MEDIA lun=2 partition=1 readonly=1 block_size={block} last_lba=15',
              ro + 'PARTITION lun=2 number=71 start_lba=512 blocks=16 signature_le=' + uuid.UUID(part['guid']).bytes_le.hex(),
              ro + 'MEDIA lun=0 partition=0 readonly=1 block_size=512 last_lba=7']
    lines += [ro + f'SAMPLE lun=0 lba={lba} bytes=512 sha256=' + 'a' * 64 for lba in (0, 1, 7)]
    lines += [host + 'SYNC_COMPLETE lun=0 tag=0 opcode=35 count=1', ro + 'SYNC lun=0 status=0',
              ro + 'END status=0 roots=2 partitions=1 samples=7', host + 'quiesce=Success hce=0']
    return '\n'.join(lines)


class ReadOnlyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / 'baseline'
        self.report = save_fixture(self.folder)
        self.baseline = self.load()

    def tearDown(self):
        self.temp.cleanup()

    def load(self, **changes):
        args = {'serial': 'test-device', 'original_boot_b': self.folder / 'original.img'}
        args.update(changes)
        return evidence.load_baseline(self.folder, **args)

    def score(self, text=None, candidate=CANDIDATE):
        return evidence.score_readonly(trace(self.baseline) if text is None else text,
                                       TOKEN, self.baseline, candidate)

    def test_raw_crc_checked_baseline_and_ordered_trace_qualify_only_named_read_path(self):
        out = self.score()
        self.assertTrue(out['uefi_read_only_pass'], out)
        self.assertTrue(out['uefi_sync_command_pass'])
        self.assertTrue(out['diagnostic_stop_pass'])
        self.assertEqual(out['verified_content_luns'], [2])
        self.assertEqual(out['observed_roots'][0]['block_size'], 512)
        for key in ('media_writes_qualified', 'persistence_qualified', 'xnu_ufs_qualified',
                    'firmware_xnu_handoff_qualified', 'device_identity_verified_by_scorer', 'physical_ufs_qualified'):
            self.assertFalse(out[key])

    def test_actual_block_size_is_used_without_lun_zero_or_four_kib_assumption(self):
        folder = Path(self.temp.name) / 'baseline512'
        save_fixture(folder, 512)
        baseline = evidence.load_baseline(folder, serial='test-device', original_boot_b=folder / 'original.img')
        self.assertTrue(evidence.score_readonly(trace(baseline), TOKEN, baseline, CANDIDATE)['uefi_read_only_pass'])

    def test_boot_sample_compares_current_candidate_instead_of_original_android(self):
        old = self.report['captures']['boot_b_first_block']['sha256']
        current = evidence.sha256(CANDIDATE[:4096])
        self.assertNotEqual(old, current)
        self.assertFalse(self.score(trace(self.baseline).replace(current, old))['uefi_read_only_pass'])
        self.assertFalse(self.score(candidate=b'D' * 8192)['uefi_read_only_pass'])

    def test_gpt_hash_geometry_partition_guid_and_readonly_must_match(self):
        log = trace(self.baseline)
        for bad in (log.replace(self.baseline['header_hashes'][1], 'f' * 64),
                    log.replace('readonly=1', 'readonly=0', 1), log.replace('last_lba=262143', 'last_lba=262144', 1),
                    log.replace('start_lba=512', 'start_lba=513'), log.replace('number=71', 'number=70'),
                    log.replace(uuid.UUID(self.baseline['partitions'][71]['guid']).bytes_le.hex(), '0' * 32),
                    log.replace('bytes=4096', 'bytes=512', 1)):
            with self.subTest(bad=bad[-80:]):
                self.assertFalse(self.score(bad)['uefi_read_only_pass'])

    def test_device_sync_counter_and_clean_hce_stop_are_required(self):
        log = trace(self.baseline)
        for bad in (log.replace('count=1', 'count=0', 1), log.replace('count=1', 'count=2', 1),
                    log.replace('opcode=35', 'opcode=ff', 1), log.replace('SYNC lun=2 status=0', 'SYNC lun=2 status=8000000000000012'),
                    '\n'.join(l for l in log.splitlines() if 'SYNC_COMPLETE lun=2' not in l),
                    log.replace('quiesce=Success', 'quiesce=Device Error'), log.replace('hce=0', 'hce=1')):
            self.assertFalse(self.score(bad)['uefi_read_only_pass'])

    def test_cache16_and_prior_connection_syncs_use_relative_completion_count(self):
        log = trace(self.baseline).replace('opcode=35', 'opcode=91', 1)
        self.assertTrue(self.score(log)['uefi_read_only_pass'])
        prefix = 'DALI-UFS-HOST: run=' + TOKEN + ' '
        prior = prefix + 'SYNC_COMPLETE lun=2 tag=0 opcode=35 count=1\n'
        prior += prefix + 'SYNC_COMPLETE lun=2 tag=0 opcode=91 count=2\n'
        log = log.replace('lun=2 tag=0 opcode=91 count=1', 'lun=2 tag=0 opcode=91 count=3')
        log = log.replace(prefix + 'connect=Success', prior + prefix + 'connect=Success')
        out = self.score(log)
        self.assertTrue(out['uefi_read_only_pass'], out)
        self.assertEqual(out['pre_diagnostic_syncs'], {2: 2})
        self.assertFalse(self.score(log.replace('opcode=91 count=3', 'opcode=91 count=4'))['uefi_read_only_pass'])

    def test_old_duplicate_truncated_reordered_unknown_and_count_mismatch_fail(self):
        log = trace(self.baseline)
        rows = log.splitlines()
        for bad in (log.replace(TOKEN, 'a' * 32), log + '\n' + log, '\n'.join(rows[:-1]),
                    '\n'.join(rows[:3] + [rows[4], rows[3]] + rows[5:]), log.replace('samples=7', 'samples=8'),
                    log + '\nDALI-UFS-RO: run=' + TOKEN + ' unknown record'):
            self.assertFalse(self.score(bad)['uefi_read_only_pass'])

    def test_foreign_token_and_unrelated_console_lines_cannot_supply_missing_evidence(self):
        log = trace(self.baseline)
        self.assertTrue(self.score('Linux noise\n' + trace(self.baseline, token='a' * 32) + '\n' + log)['uefi_read_only_pass'])
        missing = '\n'.join(l for l in log.splitlines() if 'SYNC_COMPLETE lun=2' not in l)
        self.assertFalse(self.score(missing + '\n' + trace(self.baseline, token='a' * 32))['uefi_read_only_pass'])

    def test_all_baseline_partitions_and_actual_root_boundaries_are_required(self):
        log = trace(self.baseline)
        missing = '\n'.join(l for l in log.splitlines() if 'partition=1' not in l and 'PARTITION ' not in l)
        self.assertFalse(self.score(missing.replace('partitions=1', 'partitions=0'))['uefi_read_only_pass'])
        changed = copy.deepcopy(self.baseline)
        changed['partitions'][72] = copy.deepcopy(changed['partitions'][71])
        self.assertFalse(evidence.score_readonly(log, TOKEN, changed, CANDIDATE)['uefi_read_only_pass'])
        prefix = 'DALI-UFS-RO: run=' + TOKEN + ' '
        extra = prefix + 'MEDIA lun=0 partition=1 readonly=1 block_size=512 last_lba=15\n'
        extra += prefix + 'PARTITION lun=0 number=1 start_lba=8 blocks=16 signature_le=' + '1' * 32 + '\n'
        bad = log.replace(prefix + 'END ', extra + prefix + 'END ').replace('partitions=1', 'partitions=2')
        self.assertIn('exceeds', self.score(bad)['failure'])

    def test_baseline_identity_hash_path_raw_crc_and_derived_metadata_are_rechecked(self):
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.load(serial='other-device')
        raw_path = self.folder / self.report['captures']['primary_header']['file']
        data = bytearray(raw_path.read_bytes()); data[32] ^= 1; raw_path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.load()
        self.report['captures']['primary_header']['sha256'] = evidence.sha256(data)
        (self.folder / 'baseline.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'CRC'):
            self.load()
        self.folder = Path(self.temp.name) / 'fresh'
        self.report = save_fixture(self.folder)
        self.report['captures']['live_fdt']['file'] = '../outside'
        (Path(self.temp.name) / 'outside').write_bytes(b'fdt')
        (self.folder / 'baseline.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'escaped'):
            self.load()

    def test_baseline_gpt_summary_cannot_replace_raw_capture_and_original_checks(self):
        self.report['gpt']['partitions'][0]['first_lba'] += 1
        (self.folder / 'baseline.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            self.load()


if __name__ == '__main__':
    unittest.main()
