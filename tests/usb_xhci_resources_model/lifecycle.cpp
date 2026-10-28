#include "IOKitModel.h"
#include "DaliUsbXhciResources.hpp"
#include "DaliUsbXhciOwner.hpp"
#include "DaliUsbXhciDma.hpp"

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

static void test_dma_adapter()
{
    IOService device;
    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = true;
    dali_usb_xhci_dma_context noMapper = {};
    assert(!dali_usb_xhci_dma_init(&noMapper, &device));
    assert(!noMapper.active && noMapper.mapper == nullptr);

    gIOKitDmaModel = {};
    gIOKitDmaModel.no_cpu_mapping = true;
    dali_usb_xhci_dma_context noCpuMapping = {};
    assert(dali_usb_xhci_dma_init(&noCpuMapping, &device));
    usb_dma_ops ops = {};
    assert(dali_usb_xhci_dma_get_ops(&noCpuMapping, &ops));
    usb_dma_buffer buffer = {};
    assert(usb_dma_alloc32(&ops, 512, 64, &buffer) == USB_RANGE);
    assert(!buffer.cpu && !buffer.cookie && !noCpuMapping.allocations);
    assert(gIOKitDmaModel.memory_allocations == 1 && gIOKitDmaModel.memory_releases == 1);
    assert(gIOKitDmaModel.command_allocations == 1 && gIOKitDmaModel.command_releases == 1);
    assert(gIOKitDmaModel.clear_calls == 0);
    assert(dali_usb_xhci_dma_stop(&noCpuMapping));

    gIOKitDmaModel = {};
    dali_usb_xhci_dma_context dma = {};
    assert(dali_usb_xhci_dma_init(&dma, &device));
    assert(dali_usb_xhci_dma_get_ops(&dma, &ops));
    assert(ops.qualified && ops.allocate && ops.release && ops.sync);

    buffer = {};
    assert(usb_dma_alloc32(&ops, 1024, 64, &buffer) == USB_OK);
    assert(buffer.cpu && buffer.address == gIOKitDmaModel.dma_address);
    assert(buffer.size == 1024 && buffer.coherent);
    assert((reinterpret_cast<uintptr_t>(buffer.cpu) & 63u) == 0);
    assert(gIOKitDmaModel.cpu_alignment == 64);
    assert((gIOKitDmaModel.memory_options & kIOMemoryPhysicallyContiguous) != 0);
    assert((gIOKitDmaModel.memory_options & kIOMapInhibitCache) != 0);
    assert(gIOKitDmaModel.address_bits == 32 &&
           gIOKitDmaModel.mapping_options == IODMACommand::kMapped &&
           gIOKitDmaModel.alignment == 64);
    const auto *bytes = static_cast<const uint8_t *>(buffer.cpu);
    for (size_t i = 0; i < buffer.size; ++i) assert(bytes[i] == 0);
    assert(usb_dma_sync(&ops, &buffer, 0, 512, USB_DMA_TO_DEVICE, true) == USB_OK);
    assert(usb_dma_sync(&ops, &buffer, 1023, 2, USB_DMA_FROM_DEVICE, false) == USB_RANGE);
    usb_dma_free(&ops, &buffer);
    assert(buffer.cpu == nullptr && gIOKitDmaModel.clear_calls == 1);
    assert(gIOKitDmaModel.memory_releases == 1 && gIOKitDmaModel.command_releases == 1);
    assert(dali_usb_xhci_dma_stop(&dma));
    assert(gIOKitDmaModel.mapper_releases == 1);

    gIOKitDmaModel = {};
    dali_usb_xhci_dma_context aligned64k = {};
    assert(dali_usb_xhci_dma_init(&aligned64k, &device));
    assert(dali_usb_xhci_dma_get_ops(&aligned64k, &ops));
    buffer = {};
    assert(usb_dma_alloc32(&ops, 4096, 65536, &buffer) == USB_OK);
    assert((reinterpret_cast<uintptr_t>(buffer.cpu) & 0xffffu) == 0);
    assert((buffer.address & 0xffffu) == 0);
    assert(gIOKitDmaModel.cpu_alignment == 65536);
    assert(gIOKitDmaModel.address_bits == 32 &&
           gIOKitDmaModel.mapping_options == IODMACommand::kMapped &&
           gIOKitDmaModel.alignment == 65536);
    usb_dma_free(&ops, &buffer);
    assert(dali_usb_xhci_dma_stop(&aligned64k));

    gIOKitDmaModel = {};
    dali_usb_xhci_dma_context misalignedIova = {};
    assert(dali_usb_xhci_dma_init(&misalignedIova, &device));
    assert(dali_usb_xhci_dma_get_ops(&misalignedIova, &ops));
    gIOKitDmaModel.dma_address = 0x00100001u;
    gIOKitDmaModel.ignore_iova_alignment = true;
    buffer = {};
    assert(usb_dma_alloc32(&ops, 512, 64, &buffer) == USB_RANGE);
    assert(!buffer.cpu && !buffer.cookie && gIOKitDmaModel.memory_releases == 1);
    assert(gIOKitDmaModel.command_releases == 1);
    assert(dali_usb_xhci_dma_stop(&misalignedIova));

    gIOKitDmaModel = {};
    dali_usb_xhci_dma_context split = {};
    assert(dali_usb_xhci_dma_init(&split, &device));
    assert(dali_usb_xhci_dma_get_ops(&split, &ops));
    gIOKitDmaModel.segment_count = 2;
    buffer = {};
    assert(usb_dma_alloc32(&ops, 512, 64, &buffer) == USB_RANGE);
    assert(buffer.cpu == nullptr && gIOKitDmaModel.memory_releases == 1);
    assert(gIOKitDmaModel.command_releases == 1);
    gIOKitDmaModel.segment_count = 1;
    gIOKitDmaModel.dma_address = 0x100000000ULL;
    gIOKitDmaModel.next_dma_address = 0;
    assert(usb_dma_alloc32(&ops, 64, 16, &buffer) == USB_RANGE);
    assert(buffer.cpu == nullptr && gIOKitDmaModel.memory_releases == 2);
    assert(gIOKitDmaModel.command_releases == 2);
    gIOKitDmaModel.segment_count = 1;
    gIOKitDmaModel.dma_address = 0xfffffff0u;
    gIOKitDmaModel.next_dma_address = 0;
    assert(usb_dma_alloc32(&ops, 64, 16, &buffer) == USB_RANGE);
    assert(buffer.cpu == nullptr && gIOKitDmaModel.memory_releases == 3);
    assert(gIOKitDmaModel.command_releases == 3);
    gIOKitDmaModel.dma_address = 0x00100000u;
    gIOKitDmaModel.next_dma_address = 0;
    gIOKitDmaModel.fail_prepare = true;
    assert(usb_dma_alloc32(&ops, 512, 64, &buffer) == USB_IO);
    assert(buffer.cpu == nullptr && gIOKitDmaModel.memory_releases == 4);
    assert(gIOKitDmaModel.active_prepares == 0 && gIOKitDmaModel.complete_calls == 4);
    assert(dali_usb_xhci_dma_stop(&split));

    gIOKitDmaModel = {};
    dali_usb_xhci_dma_context descriptorFailure = {};
    assert(dali_usb_xhci_dma_init(&descriptorFailure, &device));
    assert(dali_usb_xhci_dma_get_ops(&descriptorFailure, &ops));
    gIOKitDmaModel.fail_set_descriptor = true;
    buffer = {};
    assert(usb_dma_alloc32(&ops, 128, 64, &buffer) == USB_IO);
    assert(!buffer.cpu && !buffer.cookie && !descriptorFailure.allocations);
    assert(gIOKitDmaModel.clear_calls == 0);
    assert(gIOKitDmaModel.memory_releases == 1 && gIOKitDmaModel.command_releases == 1);
    assert(dali_usb_xhci_dma_stop(&descriptorFailure));

    gIOKitDmaModel = {};
    dali_usb_xhci_dma_context retained = {};
    assert(dali_usb_xhci_dma_init(&retained, &device));
    assert(dali_usb_xhci_dma_get_ops(&retained, &ops));
    assert(usb_dma_alloc32(&ops, 256, 64, &buffer) == USB_OK);
    gIOKitDmaModel.fail_clear = true;
    usb_dma_free(&ops, &buffer);
    assert(buffer.cpu == nullptr && retained.faulted && retained.allocations != nullptr);
    assert(!dali_usb_xhci_dma_get_ops(&retained, &ops));
    assert(!dali_usb_xhci_dma_stop(&retained));
    gIOKitDmaModel.fail_clear = false;
    assert(dali_usb_xhci_dma_stop(&retained));
    assert(gIOKitDmaModel.memory_releases == 1 && gIOKitDmaModel.command_releases == 1);
}

static void test_qemu_bypass_dma_adapter()
{
    static constexpr char compatibleBytes[] =
        "qemu,virt-pcie-host\0pci-host-ecam-generic\0";
    static constexpr char emptyProperty[] = "";
    static constexpr char armIoTypeBytes[] = "qemu-virt";
    static constexpr char pciTypeBytes[] = "pci";
    OSData compatible(compatibleBytes, sizeof(compatibleBytes) - 1);
    OSData coherent(emptyProperty, 0);
    IOService host;
    IORegistryEntry pciHost;
    OSData armIoType(armIoTypeBytes, sizeof(armIoTypeBytes));
    OSData pciType(pciTypeBytes, sizeof(pciTypeBytes));
    host.setTestProperty("device_type", &armIoType);
    pciHost.setTestProperty("device_type", &pciType);
    pciHost.setTestProperty("compatible", &compatible);
    pciHost.setTestProperty("dma-coherent", &coherent);
    IORegistryEntry::setTestPath("/arm-io/qemu-pcie-host", &pciHost);

    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = true;
    dali_usb_xhci_dma_context context = {};
    assert(dali_usb_xhci_dma_init_qemu(&context, &host));
    assert(context.active && context.qemu_bypass && !context.mapper);
    usb_dma_ops ops = {};
    assert(dali_usb_xhci_dma_get_ops(&context, &ops));
    assert(ops.qualified && ops.allocate && ops.release && ops.sync);

    usb_dma_buffer buffer = {};
    assert(usb_dma_alloc32(&ops, 4096, 65536, &buffer) == USB_OK);
    assert(buffer.cpu && buffer.size == 4096 && buffer.coherent);
    assert((reinterpret_cast<uintptr_t>(buffer.cpu) & 0xffffu) == 0);
    assert((buffer.address & 0xffffu) == 0 && buffer.address <= UINT32_MAX);
    assert(gIOKitDmaModel.address_bits == 32 &&
           gIOKitDmaModel.mapping_options == IODMACommand::kBypassed &&
           gIOKitDmaModel.alignment == 65536);
    assert(gIOKitDmaModel.cpu_alignment == 65536 && gIOKitDmaModel.physical_mask == 0);
    assert((gIOKitDmaModel.memory_options & kIOMemoryPhysicallyContiguous) != 0);
    assert((gIOKitDmaModel.memory_options & kIOMapInhibitCache) != 0);
    usb_dma_free(&ops, &buffer);
    assert(dali_usb_xhci_dma_stop(&context));
    assert(!context.active && !context.qemu_bypass && !context.mapper);

    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = true;
    gIOKitDmaModel.dma_address = 0x100000000ULL;
    dali_usb_xhci_dma_context over32 = {};
    assert(dali_usb_xhci_dma_init_qemu(&over32, &host));
    assert(dali_usb_xhci_dma_get_ops(&over32, &ops));
    buffer = {};
    assert(usb_dma_alloc32(&ops, 512, 64, &buffer) == USB_RANGE);
    assert(!buffer.cpu && !buffer.cookie && gIOKitDmaModel.memory_releases == 1);
    assert(dali_usb_xhci_dma_stop(&over32));

    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = true;
    gIOKitDmaModel.dma_address = 0x00100001u;
    gIOKitDmaModel.ignore_iova_alignment = true;
    dali_usb_xhci_dma_context misaligned = {};
    assert(dali_usb_xhci_dma_init_qemu(&misaligned, &host));
    assert(dali_usb_xhci_dma_get_ops(&misaligned, &ops));
    buffer = {};
    assert(usb_dma_alloc32(&ops, 512, 64, &buffer) == USB_RANGE);
    assert(!buffer.cpu && !buffer.cookie && gIOKitDmaModel.memory_releases == 1);
    assert(dali_usb_xhci_dma_stop(&misaligned));

    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = true;
    gIOKitDmaModel.system_mapper = true;
    dali_usb_xhci_dma_context systemMapper = {};
    assert(!dali_usb_xhci_dma_init_qemu(&systemMapper, &host));
    assert(!systemMapper.active && !systemMapper.qemu_bypass);

    gIOKitDmaModel = {};
    pciHost.setTestProperty("iommu-map", &coherent);
    dali_usb_xhci_dma_context iommu = {};
    assert(!dali_usb_xhci_dma_init_qemu(&iommu, &host));
    assert(!iommu.active && gIOKitDmaModel.mapper_requests == 0);
    pciHost.removeTestProperty("iommu-map");

    gIOKitDmaModel = {};
    IOService nonQemuHost;
    nonQemuHost.setTestProperty("device_type", &coherent);
    dali_usb_xhci_dma_context wrongHost = {};
    assert(!dali_usb_xhci_dma_init_qemu(&wrongHost, &nonQemuHost));
    assert(!wrongHost.active && gIOKitDmaModel.mapper_requests == 0);

    gIOKitDmaModel = {};
    gIOKitDmaModel.no_mapper = false;
    dali_usb_xhci_dma_context perDeviceMapper = {};
    assert(!dali_usb_xhci_dma_init_qemu(&perDeviceMapper, &host));
    assert(!perDeviceMapper.active && gIOKitDmaModel.mapper_requests == 1 &&
           gIOKitDmaModel.mapper_releases == 1);
    IORegistryEntry::removeTestPath("/arm-io/qemu-pcie-host", &pciHost);
}

int main()
{
    test_dma_adapter();
    test_qemu_bypass_dma_adapter();

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

    gIOKitMemoryModel = {};
    DaliUsbXhciOwner owner;
    assert(owner.start(&service));
    assert(owner.started && owner.registered && owner.hasMappedWindow());
    assert(gIOKitMemoryModel.range_calls == 1);
    assert(gIOKitMemoryModel.base == DALI_USB_XHCI_RESOURCE_V1_BASE);
    assert(gIOKitMemoryModel.length == DALI_USB_XHCI_RESOURCE_V1_LENGTH);
    assert(gIOKitMemoryModel.map_calls == 1);
    assert(gIOKitMemoryModel.map_options == (kIOMapAnywhere | kIOMapInhibitCache));
    owner.stop(&service);
    assert(!owner.started && !owner.hasMappedWindow());
    assert(gIOKitMemoryModel.memory_releases == 1 && gIOKitMemoryModel.map_releases == 1);
    owner.free();
    assert(gIOKitLockCount == 0);

    gIOKitMemoryModel = {};
    gIOKitMemoryModel.fail_range = true;
    DaliUsbXhciOwner rangeFailure;
    assert(!rangeFailure.start(&service));
    assert(!rangeFailure.started && !rangeFailure.registered);
    assert(gIOKitMemoryModel.range_calls == 1 && gIOKitMemoryModel.map_calls == 0);

    gIOKitMemoryModel = {};
    gIOKitMemoryModel.fail_map = true;
    DaliUsbXhciOwner mapFailure;
    assert(!mapFailure.start(&service));
    assert(!mapFailure.started && !mapFailure.registered);
    assert(gIOKitMemoryModel.memory_releases == 1 && gIOKitMemoryModel.map_releases == 0);

    IOService wrongOwnerProvider;
    DaliUsbXhciOwner wrongOwner;
    assert(!wrongOwner.start(&wrongOwnerProvider));
    assert(!wrongOwner.started && !wrongOwner.registered);

    service.stop(&provider);
    resources.version = 99;
    assert(!service.copyResourceDescription(&resources));
    assert(resources.version == 99);

    OSObject wrongType;
    provider.setTestProperty("resources-v1", &wrongType);
    DaliUsbXhciResources wrongTypeService;
    assert(!wrongTypeService.start(&provider));
    assert(!wrongTypeService.started);
    assert(!wrongTypeService.registered);
    assert(!wrongTypeService.copyResourceDescription(&resources));

    OSData truncated(bytes.data(), bytes.size() - 1);
    provider.setTestProperty("resources-v1", &truncated);
    DaliUsbXhciResources truncatedService;
    assert(!truncatedService.start(&provider));
    assert(!truncatedService.started);
    assert(!truncatedService.registered);

    bytes[24] = 0x59; // IRQ number 601, rejected by the exact Dali contract.
    OSData changed(bytes.data(), bytes.size());
    provider.setTestProperty("resources-v1", &changed);
    DaliUsbXhciResources changedService;
    assert(!changedService.start(&provider));
    assert(!changedService.started);
    assert(!changedService.registered);

    static constexpr char wrongCompatibleName[] = "mediatek,other-controller";
    OSData wrongCompatible(wrongCompatibleName, sizeof(wrongCompatibleName));
    provider.setTestProperty("resources-v1", &property);
    provider.setTestProperty("compatible", &wrongCompatible);
    DaliUsbXhciResources incompatibleService;
    assert(!incompatibleService.start(&provider));
    assert(!incompatibleService.started);
    assert(!incompatibleService.registered);

    IOService missingCompatibleProvider;
    missingCompatibleProvider.setTestProperty("resources-v1", &property);
    DaliUsbXhciResources missingCompatibleService;
    assert(!missingCompatibleService.start(&missingCompatibleProvider));
    assert(!missingCompatibleService.started);
    assert(!missingCompatibleService.registered);

    assert(!service.copyResourceDescription(nullptr));
    std::puts("PASS: production Dali xHCI MMIO mapping owner; controller registers untouched");
    return 0;
}
