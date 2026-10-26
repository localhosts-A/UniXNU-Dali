import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from dali_xnu_source import verify_source_tree, restored_headers, declared_patches


class XnuSourceTests(unittest.TestCase):
    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.repo = self.root/'xnu'; self.repo.mkdir()
        self.git('init', '-q')
        (self.repo/'kernel.c').write_text('upstream\n')
        (self.repo/'other.c').write_text('unchanged\n')
        (self.repo/'.gitignore').write_text('ignored.h\n')
        self.git('add', '-A')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'upstream')
        (self.repo/'kernel.c').write_text('declared patch\n')
        self.patch = self.root/'declared.patch'; self.patch.write_bytes(self.git('diff'))
        self.header = 'iokit/DriverKit/restored.h'
        target = self.repo/self.header; target.parent.mkdir(parents=True); target.write_bytes(b'public header\n')
        self.blob = hashlib.sha1(b'blob 14\0public header\n').hexdigest()

    def check(self):
        return verify_source_tree(self.repo, [self.patch], {self.header: self.blob})

    def test_exact_declared_patch_and_fixed_blob_pass_without_changing_real_index(self):
        before = self.git('status', '--porcelain'), self.git('ls-files', '--stage')
        result = self.check()
        self.assertTrue(result['untracked_and_ignored_files_checked'])
        self.assertEqual(len(result['git_tree']), 40)
        self.assertEqual(before, (self.git('status', '--porcelain'), self.git('ls-files', '--stage')))

    def test_extra_tracked_untracked_and_ignored_source_bytes_are_rejected(self):
        for name in ('other.c', 'unknown.h', 'ignored.h'):
            target = self.repo/name; previous = target.read_bytes() if target.exists() else None
            target.write_bytes(b'undeclared\n')
            with self.assertRaisesRegex(ValueError, 'differs from HEAD'):
                self.check()
            if previous is None: target.unlink()
            else: target.write_bytes(previous)

    def test_wrong_blob_missing_header_and_symlink_refuse(self):
        target = self.repo/self.header; target.write_bytes(b'wrong header')
        with self.assertRaisesRegex(ValueError, 'fixed blob'):
            self.check()
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'missing restored'):
            self.check()
        target.symlink_to(self.repo/'other.c')
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.check()

    def test_header_index_requires_fixed_tag_url_names_and_unique_blobs(self):
        cache = self.root/'.cache/sources'; cache.mkdir(parents=True)
        for name in ('xnu-driverkit-1.0.json', 'xnu-driverkit-crypto-1.0.json', 'xnu-trustcache-2.0.json'):
            (cache/name).write_text('[]')
        index = cache/'xnu-driverkit-1.0.json'
        row = dict(type='file', name='restored.h', sha=self.blob,
            download_url='https://raw.githubusercontent.com/apple-oss-distributions/xnu/xnu-1.0/'+self.header)
        index.write_text(json.dumps([row]))
        headers, _ = restored_headers(self.root, dict(driverkit_tag='xnu-1.0', trustcache_tag='xnu-2.0'))
        self.assertEqual(headers, {self.header: self.blob})
        for rows in ([row, row], [dict(row, name='../bad.h')], [dict(row, download_url='https://example.invalid/header')],
                     [dict(row, sha='not a Git blob')]):
            index.write_text(json.dumps(rows))
            with self.assertRaises(ValueError):
                restored_headers(self.root, dict(driverkit_tag='xnu-1.0', trustcache_tag='xnu-2.0'))

    def test_device_overlay_precedes_generic_then_nativeprelink_stack(self):
        device, generic = self.root/'device', self.root/'generic'
        for directory in [device/'patches/xnu', generic/'patches/xnu', generic/'kits/NativePrelink/patches']:
            directory.mkdir(parents=True)
        for path in [device/'patches/xnu/a.patch', generic/'patches/xnu/a.patch', generic/'patches/xnu/b.patch',
                     generic/'kits/NativePrelink/patches/c.patch']:
            path.write_text('patch')
        self.assertEqual(declared_patches(device, generic), [device/'patches/xnu/a.patch', generic/'patches/xnu/b.patch',
            generic/'kits/NativePrelink/patches/c.patch'])
