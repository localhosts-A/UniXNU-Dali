import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/source-kernel-cache.py"
spec = importlib.util.spec_from_file_location("source_cache", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SourceKernelCacheTests(unittest.TestCase):
    def test_exact_key_and_bytes_required_for_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            source = tree / ".cache/xnu-work"
            source.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", source], check=True)
            subprocess.run(["git", "-C", source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "pinned source"], check=True)
            kernel = tree / module.KERNEL
            kernel.parent.mkdir(parents=True)
            kernel.write_bytes(struct.pack("<8I2I", 0xfeedfacf, 0x100000c, 0, 2, 1, 8, 0, 0, 1, 8))

            def run(mode, key="a" * 64):
                return subprocess.run(["python3", SCRIPT, mode, "--tree", tree, "--key", key],
                                      capture_output=True, text=True)

            self.assertNotEqual(run("verify").returncode, 0, "missing stamp must rebuild")
            self.assertEqual(run("record").returncode, 0)
            self.assertEqual(run("verify").returncode, 0)
            self.assertNotEqual(run("verify", "b" * 64).returncode, 0, "different input must rebuild")
            kernel.write_bytes(kernel.read_bytes() + b"changed")
            self.assertNotEqual(run("verify").returncode, 0, "corrupted bytes must rebuild")
            kernel.write_bytes(b"invalid Mach-O")
            self.assertNotEqual(run("record").returncode, 0, "invalid output cannot create a cache stamp")
