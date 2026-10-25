"""Actual MT6991 I2C6/APDMA ordinary gate production code, register models."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbI2cClocksTests(unittest.TestCase):
    def test_production_i2c6_clocks(self):
        with tempfile.TemporaryDirectory(prefix='dali-i2c-clocks-') as directory:
            target = Path(directory) / 'clocks'
            command = [os.environ.get('CC', 'clang'), '-std=c11', '-O1', '-g',
                       '-Wall', '-Wextra', '-Werror', '-Wconversion',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I' + str(ROOT / 'drivers/DaliUsb/include'),
                       '-I' + str(GENERIC / 'drivers/UsbCore/include'),
                       str(GENERIC / 'drivers/UsbCore/UsbCore.c'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbI2cClocks.c'),
                       str(ROOT / 'tests/fixtures/usb_i2c_clocks_test.c'), '-o', str(target)]
            subprocess.run(command, check=True, timeout=60)
            subprocess.run([str(target)], check=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
