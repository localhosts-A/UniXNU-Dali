#!/usr/bin/env python3
"""Build the Dali early-boot log and the generic ARM-DT/GIC kernel archives."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837"
OUT = ROOT / "build/dali-xnu-platform"


def compiler_record():
    for path in sorted(OBJECTS.rglob("*.o.json")):
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
    result = subprocess.run(command, cwd=record_directory, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    output.with_suffix(output.suffix + ".log").write_text(result.stdout)
    if result.returncode:
        raise SystemExit(f"{source.name} compile failed:\n{result.stdout[-6000:]}")


def archive(name, source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    obj = destination.with_suffix(".o")
    compile_source(source, obj)
    subprocess.run(["xcrun", "ar", "rcs", destination, obj], check=True)
    return {"source": str(source.relative_to(ROOT)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest(),
            "archive": str(destination.relative_to(ROOT)),
            "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


sources = {
    "armdt": (ROOT / "drivers/ArmDTPlatform/ArmDTPlatform.cpp", ROOT / "build/armdt-platform/libArmDTPlatform.a"),
    "armcpu": (ROOT / "drivers/ArmCpu/ArmCpu.cpp", ROOT / "build/armcpu/libArmCpu.a"),
    "dali-log": (ROOT / "drivers/DaliBootLog/DaliBootLog.cpp", OUT / "libDaliBootLog.a"),
}
report = {name: archive(name, *pair) for name, pair in sources.items()}
report["xnu_version"] = {"darwin": os.environ["UNIXNU_XNU_DARWIN"], "build": os.environ["UNIXNU_XNU_VERSION"]}
report["compile_record"] = str(record_path.relative_to(ROOT))
report["compile_record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
print("PASS: Dali logging, ArmDTPlatform, and ArmCpu archives built")
