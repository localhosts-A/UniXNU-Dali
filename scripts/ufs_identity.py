"""Preserve and independently revalidate Linux UFS identity observations.

These are exact sysfs output bytes, not a claim to have captured a raw UFS
descriptor. The printable-ASCII serial is reconstructed by the same algorithm
as the existing XNU binding; runtime matching remains a separate requirement.
"""
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat

import ufs_geometry as ufs


ALGORITHM = "dali-ufs-manufacturer-serial-v1"
ATTRIBUTES = {
    "manufacturer_id": ("device_descriptor/manufacturer_id", "ufs-manufacturer-id.txt", 7),
    "serial_number": ("string_descriptors/serial_number", "ufs-serial-number.txt", 127),
}


def observation(controller, raw):
    """Decode at most one sysfs newline; never strip printable serial spaces."""
    if not isinstance(controller, str) or not controller or "\0" in controller:
        raise ValueError("invalid UFS identity controller path")
    values = {}
    for field, (_, _, limit) in ATTRIBUTES.items():
        data = raw.get(field) if isinstance(raw, dict) else None
        if not isinstance(data, bytes) or not 0 < len(data) <= limit:
            raise ValueError("missing or oversized UFS identity sysfs bytes")
        try:
            values[field] = data.removesuffix(b"\n").decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("UFS identity sysfs bytes are not lossless ASCII") from None
    digest = ufs.device_identity_binding(values["manufacturer_id"], values["serial_number"])
    return dict(schema=1, algorithm=ALGORITHM, controller_path=controller, **values), digest


def read_identity(controller, read_bytes):
    """Perform only two existing sysfs cat reads, returning bytes for capture."""
    raw = {field: read_bytes("cat " + shlex.quote(controller + "/" + relative))
           for field, (relative, _, _) in ATTRIBUTES.items()}
    record, digest = observation(controller, raw)
    return record, digest, raw


def validate_captured_identity(baseline, baseline_dir):
    """Recompute a grant binding from bounded, hash-checked capture files.

    A legacy digest-only baseline remains useful for read-only diagnostics but
    cannot supply these stronger offline write-grant inputs. No device access
    or runtime authorization is performed here.
    """
    record = baseline.get("ufs_device_identity")
    captures = baseline.get("captures")
    scsi = baseline.get("scsi_path")
    if (not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != 1 or
            record.get("algorithm") != ALGORITHM or not isinstance(captures, dict) or
            not isinstance(scsi, str) or "/host" not in scsi):
        raise ValueError("baseline lacks independently captured UFS identity evidence")
    controller = scsi.split("/host", 1)[0]
    if not controller.endswith("/16810000.ufshci") or record.get("controller_path") != controller:
        raise ValueError("UFS identity evidence belongs to another controller")
    if not isinstance(baseline_dir, (str, os.PathLike)):
        raise ValueError("UFS identity capture directory is required")
    root = Path(baseline_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("UFS identity capture directory is invalid")
    raw = {}
    for field, (_, filename, limit) in ATTRIBUTES.items():
        capture = captures.get("ufs_" + field)
        if (not isinstance(capture, dict) or capture.get("file") != filename or
                type(capture.get("bytes")) is not int or not 0 < capture["bytes"] <= limit or
                not isinstance(capture.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", capture["sha256"])):
            raise ValueError("UFS identity capture metadata is invalid")
        # Fixed single-component names and NOFOLLOW prevent a capture reference
        # from opening a symlink or arbitrary path outside its selected folder.
        with os.fdopen(os.open(root / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("UFS identity capture is not a regular file")
            data = source.read(limit + 1)
        if len(data) != capture["bytes"] or hashlib.sha256(data).hexdigest() != capture["sha256"]:
            raise ValueError("UFS identity capture bytes differ from the baseline")
        raw[field] = data
    observed, digest = observation(controller, raw)
    if any(record.get(field) != value for field, value in observed.items()):
        raise ValueError("UFS identity fields differ from captured sysfs bytes")
    if baseline.get("ufs_device_binding_sha256") != digest:
        raise ValueError("UFS identity digest differs from captured sysfs bytes")
    return digest
