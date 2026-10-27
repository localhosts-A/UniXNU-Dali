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
    mRegisterGuard = nullptr;
    mRegisterGuardContext = nullptr;
    mRegisterOpsBound = false;
    mRegisterRefHeld = false;
    if (provider == nullptr || !IOService::start(provider)) {
        return false;
    }
    if (mRegisterLock == nullptr) {
        mRegisterLock = IOLockAlloc();
    }
    if (mRegisterLock == nullptr) {
        IOService::stop(provider);
        return false;
    }

    auto *resourcesService = OSDynamicCast(DaliUsbXhciResources, provider);
    struct dali_usb_xhci_resource_v1 decoded = {};
    if (resourcesService == nullptr || !resourcesService->copyResourceDescription(&decoded)) {
        IOLog("DaliUsbXhciOwner: validated xhci0 resources provider is unavailable\n");
        IOService::stop(provider);
        releaseRegisterLock();
        return false;
    }

    IODeviceMemory *memory = IODeviceMemory::withRange(decoded.physical_base, decoded.length);
    if (memory == nullptr) {
        IOLog("DaliUsbXhciOwner: failed to describe xhci0 MMIO range\n");
        IOService::stop(provider);
        releaseRegisterLock();
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
        releaseRegisterLock();
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
    IOLock *lock = mRegisterLock;
    if (lock != nullptr) {
        IOLockLock(lock);
    }
    if (mRegisterOpsBound) {
        if (lock != nullptr) {
            IOLockUnlock(lock);
        }
        IOLog("DaliUsbXhciOwner: retaining xhci0 mapping while guarded MMIO callbacks are bound\n");
        return;
    }
    IOMemoryMap *mapping = mRegisterMap;
    mRegisterMap = nullptr;
    mMapped = false;
    mResources = {};
    if (lock != nullptr) {
        IOLockUnlock(lock);
    }
    if (mapping != nullptr) {
        mapping->release();
    }
    IOService::stop(provider);
}

void
DaliUsbXhciOwner::free()
{
    IOLock *lock = mRegisterLock;
    if (lock != nullptr) {
        IOLockLock(lock);
        bool busy = mMapped || mRegisterOpsBound || mRegisterRefHeld;
        IOLockUnlock(lock);
        if (busy) {
            panic("DaliUsbXhciOwner: freeing live xhci0 MMIO owner");
            return;
        }
    }
    releaseRegisterLock();
    IOService::free();
}

void
DaliUsbXhciOwner::releaseRegisterLock()
{
    if (mRegisterLock != nullptr) {
        IOLockFree(mRegisterLock);
        mRegisterLock = nullptr;
    }
}

bool
DaliUsbXhciOwner::bindGuardedMmio(int (*guard)(void *), void *guard_context,
                                  struct usb_mmio *out)
{
    if (!out || mRegisterLock == nullptr) {
        return false;
    }
    IOLockLock(mRegisterLock);
    if (!hasMappedWindow() || mRegisterOpsBound || !guard ||
        mResources.length < sizeof(uint32_t) || mResources.length > UINT32_MAX ||
        guard(guard_context) != USB_OK) {
        IOLockUnlock(mRegisterLock);
        return false;
    }

    struct usb_mmio candidate = {};
    candidate.context = this;
    candidate.size = static_cast<uint32_t>(mResources.length);
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
DaliUsbXhciOwner::releaseGuardedMmio(bool controller_stopped)
{
    if (mRegisterLock == nullptr) {
        return true;
    }
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
    bool releaseReference = mRegisterRefHeld;
    mRegisterRefHeld = false;
    IOLockUnlock(mRegisterLock);
    if (releaseReference) {
        release();
    }
    return true;
}

bool
DaliUsbXhciOwner::validRegisterAccess(const DaliUsbXhciOwner *owner, uint32_t offset)
{
    return owner && owner->hasMappedWindow() && owner->mRegisterOpsBound &&
        owner->mRegisterGuard && !(offset & (sizeof(uint32_t) - 1u)) &&
        owner->mResources.length >= sizeof(uint32_t) &&
        offset <= owner->mResources.length - sizeof(uint32_t);
}

int
DaliUsbXhciOwner::registerRead32(void *context, uint32_t offset, uint32_t *out)
{
    auto *owner = static_cast<DaliUsbXhciOwner *>(context);
    if (!owner || !out || owner->mRegisterLock == nullptr) {
        return USB_INVALID;
    }
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
    const uint64_t mapped = owner->mRegisterMap->getVirtualAddress();
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
DaliUsbXhciOwner::registerWrite32(void *context, uint32_t offset, uint32_t value)
{
    auto *owner = static_cast<DaliUsbXhciOwner *>(context);
    if (!owner || owner->mRegisterLock == nullptr) {
        return USB_INVALID;
    }
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
    const uint64_t mapped = owner->mRegisterMap->getVirtualAddress();
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
DaliUsbXhciOwner::registerBarrier(void *)
{
    OSMemoryBarrier();
}

void
DaliUsbXhciOwner::registerDelay(void *, uint32_t microseconds)
{
    IODelay(microseconds);
}
