"""Validate and assemble the two pinned iPadOS 17.7.11 trust-cache modules."""
import hashlib
import json
from pathlib import Path
import struct


TARGET = "iPad7,1 / iPadOS 17.7.11 / 21H461"
COMPONENT_TYPES = {
    "StaticTrustCache": b"trst",
    "Cryptex1,SystemTrustCache": b"trcs",
}


def _der_item(data, offset):
    if offset + 2 > len(data):
        raise ValueError("truncated IM4P DER item")
    tag, size = data[offset], data[offset + 1]
    offset += 2
    if size & 0x80:
        count = size & 0x7f
        if not 0 < count <= 4 or offset + count > len(data):
            raise ValueError("invalid IM4P DER length")
        if data[offset] == 0:
            raise ValueError("non-canonical IM4P DER length")
        size = int.from_bytes(data[offset:offset + count], "big")
        if size < 0x80:
            raise ValueError("non-canonical short IM4P DER length")
        offset += count
    end = offset + size
    if end > len(data):
        raise ValueError("IM4P DER item exceeds input")
    return tag, data[offset:end], end


def im4p_payload(data, component):
    """Return the version-2 trust-cache bytes from one pinned IM4P member."""
    tag, sequence, end = _der_item(data, 0)
    if tag != 0x30 or end != len(data):
        raise ValueError("trust-cache component is not one complete IM4P")
    fields = []
    cursor = 0
    while cursor < len(sequence):
        field_tag, value, cursor = _der_item(sequence, cursor)
        fields.append((field_tag, value))
    if (len(fields) != 4 or [tag for tag, _ in fields] != [0x16, 0x16, 0x16, 0x04] or
            fields[0][1] != b"IM4P" or fields[1][1] != COMPONENT_TYPES[component] or
            fields[2][1] != b"1"):
        raise ValueError(f"{component} is not its expected 21H461 IM4P trust-cache type")
    return fields[3][1]


def module_info(data):
    if len(data) < 24:
        raise ValueError("trust-cache module is truncated")
    version, = struct.unpack_from("<I", data)
    entries, = struct.unpack_from("<I", data, 20)
    if version != 2 or len(data) != 24 + entries * 24:
        raise ValueError("trust-cache module must be a complete version-2 module")
    previous = None
    for index in range(entries):
        offset = 24 + index * 24
        cdhash = data[offset:offset + 20]
        hash_type = data[offset + 20]
        if not 1 <= hash_type <= 4 or (previous is not None and previous >= cdhash):
            raise ValueError("trust-cache entries must have supported types and strictly sorted CDHashes")
        previous = cdhash
    return {"version": version, "entries": entries, "sha256": hashlib.sha256(data).hexdigest()}


def parse_bundle(data):
    if len(data) < 12:
        raise ValueError("trust-cache bundle is truncated")
    count, = struct.unpack_from("<I", data)
    if count != 2 or len(data) < 4 + 4 * count:
        raise ValueError("Dali iPadOS bundle must contain exactly the two original trust-cache modules")
    offsets = struct.unpack_from("<2I", data, 4)
    if offsets[0] < 12 or offsets[0] & 3 or offsets[1] <= offsets[0] or offsets[1] & 3:
        raise ValueError("trust-cache bundle has invalid module offsets")
    ranges = ((offsets[0], offsets[1]), (offsets[1], len(data)))
    modules = []
    for start, end in ranges:
        if end > len(data) or end - start < 24:
            raise ValueError("trust-cache bundle module range is invalid")
        module = data[start:end]
        info = module_info(module)
        modules.append({"offset": start, "bytes": len(module), **info})
    return modules


def _component_module(path, component, board):
    path = Path(path)
    expected = board.get("ipsw", {}).get("sha256", {}).get(path.name)
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError(f"board.toml does not pin {path.name}")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise ValueError(f"{component} SHA-256 differs from board.toml: {digest}")
    record_path = Path(str(path) + ".json")
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{component} has no valid ipsw fetch record: {error}") from None
    if not isinstance(record, dict):
        raise ValueError(f"{component} provenance record is not an object")
    member = record.get("member")
    if (record.get("component") != component or record.get("sha256") != digest or
            record.get("size") != len(data) or not isinstance(member, str) or
            Path(member).name != path.name or
            record.get("source") not in ("local", "remote")):
        raise ValueError(f"{component} provenance record does not match its pinned file")
    module = im4p_payload(data, component)
    details = {"component": component, "member": member, "sha256": digest,
               "module": module_info(module)}
    return module, details


def build_bundle(static_path, system_path, board, output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ValueError(f"refusing to overwrite trust-cache output: {output_dir}")
    static, static_record = _component_module(static_path, "StaticTrustCache", board)
    system, system_record = _component_module(system_path, "Cryptex1,SystemTrustCache", board)
    bundle = struct.pack("<3I", 2, 12, 12 + len(static)) + static + system
    modules = parse_bundle(bundle)
    digest = hashlib.sha256(bundle).hexdigest()
    allowed = board.get("payload", {}).get("trust_cache_sha256", [])
    if digest not in allowed:
        raise ValueError(f"assembled target trust-cache SHA-256 is not pinned by board.toml: {digest}")
    output_dir.mkdir(parents=True)
    bundle_path = output_dir / "trust-cache.bundle"
    report_path = output_dir / "trust-cache.json"
    bundle_path.write_bytes(bundle)
    report = {
        "target": TARGET,
        "physical_device": "REDMI K80 Ultra / Dali / MT6991",
        "format": "DarwinBoot trust-cache offset table with unchanged version-2 Apple modules",
        "inputs": [static_record, system_record],
        "modules": modules,
        "bundle_sha256": digest,
        "bundle_bytes": len(bundle),
        "apple_trust_chain_verified": False,
        "trust_cache_runtime_consumed": False,
        "hardware_tested": False,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return bundle_path, report_path, report
