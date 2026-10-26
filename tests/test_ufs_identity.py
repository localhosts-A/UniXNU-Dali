"""Identity fields and digests must agree with bounded original sysfs captures."""
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import ufs_identity as identity


CONTROLLER = "/sys/devices/platform/soc/16810000.ufshci"


def fixture(directory, serial=b"ABC123\n"):
    raw = dict(manufacturer_id=b"0x1234\n", serial_number=serial)
    record, digest = identity.observation(CONTROLLER, raw)
    baseline = dict(ufs_device_identity=record, ufs_device_binding_sha256=digest,
                    scsi_path=CONTROLLER+"/host0/target0:0:0/0:0:0:2", captures={})
    for field, (_, filename, _) in identity.ATTRIBUTES.items():
        (Path(directory)/filename).write_bytes(raw[field])
        baseline["captures"]["ufs_"+field] = dict(file=filename, bytes=len(raw[field]),
                                                  sha256=hashlib.sha256(raw[field]).hexdigest())
    return baseline


class UfsIdentityTests(unittest.TestCase):
    def test_raw_capture_reconstructs_same_xnu_digest_and_preserves_serial_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline = fixture(temp, b" ABC123 \n")
            self.assertEqual(baseline["ufs_device_identity"]["serial_number"], " ABC123 ")
            expected = bytes.fromhex("1234") + bytes((18, 5)) + " ABC123 ".encode("utf-16-be")
            self.assertEqual(identity.validate_captured_identity(baseline, temp), hashlib.sha256(expected).hexdigest())
            # Stripping spaces must produce a different device binding.
            plain = dict(baseline, ufs_device_binding_sha256=hashlib.sha256(
                bytes.fromhex("1234")+bytes((14, 5))+"ABC123".encode("utf-16-be")).hexdigest())
            with self.assertRaisesRegex(ValueError, "digest differs"):
                identity.validate_captured_identity(plain, temp)

    def test_unknown_format_digest_only_metadata_mismatch_and_other_controller_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            base = fixture(temp)
            variants = [dict(base, ufs_device_identity=None), dict(base, captures={}),
                        dict(base, ufs_device_identity=dict(base["ufs_device_identity"], schema=True)),
                        dict(base, ufs_device_identity=dict(base["ufs_device_identity"], algorithm="unknown")),
                        dict(base, ufs_device_identity=dict(base["ufs_device_identity"], serial_number="OTHER")),
                        dict(base, scsi_path="/sys/other/16810000.ufshci/host0/0:0:0:2"),
                        dict(base, ufs_device_binding_sha256="ab"*32)]
            for bad in variants:
                with self.subTest(bad=bad.keys()), self.assertRaises(ValueError):
                    identity.validate_captured_identity(bad, temp)

    def test_capture_length_digest_filename_and_bytes_tampering_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            base = fixture(temp)
            for change in [dict(file="../ufs-serial-number.txt"), dict(bytes=True), dict(bytes=128),
                           dict(bytes=1), dict(sha256="00"*32), dict(sha256="invalid")]:
                bad = copy.deepcopy(base)
                bad["captures"]["ufs_serial_number"].update(change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    identity.validate_captured_identity(bad, temp)
            (Path(temp)/"ufs-serial-number.txt").write_bytes(b"OTHER\n")
            with self.assertRaises(ValueError):
                identity.validate_captured_identity(base, temp)

    def test_symlink_fifo_and_oversized_file_are_rejected_without_unbounded_read(self):
        with tempfile.TemporaryDirectory() as temp:
            base = fixture(temp)
            serial = Path(temp)/"ufs-serial-number.txt"
            outside = Path(temp)/"outside.txt"; serial.rename(outside); serial.symlink_to(outside)
            with self.assertRaises(OSError):
                identity.validate_captured_identity(base, temp)
            serial.unlink(); os.mkfifo(serial)
            with self.assertRaisesRegex(ValueError, "regular file"):
                identity.validate_captured_identity(base, temp)
            serial.unlink(); serial.write_bytes(b"a"*4096)
            with self.assertRaises(ValueError):
                identity.validate_captured_identity(base, temp)

    def test_sysfs_control_bytes_lossy_unicode_or_extra_newlines_are_not_normalized(self):
        for value in [b"", b"\n", b"ABC123\n\n", b"ABC\x00123\n", b"ABC\r\n", "é".encode(), b"a"*128]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                identity.observation(CONTROLLER, dict(manufacturer_id=b"0x1234\n", serial_number=value))
        for value in [b"0x1234\n\n", b"1234", b" 0x1234", b"0x1234 "]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                identity.observation(CONTROLLER, dict(manufacturer_id=value, serial_number=b"ABC123\n"))

    def test_absent_capture_folder_is_not_a_digest_only_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            base = fixture(temp)
            with self.assertRaises(ValueError):
                identity.validate_captured_identity(base, None)
            with self.assertRaises(OSError):
                identity.validate_captured_identity(base, Path(temp)/"missing")
