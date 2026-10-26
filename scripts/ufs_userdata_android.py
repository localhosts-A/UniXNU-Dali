"""Read the dedicated userdata log partition from rooted Android.

The only device writes in this module copy verified exports into the separate
exFAT unixnu_data partition. The raw unixnu_logs block partition is read-only
throughout collection and is never restored or cleared.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

import ufs_geometry as geometry
import ufs_identity
from ufs_userdata_logs import PARTITION_BYTES, userdata_partition


def _read_local_file(path, *, maximum_bytes=64 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > maximum_bytes:
            raise ValueError("userdata export input must be a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum_bytes + 1)
    finally:
        os.close(fd)
    if len(data) != info.st_size:
        raise ValueError("userdata export input changed length while reading")
    return data


def _export_inventory(local, manifest, expected_partition, expected_manifest):
    if (not isinstance(expected_partition, bytes) or len(expected_partition) != PARTITION_BYTES or
            manifest != expected_manifest or manifest.get("kind") != "dali-userdata-ufs-log-export" or
            manifest.get("partition_file") != "unixnu_logs.img" or
            manifest.get("partition_bytes") != PARTITION_BYTES or
            manifest.get("partition_sha256") != hashlib.sha256(expected_partition).hexdigest()):
        raise ValueError("userdata export manifest is not bound to the captured device partition")
    inventory = {"unixnu_logs.img": (PARTITION_BYTES, hashlib.sha256(expected_partition).hexdigest())}
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict):
            raise ValueError("userdata export entry is malformed")
        body_name = entry.get("body_file")
        body_record = (entry.get("body_bytes"), entry.get("body_sha256"))
        if not isinstance(body_name, str) or Path(body_name).name != body_name:
            raise ValueError("userdata export body path is malformed")
        inventory[body_name] = body_record
        for record in entry.get("files", {}).values():
            if not isinstance(record, dict) or not isinstance(record.get("file"), str):
                raise ValueError("userdata export view record is malformed")
            name = record["file"]
            if Path(name).name != name:
                raise ValueError("userdata export view path is malformed")
            inventory[name] = (record.get("bytes"), record.get("sha256"))
    for record in manifest.get("supplemental_files", []):
        if not isinstance(record, dict) or not isinstance(record.get("file"), str):
            raise ValueError("userdata export supplement record is malformed")
        name = record["file"]
        if Path(name).name != name:
            raise ValueError("userdata export supplement path is malformed")
        inventory[name] = (record.get("bytes"), record.get("sha256"))
    if any(name == "manifest.json" for name in inventory):
        raise ValueError("userdata export manifest cannot list itself as payload")
    manifest_path = local / "manifest.json"
    if json.loads(_read_local_file(manifest_path, maximum_bytes=4 * 1024 * 1024)) != expected_manifest:
        raise ValueError("userdata export manifest file changed")
    inventory["manifest.json"] = None
    actual = {item.name for item in local.iterdir()}
    if actual != set(inventory):
        raise ValueError("userdata export file inventory differs from its manifest")
    files = {}
    for name, record in inventory.items():
        data = _read_local_file(local / name)
        if name == "manifest.json":
            files[name] = data
            continue
        expected_bytes, expected_sha = record
        if (type(expected_bytes) is not int or not isinstance(expected_sha, str) or
                not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or len(data) != expected_bytes or
                hashlib.sha256(data).hexdigest() != expected_sha):
            raise ValueError("userdata export file differs from its sealed manifest: " + name)
        files[name] = data
    if files["unixnu_logs.img"] != expected_partition:
        raise ValueError("userdata raw export differs from the independent device capture")
    return files


def _layout_state(layout_root, serial):
    root = Path(layout_root).resolve(strict=True)
    report = json.loads((root / "android-provisioning-verification.json").read_bytes())
    transaction = json.loads((root / "split-32gib/transaction.json").read_bytes())
    if (report.get("kind") != "dali-32gib-android-provisioning-verification" or
            report.get("serial") != serial or report.get("all18_original_hashes_verified") is not True or
            report.get("new_gpt_copies_verified") is not True or
            report.get("raw_log_region_cross_boot_verified") is not True or
            report.get("cross_boot_exfat_manifest_verified") is not True or
            report.get("automatic_mount_configured") is not False or
            report.get("uefi_xnu_log_persistence_qualified") is not False or
            transaction.get("stage") != "permanent-layout-verified" or
            transaction.get("proposal_sha256") != report.get("proposal_sha256") or
            transaction.get("storage_manifest_sha256") is None):
        raise ValueError("permanent userdata/exFAT layout verification is incomplete")
    storage = root / "split-32gib/storage-manifest.json"
    if hashlib.sha256(storage.read_bytes()).hexdigest() != transaction["storage_manifest_sha256"]:
        raise ValueError("dedicated exFAT manifest differs from the sealed layout transaction")
    return report, transaction


class AndroidUserdataLogs:
    def __init__(self, cycle, serial, records, baseline):
        self.cycle, self.serial, self.records, self.baseline = cycle, serial, records, baseline
        self.node = None
        self.disk = None
        self.block = None
        self.partition_bytes = None
        self.device_binding = None

    def output(self, command, timeout=60):
        return self.cycle.adb_output(self.serial, command, timeout=timeout)

    def text(self, command, timeout=60):
        return self.output(command, timeout).decode("utf-8", errors="strict").strip()

    def integer(self, path):
        value = self.text("cat " + shlex.quote(path))
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("invalid Android UFS sysfs integer: " + path)
        return int(value)

    def _read_gpt(self, disk, block, capacity_blocks):
        raw = []
        for lba in (1, capacity_blocks - 1):
            header = self.output(f"dd if=/dev/block/{disk} bs={block} skip={lba} count=1 status=none")
            meta = geometry.header(header, block, capacity_blocks * block, lba)
            count = (meta["entries_bytes"] + block - 1) // block
            table = self.output(f"dd if=/dev/block/{disk} bs={block} skip={meta['entries_lba']} count={count} status=none")
            if len(table) != count * block:
                raise ValueError("truncated Android GPT entry read")
            raw.extend((header, table[:meta["entries_bytes"]]))
        return geometry.validate_gpt(*raw, block, capacity_blocks * block)

    def observe(self):
        self.cycle.current_android(self.serial)
        self.cycle.compare_partition_hashes(self.serial, self.records)
        if self.baseline.get("serial") != self.serial:
            raise ValueError("userdata logs baseline serial differs")
        node = self.text("readlink -f /dev/block/by-name/unixnu_logs")
        match = re.fullmatch(r"/dev/block/(sd[a-z]+)([1-9][0-9]*)", node)
        expected_part = userdata_partition(self.baseline)
        if not match or int(match.group(2)) != expected_part["number"]:
            raise ValueError("unixnu_logs is not the exact direct GPT partition")
        disk, number = match.group(1), int(match.group(2))
        parent = "/sys/class/block/" + disk
        part = parent + str(number)
        scsi = self.text("readlink -f " + shlex.quote(parent + "/device"))
        address = re.search(r"/([0-9]+):([0-9]+):([0-9]+):([0-9]+)$", scsi)
        block = self.integer(parent + "/queue/logical_block_size")
        capacity = self.integer(parent + "/size") * 512
        if (not address or "/16810000.ufshci/" not in scsi or int(address.group(4)) != 2 or
                block != 4096 or capacity != 64577536 * block or
                self.integer(parent + "/ro") != 0 or self.integer(part + "/ro") != 0 or
                self.integer(part + "/start") * 512 != expected_part["first_lba"] * block or
                self.integer(part + "/size") * 512 != PARTITION_BYTES):
            raise ValueError("live unixnu_logs node/LUN/geometry/read-only state differs")
        events = dict(line.split("=", 1) for line in self.text("cat " + shlex.quote(part + "/uevent")).splitlines() if "=" in line)
        if events.get("PARTUUID", "").lower() != expected_part["guid"]:
            raise ValueError("live unixnu_logs PARTUUID differs")
        current_gpt = self._read_gpt(disk, block, capacity // block)
        if current_gpt != self.baseline.get("gpt"):
            raise ValueError("live reciprocal GPT differs from the sealed Android baseline")
        controller = scsi.split("/host", 1)[0]
        _, binding, _ = ufs_identity.read_identity(controller, lambda command: self.output(command))
        if binding != self.baseline.get("ufs_device_binding_sha256"):
            raise ValueError("live UFS descriptor binding differs")
        descriptors = {key: self.text("cat " + shlex.quote(parent + "/device/unit_descriptor/" + key))
                       for key in ("lu_enable", "lun_write_protect", "logical_block_size", "logical_block_count")}
        unit = geometry.unit_descriptor(dict(logical_block_bytes=block, capacity_blocks=capacity // block), descriptors)
        if unit["unit_write_protected"]:
            raise ValueError("live LUN2 is write-protected")
        self.node, self.disk, self.block, self.partition_bytes = node, disk, block, PARTITION_BYTES
        self.device_binding = binding
        return dict(node=node, disk=disk, lun=2, block_bytes=block, capacity_blocks=capacity//block,
                    partition=expected_part, device_binding_sha256=binding, read_only=False)

    def read_partition(self):
        if self.node is None:
            raise ValueError("observe the live Android UFS layout before reading logs")
        data = bytearray()
        chunk_bytes = 4 * 1024 * 1024
        for offset in range(0, PARTITION_BYTES, chunk_bytes):
            count = min(chunk_bytes, PARTITION_BYTES - offset)
            part = self.output(f"dd if={self.node} bs={self.block} skip={offset//self.block} count={count//self.block} status=none", timeout=120)
            if len(part) != count:
                raise ValueError("truncated raw unixnu_logs read")
            data.extend(part)
        if len(data) != PARTITION_BYTES:
            raise ValueError("raw userdata log capture length differs")
        return bytes(data)

    def prepare_shared_storage(self, layout_root):
        layout, transaction = _layout_state(layout_root, self.serial)
        data_part = layout.get("data")
        if (not isinstance(data_part, dict) or data_part.get("name") != "unixnu_data" or
                data_part.get("number") != 97 or data_part.get("guid") != "b9752cd4-c34c-4efa-a67c-f6181c180769" or
                data_part.get("first_lba") != 56184824 or data_part.get("blocks") != 8380416):
            raise ValueError("dedicated exFAT unixnu_data GPT identity differs")
        data_node = self.text("readlink -f /dev/block/by-name/unixnu_data")
        expected_data_node = "/dev/block/" + self.disk + str(data_part["number"])
        if data_node != expected_data_node:
            raise ValueError("live unixnu_data block node differs from GPT")
        mounts = [line.split() for line in self.text("cat /proc/mounts").splitlines()]
        target = "/data/media/0/UniXNU"
        at_target = [row for row in mounts if len(row) >= 3 and row[1] == target]
        elsewhere = [row for row in mounts if len(row) >= 2 and row[0] == data_node and row[1] != target]
        if elsewhere:
            raise ValueError("unixnu_data is already mounted at an unexpected path")
        if at_target:
            if len(at_target) != 1 or at_target[0][0] != data_node or at_target[0][2] != "exfat":
                raise ValueError("/data/media/0/UniXNU is mounted from an unexpected filesystem")
        else:
            self.output("mkdir -p " + shlex.quote(target))
            if self.text("ls -A " + shlex.quote(target)):
                raise ValueError("refuse to hide files at the unmounted exFAT mountpoint")
            self.output("mount -t exfat -o rw,noatime,uid=1023,gid=1023,fmask=0006,dmask=0007 " +
                        shlex.quote(data_node) + " " + shlex.quote(target))
        manifest_bytes = self.output("cat " + shlex.quote(target + "/Manifests/layout.json"))
        if hashlib.sha256(manifest_bytes).hexdigest() != transaction["storage_manifest_sha256"]:
            raise ValueError("mounted exFAT manifest differs from the independently verified layout")
        self.output("mkdir -p " + shlex.quote(target + "/Logs"))
        return dict(path="/sdcard/UniXNU/Logs", mountpoint=target, node=data_node,
                    layout_manifest_sha256=transaction["storage_manifest_sha256"])

    def export_files(self, local_directory, layout_root, token, *, expected_partition, expected_manifest):
        shared = self.prepare_shared_storage(layout_root)
        local = Path(local_directory).resolve(strict=True)
        manifest_data = _read_local_file(local / "manifest.json", maximum_bytes=4 * 1024 * 1024)
        manifest = json.loads(manifest_data)
        files = _export_inventory(local, manifest, expected_partition, expected_manifest)
        remote_root = "/data/local/tmp/dali-userdata-logs-" + token
        subprocess.run(["adb", "-s", self.serial, "shell", "mkdir", "-p", remote_root],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.output("chmod 0700 " + shlex.quote(remote_root))
        expected = {}
        for name, data in sorted(files.items()):
            digest = hashlib.sha256(data).hexdigest()
            expected[name] = dict(bytes=len(data), sha256=digest)
            remote = remote_root + "/" + name
            subprocess.run(["adb", "-s", self.serial, "push", str(local / name), remote], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            remote_hash = self.text("sha256sum " + shlex.quote(remote)).split()[0]
            if remote_hash != digest:
                raise ValueError("ADB staging readback differs: " + name)
        destination = "/data/media/0/UniXNU/Logs/" + token
        if self.text("[ -e " + shlex.quote(destination) + " ] && echo exists || true") == "exists":
            existing = dict(line.split()[:2] for line in self.text("cd " + shlex.quote(destination) + " && sha256sum *").splitlines() if len(line.split()) >= 2)
            if set(existing) != set(expected) or any(existing.get(name) != record["sha256"] for name, record in expected.items()):
                raise ValueError("existing Android log export differs; refusing overwrite")
        else:
            self.output("mkdir -m 700 " + shlex.quote(destination))
            for name, record in expected.items():
                src = remote_root + "/" + name
                dst = destination + "/" + name
                self.output("cp " + shlex.quote(src) + " " + shlex.quote(dst) + " && chmod 0444 " + shlex.quote(dst))
                actual = self.text("sha256sum " + shlex.quote(dst)).split()[0]
                if actual != record["sha256"]:
                    raise ValueError("Android exFAT export readback differs: " + name)
        self.output("sync")
        # Verify every file through the path Android applications and the user see.
        visible_root = "/sdcard/UniXNU/Logs/" + token
        for name, record in expected.items():
            actual = self.text("sha256sum " + shlex.quote(visible_root + "/" + name)).split()[0]
            if actual != record["sha256"]:
                raise ValueError("Android /sdcard log readback differs: " + name)
        self.output("rm -rf " + shlex.quote(remote_root))
        return dict(verified=True, destination=shared["path"] + "/" + token,
                    files=expected, layout_manifest_sha256=shared["layout_manifest_sha256"])
