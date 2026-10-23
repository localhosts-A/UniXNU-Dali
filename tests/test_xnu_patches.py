from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')


class XnuPatchFormatTests(unittest.TestCase):
    def test_dali_bootlog_patch_hunk_counts_match_body(self):
        lines = (ROOT / 'patches/xnu/dali-bootlog-personality.patch').read_text().splitlines()
        headers = [(index, HUNK.match(line)) for index, line in enumerate(lines) if HUNK.match(line)]
        self.assertEqual(len(headers), 1)

        start, header = headers[0]
        old_count = new_count = 0
        for line in lines[start + 1:]:
            if line.startswith('@@ ') or line.startswith('diff --git '):
                break
            if line.startswith(' '):
                old_count += 1
                new_count += 1
            elif line.startswith('-') and not line.startswith('---'):
                old_count += 1
            elif line.startswith('+') and not line.startswith('+++'):
                new_count += 1

        declared_old = int(header.group(1) or 1)
        declared_new = int(header.group(2) or 1)
        self.assertEqual((old_count, new_count), (declared_old, declared_new))


class BoardArchiveTests(unittest.TestCase):
    def test_kernel_archive_scripts_are_tree_script_basenames(self):
        board = tomllib.loads((ROOT / 'board.toml').read_text())
        archives = board.get('kernel', {}).get('archives', [])
        self.assertTrue(archives)
        for entry in archives:
            script = Path(entry.split()[0])
            with self.subTest(script=script):
                self.assertEqual(len(script.parts), 1)
                in_device = (ROOT / 'scripts' / script).is_file()
                in_unixnu = (ROOT.parent / 'UniXNU' / 'scripts' / script).is_file()
                self.assertTrue(in_device or in_unixnu, f'{script} is not copied into tree/scripts')


if __name__ == '__main__':
    unittest.main()
