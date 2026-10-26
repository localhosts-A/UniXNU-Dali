/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_XHCI_RESOURCE_V1_H
#define DALI_USB_XHCI_RESOURCE_V1_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES 36u
#define DALI_USB_XHCI_RESOURCE_V1_VERSION 1u
#define DALI_USB_XHCI_RESOURCE_V1_BASE UINT64_C(0x16700000)
#define DALI_USB_XHCI_RESOURCE_V1_LENGTH UINT64_C(0x1000)

/*
 * Native representation of the Dali Apple-DT resource descriptor. The wire
 * value is little-endian and packed as <IQQ4I>; never cast wire bytes to this
 * structure because its native alignment and padding are not part of the ABI.
 */
struct dali_usb_xhci_resource_v1 {
    uint32_t version;
    uint64_t physical_base;
    uint64_t length;
    uint32_t interrupt[4];
};

static inline uint32_t
dali_usb_xhci_resource_le32(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] |
           ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) |
           ((uint32_t)bytes[3] << 24);
}

static inline uint64_t
dali_usb_xhci_resource_le64(const uint8_t *bytes)
{
    return (uint64_t)dali_usb_xhci_resource_le32(bytes) |
           ((uint64_t)dali_usb_xhci_resource_le32(bytes + 4) << 32);
}

/*
 * Decode only the fixed MT6991 xHCI description emitted by make-dali-adt.py.
 * This validates a description; it does not authorize mapping, powering, or
 * accessing the controller. On failure, output remains byte-for-byte intact.
 */
static inline bool
dali_usb_xhci_decode_resource_v1(const void *wire, size_t length,
                                 struct dali_usb_xhci_resource_v1 *output)
{
    if (wire == NULL || output == NULL ||
        length != DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES) {
        return false;
    }

    const uint8_t *bytes = (const uint8_t *)wire;
    struct dali_usb_xhci_resource_v1 decoded;
    decoded.version = dali_usb_xhci_resource_le32(bytes);
    decoded.physical_base = dali_usb_xhci_resource_le64(bytes + 4);
    decoded.length = dali_usb_xhci_resource_le64(bytes + 12);
    for (size_t index = 0; index < 4; ++index) {
        decoded.interrupt[index] =
            dali_usb_xhci_resource_le32(bytes + 20 + index * 4);
    }

    if (decoded.version != DALI_USB_XHCI_RESOURCE_V1_VERSION ||
        decoded.physical_base != DALI_USB_XHCI_RESOURCE_V1_BASE ||
        decoded.length != DALI_USB_XHCI_RESOURCE_V1_LENGTH ||
        decoded.physical_base > UINT64_MAX - decoded.length ||
        decoded.interrupt[0] != 0 ||
        decoded.interrupt[1] != 600 ||
        decoded.interrupt[2] != 4 ||
        decoded.interrupt[3] != 0) {
        return false;
    }

    *output = decoded;
    return true;
}

#endif
