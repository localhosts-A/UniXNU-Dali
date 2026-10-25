"""Actual MT6991 USB-domain production C; register model, no device I/O."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbDomainTests(unittest.TestCase):
    def test_production_domains(self):
        compiler = shutil.which(os.environ.get('CC', 'clang'))
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='dali-usb-domains-') as tmp:
            binary = Path(tmp) / 'domains'
            command = [compiler, '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wconversion', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'drivers/DaliUsb/include'),
                       '-I', str(GENERIC / 'drivers/UsbCore/include'),
                       str(GENERIC / 'drivers/UsbCore/UsbCore.c'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbDomains.c'),
                       str(ROOT / 'tests/fixtures/usb_domains_test.c'), '-o', str(binary)]
            built = subprocess.run(command, text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn('PASS: production MT6991 USB domains', ran.stdout)
            print(ran.stdout.strip())


if __name__ == '__main__':
    unittest.main()
