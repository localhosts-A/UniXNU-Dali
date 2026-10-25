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
                # Managed worktrees add a nesting level, so the pinned generic
                # checkout may be beside an ancestor rather than ROOT.parent.
                in_unixnu = any((parent / 'UniXNU' / 'scripts' / script).is_file()
                                for parent in ROOT.parents)
                self.assertTrue(in_device or in_unixnu, f'{script} is not copied into tree/scripts')

    def test_corecrypto_plain_archive_precedes_namespaced_archive(self):
        board = tomllib.loads((ROOT / 'board.toml').read_text())
        archives = board['kernel']['archives']
        plain = archives.index('build-corecrypto-14.py')
        namespaced = archives.index('build-corecrypto-14.py --namespace')
        pthread = archives.index('build-libpthread-kernel.py')
        platform = archives.index('build-dali-xnu-platform.py')
        self.assertLess(plain, namespaced)
        self.assertLess(namespaced, pthread)
        self.assertLess(pthread, platform)

    def test_dali_builds_and_enables_the_real_source_pthread_provider(self):
        config = {
            line.split('=', 1)[0]: line.split('=', 1)[1]
            for line in (ROOT / 'configs/dali_defconfig').read_text().splitlines()
            if '=' in line and not line.startswith('#')
        }
        self.assertEqual(config.get('CONFIG_XNU_SOURCE_PTHREAD'), 'y')
        self.assertEqual(config.get('CONFIG_XNU_SOURCE_CORECRYPTO14'), 'y')
        archives = tomllib.loads((ROOT / 'board.toml').read_text())['kernel']['archives']
        self.assertIn('build-libpthread-kernel.py', archives)


class BootMenuConfigTests(unittest.TestCase):
    def test_hardware_and_board_menu_keep_visible_auto_xnu_timeout(self):
        hardware = tomllib.loads((ROOT / 'package/unixnu.toml').read_text())['boot']
        board = tomllib.loads((ROOT / 'board.toml').read_text())['payload']
        self.assertEqual(hardware['default'], 'xnu')
        self.assertFalse(hardware['connect_all'])
        self.assertGreaterEqual(hardware['timeout'], 10)
        self.assertEqual(board['timeout'], hardware['timeout'])


if __name__ == '__main__':
    unittest.main()
