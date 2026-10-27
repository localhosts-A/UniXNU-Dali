"""Decode a bounded expdb log. File integrity alone is not hardware qualification."""
import hashlib
import re
import struct

WINDOW_BYTES = 1048576
MAGIC = b'DALIUFSLOG1' + bytes(5)


def decode_log(window, *, token, device_binding, candidate_sha256, source_sha256,
               block_bytes, owner, sequence, window_bytes=WINDOW_BYTES):
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('invalid expected token')
    if (type(block_bytes) is not int or block_bytes < 512 or block_bytes > 65536 or
            block_bytes & (block_bytes - 1) or type(owner) is not int or owner not in (1,2) or
            type(sequence) is not int or not 0 < sequence < 1 << 64):
        raise ValueError('invalid expected geometry, owner or sequence')
    if (type(window_bytes) is not int or window_bytes < 6 * block_bytes or
            not isinstance(window, bytes) or len(window) != window_bytes):
        raise ValueError('log slot has the wrong exact length')
    bindings=[]
    for value in (device_binding,candidate_sha256,source_sha256):
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value) or int(value,16)==0:
            raise ValueError('invalid expected identity binding')
        bindings.append(bytes.fromhex(value))
    size=struct.unpack_from('<I',window,block_bytes+68)[0]
    if not 0 < size <= window_bytes-3*block_bytes:
        raise ValueError('invalid expdb log payload extent')
    body=window[2*block_bytes:2*block_bytes+size]
    digest=hashlib.sha256(body).digest()
    for offset,kind in ((0,1),(block_bytes,2),(window_bytes-block_bytes,3)):
        expected=bytearray(block_bytes)
        expected[:16]=MAGIC
        struct.pack_into('<II',expected,16,kind,1)
        expected[24:56]=token.encode('ascii')
        struct.pack_into('<IQIIQ',expected,56,owner,sequence,size,block_bytes,window_bytes//block_bytes)
        expected[84:180]=b''.join(bindings)
        expected[180:212]=digest
        expected[212:244]=hashlib.sha256(expected).digest()
        if bytes(expected)!=window[offset:offset+block_bytes]:
            raise ValueError('expdb log binding, digest, padding or commit mismatch')
    if any(window[2*block_bytes+size:window_bytes-block_bytes]):
        raise ValueError('nonzero expdb log padding')
    return body,dict(schema=1,token=token,owner=owner,sequence=sequence,bytes=size,
                     sha256=digest.hex(),block_bytes=block_bytes,window_bytes=window_bytes,
                     record_integrity_verified=True,
                     physical_write_qualified=False,persistence_qualified=False)
