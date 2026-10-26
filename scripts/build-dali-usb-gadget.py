#!/usr/bin/env python3
"""Build and verify the opt-in portable MTU3 USB device/CDC core for source XNU."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837"
OUT = ROOT / "build/dali-usb-gadget"
ARCHIVE = OUT / "libDaliUsbGadget.a"
SOURCES = tuple(ROOT / "drivers/UsbCore" / name for name in (
    "UsbMtu3Qmu.c", "UsbMtu3Ep0.c", "UsbMtu3Endpoints.c", "UsbMtu3Device.c")) + (
    ROOT / "drivers/DaliUsbGadgetLog/DaliUsbCdcLog.c",
)
REQUIRED_SYMBOLS = (
    "usb_qmu_init", "usb_qmu_submit", "usb_qmu_start", "usb_qmu_reap",
    "usb_qmu_stop", "usb_qmu_cancel", "usb_qmu_destroy",
    "usb_mtu3_ep0_init", "usb_mtu3_ep0_bus_reset", "usb_mtu3_ep0_start",
    "usb_mtu3_ep0_stop", "usb_mtu3_ep0_poll",
    "usb_mtu3_endpoints_init", "usb_mtu3_endpoints_configure",
    "usb_mtu3_endpoints_deconfigure", "usb_mtu3_endpoints_halt",
    "usb_mtu3_device_init", "usb_mtu3_device_start", "usb_mtu3_device_stop",
    "usb_mtu3_device_destroy", "usb_mtu3_device_poll", "usb_mtu3_device_log",
    "dali_usb_cdc_log_bind", "dali_usb_cdc_log_unbind", "dali_usb_cdc_log_write",
    "dali_usb_cdc_log_dropped",
)


def host_builder():
    path = ROOT / "scripts/build-dali-usb-host.py"
    spec = importlib.util.spec_from_file_location("dali_usb_host_build_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def missing_symbols(output, required):
    actual = {line.split()[-1].lstrip("_") for line in output.splitlines() if line.split()}
    return sorted(name for name in required if name not in actual)


def check_kernel_symbols(defined, undefined):
    missing = missing_symbols(defined, REQUIRED_SYMBOLS)
    unresolved = sorted(set(REQUIRED_SYMBOLS) - set(missing_symbols(undefined, REQUIRED_SYMBOLS)))
    if missing or unresolved:
        raise ValueError("USB gadget kernel link mismatch; missing=" + ",".join(missing) +
                         " unresolved=" + ",".join(unresolved))
    return list(REQUIRED_SYMBOLS)


def build():
    helper = host_builder()
    missing_sources = [str(path) for path in SOURCES if not path.is_file()]
    if missing_sources:
        raise ValueError("USB gadget source units are missing: " + ", ".join(missing_sources))
    record_path, record = helper.compiler_record(OBJECTS)
    include = ROOT / "drivers/UsbCore/include"
    OUT.mkdir(parents=True, exist_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    (OUT / "build-report.json").unlink(missing_ok=True)
    objects, rows, commands = [], [], []
    for source in SOURCES:
        obj = OUT / (source.stem + ".o")
        obj.unlink(missing_ok=True)
        command, cwd = helper.compile_command(record_path, record, source, obj, include)
        log = OUT / (obj.name + ".log")
        helper.run_logged(command, cwd, log)
        helper.validate_object(obj)
        objects.append(obj)
        commands.append(command)
        rows.append({
            "source": str(source.relative_to(ROOT)),
            "source_sha256": digest(source),
            "object": str(obj.relative_to(ROOT)),
            "object_sha256": digest(obj),
            "compile_log": str(log.relative_to(ROOT)),
            "compile_log_sha256": digest(log),
        })
    (OUT / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    subprocess.run(["xcrun", "ar", "rcs", str(ARCHIVE), *map(str, objects)], check=True)
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(ARCHIVE)], text=True)
    missing = missing_symbols(defined, REQUIRED_SYMBOLS)
    if missing:
        raise ValueError("USB gadget archive lacks production symbols: " + ", ".join(missing))
    report = {
        "scope": "source-XNU archive compile/link only; optional log sink is unbound without a provider",
        "compile_record": str(record_path.relative_to(ROOT)),
        "compile_record_sha256": digest(record_path),
        "objects": rows,
        "archive": str(ARCHIVE.relative_to(ROOT)),
        "archive_sha256": digest(ARCHIVE),
        "required_symbols": list(REQUIRED_SYMBOLS),
        "source_archive_built": True,
        "kernel_link_verified": False,
        "gadget_runtime_qualified": False,
        "controller_started": False,
        "hardware_qualified": False,
    }
    (OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: MTU3 gadget/CDC core archive built; no device controller is started")


def verify_kernel(kernel):
    kernel = kernel.resolve()
    report_path = OUT / "build-report.json"
    report = json.loads(report_path.read_text())
    if (report.get("source_archive_built") is not True or
            report.get("required_symbols") != list(REQUIRED_SYMBOLS) or
            report.get("archive_sha256") != digest(ARCHIVE) or
            len(report.get("objects", [])) != len(SOURCES) or
            [row.get("source_sha256") for row in report["objects"]] !=
            [digest(source) for source in SOURCES]):
        raise ValueError("Dali USB gadget archive/report mismatch")
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(kernel)], text=True)
    undefined = subprocess.check_output(["xcrun", "nm", "-u", str(kernel)], text=True)
    checks = check_kernel_symbols(defined, undefined)
    result = dict(report, kernel_link_verified=True, kernel_sha256=digest(kernel),
                  kernel_symbol_checks=checks, gadget_runtime_qualified=False,
                  controller_started=False, hardware_qualified=False)
    (OUT / "kernel-link-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: MTU3 gadget/CDC core symbols resolve in source XNU; runtime remains unqualified")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-kernel", type=Path)
    args = parser.parse_args(argv)
    mode = os.environ.get("XNU_NATIVE_USB_HOST", "0")
    if mode == "0":
        print("SKIP: source-only USB profile disabled")
    elif mode != "1":
        raise SystemExit("XNU_NATIVE_USB_HOST must be 0 or 1")
    elif args.verify_kernel:
        verify_kernel(args.verify_kernel)
    else:
        build()


if __name__ == "__main__":
    main()
