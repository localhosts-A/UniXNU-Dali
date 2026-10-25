"""Actual XS-PHY and repeaters; register/PMIC-byte/I2C seams, no hardware."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbPhyTests(unittest.TestCase):
    def test_production_phy(self):
        compiler = shutil.which(os.environ.get('CC', 'clang'))
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='dali-usb-phy-') as tmp:
            binary = Path(tmp) / 'phy'
            command = [compiler, '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wconversion', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'drivers/DaliUsb/include'),
                       '-I', str(GENERIC / 'drivers/UsbCore/include'),
                       str(GENERIC / 'drivers/UsbCore/UsbCore.c'),
                       *[str(ROOT / 'drivers/DaliUsb' / (unit + '.c')) for unit in
                         ('DaliUsbPhy', 'DaliUsbMt6379Repeater', 'DaliUsbNxpRepeater')],
                       str(ROOT / 'tests/fixtures/usb_phy_test.c'), '-o', str(binary)]
            built = subprocess.run(command, text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn('PASS: production Dali XS-PHY', ran.stdout)
            print(ran.stdout.strip())


if __name__ == '__main__':
    unittest.main()
