"""Actual repeater C against PMIC byte/I2C/GPIO seams; no physical device."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbRepeaterTests(unittest.TestCase):
    def test_production_repeaters(self):
        compiler = shutil.which(os.environ.get('CC', 'clang'))
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='dali-usb-repeaters-') as tmp:
            binary = Path(tmp) / 'repeaters'
            command = [compiler, '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wconversion', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'drivers/DaliUsb/include'),
                       '-I', str(GENERIC / 'drivers/UsbCore/include'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbMt6379Repeater.c'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbNxpRepeater.c'),
                       str(ROOT / 'tests/fixtures/usb_repeater_test.c'), '-o', str(binary)]
            built = subprocess.run(command, text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn('PASS: production MT6379/NXP', ran.stdout)
            print(ran.stdout.strip())


if __name__ == '__main__':
    unittest.main()
