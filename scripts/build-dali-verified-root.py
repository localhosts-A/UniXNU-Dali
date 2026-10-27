#!/usr/bin/env python3
"""Build signed OS/SystemOS policies and their XNU archive for Dali root boot.

The builder consumes the fully verified SHNGROOT image produced by the generic
userdata builder. It signs only the two raw windows with a caller-owned RSA key;
the private key is never copied into the kernel archive. When policy inputs are
absent (for example the default compile-only profile), it leaves the weak,
fail-closed provider stubs in libDaliUfs.a untouched.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def unixnu_root():
    configured = os.environ.get("UNIXNU_SOURCE_ROOT")
    candidates = ([Path(configured)] if configured else []) + [SCRIPT_ROOT, SCRIPT_ROOT.parent / "UniXNU"]
    for candidate in candidates:
        if (candidate / "kits/VerifiedRoot/tools/manifest.py").is_file() and \
                (candidate / "scripts/unixnu/userdata.py").is_file():
            return candidate.resolve()
    raise SystemExit("could not locate the pinned UniXNU source tree for VerifiedRoot inputs")


def profile_constants():
    source = (SCRIPT_ROOT / "drivers/DaliUfs/DaliUfsRoot.cpp").read_text()
    patterns = {
        "lun": r"static constexpr uint8_t kDaliRootLun = (\d+);",
        "container": r'static constexpr char kDaliRootContainer\[\] = "([A-Za-z0-9_-]+)";',
        "first_lba": r"static constexpr uint64_t kDaliContainerFirstLba = (\d+);",
        "last_lba": r"static constexpr uint64_t kDaliContainerLastLba = (\d+);",
        "OS": r'static constexpr char kDaliOsRootUuid\[\] = "([0-9A-Fa-f-]{36})";',
        "SystemOS": r'static constexpr char kDaliSystemOsUuid\[\] = "([0-9A-Fa-f-]{36})";',
    }
    values = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, source)
        if not match:
            raise SystemExit(f"DaliUfsRoot.cpp lacks its pinned {key} profile value")
        values[key] = int(match.group(1)) if key in ("lun", "first_lba", "last_lba") else match.group(1)
    return values


def import_userdata(unixnu):
    scripts = unixnu / "scripts"
    sys.path.insert(0, str(scripts))
    from unixnu.userdata import verify_built_image
    manifest_path = unixnu / "kits/VerifiedRoot/tools/manifest.py"
    spec = importlib.util.spec_from_file_location("dali_verified_root_manifest", manifest_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return verify_built_image, module.make


class WindowReader:
    """Read exactly one bounded SHNGROOT window without making a second copy."""
    def __init__(self, stream, offset, length):
        self.stream = stream
        self.stream.seek(offset)
        self.remaining = length

    def read(self, amount=-1):
        if self.remaining <= 0:
            return b""
        if amount is None or amount < 0:
            amount = self.remaining
        amount = min(amount, self.remaining)
        data = self.stream.read(amount)
        if len(data) != amount:
            raise ValueError("short read inside a verified root window")
        self.remaining -= amount
        return data


def load_verified_container(image, report_path, verify_built_image, profile):
    report = json.loads(report_path.read_text())
    expected_capacity = (profile["last_lba"] - profile["first_lba"] + 1) * 4096
    if (report.get("partition_name") != profile["container"] or report.get("lun") != profile["lun"] or
            report.get("partition_bytes") != expected_capacity or report.get("userdata_bytes") != expected_capacity):
        raise ValueError("userdata report does not match the Dali partition/LUN profile")
    windows = report.get("windows")
    if not isinstance(windows, list) or [window.get("name") for window in windows] != ["OS", "SystemOS"]:
        raise ValueError("Dali root image must contain exactly OS then SystemOS windows")
    entries = []
    for window in windows:
        try:
            entry = (window["name"], window["offset"], window["size"], bytes.fromhex(window["sha256"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid userdata window report") from error
        entries.append(entry)
    image_digest = verify_built_image(image, entries, expected_capacity)
    if report.get("image_bytes") != image.stat().st_size or report.get("image_sha256") != image_digest:
        raise ValueError("userdata report does not bind the verified image bytes")
    if not report.get("source") in ("local", "remote"):
        raise ValueError("userdata report lacks the IPSW source record")
    return report, entries, image_digest


def build_policies(image, entries, private_key, public_key, out, make, profile):
    policy_root = out / "policies"
    records = []
    with image.open("rb", buffering=0) as stream:
        for name, offset, size, expected_digest in entries:
            uuid = profile[name]
            directory = policy_root / name
            metadata = make(WindowReader(stream, offset, size), size, private_key, public_key,
                            uuid, 1, directory)
            if metadata["image_sha256"] != expected_digest.hex():
                raise ValueError(f"signed {name} policy digest differs from the verified userdata window")
            records.append({"name": name, "volume_uuid": uuid.upper(),
                            "manifest_sha256": hashlib.sha256((directory / "manifest.bin").read_bytes()).hexdigest(),
                            "image_sha256": metadata["image_sha256"], "image_bytes": size,
                            "trusted_key_sha256": metadata["trusted_key_sha256"],
                            "generation": metadata["generation"]})
    os_key = (policy_root / "OS/trusted-key.bin").read_bytes()
    systemos_key = (policy_root / "SystemOS/trusted-key.bin").read_bytes()
    if os_key != systemos_key:
        raise ValueError("OS and SystemOS policies must use the same owner anchor")
    return records, hashlib.sha256(os_key).hexdigest()


def compile_xnu_object(unixnu, source, output):
    record_path = SCRIPT_ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/bsd/DEVELOPMENT/bsd_init.o.json"
    try:
        record = json.loads(record_path.read_text().strip().rstrip(","))
    except (OSError, ValueError) as error:
        raise SystemExit(f"missing XNU C compile record for VerifiedRoot archive: {record_path}") from error
    arguments = record.get("arguments")
    if not isinstance(arguments, list) or not arguments:
        raise SystemExit("XNU bsd_init compile record has no argument vector")
    record_directory = Path(record.get("directory", unixnu)).resolve()
    record_source = Path(record["file"])
    if not record_source.is_absolute():
        record_source = (record_directory / record_source).resolve()
    replaced, command, index = False, [], 0
    while index < len(arguments):
        value = arguments[index]
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = record_directory / candidate
        if candidate.resolve() == record_source:
            command.append(str(source.resolve()))
            replaced = True
        elif value == "-o":
            command += ["-o", str(output.resolve())]; index += 1
        elif value in ("-MF", "-MJ"):
            index += 1
        elif value not in ("-MD", "-MP"):
            command.append(value)
        index += 1
    if not replaced:
        raise SystemExit(f"could not replace source path in XNU compile record {record_path}")
    # Match the source-only XNU build, which sets BUILD_WERROR=0. Its compile
    # record may include an optional EXPORT_HDRS/libsa path absent in this
    # checkout; do not turn that harmless missing-directory warning fatal.
    command += ["-I" + str(unixnu / "kits/VerifiedRoot/include")]
    result = subprocess.run(command, cwd=record_directory, capture_output=True, text=True)
    output.with_suffix(output.suffix + ".log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit(f"VerifiedRoot source compile failed: {source.name}\n" + result.stderr[-6000:])
    data = output.read_bytes()
    if struct.unpack_from("<4I", data) != (0xfeedfacf, 0x100000c, 0, 1):
        raise SystemExit(f"VerifiedRoot object is not an ordinary iOS ARM64 object: {source.name}")


def build_archive(unixnu, out):
    assembly = out / "inputs.S"
    os_manifest = out / "policies/OS/manifest.bin"
    systemos_manifest = out / "policies/SystemOS/manifest.bin"
    key = out / "policies/OS/trusted-key.bin"

    def incbin(path):
        return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')

    assembly.write_text("\n".join([
        ".section __DATA_CONST,__const", ".balign 8", ".globl _verified_root_key",
        "_verified_root_key:", f'.incbin "{incbin(key)}"', ".balign 8",
        ".globl _verified_root_os_manifest", "_verified_root_os_manifest:",
        f'.incbin "{incbin(os_manifest)}"', ".balign 8",
        ".globl _verified_root_systemos_manifest", "_verified_root_systemos_manifest:",
        f'.incbin "{incbin(systemos_manifest)}"', ".balign 8",
        ".globl _verified_root_policies", "_verified_root_policies:",
        f".quad _verified_root_os_manifest, {os_manifest.stat().st_size}",
        f".quad _verified_root_systemos_manifest, {systemos_manifest.stat().st_size}", ".balign 8",
        ".globl _verified_root_policy_count", "_verified_root_policy_count:", ".quad 2", "",
    ]))

    objects = []
    for source in (unixnu / "kits/VerifiedRoot/src/verified_root.c",
                   unixnu / "kits/VerifiedRoot/adapters/xnu_crypto.c"):
        output = out / (source.stem + ".o")
        compile_xnu_object(unixnu, source, output)
        objects.append(output)
    sdk = subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
    assembly_object = out / "inputs.o"
    subprocess.run(["xcrun", "clang", "-target", "arm64-apple-ios17.0", "-isysroot", sdk,
                    "-c", assembly, "-o", assembly_object], check=True)
    if struct.unpack_from("<4I", assembly_object.read_bytes()) != (0xfeedfacf, 0x100000c, 0, 1):
        raise SystemExit("VerifiedRoot policy input object is not ordinary iOS ARM64")
    objects.append(assembly_object)
    archive = out / "libVerifiedRootDali.a"
    archive.unlink(missing_ok=True)
    subprocess.run(["xcrun", "ar", "rcs", archive, *map(str, objects)], check=True)
    symbols = subprocess.check_output(["xcrun", "nm", "-m", "-g", archive], text=True)
    for symbol in ("_verified_root_key", "_verified_root_policies", "_verified_root_policy_count",
                   "_vr_xnu_crypto", "_vr_open", "_vr_verify_block"):
        definitions = [line for line in symbols.splitlines() if symbol in line and "undefined" not in line]
        if len(definitions) != 1 or "weak external" in definitions[0]:
            raise SystemExit(f"VerifiedRoot archive lacks one strong definition for {symbol}")
    return archive


def main():
    names = ("XNU_DALI_ROOT_IMAGE", "XNU_DALI_ROOT_REPORT", "XNU_DALI_OWNER_PRIVATE_KEY",
             "XNU_DALI_OWNER_PUBLIC_KEY", "XNU_DALI_VERIFIED_ROOT_OUT")
    values = {name: os.environ.get(name, "") for name in names}
    if not any(values.values()):
        print("SKIP: no Dali signed-root inputs; weak empty-policy fallback remains active")
        return
    if not all(values.values()):
        raise SystemExit("Dali production root build requires userdata image/report, owner key pair, and output directory")
    image, report = Path(values[names[0]]), Path(values[names[1]])
    private_key, public_key = Path(values[names[2]]), Path(values[names[3]])
    out = Path(values[names[4]])
    for path in (image, report, private_key, public_key):
        if not path.is_file():
            raise SystemExit(f"Dali VerifiedRoot input is missing: {path}")
    unixnu = unixnu_root()
    verify_built_image, make = import_userdata(unixnu)
    profile = profile_constants()
    input_report, entries, image_digest = load_verified_container(image, report, verify_built_image, profile)
    out.mkdir(parents=True, exist_ok=True)
    records, anchor_digest = build_policies(image, entries, private_key, public_key, out, make, profile)
    archive = build_archive(unixnu, out)
    (out / "build-report.json").write_text(json.dumps({
        "archive": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "userdata_image_sha256": image_digest, "userdata_bytes": input_report["image_bytes"],
        "owner_anchor_sha256": anchor_digest, "policies": records,
        "signature_verified_by_openssl": True, "private_key_in_archive": False,
        "boot_root_authenticated": False, "physical_ufs_qualified": False,
    }, indent=2) + "\n")
    print(f"Built signed Dali OS/SystemOS VerifiedRoot archive: {archive}")


if __name__ == "__main__":
    main()
