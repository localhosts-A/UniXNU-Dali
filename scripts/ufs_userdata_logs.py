"""Validate and decode the dedicated Dali 32 MiB UFS log partition.

This module never accesses a device. It validates bounded partition geometry,
journal slots and the source-XNU body before producing viewable files.
The binary snapshots remain authoritative; text files are convenience views.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import uuid

from ufs_expdb_log import (
    MAGIC_V1,
    MAGIC_V2,
    V2_BODY_FORMAT_OFFSET,
    V2_CAPTURE_STAGE_OFFSET,
    V2_FLAGS_OFFSET,
    V2_GENERATION_OFFSET,
    decode_log,
)

MIB = 1024 * 1024
PARTITION_BYTES = 32 * MIB
SLOT_BYTES = 4 * MIB
SLOT_BLOCKS = SLOT_BYTES // 4096
PAIR_BYTES = 2 * SLOT_BYTES
PARTITION_NAME = "unixnu_logs"
PARTITION_GUID = "cde9ab32-18d8-4777-8ced-691e7cd5c636"
XNU_BODY_MAGIC = b"DALI-XNU-BODY1"
XNU_BODY_HEADER = 64
XNU_MSGBUF_CAPACITY = MIB
XNU_CONSOLE_CAPACITY = 0x200000 - 12


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def userdata_partition(baseline):
    """Return the exact new partition only when both layout and write gates match."""
    if (not isinstance(baseline, dict) or baseline.get("kind") != "dali-ufs-android-read-only-baseline" or
            baseline.get("android", {}).get("product") != "dali" or
            baseline.get("android", {}).get("slot_suffix") != "_b" or
            not baseline.get("android", {}).get("root", "").startswith("uid=0(") or
            baseline.get("all_partition_hashes_match") is not True or
            baseline.get("known_block_matches_backup") is not True or
            baseline.get("device_writes_performed") is not False or
            baseline.get("device_reboot_performed") is not False):
        raise ValueError("rooted Android B read-only UFS baseline is required")
    geometry = baseline.get("geometry")
    unit = geometry.get("unit_descriptor") if isinstance(geometry, dict) else None
    if (not isinstance(geometry, dict) or geometry.get("geometry_verified") is not True or
            geometry.get("lun") != 2 or geometry.get("logical_block_bytes") != 4096 or
            geometry.get("capacity_blocks") != 64577536 or geometry.get("capacity_bytes") != 64577536 * 4096 or
            geometry.get("sysfs_read_only") is not False or geometry.get("gpt_read_only") is not False or
            geometry.get("write_protection_observed_android") is not True or
            geometry.get("unit_write_protected") is not False or not isinstance(unit, dict) or
            unit.get("lu_enable") != 1 or unit.get("lun_write_protect") != 0 or
            unit.get("logical_block_size") != 12 or unit.get("logical_block_count") != 64577536):
        raise ValueError("live UFS LUN 2 geometry/write-protection evidence differs")
    gpt = baseline.get("gpt")
    parts = gpt.get("partitions") if isinstance(gpt, dict) else None
    matches = [p for p in parts if isinstance(p, dict) and p.get("name") == PARTITION_NAME] if isinstance(parts, list) else []
    if len(matches) != 1:
        raise ValueError("GPT must contain exactly one unixnu_logs partition")
    part = matches[0]
    if (part.get("number") != 98 or part.get("guid") != PARTITION_GUID or
            part.get("first_lba") != 64565240 or part.get("last_lba") != 64573431 or
            part.get("blocks") != 8192 or part.get("attributes") != 0 or
            part.get("gpt_read_only") is not False or
            part.get("type_guid") != str(uuid.uuid5(uuid.NAMESPACE_URL, "https://unixnu.com/partitions/diagnostic-log-v1"))):
        raise ValueError("GPT unixnu_logs identity or extent differs from the provisioned layout")
    return dict(part, bytes=PARTITION_BYTES)


def _read_regular(path, expected_bytes):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            raise ValueError("userdata log capture must be an exact regular file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(expected_bytes + 1)
    finally:
        os.close(fd)
    if len(data) != expected_bytes:
        raise ValueError("userdata log capture changed length")
    return data


def validate_baseline_captures(baseline, baseline_dir, original_boot_b):
    """Recompute raw reciprocal GPT, original boot sample and device identity."""
    import ufs_geometry
    from ufs_identity import validate_captured_identity

    root = Path(baseline_dir).resolve(strict=True)
    captures = baseline.get("captures")
    if not isinstance(captures, dict):
        raise ValueError("baseline captures are missing")
    captured = {}
    for label in ("primary_header", "primary_entries", "backup_header", "backup_entries", "boot_b_first_block"):
        record = captures.get(label)
        limit = 4096 if "header" in label or label == "boot_b_first_block" else ufs_geometry.MAX_ENTRY_BYTES
        if (not isinstance(record, dict) or not isinstance(record.get("file"), str) or
                Path(record["file"]).name != record["file"] or type(record.get("bytes")) is not int or
                not 0 < record["bytes"] <= limit or ("header" in label or label == "boot_b_first_block") and record["bytes"] != 4096):
            raise ValueError("invalid bounded UFS baseline capture: " + label)
        data = _read_regular(root / record["file"], record.get("bytes"))
        if sha256(data) != record.get("sha256"):
            raise ValueError("UFS baseline capture hash mismatch: " + label)
        captured[label] = data
    geometry = baseline["geometry"]
    gpt = ufs_geometry.validate_gpt(captured["primary_header"], captured["primary_entries"],
                                    captured["backup_header"], captured["backup_entries"],
                                    4096, geometry["capacity_bytes"])
    if gpt != baseline.get("gpt"):
        raise ValueError("captured reciprocal GPT differs from the baseline")
    original = Path(original_boot_b).read_bytes()
    if (sha256(original) != baseline.get("original_boot_b_sha256") or
            captured["boot_b_first_block"] != original[:4096]):
        raise ValueError("original boot_b backup/sample differs from the baseline")
    binding = validate_captured_identity(baseline, root)
    return dict(gpt=gpt, device_binding_sha256=binding, original_boot_b_sha256=sha256(original))


def validate_partition_snapshot(raw, baseline, sequence):
    """Check exact length and require only this run's UEFI/XNU pair to be empty."""
    part = userdata_partition(baseline)
    if not isinstance(raw, bytes) or len(raw) != PARTITION_BYTES:
        raise ValueError("expected the complete 32 MiB unixnu_logs image")
    if type(sequence) is not int or not 0 < sequence <= 4:
        raise ValueError("userdata log sequence must be in 1..4")
    start = (sequence - 1) * PAIR_BYTES
    pair = raw[start:start + PAIR_BYTES]
    if len(pair) != PAIR_BYTES or any(pair):
        raise ValueError("selected userdata log owner slots are not both pristine")
    return dict(partition=part, partition_bytes=len(raw), partition_sha256=sha256(raw),
                sequence=sequence, sequence_pair_offset=start, sequence_pair_bytes=PAIR_BYTES,
                sequence_pair_sha256=sha256(pair), sequence_pair_is_zero=True)


def decode_slot(slot, *, expected_owner, expected_sequence, expected=None):
    if not isinstance(slot, bytes) or len(slot) != SLOT_BYTES:
        raise ValueError("userdata slot must be exactly 4 MiB")
    if not any(slot):
        return None
    block = 4096
    header = slot[block:2 * block]
    magic = header[:16]
    if magic == MAGIC_V1 and struct.unpack_from("<I", header, 20)[0] == 1:
        schema = 1
    elif magic == MAGIC_V2 and struct.unpack_from("<I", header, 20)[0] == 2:
        schema = 2
    else:
        raise ValueError("userdata log slot header magic is invalid")
    try:
        token = header[24:56].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("userdata log token is not ASCII") from None
    if (not re.fullmatch(r"[0-9a-f]{32}", token) or
            struct.unpack_from("<I", header, 56)[0] != expected_owner or
            struct.unpack_from("<Q", header, 60)[0] != expected_sequence or
            struct.unpack_from("<I", header, 72)[0] != block or
            struct.unpack_from("<Q", header, 76)[0] != SLOT_BLOCKS):
        raise ValueError("userdata log slot owner/sequence/geometry is misplaced")
    bindings = dict(schema=schema, token=token, owner=expected_owner, sequence=expected_sequence,
                    block_bytes=block, device_binding=header[84:116].hex(),
                    candidate_sha256=header[116:148].hex(), source_sha256=header[148:180].hex())
    if schema == 2:
        bindings.update(
            generation=struct.unpack_from("<Q", header, V2_GENERATION_OFFSET)[0],
            flags=struct.unpack_from("<I", header, V2_FLAGS_OFFSET)[0],
            capture_stage=struct.unpack_from("<I", header, V2_CAPTURE_STAGE_OFFSET)[0],
            body_format=struct.unpack_from("<I", header, V2_BODY_FORMAT_OFFSET)[0],
        )
    if expected is not None:
        for name, value in expected.items():
            if value is not None and name in bindings and value != bindings[name]:
                raise ValueError("userdata log slot binding differs: " + name)
    body, result = decode_log(slot, token=token, device_binding=bindings["device_binding"],
                              candidate_sha256=bindings["candidate_sha256"], source_sha256=bindings["source_sha256"],
                              block_bytes=block, owner=expected_owner, sequence=expected_sequence,
                              window_bytes=SLOT_BYTES, expected_schema=schema,
                              expected_generation=bindings.get("generation"),
                              expected_flags=bindings.get("flags"),
                              expected_capture_stage=bindings.get("capture_stage"),
                              expected_body_format=bindings.get("body_format"))
    return dict(body=body, record=result, bindings=bindings)


def decode_xnu_body(body):
    if not isinstance(body, bytes) or len(body) < XNU_BODY_HEADER or body[:14] != XNU_BODY_MAGIC:
        raise ValueError("XNU log body magic/length is invalid")
    version, header_bytes, diagnostic_bytes, msg_bytes, msg_size, msg_write, msg_read, console_bytes, console_capacity, result, stage, total = struct.unpack_from(
        "<12I", body, 16)
    if (version != 1 or header_bytes != XNU_BODY_HEADER or total != len(body) or
            not 0 < diagnostic_bytes <= 1024 * 1024 or
            msg_bytes != msg_size or not 0 < msg_size <= XNU_MSGBUF_CAPACITY or
            msg_write >= msg_size or msg_read >= msg_size or
            console_capacity != XNU_CONSOLE_CAPACITY or console_bytes > console_capacity or
            total != header_bytes + diagnostic_bytes + msg_bytes + console_bytes):
        raise ValueError("XNU message-buffer body metadata is inconsistent")
    at = header_bytes
    diagnostic = body[at:at + diagnostic_bytes]; at += diagnostic_bytes
    msgbuf = body[at:at + msg_bytes]; at += msg_bytes
    console = body[at:at + console_bytes]
    return dict(version=version, diagnostic=diagnostic, msgbuf_ring=msgbuf,
                msgbuf_size=msg_size, msgbuf_write_index=msg_write, msgbuf_read_index=msg_read,
                console_ring=console, console_capacity=console_capacity,
                controller_result=result, controller_stage=stage)


def decode_partition(raw, *, expected=None):
    if not isinstance(raw, bytes) or len(raw) != PARTITION_BYTES:
        raise ValueError("expected complete 32 MiB userdata log partition")
    result = dict(schema=2, bytes=len(raw), sha256=sha256(raw), entries=[])
    required = set(expected.get("required_owners", ())) if isinstance(expected, dict) else set()
    for sequence in range(1, 5):
        for owner in (1, 2):
            index = (sequence - 1) * 2 + (owner - 1)
            start = index * SLOT_BYTES
            slot = raw[start:start + SLOT_BYTES]
            want = expected if isinstance(expected, dict) and expected.get("sequence") == sequence and owner in required else None
            decoded = decode_slot(slot, expected_owner=owner, expected_sequence=sequence, expected=want)
            if decoded is None:
                if want is not None:
                    raise ValueError("required userdata log owner slot is empty")
                continue
            entry = dict(sequence=sequence, owner=owner, offset=start, slot_bytes=SLOT_BYTES,
                         bindings=decoded["bindings"], record=decoded["record"], body_sha256=sha256(decoded["body"]))
            if owner == 2:
                entry["xnu_body"] = decode_xnu_body(decoded["body"])
            else:
                entry["uefi_console_bytes"] = len(decoded["body"])
            result["entries"].append(entry)
            result.setdefault("bodies", {})[str(index)] = decoded["body"]
    v2_pairs = []
    for sequence in range(1, 5):
        pair = [entry for entry in result["entries"] if entry["sequence"] == sequence]
        owners = {entry["owner"]: entry for entry in pair}
        uefi = owners.get(1)
        xnu = owners.get(2)
        if xnu is not None and xnu["record"]["schema"] == 2:
            if uefi is None or uefi["record"]["schema"] != 2:
                raise ValueError("v2 XNU slot has no matching v2 UEFI slot")
        if uefi is not None and uefi["record"]["schema"] == 2:
            if xnu is not None:
                if (xnu["record"]["schema"] != 2 or
                        xnu["record"]["generation"] != uefi["record"]["generation"] or
                        xnu["bindings"]["token"] != uefi["bindings"]["token"]):
                    raise ValueError("v2 UEFI/XNU pair metadata differs")
            v2_pairs.append(dict(sequence=sequence, generation=uefi["record"]["generation"],
                                 token=uefi["bindings"]["token"], owners=sorted(owners)))
    result["v2_generation_order"] = sorted(v2_pairs, key=lambda pair: pair["generation"])
    generations = [pair["generation"] for pair in v2_pairs]
    if len(generations) != len(set(generations)):
        raise ValueError("multiple v2 pairs claim the same generation")
    result["v1_order"] = "unknown"
    if expected is not None:
        selected = [e for e in result["entries"] if e["sequence"] == expected.get("sequence")]
        owners = {e["owner"] for e in selected}
        result["required_owner_slots_verified"] = set(expected.get("required_owners", ())) <= owners
    return result


def _export_artifacts(raw, decoded, *, decode_error=None, supplemental_files=None):
    """Render deterministic evidence files and their manifest in memory."""
    if not isinstance(raw, bytes) or len(raw) != PARTITION_BYTES:
        raise ValueError("expected complete raw userdata log partition")
    files = {"unixnu_logs.img": raw}
    entries = []
    for entry in (decoded["entries"] if decoded is not None else []):
        body = decoded["bodies"][str((entry["sequence"] - 1) * 2 + entry["owner"] - 1)]
        prefix = f"seq-{entry['sequence']}-{'uefi' if entry['owner'] == 1 else 'xnu'}"
        body_name = prefix + "-body.bin"
        files[body_name] = body
        if entry["owner"] == 1:
            view = {"console": body}
        else:
            xnu = decode_xnu_body(body)
            view = {"diagnostic": xnu["diagnostic"], "msgbuf-ring": xnu["msgbuf_ring"],
                    "console-ring": xnu["console_ring"]}
            entry["xnu_body"] = {k: v for k, v in xnu.items() if isinstance(v, int)}
        views = {}
        for name, data in view.items():
            filename = prefix + "-" + name + ".bin"
            files[filename] = data
            if name != "diagnostic":
                text_name = prefix + "-" + name + ".txt"
                text_data = data.decode("utf-8", errors="replace").encode("utf-8")
                files[text_name] = text_data
                views[name + "_text"] = dict(file=text_name, bytes=len(text_data), sha256=sha256(text_data))
            views[name] = dict(file=filename, bytes=len(data), sha256=sha256(data))
        entries.append(dict(sequence=entry["sequence"], owner=entry["owner"], token=entry["bindings"]["token"],
                            record_schema=entry["record"]["schema"],
                            generation=entry["record"].get("generation"),
                            body_file=body_name, body_bytes=len(body), body_sha256=sha256(body), files=views,
                            record=entry["record"], bindings=entry["bindings"], xnu_body=entry.get("xnu_body")))
    supplements = []
    if supplemental_files is not None:
        if not isinstance(supplemental_files, dict):
            raise ValueError("supplemental export files must be a mapping")
        for name, data in sorted(supplemental_files.items()):
            if (not isinstance(name, str) or not re.fullmatch(r"ramoops-[A-Za-z0-9_-]+\.bin", name) or
                    not isinstance(data, bytes) or not data):
                raise ValueError("invalid supplemental ramoops capture")
            files[name] = data
            supplements.append(dict(file=name, bytes=len(data), sha256=sha256(data)))
    manifest = dict(schema=2, kind="dali-userdata-ufs-log-export", partition_file="unixnu_logs.img",
                    partition_bytes=len(raw), partition_sha256=sha256(raw), entries=entries,
                    v1_order=decoded.get("v1_order", "unknown") if decoded is not None else "unknown",
                    v2_generation_order=decoded.get("v2_generation_order", []) if decoded is not None else [],
                    supplemental_files=supplements,
                    physical_write_qualified=False, persistence_qualified=False)
    if decode_error is not None:
        manifest["decode_error"] = str(decode_error)
    return files, manifest


def write_export(directory, raw, decoded, *, decode_error=None, supplemental_files=None):
    """Write raw evidence and readable owner-specific views without overwriting."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    files, manifest = _export_artifacts(raw, decoded, decode_error=decode_error,
                                       supplemental_files=supplemental_files)
    for name, data in files.items():
        path = directory / name
        with path.open("xb") as output:
            os.chmod(path, 0o400); output.write(data); output.flush(); os.fsync(output.fileno())
    manifest_path = directory / "manifest.json"
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with manifest_path.open("xb") as output:
        os.chmod(manifest_path, 0o400); output.write(manifest_bytes); output.flush(); os.fsync(output.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return manifest


def _legacy_v1_manifest(manifest):
    """Reconstruct the prior export manifest when every stored record is v1."""
    if any(entry.get("record_schema") != 1 for entry in manifest.get("entries", [])):
        return None
    legacy = dict(manifest)
    legacy["schema"] = 1
    legacy.pop("v1_order", None)
    legacy.pop("v2_generation_order", None)
    entries = []
    for entry in legacy.get("entries", []):
        old_entry = dict(entry)
        old_entry.pop("record_schema", None)
        old_entry.pop("generation", None)
        bindings = dict(old_entry["bindings"])
        bindings.pop("schema", None)
        old_entry["bindings"] = bindings
        entries.append(old_entry)
    legacy["entries"] = entries
    return legacy


def validate_export(directory, raw, decoded, *, decode_error=None, supplemental_files=None):
    """Require an existing export to exactly match current raw and ramoops data."""
    directory = Path(directory)
    files, manifest = _export_artifacts(raw, decoded, decode_error=decode_error,
                                       supplemental_files=supplemental_files)
    manifest_path = directory / "manifest.json"
    manifest_bytes = _read_local_manifest(manifest_path)
    recorded = json.loads(manifest_bytes)
    legacy = _legacy_v1_manifest(manifest)
    if recorded != manifest and (legacy is None or recorded != legacy):
        raise ValueError("existing userdata export manifest differs from live evidence")
    expected_names = set(files) | {"manifest.json"}
    actual_names = {entry.name for entry in directory.iterdir()}
    if actual_names != expected_names:
        raise ValueError("existing userdata export file inventory differs")
    for name, expected in files.items():
        actual = _read_regular(directory / name, len(expected))
        if actual != expected:
            raise ValueError("existing userdata export file differs: " + name)
    return manifest


def _read_local_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > 4 * 1024 * 1024:
            raise ValueError("userdata export manifest is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(data) != info.st_size:
        raise ValueError("userdata export manifest changed while reading")
    return data
