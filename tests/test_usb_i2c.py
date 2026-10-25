"""Actual I2C6/GPIO/NXP production C against hardware register models only."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbI2cTests(unittest.TestCase):
    def test_production_i2c6(self):
        compiler = shutil.which(os.environ.get('CC', 'clang'))
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='dali-usb-i2c-') as tmp:
            binary = Path(tmp) / 'i2c'
            command = [compiler, '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wconversion', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'drivers/DaliUsb/include'),
                       '-I', str(GENERIC / 'drivers/UsbCore/include'),
                       str(GENERIC / 'drivers/UsbCore/UsbCore.c'),
                       *[str(ROOT / 'drivers/DaliUsb' / (unit + '.c')) for unit in
                         ('DaliUsbI2c', 'DaliUsbGpio', 'DaliUsbNxpRepeater')],
                       str(ROOT / 'tests/fixtures/usb_i2c6_test.c'), '-o', str(binary)]
            built = subprocess.run(command, text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn('PASS: production MT6991 I2C6/GPIO72/NXP', ran.stdout)
            print(ran.stdout.strip())


if __name__ == '__main__':
    unittest.main()
