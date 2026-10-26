"""Destructive layout planner invariants; all media in these tests is synthetic."""
import hashlib
import struct
import sys
from pathlib import Path
import unittest
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ufs_geometry as gpt
from ufs_userdata_layout import plan_split, apply_gpt_transaction, GIB, LOG_BYTES, RESERVE_BYTES


def fixture(block=4096, usable_first=8):
    capacity = 64*GIB
    total = capacity//block
    start = 2*GIB//block
    end = total - 16385
    table = bytearray(96*128)
    for i in range(96):
        row = memoryview(table)[i*128:(i+1)*128]
        row[:16], row[16:32] = uuid.UUID(int=1).bytes_le, uuid.UUID(int=i+2).bytes_le
        name = 'p' + str(i+1)
        first, last = usable_first + i, usable_first + i
        if i == 94:
            name, first, last = 'userdata', start, end
        if i == 95:
            name, first, last = 'flashinfo', end+1, total-16380
        struct.pack_into('<QQQ', row, 32, first, last, 0)
        raw = name.encode('utf-16-le');row[56:56+len(raw)] = raw
    # A 512-byte fixture needs a larger first usable area for its old table.
    if block == 512:
        shift=32
        for i in range(94):struct.pack_into('<QQ', table, i*128+32, usable_first+shift+i, usable_first+shift+i)
        usable_first += shift
    def header(current, other, entries):
        raw=bytearray(block)
        struct.pack_into('<8sIIIIQQQQ16sQIII', raw, 0, b'EFI PART',0x10000,92,0,0,
            current,other,usable_first,total-16379,bytes(16),entries,96,128,zlib.crc32(table))
        struct.pack_into('<I', raw, 16, zlib.crc32(raw[:92]))
        return bytes(raw)
    return header(1,total-1,2),bytes(table),header(total-1,1,total-128),bytes(table),block,capacity


def split(raw, **kwargs):
    return plan_split(*raw[:4],block_bytes=raw[4],capacity_bytes=raw[5],
        data_guid=kwargs.get('data_guid',uuid.UUID(int=1000)),log_guid=kwargs.get('log_guid',uuid.UUID(int=1001)))


class UserdataLayoutTests(unittest.TestCase):
    def test_transaction_write_flush_readback_failures_never_replay(self):
        _,patches=split(fixture())
        originals={name:bytes(len(data)) for name,_,data in patches}
        for fail in range(12):
            storage={lba:originals[name] for name,lba,_ in patches};calls=[];events=[]
            def operation(kind):
                calls.append(kind)
                if len(calls)==fail+1:raise OSError('injected failure')
            def read(lba,size):
                # Only count readback so 12 faults cover write/sync/readback.
                if storage[lba]!=bytes(size):operation('readback')
                return storage[lba]
            def write(name,lba,data):operation('write:'+name);storage[lba]=data
            def sync():operation('sync')
            with self.subTest(fail=fail),self.assertRaises(OSError):
                apply_gpt_transaction(patches,originals,read=read,write=write,sync=sync,
                    journal=lambda name,stage:events.append((name,stage)))
            writes=[c for c in calls if c.startswith('write:')]
            self.assertEqual(len(writes),len(set(writes)))
            self.assertEqual(len(calls),fail+1)

    def test_transaction_preimage_and_corrupt_readback_stop_before_next_copy(self):
        _,patches=split(fixture());originals={name:bytes(len(data)) for name,_,data in patches}
        writes=[]
        with self.assertRaisesRegex(ValueError,'preimage changed'):
            apply_gpt_transaction(patches,originals,read=lambda l,n:b'x'*n,
                write=lambda *a:writes.append(a),sync=lambda:None,journal=lambda *a:None)
        self.assertEqual(writes,[])
        with self.assertRaisesRegex(ValueError,'readback differs'):
            apply_gpt_transaction(patches,originals,read=lambda l,n:bytes(n),
                write=lambda *a:writes.append(a),sync=lambda:None,journal=lambda *a:None)
        self.assertEqual(len(writes),1)

    def test_exact_32gib_and_32mib_no_other_row_change(self):
        raw=fixture();before=tuple(raw);plan,patches=split(raw)
        self.assertEqual(tuple(raw),before)
        old=plan['original_userdata'];new=plan['userdata']
        self.assertEqual((old['blocks']-new['blocks'])*4096,RESERVE_BYTES)
        self.assertEqual(plan['data']['last_lba']+1,plan['logs']['first_lba'])
        self.assertEqual(plan['logs']['last_lba'],old['last_lba'])
        self.assertEqual(plan['logs']['blocks']*4096,LOG_BYTES)
        self.assertEqual(plan['data']['number'],97)
        for a,b in zip(plan['original_gpt']['partitions'],plan['proposed_gpt']['partitions'][:96]):
            if a['name']!='userdata': self.assertEqual(a,b)
        self.assertFalse(plan['write_authorized'])
        self.assertEqual([x[0] for x in patches],['backup-entries','backup-header','primary-entries','primary-header'])
        for desc,(_,_,data) in zip(plan['patches'],patches):self.assertEqual(desc['sha256'],hashlib.sha256(data).hexdigest())

    def test_actual_logical_block_units(self):
        for block in (512,4096,65536):
            with self.subTest(block=block):
                p,_=split(fixture(block));self.assertEqual(p['logs']['blocks']*block,LOG_BYTES)

    def test_duplicate_zero_or_existing_guids_refused(self):
        for a,b in ((1000,1000),(0,1000),(2,1000)):
            with self.subTest(a=a,b=b),self.assertRaises(ValueError):
                split(fixture(),data_guid=uuid.UUID(int=a),log_guid=uuid.UUID(int=b))

    def test_crc_or_reciprocal_table_corruption_refused(self):
        for i in range(4):
            raw=list(fixture());changed=bytearray(raw[i]);changed[40]^=1;raw[i]=bytes(changed)
            with self.subTest(i=i),self.assertRaises(ValueError):split(raw)

    def test_repeat_split_and_too_small_userdata_refused(self):
        p,patches=split(fixture());new={n:data for n,_,data in patches}
        with self.assertRaisesRegex(ValueError,'already exist'):
            plan_split(new['primary-header'],new['primary-entries'],new['backup-header'],new['backup-entries'],
                block_bytes=4096,capacity_bytes=64*GIB,data_guid=uuid.UUID(int=1002),log_guid=uuid.UUID(int=1003))
        raw=list(fixture());table=bytearray(raw[1]);struct.pack_into('<Q',table,94*128+40,2*GIB//4096+32*GIB//4096)
        for i in (0,2):
            h=bytearray(raw[i]);struct.pack_into('<I',h,88,zlib.crc32(table));struct.pack_into('<I',h,16,0)
            struct.pack_into('<I',h,16,zlib.crc32(h[:92]));raw[i]=bytes(h)
        raw[1]=raw[3]=bytes(table)
        with self.assertRaisesRegex(ValueError,'insufficient userdata'):split(raw)

    def test_expanded_array_cannot_overlap_usable_data(self):
        # 96 entries fit three 4K blocks but 128 need four; original usable=5.
        with self.assertRaisesRegex(ValueError,'expanded GPT'):
            split(fixture(4096,usable_first=5))


if __name__=='__main__':unittest.main()
