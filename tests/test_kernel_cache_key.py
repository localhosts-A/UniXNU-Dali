import importlib.util
from pathlib import Path
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

    def test_only_uefi_revision_can_change_without_invalidating_kernel(self):
        text = (ROOT / "board.toml").read_text()
        base = self.fingerprint(text)
        import tomllib
        board = tomllib.loads(text)
        self.assertEqual(base, self.fingerprint(text.replace(board["firmware"]["uefi_commit"], "f" * 40)))
        for old, new in [
            (board["firmware"]["unixnu_commit"], "e" * 40),
            ('name = "dali"', 'name = "other"'),
            ('kit = "Dali"', 'kit = "Other"'),
            ('"build-dali-xnu-platform.py"', '"other-platform.py"'),
            ('defconfig = "dali_defconfig"', 'defconfig = "other_defconfig"'),
            ('size_mib = 24', 'size_mib = 25'),
        ]:
            with self.subTest(field=old):
                self.assertIn(old, text)
                self.assertNotEqual(base, self.fingerprint(text.replace(old, new)))
        self.assertNotEqual(base, self.fingerprint(text + '\n[future]\ninput = "changed"\n'))

    def test_formatting_does_not_change_semantic_board_fingerprint(self):
        text = (ROOT / "board.toml").read_text()
        self.assertEqual(self.fingerprint(text), self.fingerprint("# comment\n\n" + text))
