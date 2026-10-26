"""Compare actual C++ provenance and all write gates against independent host hashes."""
import ctypes as C
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from dali_source_identity import source_record, verify_compiled_record, build_source_pins, FIELDS
from dali_ufs_resources import encode_scratch_grant
from ufs_scratch_authorization import source_binding_sha256


class ProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = shutil.which('clang++') or shutil.which('c++')
        if not cc:
            raise unittest.SkipTest('native C++ compiler required')
        cls.temp = tempfile.TemporaryDirectory()
        library = Path(cls.temp.name)/'provenance.so'
        source = ROOT/'drivers/DaliUfs'
        subprocess.run([cc, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
            '-I', str(source), str(source/'DaliUfsPlatform.cpp'), str(ROOT/'tests/ufs_provenance_harness.cpp'),
            '-o', str(library)], check=True)
        cls.lib = C.CDLL(str(library))
        cls.backend_type = C.CFUNCTYPE(C.c_bool, C.c_void_p, C.c_void_p, C.c_size_t, C.c_void_p)
        cls.lib.verify.argtypes = [C.c_char_p, C.c_size_t, C.c_char_p, C.c_size_t, C.c_char_p,
            cls.backend_type, C.POINTER(C.c_uint64), C.c_void_p]
        cls.lib.preimage.argtypes = [C.c_char_p, C.c_char_p, C.c_void_p, C.c_size_t]
        cls.lib.preimage.restype = C.c_size_t

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.pins = dict(zip(FIELDS, [v*40 for v in 'abcd']))
        self.measured = hashlib.sha256(b'whole original source-only Mach-O bytes').digest()
        self.source_hash = source_binding_sha256(dict(self.pins, kernel_sha256=self.measured.hex()))
        self.grant = dict(profile='boot-b-scratch-write-test-v1', write_authorized=True, media_writes=True,
            token='0123456789abcdef'*2, device_binding_sha256='11'*32, candidate_sha256=self.measured.hex(),
            source_sha256=self.source_hash, partition_guid='11111111-2222-3333-4444-555555555555',
            lun=2, block_bytes=4096, capacity_blocks=64577536, boot_start_lba=418560, boot_blocks=16384,
            payload_end_bytes=5062656, scratch_start_lba=434432, scratch_blocks=256, tail_guard_blocks=256, sequence=1)

    def verify(self, *, pins=None, measured=None, grant=None, raw=None, hash_fails=False, wire=None):
        pins = self.pins if pins is None else pins
        measured = self.measured if measured is None else measured
        wire = encode_scratch_grant(self.grant if grant is None else grant) if wire is None else wire
        raw = source_record(pins) if raw is None else raw
        calls = []
        def backend(_, data, size, digest):
            calls.append(C.string_at(data, size))
            C.memmove(digest, hashlib.sha256(calls[-1]).digest(), 32)
            return not hash_fails
        backend = self.backend_type(backend)
        out, digests = (C.c_uint64*4)(), (C.c_ubyte*64)()
        self.lib.verify(wire, len(wire), measured, len(measured), raw, backend, out, digests)
        return list(out), bytes(digests), calls

    def test_compiled_pins_plus_measured_kernel_match_host_source_hash(self):
        out, digests, calls = self.verify()
        self.assertEqual(out, [1]*4)
        self.assertEqual(digests, self.measured + bytes.fromhex(self.source_hash))
        expected = b'DALI-UFS-SOURCE-V1\0' + json.dumps(dict(self.pins, kernel_sha256=self.measured.hex()),
            sort_keys=True, separators=(',', ':')).encode('ascii')
        self.assertEqual(calls, [expected])

    def test_each_format_valid_compiled_pin_mismatch_refuses_all_writes(self):
        for key in FIELDS:
            out, digests, calls = self.verify(pins=dict(self.pins, **{key: 'f'*40}))
            self.assertEqual(out, [0]*4)
            self.assertEqual(digests, bytes(64))
            self.assertEqual(len(calls), 1)

    def test_wrong_or_missing_measurement_refuses_before_hashing(self):
        for measured in (b'', self.measured[:-1], self.measured + b'X', bytes(32), bytes([self.measured[0]^1])+self.measured[1:]):
            out, digests, calls = self.verify(measured=measured)
            self.assertEqual((out, digests, calls), ([0]*4, bytes(64), []))

    def test_grant_hashes_are_not_used_as_observations(self):
        for key in ('candidate_sha256', 'source_sha256'):
            out, digests, calls = self.verify(grant=dict(self.grant, **{key: 'ff'*32}))
            self.assertEqual((out, digests), ([0]*4, bytes(64)))
            self.assertEqual(len(calls), key == 'source_sha256')

    def test_bad_compiled_header_or_noncanonical_pin_refuses_without_hashing(self):
        raw = source_record(self.pins)
        for index in (0, 15, 16, 56):
            bad = bytearray(raw); bad[index] = ord('Z')
            self.assertEqual(self.verify(raw=bytes(bad)), ([0]*4, bytes(64), []))

    def test_hash_failure_and_invalid_grant_clear_stale_verification(self):
        out, digests, calls = self.verify(hash_fails=True)
        self.assertEqual((out, digests, len(calls)), ([0]*4, bytes(64), 1))
        self.assertEqual(self.verify(wire=bytes(232)), ([0]*4, bytes(64), []))

    def test_preimage_capacity_never_overwrites_caller_boundary(self):
        for size in (0, 1, 17, 18, 200, 351, 512):
            out = (C.c_ubyte*520)(*([0xa5]*520))
            length = self.lib.preimage(source_record(self.pins), self.measured, out, size)
            self.assertEqual(bytes(out)[size:], b'\xa5'*(520-size))
            if length:
                self.assertEqual(hashlib.sha256(bytes(out)[:length]).hexdigest(), self.source_hash)

    def test_kernel_record_requires_one_matching_compiled_identity(self):
        raw = source_record(self.pins)
        verify_compiled_record(b'kernelprefix'+raw+b'kernelend', self.pins)
        for data in (b'kernel', raw+raw, source_record(dict(self.pins, device_commit='f'*40)), raw[:-1]):
            with self.assertRaisesRegex(ValueError, 'compiled source identity'):
                verify_compiled_record(data, self.pins)


class BuildSourceIdentityTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

    def commit(self, root):
        self.git(root, 'add', '-A')
        self.git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source')

    def repo(self, root, files):
        root.mkdir(parents=True)
        self.git(root, 'init', '-q')
        for name, data in files.items():
            path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
        self.commit(root)
        return root

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name); self.tree = base/'out/tree'
        self.xnu = self.repo(self.tree/'.cache/xnu-work', {'kernel.c': 'fixed upstream'})
        self.unixnu = self.repo(base/'generic', {'drivers/UfsTransport/UfsTransport.cpp': 'transport',
            'feeds.toml': '[[source]]\nname="xnu"\ncommit="'+self.git(self.xnu, 'rev-parse', 'HEAD')+'"\n'+
                'driverkit_tag="xnu-1.0"\ntrustcache_tag="xnu-2.0"\n'})
        self.device = self.repo(base/'device', {'drivers/DaliUfs/DaliUfs.cpp': 'platform',
            'board.toml': '[firmware]\nunixnu_commit="'+self.git(self.unixnu, 'rev-parse', 'HEAD')+'"\nuefi_commit="'+'c'*40+'"\n'})
        (self.tree/'drivers').mkdir()
        for name, repo in [('DaliUfs', self.device), ('UfsTransport', self.unixnu)]:
            (self.tree/'drivers'/name).symlink_to(repo/'drivers'/name, target_is_directory=True)
        cache = self.tree/'.cache/sources'; cache.mkdir()
        for name in ['xnu-driverkit-1.0.json', 'xnu-driverkit-crypto-1.0.json', 'xnu-trustcache-2.0.json']:
            (cache/name).write_text('[]')

    def test_copied_script_tree_uses_real_driver_source_repositories(self):
        pins = build_source_pins(self.tree)
        self.assertEqual(pins, dict(device_commit=self.git(self.device,'rev-parse','HEAD'),
            unixnu_commit=self.git(self.unixnu,'rev-parse','HEAD'), uefi_commit='c'*40,
            xnu_commit=self.git(self.xnu,'rev-parse','HEAD')))
        self.assertEqual(len(source_record(pins)), 180)

    def test_dirty_source_or_different_pinned_revision_is_refused(self):
        (self.device/'drivers/DaliUfs/DaliUfs.cpp').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'clean source checkout'):
            build_source_pins(self.tree)
        self.commit(self.device)
        (self.xnu/'kernel.c').write_text('another revision'); self.commit(self.xnu)
        with self.assertRaisesRegex(ValueError, 'board/feeds'):
            build_source_pins(self.tree)


if __name__ == '__main__':
    unittest.main()
