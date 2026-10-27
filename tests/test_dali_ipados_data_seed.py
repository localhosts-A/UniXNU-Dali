import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


DEVICE = Path(__file__).resolve().parents[1]
UNIXNU = DEVICE.parent / "UniXNU"
SCRIPT = DEVICE / "scripts/build-dali-ipados-data-seed.py"
spec = importlib.util.spec_from_file_location("build_dali_ipados_data_seed", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
sys.path.insert(0, str(UNIXNU / "drivers/DarwinBoot/Test"))
import packed_ramdisk


class DaliIpadosDataSeedTests(unittest.TestCase):
    def test_base_seed_refuses_an_unpinned_os_root_before_running_macos_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os_root = root / "os-root"
            (os_root / "sbin").mkdir(parents=True)
            (os_root / "sbin/launchd").write_bytes(b"not 21H461")
            result = subprocess.run([sys.executable, UNIXNU / "scripts/prepare-owner-data-fixture.py",
                                     "--os-root", os_root, "--output", root / "out"],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exact iPad7,1", result.stderr)

    def test_os_dmg_pin_is_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "os.dmg"
            path.write_bytes(b"pinned OS input")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(builder.validate_os_dmg(path, digest), digest)
            with self.assertRaisesRegex(SystemExit, "SHA-256 mismatch"):
                builder.validate_os_dmg(path, "00" * 32)

    def test_seed_must_be_packed_and_within_payload_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "seed.raw"
            with raw.open("wb") as output:
                output.truncate(builder.MAX_DATA_LOGICAL_BYTES)
                output.seek(32)
                output.write(b"NXSB")
                output.seek(4096)
                output.write(b"A" * 4096)
            packed = root / "seed.dbr"
            packed_ramdisk.pack(raw, packed)

            metadata = builder.inspect_packed_seed(
                packed, packed_ramdisk, builder.MAX_DATA_LOGICAL_BYTES, packed.stat().st_size)
            self.assertTrue(metadata["packed"])
            self.assertEqual(metadata["bytes"], builder.MAX_DATA_LOGICAL_BYTES)
            with self.assertRaisesRegex(ValueError, "payload budget"):
                builder.inspect_packed_seed(packed, packed_ramdisk, builder.MAX_DATA_LOGICAL_BYTES,
                                            packed.stat().st_size - 1)
            with self.assertRaisesRegex(ValueError, "packed DBRDISK1"):
                builder.inspect_packed_seed(raw, packed_ramdisk, builder.MAX_DATA_LOGICAL_BYTES,
                                            builder.MAX_DATA_LOGICAL_BYTES)

    def test_seed_logical_size_must_match_the_board_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "seed.raw"
            raw.write_bytes(bytes(4096))
            packed = root / "seed.dbr"
            packed_ramdisk.pack(raw, packed)
            with self.assertRaisesRegex(ValueError, "expected packed DBRDISK1"):
                builder.inspect_packed_seed(packed, packed_ramdisk, builder.MAX_DATA_LOGICAL_BYTES,
                                            24 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
