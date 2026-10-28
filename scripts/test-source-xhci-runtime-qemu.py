#!/usr/bin/env python3
"""Write, sync, remount and verify a disposable FAT32 USB disk through source-XNU QEMU."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tomllib


RUNTIME_MARKER = (
    "DaliUsbQemuXhciRuntime: QEMU xHCI controller running; "
    "USB2 host and hub polling active")
ROOT_PORT_RE = re.compile(
    r"DaliUsbQemuXhciRuntime: USB2 port ([1-9][0-9]*) "
    r"connected=1 powered=([01]) overcurrent=([01]) speed=([0-9]+) changes=0x([0-9a-fA-F]+)")
DEVICE_RE = re.compile(
    r"DaliUsbQemuXhciRuntime: enumerated USB device at root port ([1-9][0-9]*) "
    r"depth ([0-9]+) address ([1-9][0-9]*) speed ([0-9]+)")
HID_CHILD_RE = re.compile(
    r"DaliUsbQemuXhciRuntime: published USB HID interface ([0-9]+) at address ([1-9][0-9]*)")
STORAGE_CHILD_RE = re.compile(
    r"DaliUsbQemuXhciRuntime: published USB BOT storage at address ([1-9][0-9]*) "
    r"\(([0-9]+) blocks, ([0-9]+)-byte sectors\)")
USB_FAT32_SECTOR_BYTES = 512
USB_FAT32_TOTAL_SECTORS = 66584
USB_FAT32_RESERVED_SECTORS = 32
USB_FAT32_FAT_COUNT = 2
USB_FAT32_SECTORS_PER_FAT = 513
USB_FAT32_DATA_START = USB_FAT32_RESERVED_SECTORS + USB_FAT32_FAT_COUNT * USB_FAT32_SECTORS_PER_FAT
USB_FAT32_CLUSTER_COUNT = USB_FAT32_TOTAL_SECTORS - USB_FAT32_DATA_START
USB_FAT32_README = b"Dali USB BOT FAT32 QEMU read-only\n"
USB_FAT32_PERSISTED_NAME = b"QEMUPASSTXT"
USB_FAT32_PERSISTED_CONTENTS = b"Dali USB BOT FAT32 persisted\n"
CHECKPOINT = (0x4d534446 << 32) | 0xffff


def build_usb_fat32_image(path):
    """Create a disposable, superfloppy FAT32 image with a single README file."""
    if USB_FAT32_CLUSTER_COUNT < 65525:
        raise ValueError("USB FAT32 image geometry is below the FAT32 cluster minimum")
    image_bytes = USB_FAT32_TOTAL_SECTORS * USB_FAT32_SECTOR_BYTES
    boot = bytearray(USB_FAT32_SECTOR_BYTES)
    boot[0:3] = b"\xeb\x58\x90"
    boot[3:11] = b"DALIUSB "
    struct.pack_into("<H", boot, 11, USB_FAT32_SECTOR_BYTES)
    boot[13] = 1
    struct.pack_into("<H", boot, 14, USB_FAT32_RESERVED_SECTORS)
    boot[16] = USB_FAT32_FAT_COUNT
    boot[21] = 0xf8
    struct.pack_into("<H", boot, 24, 63)
    struct.pack_into("<H", boot, 26, 255)
    struct.pack_into("<I", boot, 32, USB_FAT32_TOTAL_SECTORS)
    struct.pack_into("<I", boot, 36, USB_FAT32_SECTORS_PER_FAT)
    struct.pack_into("<I", boot, 44, 2)
    struct.pack_into("<H", boot, 48, 1)
    struct.pack_into("<H", boot, 50, 6)
    boot[64] = 0x80
    boot[66] = 0x29
    struct.pack_into("<I", boot, 67, 0x20261028)
    boot[71:82] = b"DALI USB   "
    boot[82:90] = b"FAT32   "
    boot[510:512] = b"\x55\xaa"

    fsinfo = bytearray(USB_FAT32_SECTOR_BYTES)
    struct.pack_into("<I", fsinfo, 0, 0x41615252)
    struct.pack_into("<I", fsinfo, 484, 0x61417272)
    struct.pack_into("<I", fsinfo, 488, USB_FAT32_CLUSTER_COUNT - 2)
    struct.pack_into("<I", fsinfo, 492, 4)
    struct.pack_into("<I", fsinfo, 508, 0xaa550000)

    fat = bytearray(USB_FAT32_SECTORS_PER_FAT * USB_FAT32_SECTOR_BYTES)
    for index, value in enumerate((0x0ffffff8, 0x0fffffff, 0x0fffffff, 0x0fffffff)):
        struct.pack_into("<I", fat, index * 4, value)
    root = bytearray(USB_FAT32_SECTOR_BYTES)
    root[0:11] = b"README  TXT"
    root[11] = 0x20
    struct.pack_into("<H", root, 26, 3)
    struct.pack_into("<I", root, 28, len(USB_FAT32_README))
    file_sector = bytearray(USB_FAT32_SECTOR_BYTES)
    file_sector[:len(USB_FAT32_README)] = USB_FAT32_README

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as disk:
        disk.write(bytes(image_bytes))
        disk.seek(0)
        disk.write(boot)
        disk.seek(USB_FAT32_SECTOR_BYTES)
        disk.write(fsinfo)
        disk.seek(6 * USB_FAT32_SECTOR_BYTES)
        disk.write(boot)
        disk.seek(7 * USB_FAT32_SECTOR_BYTES)
        disk.write(fsinfo)
        for fat_index in range(USB_FAT32_FAT_COUNT):
            offset = (USB_FAT32_RESERVED_SECTORS + fat_index * USB_FAT32_SECTORS_PER_FAT) * USB_FAT32_SECTOR_BYTES
            disk.seek(offset)
            disk.write(fat)
        disk.seek(USB_FAT32_DATA_START * USB_FAT32_SECTOR_BYTES)
        disk.write(root)
        disk.write(file_sector)
    return validate_usb_fat32_image(path)


def validate_usb_fat32_image(path):
    if path.stat().st_size != USB_FAT32_TOTAL_SECTORS * USB_FAT32_SECTOR_BYTES:
        raise ValueError("USB FAT32 image has an unexpected size")
    with path.open("rb") as disk:
        boot = disk.read(USB_FAT32_SECTOR_BYTES)
        if (boot[510:512] != b"\x55\xaa" or
                struct.unpack_from("<H", boot, 11)[0] != USB_FAT32_SECTOR_BYTES or
                boot[13] != 1 or struct.unpack_from("<H", boot, 14)[0] != USB_FAT32_RESERVED_SECTORS or
                boot[16] != USB_FAT32_FAT_COUNT or
                struct.unpack_from("<I", boot, 32)[0] != USB_FAT32_TOTAL_SECTORS or
                struct.unpack_from("<I", boot, 36)[0] != USB_FAT32_SECTORS_PER_FAT or
                struct.unpack_from("<I", boot, 44)[0] != 2):
            raise ValueError("USB FAT32 boot sector geometry or signature is invalid")
        disk.seek(USB_FAT32_SECTOR_BYTES)
        fsinfo = disk.read(USB_FAT32_SECTOR_BYTES)
        if (struct.unpack_from("<I", fsinfo, 0)[0] != 0x41615252 or
                struct.unpack_from("<I", fsinfo, 484)[0] != 0x61417272 or
                struct.unpack_from("<I", fsinfo, 508)[0] != 0xaa550000):
            raise ValueError("USB FAT32 FSInfo sector is invalid")
        disk.seek(6 * USB_FAT32_SECTOR_BYTES)
        if disk.read(USB_FAT32_SECTOR_BYTES) != boot:
            raise ValueError("USB FAT32 backup boot sector differs")
        disk.seek(7 * USB_FAT32_SECTOR_BYTES)
        backup_fsinfo = disk.read(USB_FAT32_SECTOR_BYTES)
        if (struct.unpack_from("<I", backup_fsinfo, 0)[0] != 0x41615252 or
                struct.unpack_from("<I", backup_fsinfo, 484)[0] != 0x61417272 or
                struct.unpack_from("<I", backup_fsinfo, 508)[0] != 0xaa550000):
            raise ValueError("USB FAT32 backup FSInfo signatures are invalid")
        fat_values = []
        for fat_index in range(USB_FAT32_FAT_COUNT):
            offset = (USB_FAT32_RESERVED_SECTORS + fat_index * USB_FAT32_SECTORS_PER_FAT) * USB_FAT32_SECTOR_BYTES
            disk.seek(offset)
            fat_values.append(struct.unpack("<4I", disk.read(16)))
        if any(values != (0x0ffffff8, 0x0fffffff, 0x0fffffff, 0x0fffffff)
               for values in fat_values):
            raise ValueError("USB FAT32 root/file clusters are not allocated in both FAT copies")
        disk.seek(USB_FAT32_DATA_START * USB_FAT32_SECTOR_BYTES)
        root = disk.read(USB_FAT32_SECTOR_BYTES)
        if (root[:11] != b"README  TXT" or root[11] != 0x20 or
                struct.unpack_from("<H", root, 26)[0] != 3 or
                struct.unpack_from("<I", root, 28)[0] != len(USB_FAT32_README)):
            raise ValueError("USB FAT32 root directory does not name the expected README")
        if disk.read(len(USB_FAT32_README)) != USB_FAT32_README:
            raise ValueError("USB FAT32 README contents differ")
    return {"filesystem": "FAT32", "sector_bytes": USB_FAT32_SECTOR_BYTES,
            "total_sectors": USB_FAT32_TOTAL_SECTORS,
            "data_start_sector": USB_FAT32_DATA_START,
            "cluster_count": USB_FAT32_CLUSTER_COUNT,
            "readme": "/mnt/README.TXT", "readme_contents": USB_FAT32_README.decode()}


def read_usb_fat32_root_file(path, short_name):
    """Read a one-cluster 8.3 file from this test image after QEMU exits."""
    if len(short_name) != 11:
        raise ValueError("FAT32 short name must be exactly 11 bytes")
    with path.open("rb") as disk:
        boot = disk.read(USB_FAT32_SECTOR_BYTES)
        sector_bytes = struct.unpack_from("<H", boot, 11)[0]
        sectors_per_cluster = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        fat_count = boot[16]
        sectors_per_fat = struct.unpack_from("<I", boot, 36)[0]
        total_sectors = struct.unpack_from("<I", boot, 32)[0]
        root_cluster = struct.unpack_from("<I", boot, 44)[0]
        if (sector_bytes != USB_FAT32_SECTOR_BYTES or sectors_per_cluster != 1 or
                root_cluster != 2 or total_sectors != USB_FAT32_TOTAL_SECTORS or
                fat_count != USB_FAT32_FAT_COUNT or
                sectors_per_fat != USB_FAT32_SECTORS_PER_FAT):
            raise ValueError("Persisted FAT32 image geometry changed")
        data_start = reserved + fat_count * sectors_per_fat
        disk.seek(data_start * sector_bytes)
        root = disk.read(sector_bytes)
        entry = None
        for offset in range(0, len(root), 32):
            candidate = root[offset:offset + 32]
            if not candidate or candidate[0] == 0:
                break
            if candidate[0] != 0xe5 and candidate[11] != 0x0f and candidate[:11] == short_name:
                entry = candidate
                break
        if entry is None:
            raise ValueError("Expected FAT32 file is absent after QEMU shutdown")
        cluster = (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from("<H", entry, 26)[0]
        size = struct.unpack_from("<I", entry, 28)[0]
        if cluster < 2 or cluster >= USB_FAT32_CLUSTER_COUNT + 2 or size > sector_bytes:
            raise ValueError("Persisted FAT32 test file is outside its bounded one-cluster case")
        fat_values = []
        for fat_index in range(fat_count):
            fat_offset = (reserved + fat_index * sectors_per_fat) * sector_bytes + cluster * 4
            disk.seek(fat_offset)
            fat_values.append(struct.unpack("<I", disk.read(4))[0])
        if any(value < 0x0ffffff8 for value in fat_values):
            raise ValueError("Persisted FAT32 test file unexpectedly needs a cluster chain")
        data_offset = (data_start + cluster - 2) * sector_bytes
        disk.seek(data_offset)
        return disk.read(size)


def validate(report, kernel_sha256, arguments, uart, image_sha256_before,
             image_sha256_after, persisted_contents):
    if (report.get("kernel_sha256") != kernel_sha256 or
            report.get("kernel_arguments") != arguments or
            report.get("bounded_timeout_interrupted") or
            not report.get("native_4k_mmu_observed") or
            not report.get("crypto14_initialization_observed") or
            report.get("stop_symbol") != "_source_msdosfs_test_checkpoint" or
            not report.get("function_return_target") or
            report.get("pc") != report.get("function_return_target") or
            int(report.get("x0", "0"), 16) != CHECKPOINT):
        raise ValueError("XNU FAT32 checkpoint is not bound to this kernel/case")
    if report.get("qemu_xhci_attached") is not True:
        raise ValueError("QEMU xHCI was not attached to the source-XNU boot")
    if report.get("usb_storage_image_sha256") != image_sha256_before:
        raise ValueError("QEMU runtime report names another USB disk image")
    if RUNTIME_MARKER not in uart:
        raise ValueError("source-XNU did not start the QEMU xHCI controller owner")
    ports = ROOT_PORT_RE.findall(uart)
    if not ports:
        raise ValueError("source-XNU did not observe a connected USB2 root port")
    devices = DEVICE_RE.findall(uart)
    if not devices:
        raise ValueError("source-XNU did not enumerate the attached USB device")
    storage_children = STORAGE_CHILD_RE.findall(uart)
    if not storage_children:
        raise ValueError("source-XNU did not publish the USB BOT storage child")
    expected_media = (USB_FAT32_TOTAL_SECTORS, USB_FAT32_SECTOR_BYTES)
    if not any((int(blocks), int(sector_bytes)) == expected_media
               for _, blocks, sector_bytes in storage_children):
        raise ValueError("USB BOT storage geometry does not match the disposable FAT32 image")
    if image_sha256_before == image_sha256_after:
        raise ValueError("writable FAT32 runtime did not change the disposable image")
    if persisted_contents != USB_FAT32_PERSISTED_CONTENTS.decode():
        raise ValueError("FAT32 file contents did not persist after unmount and remount")
    return {
        "ports": [{"port": int(port), "powered": powered == "1",
                   "overcurrent": overcurrent == "1", "speed_id": int(speed),
                   "changes": int(changes, 16)}
                  for port, powered, overcurrent, speed, changes in ports],
        "devices": [{"root_port": int(port), "depth": int(depth),
                     "address": int(address), "speed_id": int(speed)}
                    for port, depth, address, speed in devices],
        "storage_children": [{"address": int(address), "blocks": int(blocks),
                              "sector_bytes": int(sector_bytes)}
                             for address, blocks, sector_bytes in storage_children],
        "hid_child_published": bool(HID_CHILD_RE.findall(uart)),
        "storage_child_published": True,
        "fat32_readonly_mount_pass": True,
        "fat32_persistence_pass": True,
    }


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unixnu", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--uefi-efi", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    root = args.unixnu.resolve()
    kernel = args.kernel.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "source-xhci-runtime"
    destination.mkdir(parents=True, exist_ok=True)
    image = destination / "disposable-usb2-fat32.raw"
    image_geometry = build_usb_fat32_image(image)
    image_sha256_before = sha256(image)

    native_user = destination / "native-user"
    build = subprocess.run([sys.executable, root / "scripts/build-native-user-fixture.py",
                            "--output", native_user], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=180)
    (output / "native-user-build.log").write_text(build.stdout)
    if build.returncode:
        raise SystemExit("native-user QEMU root fixture build failed\n" + build.stdout[-6000:])

    device = Path(__file__).resolve().parents[1]
    board = tomllib.loads((device / "board.toml").read_text())
    arguments = " ".join([*board["payload"]["kernel_args"], "serial=3",
                           "debug=0x14e", "source-storage-test=2",
                           "source-msdosfs-test=3"])
    expected_arguments = "rd=md0 " + arguments
    kernel_sha256 = sha256(kernel)
    command = [sys.executable, root / "scripts/probe-darwinboot-xnu.py",
               "--kernel", kernel, "--uefi-efi", args.uefi_efi.resolve(),
               "--timeout", "120", "--armvirt", "--armcpu", "--nvram-proxy",
               "--native-user", "--native-user-image", native_user / "launchd.macho",
               "--kernel-base-args", arguments, "--require-crypto14",
               "--test-service", "source-storage:unixnu,source-storage-fixture",
               "--qemu-xhci", "--usb-storage-image", image,
               "--finish-function", "--break-symbol", "_source_msdosfs_test_checkpoint",
               "--output", destination]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=240)
    (output / "source-xhci-runtime.log").write_text(result.stdout)
    if result.returncode:
        raise SystemExit("source-XNU QEMU USB BOT FAT32 mount/read probe failed\n" + result.stdout[-8000:])

    report_path = destination / "gdb-stop.json"
    uart_path = destination / "gdb-uart.log"
    report = json.loads(report_path.read_text())
    image_sha256_after = sha256(image)
    validate_usb_fat32_image(image)
    persisted_contents = read_usb_fat32_root_file(image, USB_FAT32_PERSISTED_NAME).decode()
    runtime = validate(report, kernel_sha256, expected_arguments, uart_path.read_text(),
                       image_sha256_before, image_sha256_after, persisted_contents)
    runtime_report = {
        "kernel_sha256": kernel_sha256,
        "qemu_xhci_attached": True,
        "usb_storage_image_sha256": image_sha256_before,
        "usb_storage_image_sha256_after": image_sha256_after,
        "usb_storage_image_geometry": image_geometry,
        "controller_started": True,
        "connected_usb2_root_ports": runtime["ports"],
        "enumerated_usb_devices": runtime["devices"],
        "usb_storage_devices": runtime["storage_children"],
        "root_port_poll_interval_ms": 5,
        "usb_device_enumeration_pass": True,
        "hid_child_published": runtime["hid_child_published"],
        "hid_report_delivery_pass": False,
        "storage_child_published": runtime["storage_child_published"],
        "usb_storage_io_pass": True,
        "fat32_readonly_mount_pass": runtime["fat32_readonly_mount_pass"],
        "fat32_persistence_pass": runtime["fat32_persistence_pass"],
        "qemu_dma_runtime_qualified": False,
        "hardware_qualified": False,
    }
    (output / "source-xhci-runtime.json").write_text(
        json.dumps(runtime_report, indent=2) + "\n")
    print("PASS: source-XNU mounted and read FAT32 through the QEMU USB2 BOT device; persistence remains untested")


if __name__ == "__main__":
    main()
