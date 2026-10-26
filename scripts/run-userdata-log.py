#!/usr/bin/env python3
"""Run a bounded UEFI/XNU userdata-log trial and restore original Android B.

One fresh token owns one 8 MiB UEFI/XNU slot pair. The same immutable candidate
boots three times: WRITE, UEFI READBACK plus XNU write, then UEFI READBACK plus
XNU existing-record verification. ramoops remains enabled. After manual fastboot,
the original boot_b is restored and the full raw log partition is exported to
the dedicated exFAT storage at /sdcard/UniXNU/Logs/<token>.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tomllib

SCRIPTS = Path(__file__).resolve().parent
DEVICE = SCRIPTS.parent
WORKSPACE = DEVICE.parent
sys.path.insert(0, str(SCRIPTS))
probe_spec = importlib.util.spec_from_file_location("userdata_trial_probe", SCRIPTS / "run-ufs-probe.py")
probe = importlib.util.module_from_spec(probe_spec); probe_spec.loader.exec_module(probe)
from dali_ufs_resources import encode_scratch_grant
from ufs_expdb_restore import load_restore_baseline
from ufs_scratch_authorization import build_userdata_logs_grant, source_binding_sha256
from ufs_userdata_android import AndroidUserdataLogs, _layout_state
from ufs_userdata_logs import (PARTITION_BYTES, decode_partition, sha256,
                               validate_baseline_captures, validate_export,
                               validate_partition_snapshot, write_export)

cycle = probe.cycle
TOKEN = re.compile(r"[0-9a-f]{32}\Z")
KIND = "dali-userdata-logs-trial"


def copy_bytes(data, path, mode=0o400):
    path = Path(path)
    with path.open("xb") as output:
        os.chmod(path, mode)
        output.write(data); output.flush(); os.fsync(output.fileno())


def read_regular(path, *, exact_bytes=None, maximum_bytes=64 * 1024 * 1024):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > maximum_bytes or
                exact_bytes is not None and info.st_size != exact_bytes):
            raise cycle.CycleError("userdata trial input must be a bounded regular file: " + str(path))
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(maximum_bytes + 1)
    finally:
        os.close(fd)
    if len(data) != info.st_size or exact_bytes is not None and len(data) != exact_bytes:
        raise cycle.CycleError("userdata trial input changed length while reading: " + str(path))
    return data


def durable_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".partial")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600); json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n"); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def snapshot_baseline(source_dir, destination, *, serial, original_boot_b):
    source_dir = Path(source_dir).resolve(strict=True)
    raw = read_regular(source_dir / "baseline.json", maximum_bytes=4 * 1024 * 1024)
    report = json.loads(raw)
    destination = Path(destination); destination.mkdir(mode=0o700)
    for name, record in report.get("captures", {}).items():
        filename = record.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise cycle.CycleError("userdata baseline contains an unsafe capture path")
        source = source_dir / filename
        data = read_regular(source, exact_bytes=record.get("bytes"), maximum_bytes=4 * 1024 * 1024)
        if len(data) != record.get("bytes") or sha256(data) != record.get("sha256"):
            raise cycle.CycleError("userdata baseline capture changed: " + name)
        copy_bytes(data, destination / filename)
    copy_bytes(raw, destination / "baseline.json")
    baseline = load_restore_baseline(destination, serial=serial, original_boot_b=original_boot_b)
    validate_baseline_captures(baseline, destination, original_boot_b)
    return baseline


def capture_live_inputs(args, run, records):
    if args.dry_run:
        if args.baseline_dir is None or args.log_image is None:
            raise cycle.CycleError("dry-run needs a saved current baseline and complete userdata log image")
        baseline_source = args.baseline_dir
        image_source = args.log_image
    else:
        collector_spec = importlib.util.spec_from_file_location("userdata_live_baseline", SCRIPTS / "collect-ufs-baseline.py")
        collector = importlib.util.module_from_spec(collector_spec); collector_spec.loader.exec_module(collector)
        baseline_source = run / "live-baseline-source"
        collector.collect(args.serial, args.backup, baseline_source)
        image_source = None
    baseline_dir = run / "ufs-baseline"
    baseline = snapshot_baseline(baseline_source, baseline_dir, serial=args.serial,
                                 original_boot_b=args.backup / "boot_b.img")
    if args.dry_run:
        raw = read_regular(image_source, exact_bytes=PARTITION_BYTES, maximum_bytes=PARTITION_BYTES)
        initial = validate_partition_snapshot(raw, baseline, args.sequence)
        copy_bytes(raw, run / "original-unixnu_logs.img")
        device = dict(serial=args.serial, node="offline-capture", lun=2, block_bytes=4096,
                      partition=initial["partition"], device_binding_sha256=baseline["ufs_device_binding_sha256"],
                      read_only=False)
    else:
        io = AndroidUserdataLogs(cycle, args.serial, records, baseline)
        first_layout = io.observe(); first = io.read_partition()
        second_layout = io.observe(); raw = io.read_partition()
        if first_layout != second_layout or first != raw:
            raise cycle.CycleError("live userdata log partition changed during pre-candidate snapshot")
        initial = validate_partition_snapshot(raw, baseline, args.sequence)
        copy_bytes(raw, run / "original-unixnu_logs.img")
        device = second_layout
    durable_json(run / "pre-build-live-originals.json", dict(android=baseline["android"],
        all18_original_hashes_verified=baseline["all_partition_hashes_match"],
        original_boot_b_sha256=baseline["original_boot_b_sha256"], baseline_sha256=baseline["baseline_sha256"],
        log_partition_sha256=initial["partition_sha256"], sequence_pair_sha256=initial["sequence_pair_sha256"],
        device=device, device_writes_performed=False, device_reboot_performed=False))
    return baseline, baseline_dir, raw, initial


def provisional_boot_image(original):
    if len(original) < 4096:
        raise cycle.CycleError("original boot_b backup is truncated")
    length = int.from_bytes(original[8:12], "little")
    if length < 4 or length > len(original) - 4096:
        raise cycle.CycleError("original boot_b kernel extent is invalid")
    image = original[:4096 + length]
    image += bytes((-len(image)) % 4096)
    return image


def build_trial(args, lease):
    for repo in (DEVICE, args.unixnu): cycle.verify_clean_repository(repo)
    cycle.verify_clean_repository(args.uefi, allow_mu_patches=True)
    _, records = cycle.verify_backup(args.backup, args.serial)
    _layout_state(args.layout_root, args.serial)
    kernel, source = cycle.verify_kernel_artifact(args.artifact_dir, args.unixnu, DEVICE, reset_qualification=True)
    if source.get("device_commit") != cycle.run_capture(["git", "rev-parse", "HEAD"], cwd=DEVICE).strip():
        raise cycle.CycleError("source-XNU kernel does not match the current Dali device revision")
    token = secrets.token_hex(16); lease.require_new(token)
    args.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.evidence_root, 0o700)
    run = args.evidence_root / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + token[:8])
    run.mkdir(mode=0o700)
    baseline, baseline_dir, log_raw, snapshot = capture_live_inputs(args, run, records)
    live_fdt = baseline_dir / baseline["captures"]["live_fdt"]["file"]
    fdt_bytes = live_fdt.read_bytes()
    cycle.verify_live_layout(fdt_bytes, args.backup, args.uefi)
    adt = run / "dali.adt"
    cycle.run_logged([sys.executable, DEVICE / "scripts/make-dali-adt.py", "--fdt", live_fdt,
                      "--output", adt, "--dram-size", "0x300000000", "--cpu-regs", "0",
                      "--psci-method", "smc", "--ufs-readonly"], run / "adt.log", timeout=60)
    candidate = run / "candidate"; candidate.mkdir(mode=0o700)
    kernel_bytes = kernel.read_bytes()
    copy_bytes(kernel_bytes, candidate / "source-kernel.macho")
    copy_bytes((args.artifact_dir / "manifest.json").read_bytes(), candidate / "source-kernel-manifest.json")
    provisional = provisional_boot_image((args.backup / "boot_b.img").read_bytes())
    inputs = dict(serial=args.serial, token=token, sequence=args.sequence, baseline=baseline,
                  baseline_dir=baseline_dir, source_manifest=source, kernel_bytes=kernel_bytes,
                  candidate_image=provisional, original_boot_b=args.backup / "boot_b.img",
                  log_partition_bytes=log_raw)
    grant, wire, proposal = build_userdata_logs_grant(**inputs)
    copy_bytes(wire, candidate / "grant.bin")
    packed = run / "candidate-build"; packed.mkdir(mode=0o700)
    boot, manifest = cycle.build_candidate(args, args.backup, candidate / "source-kernel.macho", adt,
        source["recovery"], token, packed, xnu_ufs_readonly=True, live_fdt=live_fdt,
        userdata_log_wire=candidate / "grant.bin")
    final_grant, final_wire, final_proposal = build_userdata_logs_grant(**dict(inputs, candidate_image=boot.read_bytes()))
    if final_grant != grant or final_wire != wire:
        raise cycle.CycleError("final userdata candidate changed the bound live grant")
    proposal = final_proposal
    for filename, value in (("grant.json", json.dumps(grant, indent=2).encode() + b"\n"),
                            ("proposal.json", json.dumps(proposal, indent=2).encode() + b"\n")):
        copy_bytes(value, candidate / filename)
    for name, path in {
        "UniXNU-dali-uefi-boot.img": boot,
        "SILICIUM_UEFI.fd": args.uefi / "Build/daliPkg/DEBUG_CLANGPDB/FV/SILICIUM_UEFI.fd",
        "payload.img": args.uefi / "out/payload.img", "runtime-live.dtb": live_fdt,
        "kernel.macho.adt": adt,
    }.items(): copy_bytes(path.read_bytes(), candidate / name)
    copy_bytes(json.dumps(manifest, indent=2).encode() + b"\n", candidate / "manifest.json")
    checked, checked_manifest = probe.verify_candidate(candidate, args.uefi, args.unixnu, args.backup, "userdata-log")
    if checked_manifest != manifest or cycle.sha256(checked.read_bytes()) != manifest["sha256"]:
        raise cycle.CycleError("sealed userdata log candidate changed during independent verification")
    binding = dict(token=token, device_binding=grant["device_binding_sha256"],
                   candidate_sha256=grant["candidate_sha256"], source_sha256=grant["source_sha256"],
                   block_bytes=grant["block_bytes"], owner_slots=(1, 2), sequence=grant["sequence"],
                   partition_guid=grant["partition_guid"], partition_start_lba=grant["boot_start_lba"],
                   partition_blocks=grant["boot_blocks"], initial_partition_sha256=snapshot["partition_sha256"],
                   initial_pair_sha256=snapshot["sequence_pair_sha256"])
    restore_command = [sys.executable, str(Path(__file__).resolve()), "--serial", args.serial,
        "--fastboot-serial", args.fastboot_serial, "--backup", str(args.backup),
        "--layout-root", str(args.layout_root), "--restore-run", str(run), "--wait-seconds", "600"]
    state = dict(schema=1, kind=KIND, dry_run=args.dry_run, serial=args.serial,
        fastboot_serial=args.fastboot_serial, backup=str(args.backup), token=token,
        sequence=args.sequence, candidate_sha256=manifest["sha256"],
        candidate_record_sha256=cycle.sha256((candidate / "manifest.json").read_bytes()),
        original_boot_b_sha256=records["boot_b"]["sha256"], baseline_sha256=baseline["baseline_sha256"],
        original_userdata_logs_sha256=snapshot["partition_sha256"], log_binding=binding,
        candidate_verified_before_flash=True, device_verified_before_flash=False,
        boot_b_may_be_modified=False, userdata_logs_may_be_modified=False,
        userdata_logs_capture_verified=False, userdata_logs_android_export_verified=False,
        ramoops_retained=True, media_writes_qualified=False, persistence_qualified=False,
        restore_command=restore_command)
    durable_json(run / "restore-state.json", state)
    return run, state, records, baseline


def validate_run(args, lease):
    run = args.restore_run.resolve(strict=True)
    state = json.loads((run / "restore-state.json").read_bytes())
    if (not isinstance(state, dict) or state.get("schema") != 1 or state.get("kind") != KIND or
            state.get("dry_run") is not False or state.get("serial") != args.serial or
            state.get("fastboot_serial") != args.fastboot_serial or state.get("backup") != str(args.backup) or
            lease.data.get("pending_run") != str(run) or lease.data.get("pending_userdata_logs") is not True or
            not TOKEN.fullmatch(state.get("token", "")) or state.get("candidate_verified_before_flash") is not True or
            state.get("device_verified_before_flash") is not True):
        raise cycle.CycleError("userdata log recovery does not match the canonical pending trial")
    _, records = cycle.verify_backup(args.backup, args.serial)
    if records["boot_b"]["sha256"] != state.get("original_boot_b_sha256"):
        raise cycle.CycleError("userdata log restore backup hash differs")
    baseline = load_restore_baseline(run / "ufs-baseline", serial=args.serial,
                                     original_boot_b=args.backup / "boot_b.img")
    validate_baseline_captures(baseline, run / "ufs-baseline", args.backup / "boot_b.img")
    if baseline["baseline_sha256"] != state.get("baseline_sha256"):
        raise cycle.CycleError("userdata log recovery baseline changed")
    original_log = read_regular(run / "original-unixnu_logs.img", exact_bytes=PARTITION_BYTES,
                                maximum_bytes=PARTITION_BYTES)
    snapshot = validate_partition_snapshot(original_log, baseline, state["sequence"])
    candidate_dir = run / "candidate"
    manifest_raw = read_regular(candidate_dir / "manifest.json", maximum_bytes=4 * 1024 * 1024)
    image = read_regular(candidate_dir / "UniXNU-dali-uefi-boot.img", maximum_bytes=64 * 1024 * 1024)
    grant = json.loads(read_regular(candidate_dir / "grant.json", maximum_bytes=64 * 1024))
    wire = read_regular(candidate_dir / "grant.bin", exact_bytes=240, maximum_bytes=240)
    kernel = read_regular(candidate_dir / "source-kernel.macho", maximum_bytes=128 * 1024 * 1024)
    source = json.loads(read_regular(candidate_dir / "source-kernel-manifest.json", maximum_bytes=1024 * 1024))
    binding = state.get("log_binding")
    if (not isinstance(binding, dict) or cycle.sha256(image) != state.get("candidate_sha256") or
            cycle.sha256(manifest_raw) != state.get("candidate_record_sha256") or
            wire != encode_scratch_grant(grant) or len(wire) != 240 or
            grant.get("profile") != "userdata-logs-diagnostic-v1" or grant.get("target_partition") != "unixnu_logs" or
            grant.get("token") != state["token"] or grant.get("sequence") != state["sequence"] or
            binding.get("token") != state["token"] or
            grant.get("device_binding_sha256") != binding.get("device_binding") or
            grant.get("device_binding_sha256") != baseline.get("ufs_device_binding_sha256") or
            grant.get("candidate_sha256") != cycle.sha256(kernel) or source.get("kernel_sha256") != cycle.sha256(kernel) or
            grant.get("candidate_sha256") != binding.get("candidate_sha256") or
            grant.get("source_sha256") != source_binding_sha256(source) or
            grant.get("source_sha256") != binding.get("source_sha256") or
            binding.get("initial_partition_sha256") != snapshot["partition_sha256"] or
            binding.get("initial_pair_sha256") != snapshot["sequence_pair_sha256"] or
            grant.get("partition_guid") != snapshot["partition"]["guid"] or
            grant.get("boot_start_lba") != snapshot["partition"]["first_lba"] or
            grant.get("boot_blocks") != snapshot["partition"]["blocks"]):
        raise cycle.CycleError("userdata log recovery candidate/grant/source binding differs")
    return run, state, records, baseline


def _raw_scorer(text, token):
    return dict(run_token=token, raw_characters=len(text), token_seen=token in text,
                snapshot_pass=False, media_writes_qualified=False, persistence_qualified=False)


def score_userdata_transcript(text, token, sequence):
    """Require token-bound write/sync records and later owner readback records."""
    prefix = "DALI-UFS-LOG: run=" + token + " "
    xnu_prefix = "DALI-XNU-UFS: run=" + token + " "
    lines = [line for line in text.splitlines() if line.startswith(prefix) or line.startswith(xnu_prefix)]
    begin_pattern = re.compile(re.escape(prefix) + r"mode=([12]) target=2 begin sequence=(\d+) owner=1")
    result_pattern = re.compile(re.escape(prefix) +
        r"mode=([12]) result=([^ ]+) attempted=1 submitted=([01]) synced=([01]) verified=1 bytes=(\d+) sequence=(\d+) sha256=([0-9a-f]{64})")
    begins, results = [], []
    for line in lines:
        match = begin_pattern.fullmatch(line)
        if match and int(match[2]) == sequence: begins.append(int(match[1]))
        match = result_pattern.fullmatch(line)
        if match and int(match[6]) == sequence and match[2] in ("Success", "EFI_SUCCESS"):
            results.append(dict(mode=int(match[1]), submitted=int(match[3]), synced=int(match[4]),
                                bytes=int(match[5]), sha256=match[7]))
    writes = [entry for entry in results if entry["mode"] == 1 and entry["submitted"] == 1 and entry["synced"] == 1]
    reads = [entry for entry in results if entry["mode"] == 2 and entry["submitted"] == 0]
    xnu_pattern = re.compile(re.escape(xnu_prefix) + r"stage=([^ ]+) a=([0-9a-f]+) b=([0-9a-f]+) c=([0-9a-f]+)")
    stages = []
    for line in lines:
        match = xnu_pattern.fullmatch(line)
        if match: stages.append((match[1], int(match[2], 16), int(match[3], 16), int(match[4], 16)))
    attempts = [entry for entry in stages if entry[0] == "userdata-log-attempt-submitted" and entry[1] == sequence]
    verified = [entry for entry in stages if entry[0] == "userdata-log-verified" and entry[1] == sequence]
    existing = [entry for entry in stages if entry[0] == "userdata-log-existing-record" and entry[1] == sequence]
    finished = [entry for entry in stages if entry[0] == "userdata-log-finish" and entry[1:3] == (1, 1)]
    write = writes[0] if len(writes) == 1 else None
    readbacks_match_write = bool(write and len(reads) >= 2 and all(
        entry["bytes"] == write["bytes"] and entry["sha256"] == write["sha256"] for entry in reads))
    uefi_write = len(writes) == 1 and begins.count(1) == 1
    uefi_readback = readbacks_match_write and begins.count(2) >= 2
    xnu_write_sync = len(attempts) == 1 and len(verified) >= 2 and len(finished) >= 2
    xnu_readback = len(existing) >= 1 and len(verified) >= 2
    return dict(run_token=token, uefi_write_sync_pass=uefi_write,
        uefi_same_candidate_readback_pass=uefi_readback,
        xnu_write_sync_immediate_readback_pass=xnu_write_sync,
        xnu_same_candidate_reboot_readback_pass=xnu_readback,
        current_token_records=len(lines), firmware_write_records=len(writes),
        firmware_readback_records=len(reads), xnu_attempt_records=len(attempts),
        xnu_existing_records=len(existing), xnu_verified_records=len(verified),
        ramoops_capture_has_qualification_evidence=bool(lines),
        media_writes_qualified=bool(uefi_write and xnu_write_sync),
        persistence_qualified=bool(uefi_write and uefi_readback and xnu_write_sync and xnu_readback),
        physical_write_qualified=False)


def load_ramoops_text(run):
    output = Path(run) / "collect"
    result_path = output / "result.json"
    result = json.loads(result_path.read_bytes()) if result_path.is_file() else {}
    texts, files = [], []
    for item in result.get("files", []):
        name = item.get("name", "")
        if not re.fullmatch(r"capture-[A-Za-z0-9_-]+/console-ramoops-[0-9]+", name):
            raise cycle.CycleError("ramoops evidence path is not a fixed private capture")
        path = (output / name).resolve(strict=True)
        if output.resolve() not in path.parents:
            raise cycle.CycleError("ramoops capture escaped the trial directory")
        data = path.read_bytes()
        if len(data) != item.get("bytes") or sha256(data) != item.get("sha256"):
            raise cycle.CycleError("ramoops capture hash/length changed")
        texts.append(data.decode("utf-8", errors="replace"))
        files.append(dict(name="ramoops-" + path.name + ".bin", data=data))
    return "\n".join(texts), files


def _write_exact(path, data):
    path = Path(path)
    if path.exists():
        old = path.read_bytes()
        if old != data: raise cycle.CycleError("immutable userdata log capture changed on retry")
        return
    copy_bytes(data, path)


def post_restore_capture(args, run, state, records, baseline):
    android = cycle.current_android(args.serial)
    hashes = cycle.compare_partition_hashes(args.serial, records)
    io = AndroidUserdataLogs(cycle, args.serial, records, baseline)
    device = io.observe(); raw = io.read_partition(); after = io.observe(); confirm = io.read_partition()
    if device != after or raw != confirm:
        raise cycle.CycleError("userdata log identity or bytes changed during the stable restore capture")
    raw_path = run / "captured-unixnu_logs.img"
    _write_exact(raw_path, raw)
    expected = dict(sequence=state["sequence"], token=state["token"],
        device_binding=state["log_binding"]["device_binding"],
        candidate_sha256=state["log_binding"]["candidate_sha256"],
        source_sha256=state["log_binding"]["source_sha256"], required_owners=(1, 2))
    decoded = None; decode_error = None
    try:
        decoded = decode_partition(raw, expected=expected)
    except (ValueError, TypeError) as error:
        decode_error = str(error)
        # Preserve an inspectable raw partition in Android even when one owner
        # record is torn or absent; this never turns partial data into a pass.
        try: decoded = decode_partition(raw)
        except (ValueError, TypeError): decoded = None
    ramoops = probe.collect_now(args.serial, run, state["token"], _raw_scorer)
    if any(key in ramoops for key in ("collection_error", "collection_store_error")):
        raise cycle.CycleError("ramoops capture could not be saved; leave userdata trial pending")
    ramoops_text, ramoops_files = load_ramoops_text(run)
    qualification = score_userdata_transcript(ramoops_text, state["token"], state["sequence"])
    supplements = {item["name"]: item["data"] for item in ramoops_files}
    export_dir = run / "userdata-export"
    if not export_dir.exists():
        manifest = write_export(export_dir, raw, decoded, decode_error=decode_error, supplemental_files=supplements)
    else:
        try:
            manifest = validate_export(export_dir, raw, decoded, decode_error=decode_error,
                                       supplemental_files=supplements)
        except (OSError, ValueError, TypeError) as error:
            raise cycle.CycleError("existing host userdata export differs from captured raw/ramoops evidence: " + str(error)) from None
    export = io.export_files(export_dir, args.layout_root, state["token"],
                             expected_partition=raw, expected_manifest=manifest)
    both = bool(decoded and decoded.get("required_owner_slots_verified"))
    state.update(userdata_logs_capture_verified=True, userdata_logs_android_export_verified=export.get("verified") is True,
        userdata_logs_may_be_modified=False, userdata_logs_partition_sha256=sha256(raw),
        userdata_logs_export=export, userdata_log_decode_error=decode_error,
        userdata_log_owners_verified=both, original_boot_b_restored=True,
        all_partition_hashes_match=True, android_root_verified=True, android=android,
        partition_hashes=hashes, ramoops=ramoops, userdata_log_qualification=qualification,
        media_writes_qualified=qualification["media_writes_qualified"] and both,
        persistence_qualified=qualification["persistence_qualified"] and both)
    durable_json(run / "restore-state.json", state)
    return dict(android=android, partition_hashes=hashes, userdata_logs_export=export,
        userdata_log_owners_verified=both, userdata_log_decode_error=decode_error,
        userdata_log_qualification=qualification, ramoops=ramoops,
        original_boot_b_restored=True, all_18_original_hashes_match=True,
        media_writes_qualified=state["media_writes_qualified"],
        persistence_qualified=state["persistence_qualified"])


def restore_android(args, lease):
    run, state, records, baseline = validate_run(args, lease)
    callback = lambda: post_restore_capture(args, run, state, records, baseline)
    already_original = False
    if cycle.adb_online(args.serial):
        try:
            cycle.current_android(args.serial)
            hashes = cycle.compare_partition_hashes(args.serial, records)
            if hashes.get("boot_b") != records["boot_b"]["sha256"]:
                raise cycle.CycleError("candidate boot_b is still active")
            already_original = True
        except cycle.CycleError:
            if cycle.adb_online(args.serial): cycle.request_fastboot_from_android(args.serial, args.fastboot_serial, args.wait_seconds)
    if already_original:
        result = callback()
        cycle.compare_partition_hashes(args.serial, records)
        durable_json(run / "recovery-verification.json", result)
        durable_json(run / "trial-result.json", dict(result, complete_qualification=False,
            media_writes_qualified=state.get("media_writes_qualified", False),
            persistence_qualified=state.get("persistence_qualified", False)))
        lease.restored(run, state)
        return dict(result, restore_ledger_cleared=True)
    if not cycle.wait_fastboot(args.fastboot_serial, args.wait_seconds):
        raise cycle.CycleError("fastboot absent; userdata log trial and original boot_b restoration remain pending")
    cycle.restore_android_safely(args.serial, args.fastboot_serial, args.backup, records,
                                 args.wait_seconds, post_boot=callback)
    hashes = cycle.compare_partition_hashes(args.serial, records)
    android = cycle.current_android(args.serial)
    state.update(original_boot_b_restored=True, all_partition_hashes_match=True,
                 android_root_verified=True, android=android, partition_hashes=hashes)
    durable_json(run / "restore-state.json", state)
    report = dict(android=android, partition_hashes=hashes,
        userdata_logs_export=state.get("userdata_logs_export"),
        userdata_log_owners_verified=state.get("userdata_log_owners_verified"),
        userdata_log_decode_error=state.get("userdata_log_decode_error"),
        userdata_log_qualification=state.get("userdata_log_qualification"),
        ramoops=state.get("ramoops"), original_boot_b_restored=True,
        all_18_original_hashes_match=True,
        media_writes_qualified=state.get("media_writes_qualified", False),
        persistence_qualified=state.get("persistence_qualified", False))
    durable_json(run / "recovery-verification.json", report)
    durable_json(run / "trial-result.json", dict(report, complete_qualification=False))
    lease.restored(run, state)
    return dict(report, restore_ledger_cleared=True)


def execute_trial(args, lease, run, state, records, baseline):
    io = AndroidUserdataLogs(cycle, args.serial, records, baseline)
    io.observe(); current = io.read_partition(); io.observe()
    if sha256(current) != state["original_userdata_logs_sha256"]:
        raise cycle.CycleError("userdata log partition changed immediately before candidate flash")
    cycle.current_android(args.serial)
    cycle.compare_partition_hashes(args.serial, records)
    cycle.request_fastboot_from_android(args.serial, args.fastboot_serial, args.wait_seconds)
    cycle.verify_fastboot(args.fastboot_serial, require_slot_b=True)
    cycle.verify_original_boot_b(args.backup / "boot_b.img", records["boot_b"])
    candidate = run / "candidate/UniXNU-dali-uefi-boot.img"
    if cycle.sha256(candidate.read_bytes()) != state["candidate_sha256"]:
        raise cycle.CycleError("sealed userdata candidate changed immediately before flash")
    state.update(device_verified_before_flash=True, userdata_logs_may_be_modified=True)
    lease.arm(run, state)
    durable_json(run / "restore-state.json", state)
    print("ORIGINAL-ONLY RECOVERY: " + shlex.join(state["restore_command"]), flush=True)
    boot_steps = (
        "Boot 1: at the UEFI userdata log menu choose WRITE, confirm Power; after the normal menu appears choose Return to firmware diagnostics, confirm, then hold only Volume Down in the 10-second window to FASTBOOT.",
        "Boot 2: the host reboots the SAME candidate without reflashing. Choose READBACK and confirm Power, then choose UniXNU single-core early boot. Let XNU run about 30 seconds, then enter FASTBOOT with Power+Volume Down.",
        "Boot 3: the host reboots the SAME candidate again without reflashing. Choose READBACK and confirm Power, launch UniXNU single-core early boot, let it run about 30 seconds, then enter FASTBOOT with Power+Volume Down. The host then restores original Android B and exports userdata logs.")
    try:
        cycle.flash_boot_b(args.fastboot_serial, candidate)
        cycle.fastboot_command(args.fastboot_serial, "reboot", timeout=20)
        for index, instruction in enumerate(boot_steps):
            print(instruction, flush=True)
            if not cycle.wait_fastboot_disconnect(args.fastboot_serial, 20):
                raise cycle.CycleError("fastboot did not disconnect at candidate reboot boundary " + str(index + 1))
            if not cycle.wait_fastboot(args.fastboot_serial, args.wait_seconds):
                raise cycle.CycleError("manual fastboot recovery remains pending after candidate boot " + str(index + 1))
            if index + 1 < len(boot_steps):
                cycle.fastboot_command(args.fastboot_serial, "reboot", timeout=20)
        result = restore_android(args, lease)
    except BaseException:
        try:
            if cycle.fastboot_online(args.fastboot_serial):
                restore_android(args, lease)
        finally:
            if lease.data.get("pending_run") == str(run):
                print("RESTORE PENDING: " + shlex.join(state["restore_command"]), flush=True)
        raise
    print(json.dumps(result, indent=2))
    return 0 if (result.get("userdata_log_owners_verified") and
                 result.get("persistence_qualified")) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--fastboot-serial")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--uefi", type=Path)
    parser.add_argument("--unixnu", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--layout-root", type=Path, required=True)
    parser.add_argument("--sequence", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--log-image", type=Path)
    parser.add_argument("--restore-run", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args()
    args.fastboot_serial = args.fastboot_serial or args.serial
    if args.fastboot_serial != args.serial or not 120 <= args.wait_seconds <= 600:
        parser.error("one matching Android/fastboot serial and a bounded 120..600s recovery deadline are required")
    args.backup = args.backup.expanduser().resolve(strict=True)
    if args.restore_run is not None:
        if args.dry_run or any(getattr(args, key) is not None for key in ("uefi", "unixnu", "artifact_dir", "evidence_root", "baseline_dir", "log_image")):
            parser.error("restore-only accepts no candidate, source, baseline or dry-run inputs")
        args.layout_root = args.layout_root.expanduser().resolve(strict=True)
        args.backup = cycle.require_private_path(args.backup, (DEVICE, DEVICE.parent / "UEFI-Dali", DEVICE.parent / "UniXNU"), "backup")
        args.restore_run = cycle.require_private_path(args.restore_run, (), "restore run")
        state_root = probe.device_state_root(args.backup)
        if (probe.paths_overlap(args.restore_run, args.backup) or
                probe.paths_overlap(args.restore_run, state_root) or
                probe.paths_overlap(args.restore_run, args.layout_root)):
            parser.error("restore run must stay separate from immutable backup, layout and device-state ledger")
        _layout_state(args.layout_root, args.serial)
        with probe.DeviceLease(args.serial, state_root) as lease:
            result = restore_android(args, lease)
        print(json.dumps(result, indent=2)); return 0
    if any(getattr(args, key) is None for key in ("uefi", "unixnu", "artifact_dir", "evidence_root")):
        parser.error("new userdata trial needs UEFI, UniXNU, source artifact and private evidence paths")
    if args.dry_run != (args.baseline_dir is not None and args.log_image is not None):
        parser.error("dry-run requires both --baseline-dir and --log-image; live trials collect fresh inputs")
    args.uefi = args.uefi.expanduser().resolve(strict=True)
    args.unixnu = args.unixnu.expanduser().resolve(strict=True)
    args.artifact_dir = args.artifact_dir.expanduser().resolve(strict=True)
    args.evidence_root = args.evidence_root.expanduser().resolve()
    args.layout_root = args.layout_root.expanduser().resolve(strict=True)
    repos = (DEVICE, args.uefi, args.unixnu)
    for key in ("backup", "artifact_dir", "evidence_root", "layout_root"):
        setattr(args, key, cycle.require_private_path(getattr(args, key), repos, key))
    if args.dry_run:
        args.baseline_dir = cycle.require_private_path(args.baseline_dir.resolve(strict=True), repos, "baseline")
        args.log_image = cycle.require_private_path(args.log_image.resolve(strict=True), repos, "userdata log image")
    state_root = probe.device_state_root(args.backup)
    protected = (args.backup, args.artifact_dir, state_root, args.layout_root,
                 *([args.baseline_dir, args.log_image] if args.dry_run else []))
    if any(probe.paths_overlap(args.evidence_root, path) for path in protected):
        parser.error("evidence output must be separate from backups, source artifacts, layout and device ledger")
    with probe.DeviceLease(args.serial, state_root) as lease:
        run, state, records, baseline = build_trial(args, lease)
        if args.dry_run:
            print("DRY RUN PASS: sealed userdata UEFI/XNU log candidate and recovery/export inputs verified; no device contacted. " + str(run))
            return 0
        return execute_trial(args, lease, run, state, records, baseline)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (cycle.CycleError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("run-userdata-log: " + str(error), file=sys.stderr); raise SystemExit(1)
