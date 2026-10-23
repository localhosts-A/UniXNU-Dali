#!/usr/bin/env python3
"""Build and run bounded, boot_b-only Dali XNU recovery cycles.

The private backup is created from rooted Android when its path does not exist.
The only fastboot write issued by this program is `flash boot_b`; no slot,
misc, LK, preloader, AVB or other partition command is implemented here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import tomllib


DEVICE_ROOT = Path(__file__).resolve().parents[1]
PARTITIONS = tuple(
    name + "_" + slot
    for name in ("boot", "init_boot", "vendor_boot", "dtbo", "vbmeta", "vbmeta_system", "vbmeta_vendor", "lk")
    for slot in "ab"
) + ("misc", "boot_para")
DEFAULT_GH_REPO = "localhosts-A/UniXNU-Dali"


class CycleError(RuntimeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def run_capture(command, *, timeout=30, cwd=None, env=None):
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CycleError(f"command failed or timed out: {shlex.join(map(str, command))}: {error}") from None
    if result.returncode != 0:
        raise CycleError(f"command returned {result.returncode}: {shlex.join(map(str, command))}\n{result.stdout[-8000:]}")
    return result.stdout


def run_logged(command, log_path, *, timeout, cwd=None, env=None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        log.write("$ " + shlex.join(map(str, command)) + "\n")
        log.flush()
        try:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CycleError(f"command failed or timed out; see {log_path}: {error}") from None
    if result.returncode != 0:
        tail = log_path.read_text(errors="replace")[-8000:]
        raise CycleError(f"command returned {result.returncode}; see {log_path}\n{tail}")
    print(f"completed: {shlex.join(map(str, command))} (log: {log_path})", flush=True)


def verify_clean_repository(repository, *, allow_mu_patches=False):
    status = run_capture(["git", "status", "--porcelain", "--ignore-submodules=all"], cwd=repository)
    if status.strip():
        raise CycleError(f"source checkout must be clean: {repository}\n{status.strip()}")
    submodules = run_capture(["git", "submodule", "status"], cwd=repository)
    for line in submodules.splitlines():
        if line and line[0] != " ":
            raise CycleError(f"a source submodule is missing or not pinned in {repository}: {line}")
    if submodules.strip():
        command = ("if [ \"$name\" != \"Mu_Basecore\" ]; then "
                   "git diff --quiet --ignore-submodules=all HEAD --; fi") if allow_mu_patches else \
                  "git diff --quiet --ignore-submodules=all HEAD --"
        run_capture(["git", "submodule", "foreach", command], cwd=repository)
    if allow_mu_patches:
        run_capture([sys.executable, repository / "tools/dali/verify_mu_patches.py",
                     "--mu", repository / "Mu_Basecore",
                     "--patch-dir", repository / "Resources/MuPatches"], cwd=repository)


def require_private_path(path, repositories, label):
    resolved = path.expanduser().resolve()
    for repository in repositories:
        root = repository.resolve()
        if resolved == root or root in resolved.parents:
            raise CycleError(f"{label} must stay outside Git repositories: {resolved}")
    return resolved


def adb_output(serial, command, timeout=30):
    remote = "su -c " + shlex.quote(command)
    try:
        return subprocess.check_output(["adb", "-s", serial, "exec-out", remote], timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise CycleError(f"ADB root command failed: {command}: {error}") from None


def adb_property(serial, name):
    try:
        return subprocess.check_output(["adb", "-s", serial, "exec-out", "getprop", name],
                                       timeout=15).decode().strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise CycleError(f"could not read Android property {name}: {error}") from None


def adb_states(serial):
    try:
        output = subprocess.check_output(["adb", "devices"], timeout=10, text=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    states = []
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[0] == serial:
            states.append(fields[1])
    return states


def adb_serial_count(serial):
    return len(adb_states(serial))


def adb_online(serial):
    return "device" in adb_states(serial)


def fastboot_serial_count(serial):
    try:
        output = subprocess.check_output(["fastboot", "devices"], timeout=10, text=True,
                                         stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0
    return sum(line.split()[:2] == [serial, "fastboot"] for line in output.splitlines() if line.strip())


def fastboot_online(serial):
    return fastboot_serial_count(serial) > 0


def wait_fastboot(serial, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fastboot_online(serial):
            return True
        time.sleep(1)
    return False


def wait_fastboot_disconnect(serial, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not fastboot_online(serial):
            return True
        time.sleep(0.5)
    return False


def wait_android(serial, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if adb_online(serial):
            try:
                if adb_property(serial, "ro.product.device") == "dali" and \
                        adb_property(serial, "sys.boot_completed") == "1":
                    return True
            except CycleError:
                pass
        time.sleep(1)
    return False


def current_android(serial, *, require_booted=True):
    states = adb_states(serial)
    if states != ["device"]:
        raise CycleError(f"expected exactly one ready ADB device with serial {serial}, found states={states}")
    product = adb_property(serial, "ro.product.device")
    slot = adb_property(serial, "ro.boot.slot_suffix")
    root_id = adb_output(serial, "id").decode(errors="replace").strip()
    if product != "dali" or "uid=0(" not in root_id:
        raise CycleError(f"expected rooted dali Android; product={product!r}, id={root_id!r}")
    if slot != "_b":
        raise CycleError(f"active Android slot must remain _b, got {slot!r}")
    if require_booted and adb_property(serial, "sys.boot_completed") != "1":
        raise CycleError("Android has not completed boot")
    return {"product": product, "slot_suffix": slot, "root": root_id}


def partition_records(manifest):
    records = {}
    for filename, record in manifest.get("files", {}).items():
        source = record.get("source", "")
        prefix = "/dev/block/by-name/"
        if not source.startswith(prefix):
            continue
        name = source[len(prefix):]
        if name not in PARTITIONS or filename != name + ".img":
            raise CycleError(f"unexpected backed partition record: {filename} -> {source}")
        records[name] = record
    if set(records) != set(PARTITIONS):
        missing = sorted(set(PARTITIONS) - set(records))
        raise CycleError(f"verified backup is missing partition records: {missing}")
    return records


def verify_backup(path, serial):
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise CycleError(f"backup is not complete: {manifest_path} is missing")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise CycleError(f"invalid backup manifest: {error}") from None
    if not manifest.get("complete") or manifest.get("product") != "dali" or manifest.get("slot_suffix") != "_b":
        raise CycleError("backup must be complete, for dali, and captured from active slot _b")
    if manifest.get("serial") != serial:
        raise CycleError("backup serial does not match the attached Android device")
    records = partition_records(manifest)
    for name, record in records.items():
        image = path / (name + ".img")
        if not image.is_file() or image.stat().st_size != record.get("bytes") or sha256(image.read_bytes()) != record.get("sha256"):
            raise CycleError(f"private backup image checksum failed: {image}")
    for name in ("runtime.dtb",):
        record = manifest.get("files", {}).get(name)
        file = path / name
        if not record or not file.is_file() or file.stat().st_size != record.get("bytes") or sha256(file.read_bytes()) != record.get("sha256"):
            raise CycleError(f"private backup FDT checksum failed: {file}")
    return manifest, records


def current_partition_hashes(serial):
    paths = ["/dev/block/by-name/" + name for name in PARTITIONS]
    output = adb_output(serial, "sha256sum " + " ".join(shlex.quote(path) for path in paths), timeout=240)
    found = {}
    for line in output.decode(errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            name = Path(fields[-1]).name
            if name in PARTITIONS and re.fullmatch(r"[0-9a-f]{64}", fields[0]):
                found[name] = fields[0]
    if set(found) != set(PARTITIONS):
        raise CycleError("could not hash all backed boot/companion/LK partitions from Android")
    return found


def compare_partition_hashes(serial, records, *, allow_boot_b_candidate=False):
    current = current_partition_hashes(serial)
    mismatches = []
    for name, record in records.items():
        if allow_boot_b_candidate and name == "boot_b":
            continue
        if current[name] != record["sha256"]:
            mismatches.append(name)
    if mismatches:
        raise CycleError(f"backed partition hashes differ from the protected baseline: {mismatches}")
    if not allow_boot_b_candidate and current["boot_b"] != records["boot_b"]["sha256"]:
        raise CycleError("boot_b does not match the verified original image")
    return current


def artifact_directory(args, run_dir):
    if args.artifact_dir:
        return args.artifact_dir.expanduser().resolve()
    gh = shutil.which("gh")
    if gh is None:
        raise CycleError("GitHub CLI `gh` is required to download the latest one-day kernel artifact")
    listing = run_capture([gh, "run", "list", "--repo", args.gh_repo, "--workflow", "dali-xnu.yml",
                          "--status", "success", "-L", "1", "--json", "databaseId,headSha"], timeout=30)
    try:
        runs = json.loads(listing)
        run_id = runs[0]["databaseId"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise CycleError("no successful Dali XNU GitHub Actions run is available") from None
    destination = run_dir / "downloaded-artifact"
    destination.mkdir(mode=0o700)
    run_logged([gh, "run", "download", str(run_id), "--repo", args.gh_repo,
                "--name", "dali-xnu-source", "--dir", str(destination)],
               run_dir / "download-artifact.log", timeout=180)
    return destination


def verify_kernel_artifact(directory, unixnu, device_repo):
    kernel = directory / "kernel.macho"
    manifest_path = directory / "manifest.json"
    if not kernel.is_file() or not manifest_path.is_file():
        raise CycleError("source artifact must contain kernel.macho and manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise CycleError(f"invalid source artifact manifest: {error}") from None
    if (manifest.get("schema") != 1 or manifest.get("target") != "dali" or
            manifest.get("artifact_name") != "dali-xnu-source" or manifest.get("source_only") is not True or
            manifest.get("retention_days") != 1):
        raise CycleError("artifact is not the reviewed one-day Dali source-only kernel")
    data = kernel.read_bytes()
    if len(data) != manifest.get("kernel_bytes") or sha256(data) != manifest.get("kernel_sha256"):
        raise CycleError("source kernel size/SHA256 does not match its manifest")
    if manifest.get("unixnu_commit") != run_capture(["git", "rev-parse", "HEAD"], cwd=unixnu).strip():
        raise CycleError("source artifact was built from a different UniXNU commit")
    if manifest.get("device_commit") != run_capture(["git", "rev-parse", "HEAD"], cwd=device_repo).strip():
        raise CycleError("source artifact was built from a different UniXNU-Dali commit")
    board = tomllib.loads((device_repo / "board.toml").read_text())
    expected_uefi = board.get("firmware", {}).get("uefi_commit")
    if not expected_uefi or manifest.get("uefi_commit") != expected_uefi:
        raise CycleError("source artifact does not pin the reviewed UEFI-Dali revision")
    feeds = tomllib.loads((unixnu / "feeds.toml").read_text())
    xnu_source = next((entry for entry in feeds.get("source", []) if entry.get("name") == "xnu"), None)
    if xnu_source is None or manifest.get("xnu_commit") != xnu_source.get("commit"):
        raise CycleError("source artifact was built from a different pinned XNU source revision")
    recovery = manifest.get("recovery", {})
    if recovery.get("auto_fastboot") is not True:
        raise CycleError("automatic hardware cycles require auto_fastboot=true in the source artifact")
    try:
        host_timeout = int(recovery.get("fastboot_timeout_seconds", 0))
        watchdog_timeout = int(recovery.get("device_watchdog_timeout_seconds", 0))
    except (TypeError, ValueError):
        raise CycleError("invalid recovery timeouts in source artifact") from None
    if not 2 <= watchdog_timeout <= 31:
        raise CycleError("invalid MT6991 watchdog interval in source artifact")
    if not watchdog_timeout + 30 <= host_timeout <= 600:
        raise CycleError("host recovery timeout must exceed the device watchdog by at least 30 seconds")
    return kernel, manifest


def verify_live_layout(fdt_data, backup, uefi):
    sys.path.insert(0, str(uefi / "tools/dali"))
    from fdt import Fdt
    try:
        live = Fdt(fdt_data)
        live_layout = live.validate()
        live_io = live.validate_io()
        saved = Fdt((backup / "runtime.dtb").read_bytes())
        if live_layout != saved.validate() or live_io != saved.validate_io():
            raise CycleError("live FDT memory/MMIO layout differs from the verified backup")
    except (OSError, ValueError) as error:
        raise CycleError(f"live FDT preflight failed: {error}") from None


def fastboot_command(serial, *args, timeout=30):
    return run_capture(["fastboot", "-s", serial, *args], timeout=timeout)


def verify_fastboot(serial, *, require_slot_b):
    count = fastboot_serial_count(serial)
    if count != 1:
        raise CycleError(f"expected exactly one fastboot device with serial {serial}, found {count}")
    product = fastboot_command(serial, "getvar", "product", timeout=20)
    if not re.search(r"product:\s*dali(?:\s|$)", product, re.I):
        raise CycleError(f"fastboot product was not dali: {product.strip()}")
    if require_slot_b:
        current = fastboot_command(serial, "getvar", "current-slot", timeout=20)
        if not re.search(r"current-slot:\s*(?:b|_b)(?:\s|$)", current, re.I):
            raise CycleError(f"fastboot active slot was not B; refusing candidate flash: {current.strip()}")


def fastboot_serial_number(serial):
    output = fastboot_command(serial, "getvar", "serialno", timeout=20)
    match = re.search(r"(?:\(bootloader\)\s*)?serialno:\s*([^\s]+)", output, re.I)
    return match.group(1).strip() if match else ""


def verify_device_identity(adb_serial, fastboot_serial):
    if adb_serial == fastboot_serial:
        return
    adb_identity = adb_property(adb_serial, "ro.serialno").strip()
    if not adb_identity or adb_identity.lower() in {"unknown", "0", "none"}:
        adb_identity = adb_property(adb_serial, "ro.boot.serialno").strip()
    fastboot_identity = fastboot_serial_number(fastboot_serial)
    if not adb_identity or not fastboot_identity or adb_identity.casefold() != fastboot_identity.casefold():
        raise CycleError("ADB and fastboot serials differ and their stable device serials could not be matched")


def flash_boot_b(serial, image, timeout=180):
    # Keep the target partition literal: there is no user-supplied partition argument.
    run_capture(["fastboot", "-s", serial, "flash", "boot_b", str(image)], timeout=timeout)


def request_fastboot_from_android(adb_serial, fastboot_serial, timeout):
    if fastboot_serial_count(fastboot_serial) != 0:
        raise CycleError(f"fastboot target {fastboot_serial} was already online before the Android reboot")
    run_capture(["adb", "-s", adb_serial, "reboot", "bootloader"], timeout=20)
    deadline = time.monotonic() + timeout
    adb_disconnected = False
    while time.monotonic() < deadline:
        if adb_serial_count(adb_serial) == 0:
            adb_disconnected = True
            break
        time.sleep(0.5)
    if not adb_disconnected:
        raise CycleError("ADB device did not disconnect after reboot-to-bootloader")
    if not wait_fastboot(fastboot_serial, max(1, deadline - time.monotonic())):
        raise CycleError("Android rebooted but fastboot did not enumerate before the deadline")
    verify_device_identity(adb_serial, fastboot_serial)


def verify_original_boot_b(path, record):
    if (not path.is_file() or path.stat().st_size != record.get("bytes") or
            sha256(path.read_bytes()) != record.get("sha256")):
        raise CycleError(f"protected boot_b backup changed or failed checksum: {path}")


def restore_android(serial, fastboot_serial, backup, records, timeout):
    original_boot_b = backup / "boot_b.img"
    verify_original_boot_b(original_boot_b, records["boot_b"])
    if not fastboot_online(fastboot_serial):
        raise CycleError("cannot restore boot_b because fastboot is not enumerated")
    verify_fastboot(fastboot_serial, require_slot_b=False)
    flash_boot_b(fastboot_serial, original_boot_b)
    fastboot_command(fastboot_serial, "reboot", timeout=20)
    if not wait_fastboot_disconnect(fastboot_serial, min(timeout, 15)):
        raise CycleError("fastboot did not disconnect after restoring original boot_b")
    if not wait_android(serial, timeout):
        raise CycleError("original boot_b was flashed, but rooted Android did not return before the deadline")
    current_android(serial)
    compare_partition_hashes(serial, records)


def restore_android_safely(serial, fastboot_serial, backup, records, timeout):
    try:
        restore_android(serial, fastboot_serial, backup, records, timeout)
        return
    except CycleError as first_error:
        # A completed restore may have reached Android even if host-side USB
        # transition detection missed the brief fastboot disconnect.
        if adb_online(serial):
            try:
                current_android(serial)
                compare_partition_hashes(serial, records)
                return
            except CycleError:
                pass
        try:
            if fastboot_serial_count(fastboot_serial) == 1:
                restore_android(serial, fastboot_serial, backup, records, timeout)
                return
            if adb_online(serial):
                request_fastboot_from_android(serial, fastboot_serial, timeout)
                restore_android(serial, fastboot_serial, backup, records, timeout)
                return
        except CycleError as retry_error:
            raise CycleError(f"original boot_b restore failed: {first_error}; bounded retry failed: {retry_error}") from None
        raise CycleError(f"original boot_b restore was not verified: {first_error}; enter fastboot and restore "
                         f"{backup / 'boot_b.img'}") from None


def collect_cycle(args, cycle_dir, *, require_xnu, run_token=None, watchdog_timeout=None):
    output = cycle_dir / "collect"
    command = [sys.executable, args.uefi / "tools/dali/collect.py", "--serial", args.serial,
               "--output", output]
    if require_xnu:
        command += ["--require-xnu", "--require-fastboot-arm"]
    if run_token:
        command += ["--expected-run-token", run_token]
    if watchdog_timeout is not None:
        command += ["--expected-watchdog-timeout", str(watchdog_timeout)]
    run_logged(command, cycle_dir / "collect.log", timeout=120)
    result = json.loads((output / "result.json").read_text())
    return result


def build_candidate(args, backup, kernel, adt_path, recovery, token, run_dir):
    env = dict(os.environ)
    env["DALI_AUTO_FASTBOOT"] = "true" if recovery["auto_fastboot"] else "false"
    env["DALI_WATCHDOG_TIMEOUT_SECONDS"] = str(recovery["device_watchdog_timeout_seconds"])
    env["DALI_RUN_TOKEN"] = token
    env["UNIXNU_EDK2"] = env.get("UNIXNU_EDK2", str(args.uefi / "Mu_Basecore"))
    build_command = ["bash", DEVICE_ROOT / "scripts/build-firmware.sh", backup, args.uefi,
                     args.unixnu, kernel, adt_path]
    run_logged(build_command, run_dir / "firmware-build.log", timeout=3600, env=env)
    image = args.uefi / "out/UniXNU-dali-uefi-boot.img"
    image_manifest_path = args.uefi / "out/manifest.json"
    try:
        image_manifest = json.loads(image_manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise CycleError(f"candidate package manifest is missing or invalid: {error}") from None
    if (not image.is_file() or image.stat().st_size != image_manifest.get("bytes") or
            sha256(image.read_bytes()) != image_manifest.get("sha256")):
        raise CycleError("candidate boot image does not match its packaging manifest")
    payload = image_manifest.get("payload", {})
    expected_commits = {
        "unixnu_commit": run_capture(["git", "rev-parse", "HEAD"], cwd=args.unixnu).strip(),
        "device_commit": run_capture(["git", "rev-parse", "HEAD"], cwd=DEVICE_ROOT).strip(),
        "uefi_commit": run_capture(["git", "rev-parse", "HEAD"], cwd=args.uefi).strip(),
    }
    for key, commit in expected_commits.items():
        if image_manifest.get(key) != commit:
            raise CycleError(f"candidate package was built from a different {key}")
    if image_manifest.get("payload", {}).get("xnu_kernel_sha256") != sha256(kernel.read_bytes()):
        raise CycleError("candidate payload contains a different XNU kernel")
    if payload.get("xnu_run_token") != token:
        raise CycleError("candidate payload does not contain this cycle's unique XNU run token")
    payload_recovery = payload.get("recovery", {})
    if (payload_recovery.get("auto_fastboot") is not recovery["auto_fastboot"] or
            payload_recovery.get("watchdog_timeout_seconds") != recovery["device_watchdog_timeout_seconds"] or
            payload_recovery.get("run_token") != token):
        raise CycleError("candidate recovery configuration differs from the source artifact")
    return image, image_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="ADB/fastboot serial")
    parser.add_argument("--fastboot-serial", help="defaults to --serial")
    parser.add_argument("--backup", type=Path, required=True, help="private verified backup directory; created if absent")
    parser.add_argument("--evidence-root", type=Path, required=True, help="private output directory outside all repositories")
    parser.add_argument("--uefi", type=Path, required=True, help="UEFI-Dali checkout")
    parser.add_argument("--unixnu", type=Path, required=True, help="generic UniXNU checkout")
    parser.add_argument("--artifact-dir", type=Path, help="already downloaded one-day artifact; default downloads the latest successful run")
    parser.add_argument("--gh-repo", default=DEFAULT_GH_REPO)
    parser.add_argument("--cycles", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--dry-run", action="store_true", help="build and validate the candidate without rebooting or flashing")
    args = parser.parse_args()
    args.fastboot_serial = args.fastboot_serial or args.serial
    args.uefi = args.uefi.expanduser().resolve()
    args.unixnu = args.unixnu.expanduser().resolve()
    device_repo = DEVICE_ROOT.resolve()
    repositories = (device_repo, args.uefi, args.unixnu)
    backup = require_private_path(args.backup, repositories, "backup")
    evidence_root = require_private_path(args.evidence_root, repositories, "evidence root")
    if not args.uefi.is_dir() or not args.unixnu.is_dir():
        raise CycleError("UEFI-Dali and UniXNU checkouts must exist")
    for repository in (device_repo, args.unixnu):
        verify_clean_repository(repository)
    verify_clean_repository(args.uefi, allow_mu_patches=True)
    board = tomllib.loads((device_repo / "board.toml").read_text())
    expected_uefi = board.get("firmware", {}).get("uefi_commit")
    actual_uefi = run_capture(["git", "rev-parse", "HEAD"], cwd=args.uefi).strip()
    if not expected_uefi or actual_uefi != expected_uefi:
        raise CycleError("UEFI-Dali checkout does not match board.toml firmware.uefi_commit")
    if shutil.which("adb") is None or shutil.which("fastboot") is None:
        raise CycleError("Android platform-tools (adb and fastboot) are required")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evidence_root, 0o700)
    run_dir = evidence_root / ("run-" + timestamp)
    run_dir.mkdir(mode=0o700)

    print("Preflight: checking rooted Android, slot B and the verified baseline.", flush=True)
    current_android(args.serial)
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_logged([sys.executable, args.uefi / "tools/dali/backup.py", "--serial", args.serial,
                    "--output", backup], run_dir / "backup.log", timeout=1800)
    manifest, records = verify_backup(backup, args.serial)
    compare_partition_hashes(args.serial, records)

    artifact_dir = artifact_directory(args, run_dir)
    kernel, artifact = verify_kernel_artifact(artifact_dir, args.unixnu, device_repo)
    recovery = artifact["recovery"]
    fdt_data = adb_output(args.serial, "cat /sys/firmware/fdt", timeout=30)
    if not fdt_data:
        raise CycleError("live runtime FDT is empty")
    verify_live_layout(fdt_data, backup, args.uefi)
    fdt_path = run_dir / "runtime-live.dtb"
    fdt_path.write_bytes(fdt_data)
    os.chmod(fdt_path, 0o600)
    adt_path = run_dir / "dali.adt"
    run_logged([sys.executable, device_repo / "scripts/make-dali-adt.py", "--fdt", fdt_path,
                "--output", adt_path, "--dram-size", "0x300000000", "--cpu-regs", "0",
                "--psci-method", "smc"], run_dir / "adt.log", timeout=60)

    if args.dry_run:
        token = secrets.token_hex(16)
        preview_dir = run_dir / "dry-run"
        preview_dir.mkdir(mode=0o700)
        image, image_manifest = build_candidate(args, backup, kernel, adt_path, recovery, token, preview_dir)
        if image.stat().st_size > records["boot_b"]["bytes"]:
            raise CycleError("candidate is larger than the protected boot_b partition")
        write_json(preview_dir / "candidate.json", image_manifest)
        print(f"DRY RUN PASS: candidate {image.stat().st_size} bytes sha256={sha256(image.read_bytes())}")
        print(f"Private evidence: {run_dir}")
        return 0

    host_timeout = int(recovery["fastboot_timeout_seconds"])
    results = []
    for number in range(1, args.cycles + 1):
        cycle_dir = run_dir / f"cycle-{number}"
        cycle_dir.mkdir(mode=0o700)
        run_token = secrets.token_hex(16)
        image, image_manifest = build_candidate(args, backup, kernel, adt_path, recovery, run_token, cycle_dir)
        if image.stat().st_size > records["boot_b"]["bytes"]:
            raise CycleError("candidate is larger than the protected boot_b partition")
        write_json(cycle_dir / "candidate.json", image_manifest)
        print(f"Cycle {number}/{args.cycles}: verifying baseline and entering fastboot.", flush=True)
        current_android(args.serial)
        compare_partition_hashes(args.serial, records)
        request_fastboot_from_android(args.serial, args.fastboot_serial, host_timeout)
        verify_fastboot(args.fastboot_serial, require_slot_b=True)

        candidate_flash_started = False
        auto_fastboot_seen = False
        try:
            print("Writing candidate to boot_b only.", flush=True)
            verify_original_boot_b(backup / "boot_b.img", records["boot_b"])
            if sha256(image.read_bytes()) != image_manifest.get("sha256"):
                raise CycleError("candidate image changed after its package checksum was verified")
            candidate_flash_started = True
            flash_boot_b(args.fastboot_serial, image)
            fastboot_command(args.fastboot_serial, "reboot", timeout=20)
            if not wait_fastboot_disconnect(args.fastboot_serial, min(host_timeout, 15)):
                raise CycleError("fastboot did not disconnect after the candidate reboot request")
            print(f"Waiting up to {host_timeout}s for automatic fastboot recovery.", flush=True)
            state = None
            deadline = time.monotonic() + host_timeout
            while time.monotonic() < deadline:
                if fastboot_online(args.fastboot_serial):
                    state = "fastboot"
                    auto_fastboot_seen = True
                    break
                if adb_online(args.serial):
                    try:
                        if adb_property(args.serial, "ro.product.device") == "dali" and \
                                adb_property(args.serial, "sys.boot_completed") == "1":
                            state = "android"
                            break
                    except CycleError:
                        pass
                time.sleep(1)
            if state is None:
                raise CycleError("device did not enumerate fastboot or booted Android before the recovery deadline")
            if state == "android":
                # Capture available evidence before asking the already-rooted system to enter fastboot.
                try:
                    collect_cycle(args, cycle_dir, require_xnu=False, run_token=run_token)
                except CycleError as error:
                    (cycle_dir / "candidate-boot-collection-error.txt").write_text(str(error) + "\n")
                raise CycleError("candidate returned to Android without the expected automatic fastboot transition")
        except Exception as error:
            # If boot_b may have been touched, restore it as soon as fastboot is available.
            if candidate_flash_started:
                try:
                    if fastboot_online(args.fastboot_serial):
                        print("Failure during candidate boot; restoring original boot_b now.", flush=True)
                        restore_android_safely(args.serial, args.fastboot_serial, backup, records, host_timeout)
                    elif adb_online(args.serial):
                        print("Candidate returned Android; entering fastboot to restore original boot_b.", flush=True)
                        request_fastboot_from_android(args.serial, args.fastboot_serial, host_timeout)
                        restore_android_safely(args.serial, args.fastboot_serial, backup, records, host_timeout)
                    else:
                        raise CycleError(f"automatic recovery failed and fastboot is unavailable; boot_b may still be modified. "
                                         f"Enter fastboot manually, then restore {backup / 'boot_b.img'}")
                except Exception as restore_error:
                    raise CycleError(f"candidate boot failed: {error}; original boot_b restoration was not verified: "
                                     f"{restore_error}") from restore_error
            raise

        # The device reached fastboot without a host-triggered reboot after the candidate boot.
        print("Automatic fastboot detected; restoring original boot_b.", flush=True)
        restore_android_safely(args.serial, args.fastboot_serial, backup, records, host_timeout)
        android = current_android(args.serial)
        hashes = compare_partition_hashes(args.serial, records)
        try:
            collected = collect_cycle(args, cycle_dir, require_xnu=True, run_token=run_token,
                                      watchdog_timeout=recovery["device_watchdog_timeout_seconds"])
        except CycleError as error:
            raise CycleError(f"cycle {number} restored Android but its XNU/fastboot evidence failed: {error}") from None
        result = {
            "cycle": number,
            "pass": auto_fastboot_seen and collected["new_boot_marker"] and collected["xnu"]["pass"] and
                    collected["recovery"]["auto_fastboot_armed"] and
                    collected["recovery"]["timeout_matches_expected"],
            "automatic_fastboot": auto_fastboot_seen,
            "run_token": run_token,
            "candidate_sha256": image_manifest["sha256"],
            "android": android,
            "boot_b_sha256": hashes["boot_b"],
            "all_partition_hashes_match": True,
            "xnu": collected["xnu"],
            "recovery": collected["recovery"],
            "evidence": str(cycle_dir),
        }
        write_json(cycle_dir / "cycle-result.json", result)
        results.append(result)
        if not result["pass"]:
            raise CycleError(f"cycle {number} did not satisfy the XNU and automatic recovery checkpoints")
        print(f"Cycle {number} PASS: XNU checkpoint, fastboot recovery, Android root, boot_b and all partition hashes.", flush=True)

    summary = {"schema": 1, "device": "dali", "slot": "_b", "cycles": results,
               "backup_manifest": str(backup / "manifest.json")}
    write_json(run_dir / "result.json", summary)
    print(f"PASS: completed {len(results)} boot_b-only recovery cycles. Private evidence: {run_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CycleError as error:
        print(f"run-bootb-cycles: {error}", file=sys.stderr)
        raise SystemExit(1)
