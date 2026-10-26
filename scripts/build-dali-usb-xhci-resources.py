#!/usr/bin/env python3
"""Build and verify the opt-in XNU xhci0 device-tree resource consumer."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/dali-usb-xhci-resources"
SOURCE = ROOT / "drivers/DaliUsbXhciResources/DaliUsbXhciResources.cpp"
ARCHIVE = OUT / "libDaliUsbXhciResources.a"
SYMBOLS = (
    "DaliUsbXhciResources10gMetaClassE",
    "startEP9IOService",
    "copyResourceDescription",
)


def helpers():
    shared = ROOT / "scripts/build-source-storage.py"
    if not shared.is_file():
        shared = ROOT.parent / "UniXNU/scripts/build-source-storage.py"
    spec = importlib.util.spec_from_file_location(
        "dali_xhci_resource_build_helpers", shared)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def missing_symbols(output, required):
    actual = {line.split()[-1].lstrip("_") for line in output.splitlines() if line.split()}
    return sorted(name for name in required if not any(name in symbol for symbol in actual))


def require_symbols(output, required, description):
    missing = missing_symbols(output, required)
    if missing:
        raise ValueError(description + " lacks production symbols: " + ", ".join(missing))


def build():
    helper = helpers()
    if not SOURCE.is_file():
        raise ValueError("Dali xHCI resource service source is missing")
    record_path, record = helper.compiler_record(
        ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837")
    xnu = ROOT / ".cache/xnu-work"
    includes = [SOURCE.parent, xnu / "iokit"]
    OUT.mkdir(parents=True, exist_ok=True)
    obj = OUT / "DaliUsbXhciResources.o"
    obj.unlink(missing_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    (OUT / "build-report.json").unlink(missing_ok=True)
    command = helper.compile_command(record, SOURCE, obj, includes)
    log = OUT / "DaliUsbXhciResources.o.log"
    (OUT / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    helper.run_logged(command, Path(record["directory"]), log)
    helper.validate_object(obj)
    subprocess.run(["xcrun", "ar", "rcs", str(ARCHIVE), str(obj)], check=True)
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(ARCHIVE)], text=True)
    require_symbols(defined, SYMBOLS, "Dali xHCI resource archive")
    report = {
        "compile_record": str(record_path.relative_to(ROOT)),
        "compile_record_sha256": helper.digest(record_path),
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": helper.digest(SOURCE),
        "public_header_sha256": helper.digest(SOURCE.with_suffix(".hpp")),
        "wire_header_sha256": helper.digest(SOURCE.with_name("DaliUsbXhciResourceV1.h")),
        "object": str(obj.relative_to(ROOT)),
        "object_sha256": helper.digest(obj),
        "compile_log": str(log.relative_to(ROOT)),
        "compile_log_sha256": helper.digest(log),
        "archive": str(ARCHIVE.relative_to(ROOT)),
        "archive_sha256": helper.digest(ARCHIVE),
        "required_symbols": list(SYMBOLS),
        "source_archive_built": True,
        "kernel_link_verified": False,
        "resource_consumer_runtime_qualified": False,
        "host_runtime_qualified": False,
        "hardware_qualified": False,
    }
    (OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: xhci0 resource consumer archive built; runtime/controller qualification remains separate")


def verify_kernel(kernel):
    helper = helpers()
    kernel = kernel.resolve()
    report_path = OUT / "build-report.json"
    report = json.loads(report_path.read_text())
    if (report.get("source_archive_built") is not True or
            report.get("archive_sha256") != helper.digest(ARCHIVE) or
            report.get("source_sha256") != helper.digest(SOURCE)):
        raise ValueError("Dali xHCI resource archive/report mismatch")
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(kernel)], text=True)
    undefined = subprocess.check_output(["xcrun", "nm", "-u", str(kernel)], text=True)
    missing = missing_symbols(defined, SYMBOLS)
    unresolved = sorted(set(SYMBOLS) - set(missing_symbols(undefined, SYMBOLS)))
    kernel_symbols = SYMBOLS + ("IOService10gMetaClassE",)
    missing_kernel = missing_symbols(defined, kernel_symbols)
    if missing or unresolved or missing_kernel:
        raise ValueError("xHCI resource kernel link mismatch; missing=" +
                         ",".join(missing_kernel) + " unresolved=" + ",".join(unresolved))
    result = dict(
        report,
        kernel_link_verified=True,
        kernel_sha256=helper.digest(kernel),
        kernel_symbol_checks=list(kernel_symbols),
        resource_consumer_runtime_qualified=False,
        host_runtime_qualified=False,
        hardware_qualified=False,
    )
    (OUT / "kernel-link-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: xhci0 resource-consumer service linked in the source XNU kernel; runtime remains unqualified")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-kernel", type=Path)
    args = parser.parse_args(argv)
    mode = os.environ.get("XNU_NATIVE_USB_HOST", "0")
    if mode == "0":
        print("SKIP: source-only USB host profile disabled")
    elif mode != "1":
        raise SystemExit("XNU_NATIVE_USB_HOST must be 0 or 1")
    elif args.verify_kernel:
        verify_kernel(args.verify_kernel)
    else:
        build()


if __name__ == "__main__":
    main()
