"""Execute actual Dali controller/range code; the hardware is simulated."""
import ctypes as C
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dali_ufs_resources import encode_scratch_grant


class PlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = shutil.which('clang++') or shutil.which('c++')
        if not cc:
            raise unittest.SkipTest('C++ compiler required')
        cls.tmp = tempfile.TemporaryDirectory()
        source = Path(__file__).resolve().parents[1] / 'drivers/DaliUfs'
        library = Path(cls.tmp.name) / 'platform.so'
        subprocess.run([cc, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I', str(source), str(source/'DaliUfsPlatform.cpp'), str(Path(__file__).with_name('ufs_platform_harness.cpp')),
                        '-o', str(library)], check=True)
        cls.lib = C.CDLL(str(library))
        cls.lib.platform_case.argtypes = [C.c_uint, C.POINTER(C.c_uint64)]
        cls.lib.memory_case.argtypes = [C.c_uint64]*5 + [C.POINTER(C.c_uint64)]
        cls.lib.memory_case.restype = C.c_bool
        cls.lib.dma_case.argtypes = [C.c_uint64]*4
        cls.lib.dma_case.restype = C.c_bool
        cls.lib.scratch_policy_case.argtypes = [C.c_uint, C.POINTER(C.c_uint64)]
        cls.lib.scratch_wire_case.argtypes = [C.c_uint, C.POINTER(C.c_uint64)]
        cls.lib.scratch_wire_decode_case.argtypes = [C.POINTER(C.c_uint8), C.c_size_t, C.POINTER(C.c_uint64)]
        cls.lib.scratch_sequence_case.argtypes = [C.c_uint, C.POINTER(C.c_uint64)]
        cls.lib.guid_case.argtypes = [C.c_char_p, C.POINTER(C.c_uint8)]
        cls.lib.guid_case.restype = C.c_bool

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def case(self, mode=0):
        out = (C.c_uint64*32)(); self.lib.platform_case(mode, out)
        self.assertEqual(out[12], 0, 'unknown address or SiP ABI')
        return list(out)

    def test_fixed_source_sequence_and_no_uic_replay(self):
        r = self.case()
        self.assertEqual((r[0],r[1],r[3],r[6],r[7],r[16],r[24],r[25],r[26],r[27]), (7,7,12,1,0,2,7,0,8,0))

    def test_off_bad_resources_token_version_clocks_and_active_queues_never_write(self):
        for mode, result in ((1,0),(3,2),(4,2),(5,2),(6,2),(7,1),(8,1),(9,1),(10,1),(23,4),
                             (26,2),(27,2),(28,2),(29,2),(30,4)):
            with self.subTest(mode=mode):
                r = self.case(mode); self.assertEqual((r[0],r[2],r[3],r[4]), (result,0,0,0))

    def test_quiesced_firmware_bases_are_recorded_and_cleared_without_adopting(self):
        r = self.case(2)
        self.assertEqual((r[0],r[20],r[21],r[22],r[23]), (7,0x12345000,8,0,0))

    def test_power_failure_and_refclock_timeout_stop_without_retry(self):
        r = self.case(11); self.assertEqual((r[0],r[3],r[4],r[6]), (6,0,1,0))
        r = self.case(12); self.assertEqual((r[0],r[3],r[4],r[6]), (3,0,4,0))
        self.assertEqual(r[5],3000)

    def test_uic_failure_timeout_and_uncertain_command_preserve_first_fault(self):
        for mode, result in ((13,5),(14,3),(20,4)):
            r = self.case(mode)
            self.assertEqual((r[0],r[3],r[6],r[7],r[25],r[28]), (result,1,0,1,0,result))
            self.assertEqual(r[17],5)
        self.assertGreaterEqual(self.case(14)[5],500000)
        self.assertEqual(self.case(20)[18:20], [0x16810090,1])

    def test_non_uic_fault_addresses_and_values_survive_cleanup(self):
        for mode, address, value in ((30,0x168a0104,0xffffffff),(31,0x168a0148,4),(32,0x16810034,1)):
            r = self.case(mode)
            self.assertEqual((r[0],r[18],r[19],r[28]), (4,address,value,4))
            self.assertEqual(r[25],0)

    def test_refclock_smc_failures_and_stop_queue_readback(self):
        for mode in (35,36):
            r = self.case(mode); self.assertEqual((r[0],r[3],r[25]),(6,0,0))
        for mode in (33,34):
            r = self.case(mode)
            self.assertEqual((r[0],r[26],r[27],r[28]), (7,2,0,2))

    def test_linereset_waits_for_actual_completion_before_read_clear_and_ack(self):
        r = self.case(15)
        self.assertEqual((r[0],r[3],r[8],r[9],r[10],r[11]), (7,12,1,0,1,1))

    def test_linereset_other_layer_or_missing_completion_fails_without_ack_or_replay(self):
        r = self.case(16); self.assertEqual((r[0],r[3],r[8],r[14]), (4,6,0,0x80000010))
        r = self.case(17); self.assertEqual((r[0],r[3],r[8],r[25]), (3,6,0,0))

    def test_unknown_ack_status_and_failed_stop_are_errors(self):
        r = self.case(18); self.assertEqual((r[0],r[3]), (4,6))
        r = self.case(19); self.assertEqual((r[0],r[26],r[27],r[28]), (7,3,1,3))

    def test_device_reset_release_is_never_replayed_and_low_failure_cleans_up_once(self):
        for mode in (21,22):
            r = self.case(mode)
            self.assertEqual((r[0],r[3],r[16],r[25]), (6,0,2,0))

    def test_cache_poll_line_rounding_and_allocation_boundaries(self):
        out = (C.c_uint64*2)()
        self.assertTrue(self.lib.memory_case(0x10000,0x22000,0x10000,32,64,out))
        self.assertEqual(list(out),[0x10000,0x10040])
        self.assertTrue(self.lib.memory_case(0x10000,0x22000,0x31fff,1,128,out))
        self.assertEqual(list(out),[0x31f80,0x32000])
        for at, n, line in ((0xffff,1,64),(0x32000,1,64),(0x31fff,2,64),(0x10000,0,64),(0x10000,1,63),(0x10000,1,8192)):
            self.assertFalse(self.lib.memory_case(0x10000,0x22000,at,n,line,out))
        self.assertFalse(self.lib.memory_case(0xfffffffffffff000,8192,0xfffffffffffff000,32,64,out))

    def test_dma_full_allocation_and_low_4g_constraint(self):
        self.assertTrue(self.lib.dma_case(0xfffffff000000000,0x80000000,0x22000,0x22000))
        for cpu,pa,n,span in ((0xfffffffffffff000,0x80000000,8192,8192),(0x10001,0x80000000,4096,4096),
                              (0x10000,0xfffff000,8192,8192),(0x10000,0x100000000,4096,4096),(0x10000,0x80000000,8192,4096)):
            self.assertFalse(self.lib.dma_case(cpu,pa,n,span))

    def test_scratch_grant_binds_identity_geometry_and_exact_window(self):
        def check(mode):
            out = (C.c_uint64*2)(); self.lib.scratch_policy_case(mode, out); return tuple(out)
        self.assertEqual(check(0), (1, 1))
        self.assertEqual(check(26), (1, 1))  # Last block remains inside the window.
        for mode in range(1, 11):
            with self.subTest(invalid_grant=mode): self.assertEqual(check(mode), (0, 0))
        for mode in range(11, 26):
            with self.subTest(rejected_request=mode): self.assertEqual(check(mode), (1, 0))
        self.assertEqual(check(27), (1, 0))  # Not armed until the published scratch media is verified.

    def test_scratch_grant_adt_wire_is_strict_and_maps_relative_lbas(self):
        def check(mode):
            out=(C.c_uint64*8)();self.lib.scratch_wire_case(mode,out);return tuple(out)
        self.assertEqual(check(0),(1,1,2,434432,256,1,434687,0))
        for mode in range(1,9):
            with self.subTest(invalid_wire=mode):self.assertEqual(check(mode)[:2],(0,0))

    def test_scratch_attempt_marker_and_two_boot_policy_are_at_most_once(self):
        def check(mode):
            out=(C.c_uint64*15)();self.lib.scratch_sequence_case(mode,out);return tuple(out)
        self.assertEqual(check(0), (1,0,1,1,2,0,1,0,2,0,1,1,1,1,1))
        self.assertEqual(check(1)[:3], (1,254,255))
        self.assertEqual(check(2)[:3], (0,0,0))

    def test_python_grant_encoder_wire_is_accepted_by_runtime_decoder(self):
        grant = {
            'profile':'boot-b-scratch-write-test-v1','write_authorized':True,'media_writes':True,
            'token':'0123456789abcdef0123456789abcdef','device_binding_sha256':'11'*32,
            'candidate_sha256':'22'*32,'source_sha256':'33'*32,
            'partition_guid':'00000046-39c2-4488-9bb0-00cb43c9ccd4','lun':2,'block_bytes':4096,
            'capacity_blocks':64577536,'boot_start_lba':418560,'boot_blocks':16384,
            'payload_end_bytes':5062656,'scratch_start_lba':434432,'scratch_blocks':256,
            'tail_guard_blocks':256,'sequence':1,
        }
        wire = encode_scratch_grant(grant)
        raw = (C.c_uint8 * len(wire)).from_buffer_copy(wire)
        out = (C.c_uint64 * 10)()
        self.assertTrue(self.lib.scratch_wire_decode_case(raw, len(wire), out))
        self.assertEqual(list(out), [1,1,2,434432,256,64577536,4096,1,434687,1])

    def test_partition_guid_parser_uses_canonical_text_byte_order(self):
        out=(C.c_uint8*16)()
        self.assertTrue(self.lib.guid_case(b'00000046-39c2-4488-9bb0-00cb43c9ccd4',out))
        self.assertEqual(bytes(out),bytes.fromhex('0000004639c244889bb000cb43c9ccd4'))
        for value in (None,b'',b'00000046-39c2-4488-9bb0-00cb43c9ccd',b'00000046-39c2-4488-9bb0-00cb43c9ccdz'):
            self.assertFalse(self.lib.guid_case(value,out))
