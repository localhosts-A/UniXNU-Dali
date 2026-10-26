#!/usr/bin/env python3
"""Build the Dali early-boot log and the generic ARM-DT/GIC kernel archives."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
from dali_source_identity import build_source_identity, verify_compiled_record

ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837"
OUT = ROOT / "build/dali-xnu-platform"
SOURCE_PINS, XNU_SOURCE_PROOF = build_source_identity(ROOT)


def compiler_record():
    # XNU's MakeInc.rule writes C++ dependency records beside .cpo objects,
    # while C records use .o.json. Prefer the C++ form and keep the latter as
    # a fallback for toolchains that use a different object suffix.
    for pattern in ("*.cpo.json", "*.o.json"):
        for path in sorted(OBJECTS.rglob(pattern)):
            try:
                record = json.loads(path.read_text().strip().rstrip(","))
            except (OSError, ValueError):
                continue
            source = str(record.get("file", ""))
            arguments = record.get("arguments")
            if source.endswith((".cpp", ".cc", ".cxx")) and isinstance(arguments, list):
                return path, record
    raise SystemExit("No XNU C++ compile record; the kernel bootstrap pass must run first")


record_path, record = compiler_record()
record_args = list(record["arguments"])
record_directory = Path(record.get("directory", ROOT)).resolve()
record_source = Path(record["file"])
if not record_source.is_absolute():
    record_source = (record_directory / record_source).resolve()


def compile_source(source, output):
    source = source.resolve()
    darwin = os.environ.get("UNIXNU_XNU_DARWIN", "")
    version = os.environ.get("UNIXNU_XNU_VERSION", "")
    if not darwin or not version or any(char not in "0123456789." for char in darwin + version):
        raise SystemExit("The Dali checkpoint requires pinned numeric XNU Darwin and build versions")
    command = ["xcrun", "--sdk", "iphoneos", "clang++", *record_args[1:],
               f'-DDALI_XNU_DARWIN="{darwin}"', f'-DDALI_XNU_BUILD="{version}"']
    if source.name == 'DaliUfs.cpp':
        command += [f'-DDALI_UFS_{key.removesuffix("_commit").upper()}_COMMIT="{value}"'
                    for key, value in SOURCE_PINS.items()]
    replaced = False
    index = 4  # xcrun, --sdk, iphoneos, clang++
    while index < len(command):
        value = command[index]
        if value == "-o" and index + 1 < len(command):
            command[index + 1] = str(output)
            index += 2
            continue
        try:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = (record_directory / candidate).resolve()
            else:
                candidate = candidate.resolve()
        except (OSError, ValueError):
            candidate = None
        if candidate == record_source:
            command[index] = str(source)
            replaced = True
        index += 1
    if not replaced:
        raise SystemExit(f"Could not replace compile-record source {record_source} in {record_path}")
    command += ["-I", str(ROOT / "build/storage-abi/include"),
                "-I", str(ROOT / "refer/IOStorageFamily"), "-I", str(ROOT / "drivers/UfsTransport")]
    result = subprocess.run(command, cwd=record_directory, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    output.with_suffix(output.suffix + ".log").write_text(result.stdout)
    if result.returncode:
        # XNU's typed-allocation macros can emit enough warnings to push the
        # real error out of a tail excerpt. Keep its nearby source diagnostic.
        lines = result.stdout.splitlines()
        errors = []
        for index, line in enumerate(lines):
            if 'error:' in line:
                errors.extend(lines[max(0, index - 1):index + 5])
        raise SystemExit(f"{source.name} compile failed:\n" + '\n'.join(errors[:60]) +
                         '\nCompiler tail:\n' + result.stdout[-6000:])


def archive(name, source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    obj = destination.with_suffix(".o")
    compile_source(source, obj)
    subprocess.run(["xcrun", "ar", "rcs", destination, obj], check=True)
    return {"source": str(source.relative_to(ROOT)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest(),
            "archive": str(destination.relative_to(ROOT)),
            "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def ufs_archive():
    destination = OUT / "libDaliUfs.a"
    OUT.mkdir(parents=True, exist_ok=True)
    objects, entries = [], []
    for source in (ROOT / "drivers/DaliUfs/DaliUfsPlatform.cpp", ROOT / "drivers/DaliUfs/DaliUfs.cpp"):
        obj = OUT / (source.stem + ".o")
        obj.unlink(missing_ok=True)
        compile_source(source, obj)
        magic, cpu, _, kind = struct.unpack_from("<4I", obj.read_bytes())
        if (magic, cpu, kind) != (0xfeedfacf, 0x100000c, 1):
            raise SystemExit("Dali UFS must contain actual arm64 Mach-O objects")
        if source.name == 'DaliUfs.cpp':
            verify_compiled_record(obj.read_bytes(), SOURCE_PINS)
        objects.append(obj)
        entries.append({"source": str(source.relative_to(ROOT)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest()})
    destination.unlink(missing_ok=True)
    subprocess.run(["xcrun", "ar", "rcs", str(destination), *map(str, objects)], check=True)
    symbols = subprocess.check_output(["xcrun", "nm", "-g", str(destination)], text=True)
    (OUT / "dali-ufs-symbols.txt").write_text(symbols)
    for symbol in ("DaliUfs10gMetaClassE", "DaliUfsBlock10gMetaClassE"):
        if not any(symbol in line and " U " not in line for line in symbols.splitlines()):
            raise SystemExit(f"Dali UFS archive lacks actual metaclass {symbol}")
    headers = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
               sorted((ROOT / "drivers/DaliUfs").glob("*.h"))}
    return {"sources": entries, "headers": headers, "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "compiled_source_pins": SOURCE_PINS,
            "effective_xnu_source": XNU_SOURCE_PROOF,
            "physical_ufs_qualified": False, "write_authorized": False}


sources = {
    "armdt": (ROOT / "drivers/ArmDTPlatform/ArmDTPlatform.cpp", ROOT / "build/armdt-platform/libArmDTPlatform.a"),
    "armcpu": (ROOT / "drivers/ArmCpu/ArmCpu.cpp", ROOT / "build/armcpu/libArmCpu.a"),
    "dali-log": (ROOT / "drivers/DaliBootLog/DaliBootLog.cpp", OUT / "libDaliBootLog.a"),
}
report = {name: archive(name, *pair) for name, pair in sources.items()}
report["dali-ufs"] = ufs_archive()
report["xnu_version"] = {"darwin": os.environ["UNIXNU_XNU_DARWIN"], "build": os.environ["UNIXNU_XNU_VERSION"]}
report["compile_record"] = str(record_path.relative_to(ROOT))
report["compile_record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
print("PASS: Dali logging/UFS, ArmDTPlatform, and ArmCpu archives built")
