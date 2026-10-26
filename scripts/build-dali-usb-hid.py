#!/usr/bin/env python3
"""Build the concrete source IOHIDDevice child for USB Boot-protocol reports."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/dali-usb-hid'
FAMILY = ROOT / 'build/source-hid'
SOURCE = ROOT / 'drivers/DaliUsbHid/DaliUsbHid.cpp'
ARCHIVE = OUT / 'libDaliUsbHid.a'
REVISION = 'b126cf696bf1c6eede740b92d3bfca7e62d09c06'
SYMBOLS = ('DaliUsbHid10gMetaClassE', 'dali_usb_hid_publish', 'dali_usb_hid_submit',
           'dali_usb_hid_quiesce', 'dali_usb_hid_detach', 'dali_usb_hid_terminate')

def helpers():
    spec = importlib.util.spec_from_file_location('usb_hid_build_helpers', ROOT / 'scripts/build-source-storage.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def require_symbols(output, symbols):
    actual = {line.split()[-1].lstrip('_') for line in output.splitlines() if line.split()}
    for name in symbols:
        present = any(name in symbol for symbol in actual) if name.endswith('10gMetaClassE') else name in actual
        if not present:
            raise ValueError('USB HID object lacks production symbol: ' + name)

def build():
    helper = helpers()
    family_report = json.loads((FAMILY / 'build-report.json').read_text())
    if (family_report.get('source_revision') != REVISION or not family_report.get('source_archive_built') or
            family_report.get('archive_sha256') != helper.digest(FAMILY / 'libIOHIDFamily.a')):
        raise ValueError('Build the fixed source IOHIDFamily before its USB child')
    record_path, record = helper.compiler_record(ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837')
    xnu = ROOT / '.cache/xnu-work'
    sdk = Path(subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip())
    includes = [FAMILY / 'include', FAMILY / 'source/IOHIDFamily', FAMILY / 'source/IOHIDSystem',
                ROOT / 'drivers/UsbCore/include', xnu / 'iokit', ROOT / 'build/storage-abi/include',
                sdk / 'System/Library/Frameworks/Kernel.framework/Headers']
    OUT.mkdir(parents=True, exist_ok=True)
    obj = OUT / 'DaliUsbHid.o'
    obj.unlink(missing_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    (OUT / 'build-report.json').unlink(missing_ok=True)
    command = helper.compile_command(record, SOURCE, obj, includes)
    log = OUT / 'DaliUsbHid.o.log'
    (OUT / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    helper.run_logged(command, Path(record['directory']), log)
    helper.validate_object(obj)
    subprocess.run(['xcrun', 'ar', 'rcs', str(ARCHIVE), str(obj)], check=True)
    require_symbols(subprocess.check_output(['xcrun', 'nm', '-gU', ARCHIVE], text=True), SYMBOLS)
    report = {'io_hid_family_revision': REVISION, 'source_sha256': helper.digest(SOURCE),
              'public_header_sha256': helper.digest(SOURCE.with_suffix('.h')),
              'family_build_report_sha256': helper.digest(FAMILY / 'build-report.json'),
              'compile_record_sha256': helper.digest(record_path), 'object_sha256': helper.digest(obj),
              'compile_log_sha256': helper.digest(log), 'archive_sha256': helper.digest(ARCHIVE),
              'command': command, 'source_archive_built': True, 'kernel_link_verified': False,
              'hid_runtime_qualified': False, 'controller_owner_installed': False, 'hardware_qualified': False}
    (OUT / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: actual USB IOHIDDevice child archive built; kernel/runtime require verification')

def verify_kernel(kernel):
    helper = helpers()
    report = json.loads((OUT / 'build-report.json').read_text())
    if (not report.get('source_archive_built') or report.get('io_hid_family_revision') != REVISION or
            report.get('archive_sha256') != helper.digest(ARCHIVE)):
        raise ValueError('USB HID archive/report mismatch')
    symbols = subprocess.check_output(['xcrun', 'nm', '-gU', kernel], text=True)
    require_symbols(symbols, SYMBOLS + ('IOHIDDevice10gMetaClassE', 'usb_host_read_boot_report',
                                      'usb_host_boot_report_descriptor'))
    report.update(kernel_link_verified=True, kernel_sha256=helper.digest(kernel))
    (OUT / 'kernel-link-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: real source USB IOHIDDevice and report API linked; runtime/hardware unqualified')

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-kernel', type=Path)
    args = parser.parse_args(argv)
    mode = os.environ.get('XNU_NATIVE_USB_HOST', '0')
    if mode == '0':
        print('SKIP: USB HID disabled')
    elif mode != '1' or os.environ.get('XNU_SOURCE_HID_FAMILY', '0') != '1':
        raise SystemExit('USB HID requires XNU_NATIVE_USB_HOST=1 and XNU_SOURCE_HID_FAMILY=1')
    elif args.verify_kernel:
        verify_kernel(args.verify_kernel.resolve())
    else:
        build()

if __name__ == '__main__':
    main()
