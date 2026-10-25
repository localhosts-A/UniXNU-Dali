#!/usr/bin/env python3
"""Build the portable USB host core with the source-only XNU C ABI."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837"
OUT = ROOT / "build/dali-usb-host"
SOURCES = (ROOT / "drivers/UsbCore/UsbCore.c", ROOT / "drivers/UsbCore/UsbHost.c",
           ROOT / "drivers/UsbCore/UsbStorage.c")
REQUIRED_SYMBOLS = (
    "usb_host_enumerate", "usb_host_start_storage", "usb_host_poll_input",
    "usb_host_disconnect", "usb_host_hub_start", "usb_host_hub_poll",
    "usb_host_hub_child_route", "usb_host_hub_stop",
    "usb_parse_configuration", "usb_bot_command", "usb_bot_read_only", "usb_block_io_init",
    "usb_block_io_transfer", "usb_block_io_sync", "usb_block_io_quiesce",
    "usb_block_io_detach",
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
    for path in sorted(objects.rglob("*.o.json")):
        try:
            record = json.loads(path.read_text().strip().rstrip(","))
        except (OSError, ValueError):
            continue
        arguments = record.get("arguments")
        source = str(record.get("file", ""))
        if source.endswith(".c") and isinstance(arguments, list) and arguments:
            return path, record
    raise ValueError("No XNU C compile record; run the kernel bootstrap pass first")


def compile_command(record_path, record, source, output, include):
    directory = record_directory(record, record_path)
    original = source_path(record, record_path)
    arguments = record["arguments"][1:]
    paired = {"-o", "-MF", "-MT", "-MQ", "-MJ", "-serialize-diagnostics"}
    single = {"-MD", "-MMD", "-MP", "-MG"}
    command = ["xcrun", "--sdk", "iphoneos", "clang"]
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
        raise ValueError("Expected exactly one original C source in the XNU compile record")
    command += ["-I", str(include.resolve()), "-o", str(output.resolve())]
    return command, directory


def validate_object(path):
    data = path.read_bytes()
    if len(data) < 32:
        raise ValueError("Truncated USB host/storage object: " + str(path))
    magic, cpu, subtype, filetype, ncmds, sizeofcmds, _, _ = struct.unpack_from("<8I", data)
    if (magic, cpu, subtype, filetype) != (0xfeedfacf, 0x100000c, 0, 1) or 32 + sizeofcmds > len(data):
        raise ValueError("USB host/storage object is not an arm64 XNU MH_OBJECT: " + str(path))
    offset, platform = 32, None
    for _ in range(ncmds):
        if offset + 8 > 32 + sizeofcmds:
            raise ValueError("Truncated USB host/storage object load command")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > 32 + sizeofcmds:
            raise ValueError("Invalid USB host/storage object load command")
        if command == 0x32:
            if size < 24:
                raise ValueError("Truncated USB host/storage object build-version command")
            platform, minimum = struct.unpack_from("<II", data, offset + 8)
            if (platform, minimum) != (2, 0x110000):
                raise ValueError("USB host/storage object does not target the kernel iOS 17 ABI")
        offset += size
    if offset != 32 + sizeofcmds or platform != 2:
        raise ValueError("USB host/storage object lacks the expected iOS build version")


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


def build():
    record_path, record = compiler_record()
    source = source_path(record, record_path)
    if not source.is_file():
        raise ValueError("XNU C compile-record source no longer exists: " + str(source))
    directory = record_directory(record, record_path)
    include = ROOT / "drivers/UsbCore/include"
    OUT.mkdir(parents=True, exist_ok=True)
    objects, object_rows = [], []
    for source in SOURCES:
        if not source.is_file():
            raise ValueError("USB host/storage core source is missing: " + str(source))
        obj = OUT / (source.name + ".o")
        obj.unlink(missing_ok=True)
        command, cwd = compile_command(record_path, record, source, obj, include)
        log = obj.with_suffix(obj.suffix + ".log")
        run_logged(command, cwd, log)
        validate_object(obj)
        objects.append(obj)
        object_rows.append({"source": str(source.relative_to(ROOT)), "source_sha256": digest(source),
                            "object": str(obj.relative_to(ROOT)), "object_sha256": digest(obj),
                            "command": command, "log": str(log.relative_to(ROOT)),
                            "log_sha256": digest(log)})
    archive = OUT / "libDaliUsbHost.a"
    archive.unlink(missing_ok=True)
    subprocess.run(["xcrun", "ar", "rcs", str(archive), *map(str, objects)], check=True)
    symbols = subprocess.check_output(["xcrun", "nm", "-gU", str(archive)], text=True)
    normalized = {line.split()[-1].lstrip("_") for line in symbols.splitlines() if line.split()}
    missing = sorted(set(REQUIRED_SYMBOLS) - normalized)
    if missing:
        raise ValueError("USB host/storage archive lacks production symbols: " + ", ".join(missing))
    report = {
        "compile_record": str(record_path.relative_to(ROOT)),
        "compile_record_sha256": digest(record_path),
        "objects": object_rows,
        "archive": str(archive.relative_to(ROOT)),
        "archive_sha256": digest(archive),
        "required_symbols": list(REQUIRED_SYMBOLS),
        "source_archive_built": True,
        "kernel_link_verified": False,
        "host_runtime_qualified": False,
        "hardware_qualified": False,
    }
    (OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: Dali USB host/storage core archive built from the XNU C compile record; runtime remains unqualified")


def main():
    enabled = os.environ.get("XNU_NATIVE_USB_HOST", "0")
    if enabled == "0":
        print("SKIP: source-only USB host/storage core disabled")
    elif enabled == "1":
        build()
    else:
        raise SystemExit("XNU_NATIVE_USB_HOST must be 0 or 1")


if __name__ == "__main__":
    main()
