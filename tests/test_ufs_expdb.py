"""Expdb grants re-read original bytes and raw GPT; all tests are offline."""
import copy
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import uuid

from test_ufs_geometry import fixture as gpt_fixture, fix_tables, ufs
import test_ufs_scratch_authorization as fixtures
from ufs_scratch_authorization import build_xnu_scratch_grant
from ufs_expdb import MIB

auth = fixtures.auth


class ExpdbTests(unittest.TestCase):
    def fixture(self, root):
        # Use small complete media evidence and retain nonzero vendor bytes
        # after the writable window. Actual device backup replay is separate.
        original, _, base, candidate = fixtures.ScratchAuthorizationTests().fixture(root)
        raw = gpt_fixture()
        for table in (raw[1], raw[3]):
            row = memoryview(table)[:128]
            row[:16] = uuid.UUID(int=99).bytes_le
            row[16:32] = uuid.UUID('00000003-39c2-4488-9bb0-00cb43c9ccd4').bytes_le
            first = 32768
            struct.pack_into('<QQQ', row, 32, first, first+8*MIB//4096-1, 0)
            row[56:66] = 'expdb'.encode('utf-16-le')
        fix_tables(raw)
        capacity = 262144*4096
        gpt = ufs.validate_gpt(*raw, 4096, capacity)
        part = next(part for part in gpt['partitions'] if part['name']=='expdb')
        geo = base['geometry']; geo.update(capacity_bytes=capacity, capacity_blocks=262144)
        geo['unit_descriptor']['logical_block_count'] = 262144
        boot = next(part for part in gpt['partitions'] if part['name']=='boot_b')
        geo['boot_b'] = dict(boot, bytes=64*MIB); base['gpt'] = gpt
        image = bytearray(8*MIB); image[-4096:] = b'L'*4096
        (root/'expdb.img').write_bytes(image)
        sha = hashlib.sha256(image).hexdigest()
        hashes = {'boot_b':base['original_boot_b_sha256'], **{str(i):'a'*64 for i in range(17)}}
        expdb = dict(kind='dali-expdb-readonly-baseline', serial='DALI123', media_writes_performed=False,
                     device_reboot_performed=False, android=dict(product='dali',slot_suffix='_b',root='uid=0(root)'),
                     all18_before=hashes, all18_after=dict(hashes), lun=2, logical_block_bytes=4096,
                     capacity_bytes=capacity, blockdev_read_only=0, bytes=len(image),
                     sha256=sha, device_sha256_before=sha, device_sha256_after=sha,
                     partition=part, gpt=gpt, captures={})
        for name, data in zip(('primary-header.bin','primary-entries.bin','backup-header.bin','backup-entries.bin'), raw):
            (root/name).write_bytes(data)
            expdb['captures'][name] = dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        args = dict(serial='DALI123',token='0123456789abcdef0123456789abcdef',sequence=1,
                    baseline=base,baseline_dir=root,source_manifest=fixtures.source_manifest(b'kernel'),kernel_bytes=b'kernel',
                    candidate_image=candidate,original_boot_b=original,target_partition='expdb',
                    expdb_baseline=expdb,expdb_dir=root)
        return args

    def test_full_grant_separates_boot_candidate_from_expdb_extent_and_binds_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            args=self.fixture(Path(temp));grant,wire,proposal=auth.build_xnu_scratch_grant(**args)
            self.assertEqual((grant['profile'],len(wire),grant['payload_end_bytes']),('expdb-scratch-write-test-v1',236,0))
            self.assertEqual((grant['boot_start_lba'],grant['scratch_start_lba'],grant['tail_guard_blocks']),
                             (32768,33024,1536))
            self.assertEqual(grant['partition_guid'],args['expdb_baseline']['partition']['guid'])
            self.assertEqual(grant['original_partition_sha256'],args['expdb_baseline']['sha256'])
            self.assertEqual(proposal['boot_payload_end_bytes'],8192)
            self.assertIsNone(proposal['original_avb'])
            self.assertFalse(proposal['free_space_proven'])

    def test_expdb_profile_refuses_changed_or_incomplete_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            args=self.fixture(Path(temp))
            mutations=({'serial':'other'}, {'media_writes_performed':True}, {'device_reboot_performed':True},
                       {'lun':1}, {'blockdev_read_only':1}, {'blockdev_read_only':False},
                       {'logical_block_bytes':512}, {'sha256':'f'*64}, {'device_sha256_after':'f'*64},
                       {'bytes':7*MIB}, {'android':{}}, {'all18_after':{}}, {'partition':{}})
            for change in mutations:
                with self.subTest(change=change),self.assertRaises(ValueError):
                    auth.build_xnu_scratch_grant(**dict(args,expdb_baseline=dict(args['expdb_baseline'],**change)))

    def test_every_capture_and_backup_bytes_are_reopened_and_checked(self):
        for filename in ('primary-header.bin','primary-entries.bin','backup-header.bin','backup-entries.bin','expdb.img'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);args=self.fixture(root)
                with (root/filename).open('r+b') as f:
                    f.seek(-1,2);f.write(b'X')
                with self.assertRaises(ValueError): auth.build_xnu_scratch_grant(**args)

    def test_nonzero_window_fails_even_if_all_manifest_hashes_are_updated(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);args=self.fixture(root);data=bytearray((root/'expdb.img').read_bytes());data[MIB]=1
            (root/'expdb.img').write_bytes(data);digest=hashlib.sha256(data).hexdigest()
            for key in ('sha256','device_sha256_before','device_sha256_after'): args['expdb_baseline'][key]=digest
            with self.assertRaisesRegex(ValueError,'not zero'): auth.build_xnu_scratch_grant(**args)

    def test_symlink_truncation_and_other_gpt_baseline_are_refused(self):
        for kind in ('symlink','truncated','other-gpt'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);args=self.fixture(root)
                if kind=='symlink':
                    (root/'expdb.img').rename(root/'other.img');(root/'expdb.img').symlink_to(root/'other.img')
                elif kind=='truncated':
                    with (root/'expdb.img').open('r+b') as f:f.truncate(MIB)
                else:
                    args['baseline']['gpt']=copy.deepcopy(args['baseline']['gpt']);args['baseline']['gpt']['partitions'].pop()
                with self.assertRaises((OSError,ValueError)): auth.build_xnu_scratch_grant(**args)

    def test_old_profile_cannot_silently_accept_expdb_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            args=self.fixture(Path(temp));args['target_partition']='boot_b'
            with self.assertRaises(ValueError):auth.build_xnu_scratch_grant(**args)
