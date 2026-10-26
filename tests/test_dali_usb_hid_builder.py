import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('usb_hid_builder', ROOT / 'scripts/build-dali-usb-hid.py')
HID = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HID)

class DaliUsbHidBuilderTests(unittest.TestCase):
    def test_usb_profile_builds_and_force_loads_source_hid_family(self):
        board = tomllib.loads((ROOT / 'board.toml').read_text())
        self.assertIn('build-source-hid.py', board['kernel']['archives'])
        profile = (ROOT / 'configs/dali_usb_defconfig').read_text()
        for fragment in (
            'CONFIG_XNU_BUILD_ARCHIVES=y',
            'CONFIG_XNU_SOURCE_HID_FAMILY=y',
            '-Wl,-force_load,{tree}/build/source-hid/libIOHIDFamily.a',
            '{tree}/build/source-hid/libIOHIDFamily.a',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, profile)

    def test_disabled_skips_apple_tools_and_enabled_requires_source_family(self):
        with patch.dict(os.environ, {'XNU_NATIVE_USB_HOST': '0'}), redirect_stdout(io.StringIO()) as output:
            HID.main([])
        self.assertIn('SKIP', output.getvalue())
        with patch.dict(os.environ, {'XNU_NATIVE_USB_HOST': '1', 'XNU_SOURCE_HID_FAMILY': '0'}):
            with self.assertRaises(SystemExit):
                HID.main([])

    def test_kernel_symbol_verifier_requires_concrete_child_and_report_functions(self):
        symbols = '\n'.join('00000000 T _' + name for name in HID.SYMBOLS)
        HID.require_symbols(symbols, HID.SYMBOLS)
        with self.assertRaises(ValueError):
            HID.require_symbols(symbols.replace('_dali_usb_hid_submit', '_wrong_submit'), HID.SYMBOLS)

    def test_actual_child_lifecycle_with_test_only_iokit_seam(self):
        generic = ROOT.parent / 'UniXNU'
        self.assertTrue((generic / 'drivers/UsbCore/UsbHost.c').is_file())
        compiler = shutil.which(os.environ.get('CC', 'clang'))
        cpp = shutil.which('g++' if Path(compiler).name == 'gcc' else 'clang++')
        self.assertIsNotNone(cpp)
        model = ROOT / 'tests/usb_hid_model'
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            for header in ('IOKit/hid/IOHIDDevice.h', 'IOKit/IOBufferMemoryDescriptor.h',
                           'IOKit/IOLib.h', 'IOKit/IOLocks.h'):
                path = out / header
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#include "IOKitModel.h"\n')
            flags = ['-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                     '-fno-omit-frame-pointer', '-pthread', '-I', str(generic / 'drivers/UsbCore/include')]
            objects = []
            for name in ('UsbCore', 'UsbHost'):
                obj = out / (name + '.o')
                result = subprocess.run([compiler, '-std=c11', *flags, '-c',
                                         str(generic / 'drivers/UsbCore' / (name + '.c')), '-o', str(obj)],
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                objects.append(str(obj))
            binary = out / 'lifecycle'
            result = subprocess.run([cpp, '-std=c++17', *flags, '-I', str(model), '-I', str(out),
                                     '-I', str(ROOT / 'drivers/DaliUsbHid'),
                                     str(ROOT / 'drivers/DaliUsbHid/DaliUsbHid.cpp'), str(model / 'lifecycle.cpp'),
                                     *objects, '-o', str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS: production USB IOHID child', result.stdout)

if __name__ == '__main__':
    unittest.main()
