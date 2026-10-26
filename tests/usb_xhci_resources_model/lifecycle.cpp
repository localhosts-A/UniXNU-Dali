#include "IOKitModel.h"
#include "DaliUsbXhciResources.hpp"

#include <cassert>
#include <cstdio>
#include <vector>

static void put_le32(uint8_t *bytes, uint32_t value)
{
    bytes[0] = static_cast<uint8_t>(value);
    bytes[1] = static_cast<uint8_t>(value >> 8);
    bytes[2] = static_cast<uint8_t>(value >> 16);
    bytes[3] = static_cast<uint8_t>(value >> 24);
}

static void put_le64(uint8_t *bytes, uint64_t value)
{
    put_le32(bytes, static_cast<uint32_t>(value));
    put_le32(bytes + 4, static_cast<uint32_t>(value >> 32));
}

static std::vector<uint8_t> valid_wire()
{
    std::vector<uint8_t> bytes(DALI_USB_XHCI_RESOURCE_V1_WIRE_BYTES);
    put_le32(bytes.data(), 1);
    put_le64(bytes.data() + 4, DALI_USB_XHCI_RESOURCE_V1_BASE);
    put_le64(bytes.data() + 12, DALI_USB_XHCI_RESOURCE_V1_LENGTH);
    put_le32(bytes.data() + 20, 0);
    put_le32(bytes.data() + 24, 600);
    put_le32(bytes.data() + 28, 4);
    put_le32(bytes.data() + 32, 0);
    return bytes;
}

int main()
{
    auto bytes = valid_wire();
    OSData property(bytes.data(), bytes.size());
    static constexpr char compatibleName[] = "mediatek,mtk-xhci";
    OSData compatible(compatibleName, sizeof(compatibleName));
    IOService provider;
    provider.setTestProperty("resources-v1", &property);
    provider.setTestProperty("compatible", &compatible);

    DaliUsbXhciResources service;
    assert(service.start(&provider));
    assert(service.registered);
    dali_usb_xhci_resource_v1 resources = {};
    assert(service.copyResourceDescription(&resources));
    assert(resources.physical_base == DALI_USB_XHCI_RESOURCE_V1_BASE);
    assert(resources.length == DALI_USB_XHCI_RESOURCE_V1_LENGTH);
    assert(resources.interrupt[1] == 600 && resources.interrupt[2] == 4);

    service.stop(&provider);
    resources.version = 99;
    assert(!service.copyResourceDescription(&resources));
    assert(resources.version == 99);

    OSObject wrongType;
    provider.setTestProperty("resources-v1", &wrongType);
    DaliUsbXhciResources wrongTypeService;
    assert(!wrongTypeService.start(&provider));
    assert(!wrongTypeService.registered);
    assert(!wrongTypeService.copyResourceDescription(&resources));

    OSData truncated(bytes.data(), bytes.size() - 1);
    provider.setTestProperty("resources-v1", &truncated);
    DaliUsbXhciResources truncatedService;
    assert(!truncatedService.start(&provider));
    assert(!truncatedService.registered);

    bytes[24] = 0x59; // IRQ number 601, rejected by the exact Dali contract.
    OSData changed(bytes.data(), bytes.size());
    provider.setTestProperty("resources-v1", &changed);
    DaliUsbXhciResources changedService;
    assert(!changedService.start(&provider));
    assert(!changedService.registered);

    static constexpr char wrongCompatibleName[] = "mediatek,other-controller";
    OSData wrongCompatible(wrongCompatibleName, sizeof(wrongCompatibleName));
    provider.setTestProperty("resources-v1", &property);
    provider.setTestProperty("compatible", &wrongCompatible);
    DaliUsbXhciResources incompatibleService;
    assert(!incompatibleService.start(&provider));
    assert(!incompatibleService.registered);

    IOService missingCompatibleProvider;
    missingCompatibleProvider.setTestProperty("resources-v1", &property);
    DaliUsbXhciResources missingCompatibleService;
    assert(!missingCompatibleService.start(&missingCompatibleProvider));
    assert(!missingCompatibleService.registered);

    assert(!service.copyResourceDescription(nullptr));
    std::puts("PASS: production Dali xHCI IOKit resource consumer; no MMIO access attempted");
    return 0;
}
