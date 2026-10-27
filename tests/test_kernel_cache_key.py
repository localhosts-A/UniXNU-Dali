import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kernel_cache_key", ROOT / "scripts/kernel-cache-key.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class KernelCacheKeyTests(unittest.TestCase):
    def fingerprint(self, text):
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "board.toml"
            board.write_text(text)
            return module.board_fingerprint(board)

    def test_compiled_source_identity_includes_both_revision_pins(self):
        text = (ROOT / "board.toml").read_text()
        base = self.fingerprint(text)
        import tomllib
        board = tomllib.loads(text)
        self.assertNotEqual(base, self.fingerprint(text.replace(board["firmware"]["uefi_commit"], "f" * 40)))
        self.assertNotEqual(base, self.fingerprint(text.replace(board["firmware"]["unixnu_commit"], "e" * 40)))
        for old, new in [
            ('name = "dali"', 'name = "other"'),
            ('kit = "Dali"', 'kit = "Other"'),
            ('"build-dali-xnu-platform.py"', '"other-platform.py"'),
            ('defconfig = "dali_ipados_defconfig"', 'defconfig = "other_defconfig"'),
            ('build = "21H461"', 'build = "21H462"'),
            ('partition_name = "unixnu_data"', 'partition_name = "userdata"'),
            ('lun = 2', 'lun = 3'),
            ('size_mib = 24', 'size_mib = 25'),
        ]:
            with self.subTest(field=old):
                self.assertIn(old, text)
                self.assertNotEqual(base, self.fingerprint(text.replace(old, new)))
        self.assertNotEqual(base, self.fingerprint(text + '\n[future]\ninput = "changed"\n'))

    def test_formatting_does_not_change_semantic_board_fingerprint(self):
        text = (ROOT / "board.toml").read_text()
        self.assertEqual(self.fingerprint(text), self.fingerprint("# comment\n\n" + text))


class SourceGraphTests(unittest.TestCase):
    def git(self, path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.STDOUT)

    def commit(self, path):
        self.git(path, "add", "-A")
        self.git(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "inputs", "--allow-empty")

    def repo(self, path, files):
        path.mkdir()
        self.git(path, "init", "-q")
        for name, value in files.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        self.commit(path)
        return path

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.unixnu = self.repo(base / "UniXNU", {"feeds.toml": "# pinned sources",
            "scripts/unixnu/feeds.py": "# fetcher", "patches/xnu/entry.patch": "patch",
            "scripts/test-darwinboot.py": "fixture", "README.md": "docs"})
        self.device = self.repo(base / "Device", {"board.toml": (ROOT / "board.toml").read_text(),
            "configs/dali_defconfig": "config", "README.md": "docs"})

    def keys(self, toolchain="Xcode+SDK+LLVM", profile="default"):
        return module.fingerprints(self.unixnu, self.device, toolchain, profile)

    def change(self, root, path):
        (root / path).write_text((root / path).read_text() + "\nchanged")
        self.commit(root)

    def test_revision_changes_invalidate_compiled_identity_even_for_efi_only_sources(self):
        before = self.keys()
        self.change(self.unixnu, "scripts/test-darwinboot.py")
        self.assertNotEqual(before["kernel"], self.keys()["kernel"])
        self.assertNotEqual(before["edk"], self.keys()["edk"])
        self.change(self.device, "README.md")
        self.assertNotEqual(before["kernel"], self.keys()["kernel"])

    def test_patch_config_unknown_inputs_and_toolchain_invalidate(self):
        for root, path in [(self.unixnu, "patches/xnu/entry.patch"),
                           (self.device, "configs/dali_defconfig")]:
            before = self.keys()
            self.change(root, path)
            self.assertNotEqual(before["kernel"], self.keys()["kernel"])
            self.assertEqual(before["feeds"], self.keys()["feeds"])
        before = self.keys()
        (self.unixnu / "future-input").write_text("unknown build input")
        self.commit(self.unixnu)
        self.assertNotEqual(before["kernel"], self.keys()["kernel"])
        self.assertNotEqual(self.keys()["kernel"], self.keys("new SDK build")["kernel"])
        self.assertNotEqual(self.keys()["feeds"], self.keys("new SDK build")["feeds"])

    def test_feed_revision_invalidates_both_source_and_feed_caches(self):
        before = self.keys()
        self.change(self.unixnu, "feeds.toml")
        self.assertNotEqual(before["kernel"], self.keys()["kernel"])
        self.assertNotEqual(before["feeds"], self.keys()["feeds"])

    def test_selected_validation_profile_invalidates_only_kernel_cache(self):
        base = self.keys(profile="reset-qualification")
        root = self.keys(profile="ipados-root-compile")
        self.assertNotEqual(base["kernel"], root["kernel"])
        self.assertEqual(base["feeds"], root["feeds"])
        self.assertEqual(base["edk"], root["edk"])

    def test_dirty_or_untracked_input_is_rejected(self):
        (self.device / "local.toml").write_text("local overrides")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            self.keys()

    def test_only_darwinboot_gitlink_is_excluded(self):
        for name in ("DarwinBoot", "OtherDriver"):
            driver = self.repo(self.unixnu / name, {"source.c": "driver"})
            target = self.unixnu / "drivers" / name
            target.parent.mkdir(exist_ok=True)
            driver.rename(target)
            oid = self.git(target, "rev-parse", "HEAD").decode().strip()
            self.git(self.unixnu, "update-index", "--add", "--cacheinfo", f"160000,{oid},drivers/{name}")
            self.commit(self.unixnu)
            before = self.keys()
            self.change(target, "source.c")
            self.commit(self.unixnu)
            if name == "DarwinBoot":
                self.assertNotEqual(before["kernel"], self.keys()["kernel"])
                self.assertNotEqual(before["edk"], self.keys()["edk"])
            else:
                self.assertNotEqual(before["kernel"], self.keys()["kernel"])
                self.git(target, "checkout", "-q", "HEAD~1")
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.keys()
