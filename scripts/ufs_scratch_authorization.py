"""Build a host-validated, token-bound XNU scratch grant.

This is an input validator for the future manual scratch runner. It performs no
device I/O and does not qualify runtime controller ownership or media writes.
"""
import hashlib
import json
import re
from pathlib import Path
import uuid

import ufs_geometry as ufs
from ufs_identity import validate_captured_identity
from dali_ufs_resources import encode_scratch_grant
from unattended_layout import boot_image_extent, plan_scratch, verify_original_avb_and_scratch
from ufs_expdb import validate_expdb_backup


TOKEN = re.compile(r"[0-9a-f]{32}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def source_binding_sha256(manifest):
    """Hash the exact source revisions and kernel artifact represented by an XNU manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("source XNU manifest must be an object")
    fields = ("device_commit", "unixnu_commit", "uefi_commit", "xnu_commit")
    identity = {}
    for field in fields:
        value = manifest.get(field)
        if not isinstance(value, str) or not COMMIT.fullmatch(value):
            raise ValueError("source XNU manifest has an invalid " + field)
        identity[field] = value
    kernel_sha = manifest.get("kernel_sha256")
    if not isinstance(kernel_sha, str) or not SHA256.fullmatch(kernel_sha):
        raise ValueError("source XNU manifest has an invalid kernel_sha256")
    identity["kernel_sha256"] = kernel_sha
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(b"DALI-UFS-SOURCE-V1\0" + canonical).hexdigest()


def build_xnu_scratch_grant(*, serial, token, sequence, baseline, baseline_dir, source_manifest, kernel_bytes,
                            candidate_image, original_boot_b, target_partition="boot_b",
                            expdb_baseline=None, expdb_dir=None):
    """Validate offline inputs, then return grant/wire/proposal bound to one XNU token.

    ``candidate_sha256`` intentionally names the exact source-XNU kernel payload:
    the final boot image contains this grant and therefore cannot hash itself.
    The scratch runner separately seals the final image hash in its durable lease.
    ``baseline_dir`` retains the exact identity sysfs captures; a JSON digest
    alone cannot supply the UFS device identity for a write-grant input.
    """
    if not isinstance(serial, str) or not serial or len(serial) > 128:
        raise ValueError("invalid device serial")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError("invalid fresh scratch run token")
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("scratch sequence must be a positive integer")
    if target_partition not in ("boot_b", "expdb"):
        raise ValueError("unsupported scratch target partition")
    if not isinstance(baseline, dict) or baseline.get("kind") != "dali-ufs-android-read-only-baseline":
        raise ValueError("verified Android UFS baseline is required")
    if (baseline.get("serial") != serial or baseline.get("all_partition_hashes_match") is not True or
            baseline.get("known_block_matches_backup") is not True or
            baseline.get("device_writes_performed") is not False or
            baseline.get("device_reboot_performed") is not False):
        raise ValueError("baseline identity or read-only provenance does not match this device")
    original = Path(original_boot_b).read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    if baseline.get("original_boot_b_sha256") != original_sha:
        raise ValueError("baseline does not bind the supplied original boot_b")

    geometry = baseline.get("geometry")
    if (not isinstance(geometry, dict) or geometry.get("geometry_verified") is not True or
            geometry.get("write_protection_observed_android") is not True or
            geometry.get("sysfs_read_only") is not False or geometry.get("gpt_read_only") is not False or
            geometry.get("unit_write_protected") is not False):
        raise ValueError("Android baseline geometry or write-protection observation is incomplete")
    device_binding = validate_captured_identity(baseline, baseline_dir)

    block_bytes = ufs.block_size(geometry["logical_block_bytes"])
    capacity_bytes = ufs.natural(geometry.get("capacity_bytes"), "baseline LU capacity", positive=True)
    capacity_blocks = ufs.natural(geometry.get("capacity_blocks"), "baseline LU blocks", positive=True)
    lun = ufs.natural(geometry.get("lun"), "baseline LUN")
    if lun >= 8 or capacity_bytes % block_bytes or capacity_blocks != capacity_bytes // block_bytes:
        raise ValueError("baseline UFS LUN/capacity geometry is inconsistent")
    part = geometry.get("boot_b")
    if not isinstance(part, dict) or part.get("name") != "boot_b" or part.get("bytes") != len(original):
        raise ValueError("baseline boot_b geometry differs from the original backup")
    first = ufs.natural(part.get("first_lba"), "boot_b start LBA")
    blocks = ufs.natural(part.get("blocks"), "boot_b blocks", positive=True)
    if (part.get("last_lba") != first + blocks - 1 or part["bytes"] != blocks * block_bytes or
            first >= capacity_blocks or blocks > capacity_blocks - first):
        raise ValueError("baseline boot_b geometry is internally inconsistent")
    guid = part.get("guid")
    try:
        if not isinstance(guid, str) or str(uuid.UUID(guid)) != guid:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("baseline boot_b GUID is invalid") from None
    gpt = baseline.get("gpt")
    table = gpt.get("partitions") if isinstance(gpt, dict) else None
    if not isinstance(table, list):
        raise ValueError("baseline lacks the validated GPT partition table")
    matches = [row for row in table if isinstance(row, dict) and row.get("name") == "boot_b"]
    if (len(matches) != 1 or matches[0].get("guid") != guid or matches[0].get("first_lba") != first or
            matches[0].get("blocks") != blocks or matches[0].get("last_lba") != part["last_lba"]):
        raise ValueError("baseline boot_b geometry differs from the captured GPT")
    unit = geometry.get("unit_descriptor")
    if (not isinstance(unit, dict) or unit.get("lu_enable") != 1 or unit.get("lun_write_protect") != 0 or
            unit.get("logical_block_size") != block_bytes.bit_length() - 1 or
            unit.get("logical_block_count") != capacity_blocks):
        raise ValueError("baseline UFS unit descriptor differs from the boot_b geometry")
    source_sha = source_binding_sha256(source_manifest)
    kernel_sha = hashlib.sha256(kernel_bytes).hexdigest()
    if source_manifest.get("kernel_sha256") != kernel_sha:
        raise ValueError("XNU kernel bytes differ from the pinned source manifest")

    if target_partition == "expdb":
        boot_payload_end = boot_image_extent(candidate_image, len(original))
        verified_expdb = validate_expdb_backup(expdb_baseline, expdb_dir, serial=serial,
                                             geometry=geometry, reference_gpt=gpt, original_boot_sha=original_sha)
        part = verified_expdb["partition"]
        proposal = dict(verified_expdb, schema=1, kind="offline-expdb-scratch-proposal",
                        candidate_bytes=len(candidate_image), candidate_sha256=hashlib.sha256(candidate_image).hexdigest(),
                        boot_payload_end_bytes=boot_payload_end, device_access_performed=False,
                        media_writes_qualified=False)
        avb = None  # Candidate is in boot_b; expdb has no boot-image extent.
    else:
        if expdb_baseline is not None or expdb_dir is not None:
            raise ValueError("expdb evidence cannot be attached to a legacy boot_b grant")
        proposal = plan_scratch(candidate_image, len(original))
        avb = verify_original_avb_and_scratch(original_boot_b, len(original), proposal["scratch"])
        if (avb.get("scratch_disjoint") is not True or avb.get("scratch_was_zero_in_original") is not True or
                avb.get("tail_guard_covers_footer") is not True or not avb.get("tail_guard_only_footer_data")):
            raise ValueError("original boot_b AVB/scratch layout is not safe for a scratch test")

    logical = block_bytes
    offset = proposal["scratch"]["offset_bytes"]
    length = proposal["scratch"]["bytes"]
    if offset % logical or length % logical:
        raise ValueError("scratch reservation is not aligned to the actual UFS block size")
    scratch_start = part["first_lba"] + offset // logical
    scratch_blocks = length // logical
    tail_guard_blocks = proposal["scratch"]["tail_guard_bytes"] // logical
    boot_blocks = part["blocks"]
    payload_end = 0 if target_partition == "expdb" else proposal["boot_payload_end_bytes"]
    grant = {
        "profile": "expdb-scratch-write-test-v1" if target_partition == "expdb" else "boot-b-scratch-write-test-v1",
        "target_partition": target_partition, "write_authorized": True, "media_writes": True,
        "token": token, "device_binding_sha256": device_binding,
        "candidate_sha256": kernel_sha, "source_sha256": source_sha,
        "partition_guid": str(uuid.UUID(part["guid"])), "lun": lun,
        "block_bytes": logical, "capacity_blocks": capacity_blocks,
        "boot_start_lba": part["first_lba"], "boot_blocks": boot_blocks,
        "payload_end_bytes": payload_end, "scratch_start_lba": scratch_start,
        "scratch_blocks": scratch_blocks, "tail_guard_blocks": tail_guard_blocks,
        "sequence": sequence,
    }
    if target_partition == "expdb":
        grant["original_partition_sha256"] = proposal["original_partition_sha256"]
        grant["original_scratch_sha256"] = proposal["original_scratch_sha256"]
    wire = encode_scratch_grant(grant)
    return grant, wire, dict(proposal, original_avb=avb)
