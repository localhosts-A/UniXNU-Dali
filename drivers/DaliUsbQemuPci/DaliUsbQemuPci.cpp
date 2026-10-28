// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "DaliUsbQemuPci.h"

#include <string.h>

static uint32_t
read_le32(const uint8_t *bytes)
{
    return static_cast<uint32_t>(bytes[0]) |
        (static_cast<uint32_t>(bytes[1]) << 8) |
        (static_cast<uint32_t>(bytes[2]) << 16) |
        (static_cast<uint32_t>(bytes[3]) << 24);
}

static uint64_t
read_le64(const uint8_t *bytes)
{
    return static_cast<uint64_t>(read_le32(bytes)) |
        (static_cast<uint64_t>(read_le32(bytes + 4)) << 32);
}

static bool
checked_last(uint64_t base, uint64_t length, uint64_t *last)
{
    if (!length || base > UINT64_MAX - (length - 1)) return false;
    *last = base + length - 1;
    return true;
}

static bool
overlaps(uint64_t left, uint64_t left_length, uint64_t right, uint64_t right_length)
{
    uint64_t left_last = 0;
    uint64_t right_last = 0;
    if (!checked_last(left, left_length, &left_last) ||
        !checked_last(right, right_length, &right_last)) return true;
    return left <= right_last && right <= left_last;
}

extern "C" int
dali_qemu_pci_decode_host(const void *reg_pointer, size_t reg_length,
                          const void *bus_range_pointer, size_t bus_range_length,
                          const void *ranges_pointer, size_t ranges_length,
                          struct dali_qemu_pci_host *out)
{
    if (!reg_pointer || !bus_range_pointer || !ranges_pointer || !out ||
        reg_length != 16u || bus_range_length != 8u ||
        ranges_length != DALI_QEMU_PCI_WINDOW_COUNT * 7u * sizeof(uint32_t))
        return DALI_QEMU_PCI_INVALID;

    const auto *reg = static_cast<const uint8_t *>(reg_pointer);
    const auto *bus_range = static_cast<const uint8_t *>(bus_range_pointer);
    const auto *ranges = static_cast<const uint8_t *>(ranges_pointer);
    struct dali_qemu_pci_host candidate = {};
    candidate.ecam_base = read_le64(reg);
    candidate.ecam_length = read_le64(reg + 8);
    candidate.bus_first = read_le32(bus_range);
    candidate.bus_last = read_le32(bus_range + 4);

    if (candidate.bus_first != 0 || candidate.bus_last > 255u ||
        candidate.bus_first > candidate.bus_last ||
        (candidate.ecam_base & 0xfffffu) != 0 ||
        (candidate.ecam_length & 0xfffffu) != 0 ||
        candidate.ecam_length <
            (static_cast<uint64_t>(candidate.bus_last - candidate.bus_first) + 1u) * 0x100000u)
        return DALI_QEMU_PCI_RANGE;

    uint64_t ecam_last = 0;
    if (!checked_last(candidate.ecam_base, candidate.ecam_length, &ecam_last))
        return DALI_QEMU_PCI_RANGE;

    uint32_t seen_kinds = 0;
    for (uint32_t index = 0; index < DALI_QEMU_PCI_WINDOW_COUNT; ++index) {
        const uint8_t *tuple = ranges + index * 7u * sizeof(uint32_t);
        const uint32_t flags = read_le32(tuple);
        const uint32_t kind = flags & 0x03000000u;
        const uint64_t pci_base = (static_cast<uint64_t>(read_le32(tuple + 4)) << 32) |
            read_le32(tuple + 8);
        const uint64_t cpu_base = (static_cast<uint64_t>(read_le32(tuple + 12)) << 32) |
            read_le32(tuple + 16);
        const uint64_t length = (static_cast<uint64_t>(read_le32(tuple + 20)) << 32) |
            read_le32(tuple + 24);

        if (flags != kind || kind < 0x01000000u || kind > 0x03000000u ||
            (kind != 0x01000000u && kind != 0x02000000u && kind != 0x03000000u) ||
            !length || (seen_kinds & (1u << (kind >> 24))) != 0)
            return DALI_QEMU_PCI_INVALID;
        seen_kinds |= 1u << (kind >> 24);

        struct dali_qemu_pci_window window = {};
        window.kind = kind >> 24;
        window.pci_base = pci_base;
        window.cpu_base = cpu_base;
        window.length = length;
        uint64_t ignored_last = 0;
        if (!checked_last(window.pci_base, window.length, &ignored_last) ||
            !checked_last(window.cpu_base, window.length, &ignored_last) ||
            overlaps(candidate.ecam_base, candidate.ecam_length,
                     window.cpu_base, window.length))
            return DALI_QEMU_PCI_RANGE;

        for (uint32_t old = 0; old < index; ++old) {
            const struct dali_qemu_pci_window *previous = &candidate.windows[old];
            if (overlaps(window.pci_base, window.length, previous->pci_base, previous->length) ||
                overlaps(window.cpu_base, window.length, previous->cpu_base, previous->length))
                return DALI_QEMU_PCI_RANGE;
        }
        candidate.windows[index] = window;
    }

    if (seen_kinds != ((1u << 1) | (1u << 2) | (1u << 3))) return DALI_QEMU_PCI_INVALID;
    (void)ecam_last;
    *out = candidate;
    return DALI_QEMU_PCI_OK;
}

static uint16_t
read_config_le16(const uint8_t *config, size_t offset)
{
    return static_cast<uint16_t>(config[offset]) |
        static_cast<uint16_t>(static_cast<uint16_t>(config[offset + 1]) << 8);
}

static uint32_t
read_config_le32(const uint8_t *config, size_t offset)
{
    return static_cast<uint32_t>(config[offset]) |
        (static_cast<uint32_t>(config[offset + 1]) << 8) |
        (static_cast<uint32_t>(config[offset + 2]) << 16) |
        (static_cast<uint32_t>(config[offset + 3]) << 24);
}

static int
decode_function(const struct dali_qemu_pci_host *host, uint8_t bus,
                uint8_t device, uint8_t function, const uint8_t *config,
                struct dali_qemu_pci_xhci *out)
{
    const uint16_t vendor_id = read_config_le16(config, 0);
    const uint16_t device_id = read_config_le16(config, 2);
    if (vendor_id != 0x1b36u || device_id != 0x000du) return DALI_QEMU_PCI_NOT_FOUND;

    const uint32_t class_code =
        (static_cast<uint32_t>(config[0x0bu]) << 16) |
        (static_cast<uint32_t>(config[0x0au]) << 8) |
        static_cast<uint32_t>(config[0x09u]);
    if (class_code != 0x0c0330u || (config[0x0eu] & 0x7fu) != 0)
        return DALI_QEMU_PCI_UNSUPPORTED;

    const uint32_t bar_low = read_config_le32(config, 0x10u);
    const uint32_t bar_high = read_config_le32(config, 0x14u);
    if ((bar_low & 1u) != 0 || (bar_low & 8u) != 0 || ((bar_low >> 1) & 3u) != 2u)
        return DALI_QEMU_PCI_UNSUPPORTED;
    const uint64_t bar_base = (static_cast<uint64_t>(bar_high) << 32) |
        static_cast<uint64_t>(bar_low & 0xfffffff0u);
    if (!bar_base || (bar_base & (DALI_QEMU_XHCI_BAR0_BYTES - 1u)) != 0)
        return DALI_QEMU_PCI_RANGE;

    uint64_t bar_last = 0;
    if (!checked_last(bar_base, DALI_QEMU_XHCI_BAR0_BYTES, &bar_last))
        return DALI_QEMU_PCI_RANGE;
    for (size_t index = 0; index < DALI_QEMU_PCI_WINDOW_COUNT; ++index) {
        const struct dali_qemu_pci_window *window = &host->windows[index];
        if (window->kind == DALI_QEMU_PCI_IO_WINDOW || bar_base < window->pci_base ||
            bar_last - window->pci_base >= window->length) continue;
        const uint64_t delta = bar_base - window->pci_base;
        if (window->cpu_base > UINT64_MAX - delta) return DALI_QEMU_PCI_RANGE;
        const uint64_t cpu_base = window->cpu_base + delta;
        uint64_t cpu_last = 0;
        if (!checked_last(cpu_base, DALI_QEMU_XHCI_BAR0_BYTES, &cpu_last))
            return DALI_QEMU_PCI_RANGE;
        (void)cpu_last;

        struct dali_qemu_pci_xhci candidate = {};
        candidate.bus = bus;
        candidate.device = device;
        candidate.function = function;
        candidate.vendor_id = vendor_id;
        candidate.device_id = device_id;
        candidate.class_code = class_code;
        candidate.command = read_config_le16(config, 4u);
        candidate.bar0_pci_base = bar_base;
        candidate.bar0_cpu_base = cpu_base;
        candidate.bar0_length = DALI_QEMU_XHCI_BAR0_BYTES;
        *out = candidate;
        return DALI_QEMU_PCI_OK;
    }
    return DALI_QEMU_PCI_RANGE;
}

extern "C" int
dali_qemu_pci_find_xhci(const struct dali_qemu_pci_host *host,
                        dali_qemu_pci_read_function read_function,
                        void *read_context, struct dali_qemu_pci_xhci *out)
{
    if (!host || !read_function || !out || host->bus_first != 0 || host->bus_last > 255u)
        return DALI_QEMU_PCI_INVALID;

    struct dali_qemu_pci_xhci found = {};
    bool has_match = false;
    for (uint8_t device = 0; device < 32u; ++device) {
        uint8_t config[DALI_QEMU_PCI_CONFIG_BYTES];
        if (!read_function(read_context, 0, device, 0, config)) return DALI_QEMU_PCI_IO;
        if (read_config_le16(config, 0) == 0xffffu) continue;

        int decoded = decode_function(host, 0, device, 0, config, &found);
        if (decoded == DALI_QEMU_PCI_OK) {
            if (has_match) return DALI_QEMU_PCI_AMBIGUOUS;
            has_match = true;
        } else if (decoded != DALI_QEMU_PCI_NOT_FOUND) {
            return decoded;
        }

        if ((config[0x0eu] & 0x80u) == 0) continue;
        for (uint8_t function = 1; function < 8u; ++function) {
            if (!read_function(read_context, 0, device, function, config))
                return DALI_QEMU_PCI_IO;
            if (read_config_le16(config, 0) == 0xffffu) continue;
            decoded = decode_function(host, 0, device, function, config, &found);
            if (decoded == DALI_QEMU_PCI_OK) {
                if (has_match) return DALI_QEMU_PCI_AMBIGUOUS;
                has_match = true;
            } else if (decoded != DALI_QEMU_PCI_NOT_FOUND) {
                return decoded;
            }
        }
    }
    if (!has_match) return DALI_QEMU_PCI_NOT_FOUND;
    *out = found;
    return DALI_QEMU_PCI_OK;
}
