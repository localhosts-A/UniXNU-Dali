import importlib.util
import json
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

            def run(mode, key="a" * 64, profile="reset-qualification"):
                return subprocess.run(["python3", SCRIPT, mode, "--tree", tree, "--key", key,
                                       "--profile", profile],
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

    def test_qemu_root_profile_keeps_exact_pins_and_rejects_embedded_physical_ufs(self):
        with tempfile.TemporaryDirectory() as directory:
            tree, _, _, _ = tree_fixture(Path(directory))
            kernel = tree / module.KERNEL
            kernel.parent.mkdir(parents=True)
            header = struct.pack("<8I2I", 0xfeedfacf, 0x100000c, 0, 2, 1, 8, 0, 0, 1, 8)
            kernel.write_bytes(header)
            stamp = module.snapshot(tree, "a" * 64, "ipados-qemu-root")
            self.assertEqual(stamp["profile"], "ipados-qemu-root")
            self.assertEqual(stamp["source_identity_mode"], "exact-build-input-pins-not-embedded-qemu-only")
            self.assertTrue(stamp["compiled_source_pins"])
            with self.assertRaises(ValueError):
                module.snapshot(tree, "a" * 64, "reset-qualification")
            kernel.write_bytes(header + b"DALIUFSSRC1")
            with self.assertRaisesRegex(ValueError, "unlinked Dali UFS"):
                module.snapshot(tree, "a" * 64, "ipados-qemu-root")

    def test_donor_runtime_profile_records_pins_without_an_unlinked_ufs_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            tree, _, _, _ = tree_fixture(Path(directory))
            kernel = tree / module.KERNEL
            kernel.parent.mkdir(parents=True)
            kernel.write_bytes(struct.pack("<8I2I", 0xfeedfacf, 0x100000c, 0, 2, 1, 8, 0, 0, 1, 8))

            def run(mode, profile="ipados-donor-runtime-audit"):
                return subprocess.run(["python3", SCRIPT, mode, "--tree", tree, "--key", "a" * 64,
                                      "--profile", profile], capture_output=True, text=True)

            self.assertEqual(run("record").returncode, 0)
            self.assertEqual(run("verify").returncode, 0)
            stamp = json.loads((tree / ".cache/source-kernel-cache.json").read_text())
            self.assertEqual(stamp["schema"], 3)
            self.assertEqual(stamp["profile"], "ipados-donor-runtime-audit")
            self.assertEqual(stamp["source_identity_mode"], "exact-build-input-pins-not-embedded-audit-only")
            self.assertNotEqual(run("verify", profile="reset-qualification").returncode, 0,
                                "audit-only stamp cannot authorize another profile")

            source = tree / "drivers/DaliUfs/DaliUfs.cpp"
            source.write_text(source.read_text() + "\n// dirty source\n")
            self.assertNotEqual(run("verify").returncode, 0,
                                "audit mode still requires every pinned source tree to be clean")
