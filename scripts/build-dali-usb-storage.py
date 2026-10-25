#!/usr/bin/env python3
"""Build and verify the source-only XNU USB BOT block-storage service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837"
SOURCE = ROOT / "drivers/DaliUsbStorage/DaliUsbStorage.cpp"
USB_INCLUDE = ROOT / "drivers/UsbCore/include"
STORAGE_SOURCE = ROOT / "refer/IOStorageFamily"
STORAGE_INCLUDE = ROOT / "build/storage-abi/include"
OUT = ROOT / "build/dali-usb-storage"
REVISION = "e45fbe30a7879e0b16e26dddb78538f1b00e0bff"
ARCHIVE = OUT / "libDaliUsbStorage.a"
CLASS_SYMBOL = "DaliUsbStorage10gMetaClassE"
REQUIRED_SYMBOLS = (
    CLASS_SYMBOL,
    "dali_usb_storage_publish",
    "dali_usb_storage_quiesce",
    "dali_usb_storage_detach",
    "dali_usb_storage_terminate",
)
KERNEL_SYMBOLS = REQUIRED_SYMBOLS + (
    "IOBlockStorageDevice10gMetaClassE",
    "usb_bot_read_only",
    "usb_block_io_transfer",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_directory(record, record_path):
    directory = Path(record.get("directory", record_path.parent))
    return directory.resolve() if directory.is_absolute() else (record_path.parent / directory).resolve()


def source_path(record, record_path):
    source = Path(record["file"])
    if not source.is_absolute():
        source = record_directory(record, record_path) / source
    return source.resolve()


def compiler_record(objects=OBJECTS):
    preferred = objects / "iokit/DEVELOPMENT/IOService.cpo.json"
    for path in [preferred, *sorted(objects.rglob("*.cpo.json"))]:
        try:
            record = json.loads(path.read_text().strip().rstrip(","))
        except (OSError, ValueError):
            continue
        arguments = record.get("arguments")
        if str(record.get("file", "")).endswith((".cpp", ".cc", ".cxx")) and isinstance(arguments, list) and arguments:
            return path, record
    raise ValueError("No XNU C++ compile record; run the kernel bootstrap pass first")


def compile_command(record_path, record, source, output, include_paths):
    directory = record_directory(record, record_path)
    original = source_path(record, record_path)
    arguments = record["arguments"][1:]
    paired = {"-o", "-MF", "-MT", "-MQ", "-MJ", "-serialize-diagnostics"}
    single = {"-MD", "-MMD", "-MP", "-MG"}
    command = ["xcrun", "--sdk", "iphoneos", "clang++"]
    replaced = 0
    at = 0
    while at < len(arguments):
        value = arguments[at]
        if value in paired:
            if at + 1 >= len(arguments):
                raise ValueError("Truncated XNU compile-record output option: " + value)
            at += 2
            continue
        if value in single:
            at += 1
            continue
        candidate = Path(value)
        try:
            resolved = candidate.resolve() if candidate.is_absolute() else (directory / candidate).resolve()
        except (OSError, ValueError):
            resolved = None
        if resolved == original:
            command.append(str(source.resolve()))
            replaced += 1
        else:
            command.append(value)
        at += 1
    if replaced != 1:
        raise ValueError("Expected exactly one original C++ source in the XNU compile record")
    # Quote includes ensure the class declaration comes from this exact public
    # IOStorageFamily revision even if the base XNU tree has a private header.
    for path in include_paths:
        command += ["-iquote", str(path.resolve())]
    command += ["-I", str(STORAGE_INCLUDE.resolve()),
                "-I", str((ROOT / ".cache/xnu-work/iokit").resolve()),
                "-I", str(USB_INCLUDE.resolve()), "-o", str(output.resolve())]
    return command, directory


def validate_object(path):
    data = path.read_bytes()
    if len(data) < 32:
        raise ValueError("Truncated Dali USB storage object: " + str(path))
    magic, cpu, subtype, filetype, ncmds, sizeofcmds, _, _ = struct.unpack_from("<8I", data)
    if (magic, cpu, subtype, filetype) != (0xfeedfacf, 0x100000c, 0, 1) or 32 + sizeofcmds > len(data):
        raise ValueError("Dali USB storage object is not an arm64 XNU MH_OBJECT: " + str(path))
    offset, platform = 32, None
    for _ in range(ncmds):
        if offset + 8 > 32 + sizeofcmds:
            raise ValueError("Truncated Dali USB storage object load command")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > 32 + sizeofcmds:
            raise ValueError("Invalid Dali USB storage object load command")
        if command == 0x32:
            if size < 24:
                raise ValueError("Truncated Dali USB storage object build-version command")
            platform, minimum = struct.unpack_from("<II", data, offset + 8)
            if (platform, minimum) != (2, 0x110000):
                raise ValueError("Dali USB storage object does not target the kernel iOS 17 ABI")
        offset += size
    if offset != 32 + sizeofcmds or platform != 2:
        raise ValueError("Dali USB storage object lacks the expected iOS build version")


def run_logged(command, directory, log):
    result = subprocess.run(command, cwd=directory, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    log.write_text(result.stdout)
    if result.returncode:
        lines = result.stdout.splitlines()
        errors = []
        for at, line in enumerate(lines):
            if "error:" in line or "fatal error" in line:
                errors += lines[max(0, at - 1):at + 5]
        raise RuntimeError(f"{log.name}: command failed ({result.returncode})\n" +
                           "\n".join(errors[:80]) + "\n" + result.stdout[-6000:])


def normalized_symbols(output):
    return {line.split()[-1].lstrip("_") for line in output.splitlines() if line.split()}


def missing_symbols(output, required):
    symbols = normalized_symbols(output)
    missing = []
    for expected in required:
        found = any(expected in symbol for symbol in symbols) if expected.endswith("10gMetaClassE") else expected in symbols
        if not found:
            missing.append(expected)
    return missing


def build():
    if not STORAGE_SOURCE.is_dir():
        raise ValueError("The pinned IOStorageFamily feed is required before the USB storage archive")
    revision = subprocess.check_output(["git", "-C", str(STORAGE_SOURCE), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise ValueError("Public IOStorageFamily revision differs from the fixed source pin")
    if subprocess.check_output(["git", "-C", str(STORAGE_SOURCE), "status", "--porcelain"], text=True).strip():
        raise ValueError("Public IOStorageFamily source must be clean")
    if not (STORAGE_INCLUDE / "IOKit/storage/IOStorageDeviceCharacteristics.h").is_file():
        raise ValueError("Prepared public storage headers are required; run prepare-storage-headers.py first")
    record_path, record = compiler_record()
    source_record = source_path(record, record_path)
    if not source_record.is_file() or not SOURCE.is_file():
        raise ValueError("XNU compile-record source or Dali USB storage source is missing")
    OUT.mkdir(parents=True, exist_ok=True)
    obj = OUT / "DaliUsbStorage.o"
    obj.unlink(missing_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    (OUT / "build-report.json").unlink(missing_ok=True)
    command, directory = compile_command(record_path, record, SOURCE, obj, [STORAGE_SOURCE])
    log = OUT / "DaliUsbStorage.o.log"
    run_logged(command, directory, log)
    validate_object(obj)
    subprocess.run(["xcrun", "ar", "rcs", str(ARCHIVE), str(obj)], check=True)
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(ARCHIVE)], text=True)
    missing = sorted(missing_symbols(defined, REQUIRED_SYMBOLS))
    if missing:
        raise ValueError("Dali USB storage archive lacks production symbols: " + ", ".join(missing))
    report = {
        "io_storage_family_revision": revision,
        "compile_record": str(record_path.relative_to(ROOT)),
        "compile_record_sha256": digest(record_path),
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": digest(SOURCE),
        "public_header_sha256": digest(STORAGE_SOURCE / "IOBlockStorageDevice.h"),
        "object": str(obj.relative_to(ROOT)),
        "object_sha256": digest(obj),
        "compile_log": str(log.relative_to(ROOT)),
        "compile_log_sha256": digest(log),
        "archive": str(ARCHIVE.relative_to(ROOT)),
        "archive_sha256": digest(ARCHIVE),
        "required_symbols": list(REQUIRED_SYMBOLS),
        "source_archive_built": True,
        "kernel_link_verified": False,
        "storage_runtime_qualified": False,
        "hardware_qualified": False,
    }
    (OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: Dali USB BOT IOBlockStorageDevice source archive built; kernel link/runtime remain separate gates")


def verify_kernel(kernel):
    kernel = kernel.resolve()
    report_path = OUT / "build-report.json"
    report = json.loads(report_path.read_text())
    if (report.get("io_storage_family_revision") != REVISION or
            report.get("source_archive_built") is not True or
            report.get("archive_sha256") != digest(ARCHIVE)):
        raise ValueError("Dali USB storage archive/report mismatch")
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(kernel)], text=True)
    undefined = subprocess.check_output(["xcrun", "nm", "-u", str(kernel)], text=True)
    missing = sorted(missing_symbols(defined, KERNEL_SYMBOLS))
    unresolved_required = sorted(set(KERNEL_SYMBOLS) - set(missing_symbols(undefined, KERNEL_SYMBOLS)))
    if missing or unresolved_required:
        raise ValueError("USB storage kernel link mismatch; missing=" + ",".join(missing) +
                         " unresolved=" + ",".join(unresolved_required))
    result = dict(report, kernel_link_verified=True, kernel_sha256=digest(kernel),
                  kernel_symbol_checks=list(KERNEL_SYMBOLS), storage_runtime_qualified=False,
                  hardware_qualified=False)
    target = OUT / "kernel-link-report.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: USB BOT IOBlockStorageDevice and source IOStorageFamily resolve in the XNU kernel")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-kernel", type=Path)
    args = parser.parse_args(argv)
    enabled = os.environ.get("XNU_NATIVE_USB_HOST", "0")
    if enabled == "0":
        print("SKIP: source-only USB BOT block service disabled")
    elif enabled != "1":
        raise SystemExit("XNU_NATIVE_USB_HOST must be 0 or 1")
    else:
        verify_kernel(args.verify_kernel) if args.verify_kernel else build()


if __name__ == "__main__":
    main()
