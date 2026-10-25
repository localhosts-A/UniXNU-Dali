"""Production read-only MT6991 I2C6 pin reader with register/error models."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbI2cPinsTests(unittest.TestCase):
    def test_production_i2c6_pins(self):
        with tempfile.TemporaryDirectory(prefix='dali-usb-pins-') as directory:
            target = Path(directory) / 'pins'
            command = [os.environ.get('CC', 'clang'), '-std=c11', '-O1', '-g',
                       '-Wall', '-Wextra', '-Werror', '-Wconversion',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I' + str(ROOT / 'drivers/DaliUsb/include'),
                       '-I' + str(GENERIC / 'drivers/UsbCore/include'),
                       str(GENERIC / 'drivers/UsbCore/UsbCore.c'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbI2cPins.c'),
                       str(ROOT / 'tests/fixtures/usb_i2c_pins_test.c'), '-o', str(target)]
            subprocess.run(command, check=True, timeout=60)
            subprocess.run([str(target)], check=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
