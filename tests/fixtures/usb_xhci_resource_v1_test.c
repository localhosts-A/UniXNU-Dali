/* Production resource decoder; no controller registers or hardware are used. */
#include "DaliUsbXhciResourceV1.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void put_le32(uint8_t *bytes, uint32_t value)
{
    bytes[0] = (uint8_t)value;
    bytes[1] = (uint8_t)(value >> 8);
    bytes[2] = (uint8_t)(value >> 16);
    bytes[3] = (uint8_t)(value >> 24);
}

static void put_le64(uint8_t *bytes, uint64_t value)
{
    put_le32(bytes, (uint32_t)value);
    put_le32(bytes + 4, (uint32_t)(value >> 32));
}

static void make_valid(uint8_t bytes[DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES])
{
    put_le32(bytes, 1);
    put_le64(bytes + 4, DALI_USB_XHCI_RESOURCE_V1_BASE);
    put_le64(bytes + 12, DALI_USB_XHCI_RESOURCE_V1_LENGTH);
    put_le32(bytes + 20, 0);
    put_le32(bytes + 24, 600);
    put_le32(bytes + 28, 4);
    put_le32(bytes + 32, 0);
}

static void refuse_unchanged(
    const uint8_t bytes[DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES], size_t length)
{
    struct dali_usb_xhci_resource_v1 output;
    memset(&output, 0xa5, sizeof(output));
    struct dali_usb_xhci_resource_v1 before;
    memcpy(&before, &output, sizeof(before));
    assert(!dali_usb_xhci_decode_resource_v1(bytes, length, &output));
    assert(memcmp(&output, &before, sizeof(output)) == 0);
}

int main(void)
{
    uint8_t bytes[DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES];
    make_valid(bytes);

    struct dali_usb_xhci_resource_v1 decoded = {0};
    assert(dali_usb_xhci_decode_resource_v1(bytes, sizeof(bytes), &decoded));
    assert(decoded.version == 1);
    assert(decoded.physical_base == DALI_USB_XHCI_RESOURCE_V1_BASE);
    assert(decoded.length == DALI_USB_XHCI_RESOURCE_V1_LENGTH);
    assert(decoded.interrupt[0] == 0 && decoded.interrupt[1] == 600);
    assert(decoded.interrupt[2] == 4 && decoded.interrupt[3] == 0);

    for (size_t length = 0; length < sizeof(bytes); ++length) {
        refuse_unchanged(bytes, length);
    }
    refuse_unchanged(bytes, sizeof(bytes) + 1);
    assert(!dali_usb_xhci_decode_resource_v1(NULL, sizeof(bytes), &decoded));
    assert(!dali_usb_xhci_decode_resource_v1(bytes, sizeof(bytes), NULL));

    put_le32(bytes, 2);
    refuse_unchanged(bytes, sizeof(bytes));
    make_valid(bytes);
    put_le64(bytes + 4, UINT64_C(0x100000000));
    refuse_unchanged(bytes, sizeof(bytes));
    make_valid(bytes);
    put_le64(bytes + 12, UINT64_C(0x2000));
    refuse_unchanged(bytes, sizeof(bytes));
    make_valid(bytes);
    put_le32(bytes + 20, 1);
    refuse_unchanged(bytes, sizeof(bytes));
    make_valid(bytes);
    put_le32(bytes + 24, 601);
    refuse_unchanged(bytes, sizeof(bytes));
    make_valid(bytes);
    put_le32(bytes + 28, 8);
    refuse_unchanged(bytes, sizeof(bytes));
    make_valid(bytes);
    put_le32(bytes + 32, 1);
    refuse_unchanged(bytes, sizeof(bytes));

    puts("PASS: production Dali xHCI resources-v1 decoder; controller remains unqualified");
    return 0;
}
