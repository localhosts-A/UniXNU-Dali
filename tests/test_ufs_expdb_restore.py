"""Recovery must preserve evidence/guards and never repeat an uncertain write."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_ufs_expdb as expdb_fixtures
import test_ufs_expdb_log as log_fixtures
from ufs_expdb_restore import AndroidExpdb, MIB, prepare_restore, recover_window


class MemoryIo:
    def __init__(self, image, window, fault=None):
        self.data = bytearray(image['original'])
        self.data[MIB:2*MIB] = window
        self.fault, self.writes, self.observations = fault, 0, 0

    def observe(self, image):
        self.observations += 1
        if self.fault == 'identity' and self.observations == 2:
            raise ValueError('identity changed')

    def read(self, offset, count):
        data = bytes(self.data[offset:offset+count])
        return data[:-1] if self.fault == 'short' else data

    def write_original_once(self, image, data, output):
        self.writes += 1
        # The full capture and durable write intent precede hardware submission.
        assert (output/'before.img').read_bytes() == bytes(self.data)
        assert json.loads((output/'recovery.json').read_text())['write_attempted'] is True
        if self.fault == 'partial':
            self.data[MIB:MIB+4096] = data[:4096]
            raise TimeoutError('uncertain partial write')
        self.data[MIB:2*MIB] = data
        if self.fault == 'sync':
            raise OSError('sync failed')
        if self.fault == 'tail':
            self.data[-1] ^= 1


class ExpdbRestoreTests(unittest.TestCase):
    def fixture(self, root):
        args = expdb_fixtures.ExpdbTests().fixture(root)
        image = prepare_restore(serial=args['serial'], baseline=args['baseline'], baseline_dir=root,
                                expdb_baseline=args['expdb_baseline'], expdb_dir=root)
        window, body, expected = log_fixtures.ExpdbLogTests().fixture()
        return image, window, body, expected, args

    def test_capture_decode_then_one_bounded_restore_and_full_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);image,window,body,expected,_=self.fixture(root)
            io=MemoryIo(image,window);out=root/'recovery'
            result=recover_window(image,io,out,expected_log=expected)
            self.assertEqual(io.writes,1);self.assertEqual(io.observations,3)
            self.assertEqual((out/'expdb-console.bin').read_bytes(),body)
            self.assertEqual(bytes(io.data),image['original'])
            self.assertTrue(result['expdb_restore_verified']);self.assertTrue(result['device_sync_completed'])
            self.assertTrue(result['record_integrity_verified']);self.assertFalse(result['persistence_qualified'])

    def test_invalid_or_empty_log_still_recovers_but_cannot_qualify(self):
        for window_kind in ('torn','original'):
            with self.subTest(kind=window_kind),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);image,window,_,expected,_=self.fixture(root)
                window=bytes(MIB) if window_kind=='original' else b'X'+window[1:]
                io=MemoryIo(image,window);result=recover_window(image,io,root/'out',expected_log=expected)
                self.assertTrue(result['expdb_restore_verified']);self.assertFalse(result['record_integrity_verified'])
                self.assertFalse(result['physical_write_qualified']);self.assertFalse(result['persistence_qualified'])
                self.assertEqual(io.writes,0 if window_kind=='original' else 1)

    def test_guard_change_truncation_and_identity_change_refuse_before_write(self):
        for fault in ('head','after','short','identity'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);image,window,_,expected,_=self.fixture(root);io=MemoryIo(image,window,fault)
                if fault in ('head','after'):io.data[0 if fault=='head' else -1]^=1
                with self.assertRaises(ValueError):recover_window(image,io,root/'out',expected_log=expected)
                self.assertEqual(io.writes,0)
                self.assertFalse(json.loads((root/'out/recovery.json').read_text())['expdb_restore_verified'])

    def test_partial_write_sync_failure_and_changed_tail_remain_pending_without_retry(self):
        for fault in ('partial','sync','tail'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);image,window,_,expected,_=self.fixture(root);io=MemoryIo(image,window,fault)
                with self.assertRaises((ValueError,OSError)):recover_window(image,io,root/'out',expected_log=expected)
                report=json.loads((root/'out/recovery.json').read_text())
                self.assertEqual(io.writes,1);self.assertTrue(report['write_attempted'])
                self.assertFalse(report['expdb_restore_verified'])
                self.assertEqual((root/'out/before.img').read_bytes()[MIB:2*MIB],window)

    def test_preparation_reopens_original_and_identity_files(self):
        for filename in ('expdb.img','ufs-manufacturer-id.txt'):
            with self.subTest(filename=filename),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);_,_,_,_,args=self.fixture(root)
                with (root/filename).open('r+b') as target:target.write(b'X')
                with self.assertRaises(ValueError):
                    prepare_restore(serial=args['serial'],baseline=args['baseline'],baseline_dir=root,
                                    expdb_baseline=args['expdb_baseline'],expdb_dir=root)

    def test_actual_android_write_uses_regular_staging_file_and_one_fixed_window(self):
        class FakeCycle:
            CycleError=RuntimeError
            def __init__(self,fault):self.commands=[];self.fault=fault
            def adb_output(self,serial,command,timeout=30):
                self.commands.append(command)
                if command.startswith('stat '):return str(MIB).encode()
                if command.startswith('sha256sum '):
                    return (hashlib.sha256(bytes(MIB)).hexdigest()+'  '+command.split()[-1]+'\n').encode()
                if command.startswith('dd ') and self.fault:raise RuntimeError('timeout')
                return b''
        for fault in (False,True):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);image,_,_,_,_=self.fixture(root);cycle=FakeCycle(fault)
                io=AndroidExpdb(cycle,image['serial'],{});io.node='/dev/block/sdc4';io.block=4096
                with patch('ufs_expdb_restore.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as transfer:
                    if fault:
                        with self.assertRaises(RuntimeError):io.write_original_once(image,bytes(MIB),root)
                    else:io.write_original_once(image,bytes(MIB),root)
                    self.assertEqual(transfer.call_args.kwargs['input'],bytes(MIB))
                writes=[c for c in cycle.commands if c.startswith('dd ')]
                self.assertEqual(len(writes),1)
                self.assertIn('of=/dev/block/sdc4 bs=4096 seek=256 count=256 conv=notrunc,fsync',writes[0])
                self.assertEqual(any(c.startswith('blockdev --flushbufs ') for c in cycle.commands),not fault)


if __name__=='__main__':unittest.main()
