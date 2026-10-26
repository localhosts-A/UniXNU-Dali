import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from ufs_source_fixture import tree_fixture


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/source-kernel-cache.py"
spec = importlib.util.spec_from_file_location("source_cache", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from dali_source_identity import source_record


class SourceKernelCacheTests(unittest.TestCase):
    def test_exact_key_and_bytes_required_for_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            tree, _, _, source = tree_fixture(Path(directory))
            kernel = tree / module.KERNEL
            kernel.parent.mkdir(parents=True)
            pins, _ = module.build_source_identity(tree)
            kernel.write_bytes(struct.pack("<8I2I", 0xfeedfacf, 0x100000c, 0, 2, 1, 8, 0, 0, 1, 8) + source_record(pins))

            def run(mode, key="a" * 64):
                return subprocess.run(["python3", SCRIPT, mode, "--tree", tree, "--key", key],
                                      capture_output=True, text=True)

            self.assertNotEqual(run("verify").returncode, 0, "missing stamp must rebuild")
            self.assertEqual(run("record").returncode, 0)
            self.assertEqual(run("verify").returncode, 0)
            (source/'unknown.h').write_text('undeclared source')
            self.assertNotEqual(run("verify").returncode, 0, "undeclared XNU bytes must rebuild")
            (source/'unknown.h').unlink()
            self.assertNotEqual(run("verify", "b" * 64).returncode, 0, "different input must rebuild")
            kernel.write_bytes(kernel.read_bytes() + b"changed")
            self.assertNotEqual(run("verify").returncode, 0, "corrupted bytes must rebuild")
            kernel.write_bytes(b"invalid Mach-O")
            self.assertNotEqual(run("record").returncode, 0, "invalid output cannot create a cache stamp")
