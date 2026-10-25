"""Actual SID5 PMIF/VUSB code with register/lease/failure models; no device."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GENERIC = Path(os.environ.get('UNIXNU_ROOT', ROOT.parent / 'UniXNU'))


class UsbVusbTests(unittest.TestCase):
    def test_production_vusb_pmif(self):
        with tempfile.TemporaryDirectory(prefix='dali-usb-vusb-') as directory:
            target = Path(directory) / 'vusb'
            command = [os.environ.get('CC', 'clang'), '-std=c11', '-O1', '-g',
                       '-Wall', '-Wextra', '-Werror', '-Wconversion',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I' + str(ROOT / 'drivers/DaliUsb/include'),
                       '-I' + str(GENERIC / 'drivers/UsbCore/include'),
                       str(GENERIC / 'drivers/UsbCore/UsbCore.c'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbPmif.c'),
                       str(ROOT / 'drivers/DaliUsb/DaliUsbVusb.c'),
                       str(ROOT / 'tests/fixtures/usb_vusb_test.c'), '-o', str(target)]
            subprocess.run(command, check=True, timeout=60)
            subprocess.run([str(target)], check=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
