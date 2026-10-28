#!/usr/bin/env python3
"""Build and verify the opt-in XNU xhci0 device-tree resource consumer."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/dali-usb-xhci-resources"
SOURCE = ROOT / "drivers/DaliUsbXhciResources/DaliUsbXhciResources.cpp"
OWNER_SOURCE = ROOT / "drivers/DaliUsbXhciOwner/DaliUsbXhciOwner.cpp"
DMA_SOURCE = ROOT / "drivers/DaliUsbXhciDma/DaliUsbXhciDma.cpp"
QEMU_PCI_SOURCE = ROOT / "drivers/DaliUsbQemuPci/DaliUsbQemuPci.cpp"
QEMU_PCI_OWNER_SOURCE = ROOT / "drivers/DaliUsbQemuPciOwner/DaliUsbQemuPciOwner.cpp"
QEMU_XHCI_RUNTIME_SOURCE = ROOT / "drivers/DaliUsbQemuXhciRuntime/DaliUsbQemuXhciRuntime.cpp"
SOURCES = (SOURCE, OWNER_SOURCE, DMA_SOURCE, QEMU_PCI_SOURCE,
           QEMU_PCI_OWNER_SOURCE, QEMU_XHCI_RUNTIME_SOURCE)
ARCHIVE = OUT / "libDaliUsbXhciResources.a"
ARCHIVE_SYMBOLS = (
    "ZN20DaliUsbXhciResources10gMetaClassE",
    "ZN20DaliUsbXhciResources5startEP9IOService",
    "ZN20DaliUsbXhciResources4stopEP9IOService",
    "ZNK20DaliUsbXhciResources23copyResourceDescriptionEP25dali_usb_xhci_resource_v1",
    "ZN16DaliUsbXhciOwner10gMetaClassE",
    "ZN16DaliUsbXhciOwner5startEP9IOService",
    "ZN16DaliUsbXhciOwner4stopEP9IOService",
    "ZN16DaliUsbXhciOwner15bindGuardedMmioEPFiPvES0_P8usb_mmio",
    "ZN16DaliUsbXhciOwner18releaseGuardedMmioEb",
    "dali_usb_xhci_dma_init",
    "dali_usb_xhci_dma_init_qemu",
    "dali_usb_xhci_dma_get_ops",
    "dali_usb_xhci_dma_stop",
    "dali_qemu_pci_decode_host",
    "dali_qemu_pci_find_xhci",
    "ZN19DaliUsbQemuPciOwner10gMetaClassE",
    "ZN19DaliUsbQemuPciOwner5startEP9IOService",
    "ZN19DaliUsbQemuPciOwner4stopEP9IOService",
    "ZNK19DaliUsbQemuPciOwner19copyXhciDescriptionEP18dali_qemu_pci_xhci",
    "ZN19DaliUsbQemuPciOwner15bindGuardedMmioEPFiPvES0_P8usb_mmio",
    "ZN19DaliUsbQemuPciOwner18releaseGuardedMmioEb",
    "ZN22DaliUsbQemuXhciRuntime10gMetaClassE",
    "ZN22DaliUsbQemuXhciRuntime5startEP9IOService",
    "ZN22DaliUsbQemuXhciRuntime4stopEP9IOService",
    "ZNK22DaliUsbQemuXhciRuntime19copyRuntimeSnapshotEP31dali_qemu_xhci_runtime_snapshot",
)

SYMBOLS = ARCHIVE_SYMBOLS + (
    "usb_xhci_read_capabilities",
    "usb_xhci_controller_start",
    "usb_xhci_controller_stop",
    "usb_xhci_host_adapter_init",
    "usb_xhci_host_adapter_get_ops",
    "usb_xhci_host_adapter_reset_root_port",
    "usb_xhci_host_adapter_root_port_get_status",
    "usb_xhci_host_adapter_root_port_ack_changes",
    "usb_xhci_host_adapter_next_event",
    "usb_xhci_host_adapter_release_stopped",
    "usb_host_enumerate",
    "usb_host_start_storage",
    "usb_host_hub_child_route",
    "usb_host_hub_start",
    "usb_host_hub_poll",
    "usb_host_hub_stop",
    "usb_host_stop_device",
    "usb_host_confirm_controller_stopped",
    "usb_host_release_stopped_device",
    "dali_usb_hid_publish",
    "dali_usb_hid_quiesce",
    "dali_usb_hid_detach",
    "dali_usb_hid_terminate",
    "dali_usb_storage_publish",
    "dali_usb_storage_quiesce",
    "dali_usb_storage_detach",
    "dali_usb_storage_terminate",
)


def helpers():
    shared = ROOT / "scripts/build-source-storage.py"
    if not shared.is_file():
        shared = ROOT.parent / "UniXNU/scripts/build-source-storage.py"
    spec = importlib.util.spec_from_file_location(
        "dali_xhci_resource_build_helpers", shared)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def missing_symbols(output, required):
    actual = {line.split()[-1].lstrip("_") for line in output.splitlines() if line.split()}
    return sorted(set(required) - actual)


def require_symbols(output, required, description):
    missing = missing_symbols(output, required)
    if missing:
        raise ValueError(description + " lacks production symbols: " + ", ".join(missing))


def generic_usb_core_include(root=ROOT):
    candidates = [
        root / "UniXNU/drivers/UsbCore/include",
        root.parent / "UniXNU/drivers/UsbCore/include",
    ]
    unixnu_out = os.environ.get("UNIXNU_OUT")
    if unixnu_out:
        candidates.append(
            Path(unixnu_out).resolve().parent / "drivers/UsbCore/include")
    candidates.extend(parent / "drivers/UsbCore/include" for parent in root.parents)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def build():
    helper = helpers()
    missing_sources = [str(path) for path in SOURCES if not path.is_file()]
    if missing_sources:
        raise ValueError("Dali xHCI resource/owner sources are missing: " + ", ".join(missing_sources))
    record_path, record = helper.compiler_record(
        ROOT / "build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837")
    xnu = ROOT / ".cache/xnu-work"
    core_include = generic_usb_core_include()
    if not core_include.is_dir():
        raise ValueError("pinned generic UsbCore headers are missing: " + str(core_include))
    includes = [SOURCE.parent, OWNER_SOURCE.parent, DMA_SOURCE.parent,
                QEMU_PCI_SOURCE.parent, QEMU_PCI_OWNER_SOURCE.parent,
                QEMU_XHCI_RUNTIME_SOURCE.parent, ROOT / "drivers/DaliUsbHid",
                ROOT / "drivers/DaliUsbStorage", core_include, xnu / "iokit"]
    OUT.mkdir(parents=True, exist_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    (OUT / "build-report.json").unlink(missing_ok=True)
    objects = []
    object_rows = []
    commands = []
    for source in SOURCES:
        obj = OUT / (source.stem + ".o")
        obj.unlink(missing_ok=True)
        command = helper.compile_command(record, source, obj, includes)
        log = OUT / (obj.name + ".log")
        commands.append(command)
        helper.run_logged(command, Path(record["directory"]), log)
        helper.validate_object(obj)
        objects.append(obj)
        object_rows.append({
            "source": str(source.relative_to(ROOT)),
            "source_sha256": helper.digest(source),
            "object": str(obj.relative_to(ROOT)),
            "object_sha256": helper.digest(obj),
            "compile_log": str(log.relative_to(ROOT)),
            "compile_log_sha256": helper.digest(log),
        })
    (OUT / "command.json").write_text(json.dumps(commands, indent=2) + "\n")
    subprocess.run(["xcrun", "ar", "rcs", str(ARCHIVE), *map(str, objects)], check=True)
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(ARCHIVE)], text=True)
    require_symbols(defined, ARCHIVE_SYMBOLS, "Dali xHCI resource archive")
    report = {
        "compile_record": str(record_path.relative_to(ROOT)),
        "compile_record_sha256": helper.digest(record_path),
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": helper.digest(SOURCE),
        "owner_source": str(OWNER_SOURCE.relative_to(ROOT)),
        "owner_source_sha256": helper.digest(OWNER_SOURCE),
        "dma_source": str(DMA_SOURCE.relative_to(ROOT)),
        "dma_source_sha256": helper.digest(DMA_SOURCE),
        "dma_public_header_sha256": helper.digest(DMA_SOURCE.with_suffix(".hpp")),
        "qemu_pci_source_sha256": helper.digest(QEMU_PCI_SOURCE),
        "qemu_pci_header_sha256": helper.digest(QEMU_PCI_SOURCE.with_suffix(".h")),
        "qemu_pci_owner_source": str(QEMU_PCI_OWNER_SOURCE.relative_to(ROOT)),
        "qemu_pci_owner_source_sha256": helper.digest(QEMU_PCI_OWNER_SOURCE),
        "qemu_pci_owner_header_sha256": helper.digest(QEMU_PCI_OWNER_SOURCE.with_suffix(".hpp")),
        "qemu_xhci_runtime_source": str(QEMU_XHCI_RUNTIME_SOURCE.relative_to(ROOT)),
        "qemu_xhci_runtime_source_sha256": helper.digest(QEMU_XHCI_RUNTIME_SOURCE),
        "qemu_xhci_runtime_header_sha256": helper.digest(QEMU_XHCI_RUNTIME_SOURCE.with_suffix(".hpp")),
        "objects": object_rows,
        "public_header_sha256": helper.digest(SOURCE.with_suffix(".hpp")),
        "wire_header_sha256": helper.digest(SOURCE.with_name("DaliUsbXhciResourceV1.h")),
        "owner_public_header_sha256": helper.digest(OWNER_SOURCE.with_suffix(".hpp")),
        "archive": str(ARCHIVE.relative_to(ROOT)),
        "archive_sha256": helper.digest(ARCHIVE),
        "required_symbols": list(SYMBOLS),
        "source_archive_built": True,
        "kernel_link_verified": False,
        "resource_consumer_runtime_qualified": False,
        "owner_runtime_qualified": False,
        "dma_runtime_qualified": False,
        "qemu_dma_runtime_qualified": False,
        "qemu_pci_discovery_runtime_qualified": False,
        "qemu_pci_bar_owner_runtime_qualified": False,
        "mmio_register_accessed": False,
        "host_runtime_qualified": False,
        "hardware_qualified": False,
    }
    (OUT / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: xhci0 resources, QEMU PCI/BAR owner, QEMU xHCI runtime and DMA adapters built; runtime remains unqualified")


def verify_kernel(kernel):
    helper = helpers()
    kernel = kernel.resolve()
    report_path = OUT / "build-report.json"
    report = json.loads(report_path.read_text())
    if (report.get("source_archive_built") is not True or
            report.get("archive_sha256") != helper.digest(ARCHIVE) or
            report.get("source_sha256") != helper.digest(SOURCE) or
            report.get("owner_source_sha256") != helper.digest(OWNER_SOURCE) or
            report.get("dma_source_sha256") != helper.digest(DMA_SOURCE) or
            report.get("dma_public_header_sha256") != helper.digest(DMA_SOURCE.with_suffix(".hpp")) or
            report.get("qemu_pci_source_sha256") != helper.digest(QEMU_PCI_SOURCE) or
            report.get("qemu_pci_header_sha256") != helper.digest(QEMU_PCI_SOURCE.with_suffix(".h")) or
            report.get("qemu_pci_owner_source_sha256") != helper.digest(QEMU_PCI_OWNER_SOURCE) or
            report.get("qemu_pci_owner_header_sha256") != helper.digest(QEMU_PCI_OWNER_SOURCE.with_suffix(".hpp")) or
            report.get("qemu_xhci_runtime_source_sha256") != helper.digest(QEMU_XHCI_RUNTIME_SOURCE) or
            report.get("qemu_xhci_runtime_header_sha256") != helper.digest(QEMU_XHCI_RUNTIME_SOURCE.with_suffix(".hpp"))):
        raise ValueError("Dali xHCI resource archive/report mismatch")
    defined = subprocess.check_output(["xcrun", "nm", "-gU", str(kernel)], text=True)
    undefined = subprocess.check_output(["xcrun", "nm", "-u", str(kernel)], text=True)
    missing = missing_symbols(defined, SYMBOLS)
    unresolved = sorted(set(SYMBOLS) - set(missing_symbols(undefined, SYMBOLS)))
    kernel_symbols = SYMBOLS + ("ZN9IOService10gMetaClassE",)
    missing_kernel = missing_symbols(defined, kernel_symbols)
    if missing or unresolved or missing_kernel:
        raise ValueError("xHCI resource kernel link mismatch; missing=" +
                         ",".join(missing_kernel) + " unresolved=" + ",".join(unresolved))
    result = dict(
        report,
        kernel_link_verified=True,
        kernel_sha256=helper.digest(kernel),
        kernel_symbol_checks=list(kernel_symbols),
        resource_consumer_runtime_qualified=False,
        owner_runtime_qualified=False,
        dma_runtime_qualified=False,
        qemu_dma_runtime_qualified=False,
        qemu_pci_discovery_runtime_qualified=False,
        qemu_pci_bar_owner_runtime_qualified=False,
        mmio_register_accessed=False,
        host_runtime_qualified=False,
        hardware_qualified=False,
    )
    (OUT / "kernel-link-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: source XNU links xhci0 resources, QEMU PCI/BAR owner, polling runtime and DMA adapters; runtime remains unqualified")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-kernel", type=Path)
    args = parser.parse_args(argv)
    mode = os.environ.get("XNU_NATIVE_USB_HOST", "0")
    if mode == "0":
        print("SKIP: source-only USB host profile disabled")
    elif mode != "1":
        raise SystemExit("XNU_NATIVE_USB_HOST must be 0 or 1")
    elif args.verify_kernel:
        verify_kernel(args.verify_kernel)
    else:
        build()


if __name__ == "__main__":
    main()
