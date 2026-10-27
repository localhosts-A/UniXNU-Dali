"""Decode a bounded expdb log. File integrity alone is not hardware qualification."""
import hashlib
import re
import struct

WINDOW_BYTES = 1048576
MAGIC_V1 = b'DALIUFSLOG1' + bytes(5)
MAGIC_V2 = b'DALIUFSLOG2' + bytes(5)
V2_GENERATION_OFFSET = 244
V2_FLAGS_OFFSET = 252
V2_CAPTURE_STAGE_OFFSET = 256
V2_BODY_FORMAT_OFFSET = 260
V2_TRUNCATED = 1


def decode_log(window, *, token, device_binding, candidate_sha256, source_sha256,
               block_bytes, owner, sequence, window_bytes=WINDOW_BYTES,
               expected_schema=None, expected_generation=None, expected_flags=None,
               expected_capture_stage=None, expected_body_format=None):
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('invalid expected token')
    if (type(block_bytes) is not int or block_bytes < 512 or block_bytes > 65536 or
            block_bytes & (block_bytes - 1) or type(owner) is not int or owner not in (1,2) or
            type(sequence) is not int or not 0 < sequence < 1 << 64):
        raise ValueError('invalid expected geometry, owner or sequence')
    if (type(window_bytes) is not int or window_bytes < 6 * block_bytes or
            not isinstance(window, bytes) or len(window) != window_bytes):
        raise ValueError('log slot has the wrong exact length')
    header = window[block_bytes:2 * block_bytes]
    if header[:16] == MAGIC_V1 and struct.unpack_from('<I', header, 20)[0] == 1:
        schema, magic = 1, MAGIC_V1
        generation = flags = capture_stage = body_format = None
    elif header[:16] == MAGIC_V2 and struct.unpack_from('<I', header, 20)[0] == 2:
        schema, magic = 2, MAGIC_V2
        generation = struct.unpack_from('<Q', header, V2_GENERATION_OFFSET)[0]
        flags = struct.unpack_from('<I', header, V2_FLAGS_OFFSET)[0]
        capture_stage = struct.unpack_from('<I', header, V2_CAPTURE_STAGE_OFFSET)[0]
        body_format = struct.unpack_from('<I', header, V2_BODY_FORMAT_OFFSET)[0]
        if (generation == 0 or flags & ~V2_TRUNCATED or
                capture_stage not in (1, 2) or body_format not in (1, 2) or
                (owner, capture_stage, body_format) not in ((1, 1, 1), (2, 2, 2))):
            raise ValueError('invalid v2 generation, flags, stage or body format')
    else:
        raise ValueError('unsupported userdata log schema or version')
    if expected_schema is not None and expected_schema != schema:
        raise ValueError('userdata log schema differs from expected schema')
    if schema == 1 and any(value is not None for value in (
            expected_generation, expected_flags, expected_capture_stage, expected_body_format)):
        raise ValueError('v2 metadata was expected for a v1 record')
    if schema == 2:
        checks = ((expected_generation, generation), (expected_flags, flags),
                  (expected_capture_stage, capture_stage), (expected_body_format, body_format))
        if any(expected is not None and expected != actual for expected, actual in checks):
            raise ValueError('v2 log metadata differs from expected values')
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
        expected[:16]=magic
        struct.pack_into('<II',expected,16,kind,schema)
        expected[24:56]=token.encode('ascii')
        struct.pack_into('<IQIIQ',expected,56,owner,sequence,size,block_bytes,window_bytes//block_bytes)
        expected[84:180]=b''.join(bindings)
        expected[180:212]=digest
        if schema == 2:
            struct.pack_into('<QIII', expected, V2_GENERATION_OFFSET,
                             generation, flags, capture_stage, body_format)
        expected[212:244]=hashlib.sha256(expected).digest()
        if bytes(expected)!=window[offset:offset+block_bytes]:
            raise ValueError('expdb log binding, digest, padding or commit mismatch')
    if any(window[2*block_bytes+size:window_bytes-block_bytes]):
        raise ValueError('nonzero expdb log padding')
    record = dict(schema=schema, token=token, owner=owner, sequence=sequence, bytes=size,
                  sha256=digest.hex(), block_bytes=block_bytes, window_bytes=window_bytes,
                  record_integrity_verified=True, physical_write_qualified=False,
                  persistence_qualified=False)
    if schema == 2:
        record.update(generation=generation, flags=flags, capture_stage=capture_stage,
                      body_format=body_format, truncated=bool(flags & V2_TRUNCATED))
    return body, record
