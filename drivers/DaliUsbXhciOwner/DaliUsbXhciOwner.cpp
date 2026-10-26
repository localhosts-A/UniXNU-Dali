// SPDX-License-Identifier: BSD-2-Clause-Patent
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceMemory.h>
#include "DaliUsbXhciOwner.hpp"
#include "DaliUsbXhciResources.hpp"

OSDefineMetaClassAndStructors(DaliUsbXhciOwner, IOService)

bool
DaliUsbXhciOwner::start(IOService *provider)
{
    if (mRegisterMap != nullptr) {
        return false;
    }
    mResources = {};
    mMapped = false;
    if (provider == nullptr || !IOService::start(provider)) {
        return false;
    }

    auto *resourcesService = OSDynamicCast(DaliUsbXhciResources, provider);
    struct dali_usb_xhci_resource_v1 decoded = {};
    if (resourcesService == nullptr || !resourcesService->copyResourceDescription(&decoded)) {
        IOLog("DaliUsbXhciOwner: validated xhci0 resources provider is unavailable\n");
        IOService::stop(provider);
        return false;
    }

    IODeviceMemory *memory = IODeviceMemory::withRange(decoded.physical_base, decoded.length);
    if (memory == nullptr) {
        IOLog("DaliUsbXhciOwner: failed to describe xhci0 MMIO range\n");
        IOService::stop(provider);
        return false;
    }
    IOMemoryMap *mapping = memory->map(kIOMapAnywhere | kIOMapInhibitCache);
    memory->release();
    if (mapping == nullptr || mapping->getVirtualAddress() == 0) {
        if (mapping != nullptr) {
            mapping->release();
        }
        IOLog("DaliUsbXhciOwner: failed to map xhci0 MMIO range\n");
        IOService::stop(provider);
        return false;
    }

    mResources = decoded;
    mRegisterMap = mapping;
    mMapped = true;
    IOLog("DaliUsbXhciOwner: xhci0 MMIO mapped; controller registers untouched\n");
    registerService();
    return true;
}

void
DaliUsbXhciOwner::stop(IOService *provider)
{
    mMapped = false;
    mResources = {};
    if (mRegisterMap != nullptr) {
        mRegisterMap->release();
        mRegisterMap = nullptr;
    }
    IOService::stop(provider);
}
