/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_QEMU_PCI_H
#define DALI_USB_QEMU_PCI_H

#include <stddef.h>
#include <stdint.h>

#define DALI_QEMU_PCI_CONFIG_BYTES 256u
#define DALI_QEMU_PCI_WINDOW_COUNT 3u
#define DALI_QEMU_XHCI_BAR0_BYTES 0x4000u

enum dali_qemu_pci_result {
    DALI_QEMU_PCI_OK = 0,
    DALI_QEMU_PCI_INVALID = -1,
    DALI_QEMU_PCI_IO = -2,
    DALI_QEMU_PCI_NOT_FOUND = -3,
    DALI_QEMU_PCI_AMBIGUOUS = -4,
    DALI_QEMU_PCI_UNSUPPORTED = -5,
    DALI_QEMU_PCI_RANGE = -6,
};

enum dali_qemu_pci_window_kind {
    DALI_QEMU_PCI_IO_WINDOW = 1,
    DALI_QEMU_PCI_MEM32_WINDOW = 2,
    DALI_QEMU_PCI_MEM64_WINDOW = 3,
};

struct dali_qemu_pci_window {
    uint32_t kind;
    uint64_t pci_base;
    uint64_t cpu_base;
    uint64_t length;
};

struct dali_qemu_pci_host {
    uint64_t ecam_base;
    uint64_t ecam_length;
    uint32_t bus_first;
    uint32_t bus_last;
    struct dali_qemu_pci_window windows[DALI_QEMU_PCI_WINDOW_COUNT];
};

struct dali_qemu_pci_xhci {
    uint8_t bus;
    uint8_t device;
    uint8_t function;
    uint16_t vendor_id;
    uint16_t device_id;
    uint32_t class_code;
    uint16_t command;
    uint64_t bar0_pci_base;
    uint64_t bar0_cpu_base;
    uint32_t bar0_length;
};

typedef bool (*dali_qemu_pci_read_function)(void *context, uint8_t bus,
    uint8_t device, uint8_t function,
    uint8_t config[DALI_QEMU_PCI_CONFIG_BYTES]);

#ifdef __cplusplus
extern "C" {
#endif

/* QEMU ADT properties are byte-packed little-endian values emitted by
 * UniXNU/scripts/qemu-entry/make-dt.py, not raw big-endian FDT cells. */
int dali_qemu_pci_decode_host(const void *reg, size_t reg_length,
    const void *bus_range, size_t bus_range_length,
    const void *ranges, size_t ranges_length,
    struct dali_qemu_pci_host *out);

/* Scan the QEMU virt root bus without writing PCI config space. The function
 * requires the known QEMU qemu-xhci ID and its fixed 16-KiB 64-bit BAR0. */
int dali_qemu_pci_find_xhci(const struct dali_qemu_pci_host *host,
    dali_qemu_pci_read_function read_function, void *read_context,
    struct dali_qemu_pci_xhci *out);

#ifdef __cplusplus
}
#endif

#endif
