#!/usr/bin/env python3
"""Build the Dali early-boot log and the generic ARM-DT/GIC kernel archives."""
import hashlib
import json
import os
import plistlib
from pathlib import Path
import subprocess
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837"
OUT = ROOT / "build/dali-xnu-platform"


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
                "-I", str(ROOT / "refer/IOStorageFamily"), "-I", str(ROOT / "drivers/UfsTransport"),
                "-I", str(ROOT / "drivers/NativeMali")]
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
            "physical_ufs_qualified": False, "write_authorized": False}


def mali_archive():
    destination = OUT / "libDaliMali.a"
    sources = sorted((ROOT / "drivers/NativeMali").glob("*.cpp")) + sorted((ROOT / "drivers/DaliMali").glob("*.cpp"))
    if len(sources) < 14:
        raise SystemExit("NativeMali core/IOKit sources missing from pinned generic/device tree")
    objects, entries = [], []
    for source in sources:
        obj = OUT / (source.stem + ".o")
        obj.unlink(missing_ok=True)
        compile_source(source, obj)
        magic, cpu, _, kind = struct.unpack_from("<4I", obj.read_bytes())
        if (magic, cpu, kind) != (0xfeedfacf, 0x100000c, 1):
            raise SystemExit("NativeMali must contain arm64 Mach-O objects")
        objects.append(obj)
        entries.append({"source": str(source.relative_to(ROOT)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest()})
    destination.unlink(missing_ok=True)
    subprocess.run(["xcrun", "ar", "rcs", str(destination), *map(str, objects)], check=True)
    symbols = subprocess.check_output(["xcrun", "nm", "-g", str(destination)], text=True)
    (OUT / "dali-mali-symbols.txt").write_text(symbols)
    for symbol in ("DaliMali10gMetaClassE", "DaliMaliUserClient10gMetaClassE"):
        if not any(symbol in line and " U " not in line for line in symbols.splitlines()):
            raise SystemExit(f"NativeMali archive lacks actual metaclass {symbol}")
    headers = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for directory in
               (ROOT / "drivers/NativeMali", ROOT / "drivers/DaliMali") for p in sorted(directory.glob("*.h"))}
    return {"sources": entries, "headers": headers, "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "hardware_exercised": False, "abi_version": 1, "requires_bootargs": ["dali-mali=1", "maxmem=1992"]}


def mali_accelerator():
    source = ROOT/'drivers/DaliMaliAccelerator/DaliMaliAccelerator.cpp'
    obj = OUT/'DaliMaliAccelerator.o'
    compile_source(source, obj)
    bundle = OUT/'DaliMaliAccelerator.kext'
    bundle.mkdir(parents=True, exist_ok=True)
    executable = bundle/'DaliMaliAccelerator'
    command = ['xcrun', 'ld', '-kext', '-arch', 'arm64', '-undefined', 'dynamic_lookup',
               '-platform_version', 'ios', '17.0', '26.0', '-o', executable, obj]
    result = subprocess.run([str(x) for x in command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (OUT/'DaliMaliAccelerator-link.log').write_text(result.stdout)
    if result.returncode:raise SystemExit('Mali accelerator kext link failed:\n'+result.stdout)
    magic, cpu, _, kind = struct.unpack_from('<4I', executable.read_bytes())
    if (magic, cpu, kind) != (0xfeedfacf, 0x100000c, 11):
        raise SystemExit('Accelerator companion must be actual arm64 MH_KEXT_BUNDLE')
    symbols = subprocess.check_output(['xcrun','nm','-g',executable],text=True)
    (OUT/'DaliMaliAccelerator-symbols.txt').write_text(symbols)
    for symbol in ['_kmod_info','DaliMaliAccelerator10gMetaClassE']:
        if not any(symbol in line and ' U ' not in line for line in symbols.splitlines()):
            raise SystemExit('Accelerator companion missing '+symbol)
    (bundle/'Info.plist').write_bytes(plistlib.dumps(dict(
        CFBundleIdentifier='org.unixnu.DaliMaliAccelerator',CFBundleExecutable='DaliMaliAccelerator',
        CFBundlePackageType='KEXT',CFBundleVersion='1.0.0',OSBundleRequired='Root',
        OSBundleLibraries={'com.apple.iokit.IOGPUFamily':'1.0.0','com.apple.kpi.iokit':'23.0.0','com.apple.kpi.libkern':'23.0.0'},
        IOKitPersonalities={'Dali Mali Metal':dict(IOClass='DaliMaliAccelerator',IOProviderClass='DaliMali',
            CFBundleIdentifier='org.unixnu.DaliMaliAccelerator',MetalPluginName='NativeMaliMetal',MetalPluginClassName='NativeMaliMetalDevice')})))
    return {'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
            'dependency':'com.apple.iokit.IOGPUFamily','hardware_exercised':False,'runtime_link_qualified':False}


sources = {
    "armdt": (ROOT / "drivers/ArmDTPlatform/ArmDTPlatform.cpp", ROOT / "build/armdt-platform/libArmDTPlatform.a"),
    "armcpu": (ROOT / "drivers/ArmCpu/ArmCpu.cpp", ROOT / "build/armcpu/libArmCpu.a"),
    "dali-log": (ROOT / "drivers/DaliBootLog/DaliBootLog.cpp", OUT / "libDaliBootLog.a"),
}
report = {name: archive(name, *pair) for name, pair in sources.items()}
report["dali-ufs"] = ufs_archive()
report["dali-mali"] = mali_archive()
report['dali-mali-accelerator'] = mali_accelerator()
report["xnu_version"] = {"darwin": os.environ["UNIXNU_XNU_DARWIN"], "build": os.environ["UNIXNU_XNU_VERSION"]}
report["compile_record"] = str(record_path.relative_to(ROOT))
report["compile_record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
print("PASS: Dali logging/UFS/Mali, ArmDTPlatform, and ArmCpu archives built")
