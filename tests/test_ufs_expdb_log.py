import hashlib
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ufs_expdb_log import decode_log


class ExpdbLogTests(unittest.TestCase):
    def fixture(self,block=4096):
        body=b'DALI: expdb log\r\n'+bytes(range(256))*10
        data=bytearray(1048576);data[2*block:2*block+len(body)]=body
        kwargs=dict(token='a'*32,device_binding='01'*32,candidate_sha256='02'*32,
                    source_sha256='03'*32,block_bytes=block,owner=1,sequence=1)
        for at,kind in ((0,1),(block,2),(1048576-block,3)):
            record=bytearray(block);record[:16]=b'DALIUFSLOG1'+bytes(5)
            struct.pack_into('<II',record,16,kind,1);record[24:56]=b'a'*32
            struct.pack_into('<IQIIQ',record,56,1,1,len(body),block,1048576//block)
            record[84:180]=bytes([1])*32+bytes([2])*32+bytes([3])*32
            record[180:212]=hashlib.sha256(body).digest();record[212:244]=hashlib.sha256(record).digest()
            data[at:at+block]=record
        return bytes(data),body,kwargs

    def test_exact_body_and_metadata_without_hardware_promotion(self):
        for block in (512,4096,65536):
            data,body,kwargs=self.fixture(block);observed,record=decode_log(data,**kwargs)
            self.assertEqual(observed,body);self.assertTrue(record['record_integrity_verified'])
            self.assertFalse(record['physical_write_qualified']);self.assertFalse(record['persistence_qualified'])

    def test_torn_metadata_digest_body_and_commit_fail_closed(self):
        data,_,kwargs=self.fixture()
        for at in (0,16,24,56,68,84,116,148,180,212,300,4096,8192,12000,1048575):
            corrupt=bytearray(data);corrupt[at]^=1
            with self.assertRaises(ValueError,msg=str(at)):decode_log(bytes(corrupt),**kwargs)
        for field,value in [('token','b'*32),('owner',2),('sequence',2),('device_binding','04'*32)]:
            with self.assertRaises(ValueError):decode_log(data,**dict(kwargs,**{field:value}))

    def test_invalid_input_extent_and_expected_parameters(self):
        data,_,kwargs=self.fixture()
        for bad in (b'',data[:-1],data+bytes(1),bytearray(data)):
            with self.assertRaises(ValueError):decode_log(bad,**kwargs)
        for field,value in [('block_bytes',0),('block_bytes',4095),('sequence',True),
                            ('owner',0),('candidate_sha256','00'*32),('source_sha256','A'*64)]:
            with self.assertRaises(ValueError):decode_log(data,**dict(kwargs,**{field:value}))


if __name__=='__main__':unittest.main()
