"""Production ATAG decoder; no device access or private fixture data."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbCalibrationTests(unittest.TestCase):
    def test_production_calibration(self):
        compiler = shutil.which(os.environ.get('CC', 'clang'))
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='dali-usb-calibration-') as tmp:
            binary = Path(tmp) / 'calibration'
            command = [compiler, '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wconversion', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'drivers/DaliUsb/include'),
                       '-I', str(GENERIC / 'drivers/UsbCore/include'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbCalibration.c'),
                       str(ROOT / 'tests/fixtures/usb_calibration_test.c'), '-o', str(binary)]
            built = subprocess.run(command, text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn('PASS: production Dali ATAG calibration decoder', ran.stdout)
            print(ran.stdout.strip())


if __name__ == '__main__':
    unittest.main()
