// SPDX-License-Identifier: BSD-2-Clause-Patent
#define IOKIT_ENABLE_EXPERIMENTAL_SHARED_PTR_IN_API
#define IOKIT_ENABLE_SHARED_PTR

#include <IOKit/IOLib.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <libkern/c++/OSData.h>

#include "DaliUsbQemuPciOwner.hpp"

OSDefineMetaClassAndStructors(DaliUsbQemuPciOwner, IOService)

static constexpr uint64_t kQemuEcamBusBytes = 0x100000u;
static constexpr char kQemuPciHostDtPath[] = "/arm-io/qemu-pcie-host";
static constexpr uint16_t kPciCommandMemorySpace = 1u << 1;
static constexpr uint16_t kPciCommandBusMaster = 1u << 2;

static bool
propertyEquals(IORegistryEntry *provider, const char *key, const void *expected,
               size_t expected_length)
{
    auto *data = provider ? OSDynamicCast(OSData, provider->getProperty(key)) : nullptr;
    if (!data || data->getLength() != expected_length || !expected) return false;
    const auto *actual = static_cast<const uint8_t *>(data->getBytesNoCopy());
    const auto *wanted = static_cast<const uint8_t *>(expected);
    for (size_t index = 0; index < expected_length; ++index) {
        if (actual[index] != wanted[index]) return false;
    }
    return true;
}

static bool
hasQemuArmIoContract(IOService *provider)
{
    static constexpr char device_type[] = "qemu-virt\0";
    return propertyEquals(provider, "device_type", device_type,
                          sizeof(device_type) - 1u);
}

static bool
hasQemuPciHostContract(IORegistryEntry *provider)
{
    static constexpr char compatible[] =
        "qemu,virt-pcie-host\0pci-host-ecam-generic\0";
    static constexpr char device_type[] = "pci\0";
    static constexpr uint8_t address_cells[] = {3, 0, 0, 0};
    static constexpr uint8_t size_cells[] = {2, 0, 0, 0};
    static constexpr char coherent[] = "";

    if (!propertyEquals(provider, "compatible", compatible, sizeof(compatible) - 1u) ||
        !propertyEquals(provider, "device_type", device_type, sizeof(device_type) - 1u) ||
        !propertyEquals(provider, "#address-cells", address_cells, sizeof(address_cells)) ||
        !propertyEquals(provider, "#size-cells", size_cells, sizeof(size_cells)) ||
        !propertyEquals(provider, "dma-coherent", coherent, 0)) return false;

    return provider->getProperty("iommu-map") == nullptr &&
        provider->getProperty("iommu-map-mask") == nullptr &&
        provider->getProperty("iommus") == nullptr &&
        provider->getProperty("dma-ranges") == nullptr;
}

bool
DaliUsbQemuPciOwner::start(IOService *provider)
{
    if (mEcamMap || mBarMap || mRegisterOpsBound) return false;
    mHost = {};
    mXhci = {};
    mMapped = false;
    mRegisterGuard = nullptr;
    mRegisterGuardContext = nullptr;
    mRegisterRefHeld = false;
    OSPtr<IORegistryEntry> hostNode;

    if (provider == nullptr || !IOService::start(provider)) return false;
    if (mRegisterLock == nullptr) mRegisterLock = IOLockAlloc();
    if (mRegisterLock == nullptr) {
        IOService::stop(provider);
        return false;
    }

    auto failStart = [this, provider, &hostNode]() {
        hostNode = {};
        if (mEcamMap != nullptr) {
            mEcamMap->release();
            mEcamMap = nullptr;
        }
        if (mBarMap != nullptr) {
            mBarMap->release();
            mBarMap = nullptr;
        }
        mHost = {};
        mXhci = {};
        mMapped = false;
        IOService::stop(provider);
        releaseRegisterLock();
        return false;
    };

    if (!hasQemuArmIoContract(provider)) {
        IOLog("DaliUsbQemuPciOwner: rejected non-QEMU arm-io provider\n");
        return failStart();
    }
    hostNode = IORegistryEntry::fromPath(kQemuPciHostDtPath, gIODTPlane);
    if (!hostNode || !hasQemuPciHostContract(hostNode.get())) {
        IOLog("DaliUsbQemuPciOwner: missing or invalid nested QEMU PCI host node\n");
        return failStart();
    }

    auto *reg = OSDynamicCast(OSData, hostNode->getProperty("reg"));
    auto *busRange = OSDynamicCast(OSData, hostNode->getProperty("bus-range"));
    auto *ranges = OSDynamicCast(OSData, hostNode->getProperty("ranges"));
    struct dali_qemu_pci_host host = {};
    if (reg == nullptr || busRange == nullptr || ranges == nullptr ||
        dali_qemu_pci_decode_host(reg->getBytesNoCopy(), reg->getLength(),
            busRange->getBytesNoCopy(), busRange->getLength(),
            ranges->getBytesNoCopy(), ranges->getLength(), &host) != DALI_QEMU_PCI_OK ||
        host.bus_first != 0 || host.ecam_length < kQemuEcamBusBytes) {
        IOLog("DaliUsbQemuPciOwner: rejected invalid QEMU ECAM description\n");
        return failStart();
    }

    IODeviceMemory *ecam = IODeviceMemory::withRange(host.ecam_base, kQemuEcamBusBytes);
    if (ecam == nullptr) {
        IOLog("DaliUsbQemuPciOwner: failed to describe bus-0 ECAM\n");
        return failStart();
    }
    mEcamMap = ecam->map(kIOMapAnywhere | kIOMapInhibitCache);
    ecam->release();
    if (mEcamMap == nullptr || mEcamMap->getVirtualAddress() == 0) {
        IOLog("DaliUsbQemuPciOwner: failed to map bus-0 ECAM\n");
        return failStart();
    }

    struct dali_qemu_pci_xhci xhci = {};
    const int scan = dali_qemu_pci_find_xhci(&host, readConfigFunction, this, &xhci);
    const uint16_t required_command =
        kPciCommandMemorySpace | kPciCommandBusMaster;
    if (scan != DALI_QEMU_PCI_OK ||
        (xhci.command & required_command) != required_command ||
        xhci.bar0_length != DALI_QEMU_XHCI_BAR0_BYTES) {
        IOLog("DaliUsbQemuPciOwner: no uniquely assigned, enabled QEMU xHCI BAR0\n");
        return failStart();
    }

    IODeviceMemory *bar = IODeviceMemory::withRange(
        xhci.bar0_cpu_base, xhci.bar0_length);
    if (bar == nullptr) {
        IOLog("DaliUsbQemuPciOwner: failed to describe translated xHCI BAR0\n");
        return failStart();
    }
    mBarMap = bar->map(kIOMapAnywhere | kIOMapInhibitCache);
    bar->release();
    const uint64_t mapped = mBarMap ? mBarMap->getVirtualAddress() : 0;
    if (!mapped || (mapped & (sizeof(uint32_t) - 1u)) ||
        mapped > UINTPTR_MAX - (xhci.bar0_length - 1u)) {
        IOLog("DaliUsbQemuPciOwner: failed to map translated xHCI BAR0\n");
        return failStart();
    }

    mEcamMap->release();
    mEcamMap = nullptr;
    mHost = host;
    mXhci = xhci;
    mMapped = true;
    hostNode = {};
    IOLog("DaliUsbQemuPciOwner: assigned QEMU xHCI BAR0 mapped; controller untouched\n");
    registerService();
    return true;
}

void
DaliUsbQemuPciOwner::stop(IOService *provider)
{
    IOLock *lock = mRegisterLock;
    if (lock != nullptr) IOLockLock(lock);
    if (mRegisterOpsBound) {
        if (lock != nullptr) IOLockUnlock(lock);
        IOLog("DaliUsbQemuPciOwner: retaining BAR mapping while guarded callbacks are bound\n");
        return;
    }
    IOMemoryMap *ecam = mEcamMap;
    IOMemoryMap *bar = mBarMap;
    mEcamMap = nullptr;
    mBarMap = nullptr;
    mMapped = false;
    mHost = {};
    mXhci = {};
    if (lock != nullptr) IOLockUnlock(lock);
    if (ecam != nullptr) ecam->release();
    if (bar != nullptr) bar->release();
    IOService::stop(provider);
}

void
DaliUsbQemuPciOwner::free()
{
    IOLock *lock = mRegisterLock;
    if (lock != nullptr) {
        IOLockLock(lock);
        const bool busy = mEcamMap || mBarMap || mMapped ||
            mRegisterOpsBound || mRegisterRefHeld;
        IOLockUnlock(lock);
        if (busy) {
            panic("DaliUsbQemuPciOwner: freeing live QEMU xHCI owner");
            return;
        }
    }
    releaseRegisterLock();
    IOService::free();
}

void
DaliUsbQemuPciOwner::releaseRegisterLock()
{
    if (mRegisterLock != nullptr) {
        IOLockFree(mRegisterLock);
        mRegisterLock = nullptr;
    }
}

bool
DaliUsbQemuPciOwner::readConfigFunction(
    void *context, uint8_t bus, uint8_t device, uint8_t function,
    uint8_t config[DALI_QEMU_PCI_CONFIG_BYTES])
{
    auto *owner = static_cast<DaliUsbQemuPciOwner *>(context);
    if (!owner || !config || !owner->mEcamMap || bus != 0 ||
        device >= 32u || function >= 8u) return false;

    const uint64_t offset = static_cast<uint64_t>(device) * 0x8000u +
        static_cast<uint64_t>(function) * 0x1000u;
    if (offset > kQemuEcamBusBytes - DALI_QEMU_PCI_CONFIG_BYTES) return false;
    const uint64_t base = owner->mEcamMap->getVirtualAddress();
    if (!base || (base & (sizeof(uint32_t) - 1u)) ||
        base > UINTPTR_MAX - (kQemuEcamBusBytes - 1u) ||
        offset > UINTPTR_MAX - base - DALI_QEMU_PCI_CONFIG_BYTES) return false;

    const auto *registers = reinterpret_cast<volatile const uint32_t *>(
        static_cast<uintptr_t>(base + offset));
    for (size_t index = 0; index < DALI_QEMU_PCI_CONFIG_BYTES; index += sizeof(uint32_t)) {
        OSMemoryBarrier();
        const uint32_t value = registers[index / sizeof(uint32_t)];
        OSMemoryBarrier();
        config[index] = static_cast<uint8_t>(value);
        config[index + 1u] = static_cast<uint8_t>(value >> 8);
        config[index + 2u] = static_cast<uint8_t>(value >> 16);
        config[index + 3u] = static_cast<uint8_t>(value >> 24);
    }
    return true;
}

bool
DaliUsbQemuPciOwner::copyXhciDescription(struct dali_qemu_pci_xhci *output) const
{
    if (!output || mRegisterLock == nullptr) return false;
    IOLockLock(mRegisterLock);
    if (!hasMappedBar()) {
        IOLockUnlock(mRegisterLock);
        return false;
    }
    *output = mXhci;
    IOLockUnlock(mRegisterLock);
    return true;
}

bool
DaliUsbQemuPciOwner::bindGuardedMmio(
    int (*guard)(void *), void *guard_context, struct usb_mmio *out)
{
    if (!out || mRegisterLock == nullptr) return false;
    IOLockLock(mRegisterLock);
    if (!hasMappedBar() || mRegisterOpsBound || !guard ||
        mXhci.bar0_length < sizeof(uint32_t) || guard(guard_context) != USB_OK) {
        IOLockUnlock(mRegisterLock);
        return false;
    }
    struct usb_mmio candidate = {};
    candidate.context = this;
    candidate.size = mXhci.bar0_length;
    candidate.read32 = registerRead32;
    candidate.write32 = registerWrite32;
    candidate.barrier = registerBarrier;
    candidate.delay_us = registerDelay;
    mRegisterGuard = guard;
    mRegisterGuardContext = guard_context;
    mRegisterOpsBound = true;
    mRegisterRefHeld = true;
    retain();
    IOLockUnlock(mRegisterLock);
    *out = candidate;
    return true;
}

bool
DaliUsbQemuPciOwner::releaseGuardedMmio(bool controller_stopped)
{
    if (mRegisterLock == nullptr) return true;
    IOLockLock(mRegisterLock);
    if (!mRegisterOpsBound) {
        IOLockUnlock(mRegisterLock);
        return true;
    }
    if (!controller_stopped || !mRegisterGuard ||
        mRegisterGuard(mRegisterGuardContext) != USB_OK) {
        IOLockUnlock(mRegisterLock);
        return false;
    }
    mRegisterOpsBound = false;
    mRegisterGuard = nullptr;
    mRegisterGuardContext = nullptr;
    const bool releaseReference = mRegisterRefHeld;
    mRegisterRefHeld = false;
    IOLockUnlock(mRegisterLock);
    if (releaseReference) release();
    return true;
}

bool
DaliUsbQemuPciOwner::validRegisterAccess(
    const DaliUsbQemuPciOwner *owner, uint32_t offset)
{
    return owner && owner->hasMappedBar() && owner->mRegisterOpsBound &&
        owner->mRegisterGuard && !(offset & (sizeof(uint32_t) - 1u)) &&
        owner->mXhci.bar0_length >= sizeof(uint32_t) &&
        offset <= owner->mXhci.bar0_length - sizeof(uint32_t);
}

int
DaliUsbQemuPciOwner::registerRead32(void *context, uint32_t offset, uint32_t *out)
{
    auto *owner = static_cast<DaliUsbQemuPciOwner *>(context);
    if (!owner || !out || owner->mRegisterLock == nullptr) return USB_INVALID;
    IOLockLock(owner->mRegisterLock);
    if (!validRegisterAccess(owner, offset)) {
        IOLockUnlock(owner->mRegisterLock);
        return USB_RANGE;
    }
    const int guard = owner->mRegisterGuard(owner->mRegisterGuardContext);
    if (guard != USB_OK) {
        IOLockUnlock(owner->mRegisterLock);
        return guard;
    }
    const uint64_t mapped = owner->mBarMap->getVirtualAddress();
    if (!mapped || (mapped & (sizeof(uint32_t) - 1u)) ||
        mapped > UINTPTR_MAX - (sizeof(uint32_t) - 1u) ||
        offset > UINTPTR_MAX - mapped - (sizeof(uint32_t) - 1u)) {
        IOLockUnlock(owner->mRegisterLock);
        return USB_RANGE;
    }
    OSMemoryBarrier();
    const auto *address = reinterpret_cast<volatile const uint32_t *>(
        static_cast<uintptr_t>(mapped + offset));
    const uint32_t value = *address;
    OSMemoryBarrier();
    *out = value;
    IOLockUnlock(owner->mRegisterLock);
    return USB_OK;
}

int
DaliUsbQemuPciOwner::registerWrite32(void *context, uint32_t offset, uint32_t value)
{
    auto *owner = static_cast<DaliUsbQemuPciOwner *>(context);
    if (!owner || owner->mRegisterLock == nullptr) return USB_INVALID;
    IOLockLock(owner->mRegisterLock);
    if (!validRegisterAccess(owner, offset)) {
        IOLockUnlock(owner->mRegisterLock);
        return USB_RANGE;
    }
    const int guard = owner->mRegisterGuard(owner->mRegisterGuardContext);
    if (guard != USB_OK) {
        IOLockUnlock(owner->mRegisterLock);
        return guard;
    }
    const uint64_t mapped = owner->mBarMap->getVirtualAddress();
    if (!mapped || (mapped & (sizeof(uint32_t) - 1u)) ||
        mapped > UINTPTR_MAX - (sizeof(uint32_t) - 1u) ||
        offset > UINTPTR_MAX - mapped - (sizeof(uint32_t) - 1u)) {
        IOLockUnlock(owner->mRegisterLock);
        return USB_RANGE;
    }
    OSMemoryBarrier();
    auto *address = reinterpret_cast<volatile uint32_t *>(
        static_cast<uintptr_t>(mapped + offset));
    *address = value;
    OSMemoryBarrier();
    IOLockUnlock(owner->mRegisterLock);
    return USB_OK;
}

void
DaliUsbQemuPciOwner::registerBarrier(void *)
{
    OSMemoryBarrier();
}

void
DaliUsbQemuPciOwner::registerDelay(void *, uint32_t microseconds)
{
    IODelay(microseconds);
}
