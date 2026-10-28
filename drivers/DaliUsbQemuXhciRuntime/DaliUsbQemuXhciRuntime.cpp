// SPDX-License-Identifier: BSD-2-Clause-Patent
#include <IOKit/IOLib.h>
#include <IOKit/IOTimerEventSource.h>
#include <IOKit/IOWorkLoop.h>
#include <kern/clock.h>
#include <libkern/c++/OSPtr.h>

#include "DaliUsbHid.h"
#include "DaliUsbStorage.h"
#include "DaliUsbQemuXhciRuntime.hpp"

OSDefineMetaClassAndStructors(DaliUsbQemuXhciRuntime, IOService)

static constexpr uint32_t kPollIntervalMs = 5u;
static constexpr uint32_t kCommandRingEntries = 64u;
static constexpr uint32_t kEventRingEntries = 256u;
static constexpr uint8_t kMaximumSlots = 32u;
static constexpr uint32_t kCommandTimeoutMs = 1000u;
static constexpr uint32_t kHubPollTimeoutMs = 1u;
static constexpr uint32_t kRootPortResetTimeoutMs = 1000u;
static constexpr uint32_t kTopologyRetryDelayMs = 500u;

static bool
deadline_pending(uint32_t now_ms, uint32_t deadline_ms)
{
    const uint32_t until_deadline = deadline_ms - now_ms;
    return until_deadline != 0 && until_deadline < 0x80000000u;
}

int
DaliUsbQemuXhciRuntime::controllerGuard(void *context)
{
    auto *runtime = static_cast<DaliUsbQemuXhciRuntime *>(context);
    return runtime != nullptr && runtime->mGuardActive ? USB_OK : USB_BUSY;
}

int
DaliUsbQemuXhciRuntime::adapterLock(void *context)
{
    auto *runtime = static_cast<DaliUsbQemuXhciRuntime *>(context);
    if (runtime == nullptr || runtime->mAdapterLock == nullptr) return USB_INVALID;
    IOLockLock(runtime->mAdapterLock);
    return USB_OK;
}

void
DaliUsbQemuXhciRuntime::adapterUnlock(void *context)
{
    auto *runtime = static_cast<DaliUsbQemuXhciRuntime *>(context);
    if (runtime != nullptr && runtime->mAdapterLock != nullptr)
        IOLockUnlock(runtime->mAdapterLock);
}

uint32_t
DaliUsbQemuXhciRuntime::uptimeMilliseconds(void *)
{
    uint64_t uptime = 0;
    uint64_t nanoseconds = 0;
    clock_get_uptime(&uptime);
    absolutetime_to_nanoseconds(uptime, &nanoseconds);
    return static_cast<uint32_t>(nanoseconds / 1000000u);
}

int
DaliUsbQemuXhciRuntime::hubEvent(void *context, enum usb_host_hub_event_kind kind,
                                uint8_t, const struct usb_host_route *route,
                                enum usb_host_speed speed)
{
    auto *runtime = static_cast<DaliUsbQemuXhciRuntime *>(context);
    if (runtime == nullptr || route == nullptr) return USB_INVALID;
    return runtime->handleHubEvent(kind, route, speed);
}

bool
DaliUsbQemuXhciRuntime::start(IOService *provider)
{
    if (mPciOwner != nullptr || mController != nullptr || mRetained ||
        provider == nullptr || !IOService::start(provider)) return false;

    auto *pciOwner = OSDynamicCast(DaliUsbQemuPciOwner, provider);
    if (pciOwner == nullptr || !pciOwner->copyXhciDescription(&mPciDescription)) {
        IOLog("DaliUsbQemuXhciRuntime: rejected non-QEMU PCI owner\n");
        IOService::stop(provider);
        return false;
    }

    mPciOwner = pciOwner;
    mPciOwner->retain();
    mSnapshot = {};
    mSnapshot.version = DALI_QEMU_XHCI_RUNTIME_SNAPSHOT_VERSION;
    mTopologyFaulted = false;
    for (size_t port = 0; port < sizeof(mRootRetryPending); ++port) {
        mRootRetryPending[port] = false;
        mRootRetryAfterMs[port] = 0;
        mRootConnectionEvent[port] = false;
    }

    mSnapshotLock = IOLockAlloc();
    auto workLoop = IOWorkLoop::workLoop();
    auto timer = IOTimerEventSource::timerEventSource(this, pollTimerAction);
    if (mSnapshotLock == nullptr || !workLoop || !timer) {
        return failStart(provider, "workloop, timer, or snapshot lock allocation failed");
    }
    mWorkLoop = workLoop.get();
    mWorkLoop->retain();
    mPollTimer = timer.get();
    mPollTimer->retain();
    if (mWorkLoop->addEventSource(mPollTimer) != kIOReturnSuccess) {
        return failStart(provider, "failed to add poll timer to workloop");
    }
    mTimerAdded = true;
    mPollTimer->enable();

    mController = static_cast<struct usb_xhci_controller *>(
        IOMallocZero(sizeof(struct usb_xhci_controller)));
    if (mController == nullptr) return failStart(provider, "controller allocation failed");

    IOService *pciHost = mPciOwner->getProvider();
    if (pciHost == nullptr || !dali_usb_xhci_dma_init_qemu(&mDma, pciHost) ||
        !dali_usb_xhci_dma_get_ops(&mDma, &mDmaOps)) {
        return failStart(provider, "QEMU bypass DMA contract rejected");
    }

    mGuardActive = true;
    if (!mPciOwner->bindGuardedMmio(controllerGuard, this, &mMmio)) {
        return failStart(provider, "guarded BAR binding failed");
    }
    mMmioBound = true;

    if (usb_xhci_read_capabilities(&mMmio, &mCapabilities) != USB_OK ||
        mCapabilities.max_slots == 0 || mCapabilities.max_ports == 0 ||
        mCapabilities.usb2_port_count == 0) {
        return failStart(provider, "xHCI capabilities have no usable USB2 resources");
    }

    uint8_t slots = mCapabilities.max_slots;
    if (slots > kMaximumSlots) slots = kMaximumSlots;
    const int started = usb_xhci_controller_start(mController, &mMmio, &mDmaOps,
        controllerGuard, this, slots, kCommandRingEntries, kEventRingEntries);
    if (started != USB_OK) return failStart(provider, "xHCI controller start failed");
    mStartedController = true;

    mDeviceCapacity = slots;
    mAdapterLock = IOLockAlloc();
    mHostDevices = static_cast<struct usb_xhci_host_device *>(
        IOMallocZero(sizeof(*mHostDevices) * mDeviceCapacity));
    mDevices = static_cast<DeviceRecord *>(
        IOMallocZero(sizeof(*mDevices) * mDeviceCapacity));
    if (mAdapterLock == nullptr || mHostDevices == nullptr || mDevices == nullptr)
        return failStart(provider, "USB host topology allocation failed");
    const int adapter_started = usb_xhci_host_adapter_init(
        &mHostAdapter, mController, mHostDevices, mDeviceCapacity,
        adapterLock, adapterUnlock, this, uptimeMilliseconds, this,
        kCommandTimeoutMs);
    if (adapter_started != USB_OK)
        return failStart(provider, "USB host adapter initialization failed");
    mHostAdapterReady = true;
    if (usb_xhci_host_adapter_get_ops(&mHostAdapter, &mHostOps) != USB_OK)
        return failStart(provider, "USB host adapter operation table failed");

    IOLockLock(mSnapshotLock);
    mSnapshot.max_ports = mCapabilities.max_ports;
    mSnapshot.controller_running = true;
    mSnapshot.faulted = false;
    IOLockUnlock(mSnapshotLock);

    if (mPollTimer->setTimeoutMS(kPollIntervalMs) != kIOReturnSuccess) {
        return failStart(provider, "failed to schedule USB2 root-port polling");
    }

    IOLog("DaliUsbQemuXhciRuntime: QEMU xHCI controller running; USB2 host and hub polling active\n");
    registerService();
    return true;
}

bool
DaliUsbQemuXhciRuntime::failStart(IOService *provider, const char *reason)
{
    (void)reason;
    IOLog("DaliUsbQemuXhciRuntime: %s\n", reason ? reason : "startup failed");
    if (!stopTimer() || !releaseControllerOwnership()) {
        mRetained = true;
        if (mSnapshotLock != nullptr) {
            IOLockLock(mSnapshotLock);
            mSnapshot.faulted = true;
            IOLockUnlock(mSnapshotLock);
        }
        /* Keep the service alive so IOKit and the parent cannot free a live
         * controller, DMA allocation or MMIO callback context. */
        registerService();
        return true;
    }
    releaseLocalObjects();
    mPciOwner->release();
    mPciOwner = nullptr;
    IOService::stop(provider);
    return false;
}

bool
DaliUsbQemuXhciRuntime::stopTimer()
{
    if (mPollTimer == nullptr) return true;
    mPollTimer->cancelTimeout();
    mPollTimer->disable();
    if (mTimerAdded) {
        if (mWorkLoop == nullptr ||
            mWorkLoop->removeEventSource(mPollTimer) != kIOReturnSuccess) return false;
        mTimerAdded = false;
    }
    return true;
}

static bool
same_host_route(const struct usb_host_route *left, const struct usb_host_route *right)
{
    if (left == nullptr || right == nullptr || left->root_port != right->root_port ||
        left->depth != right->depth || left->tt_hub_address != right->tt_hub_address ||
        left->tt_port != right->tt_port || left->tt_think_time_ns != right->tt_think_time_ns ||
        left->multi_tt != right->multi_tt) return false;
    for (uint8_t i = 0; i < left->depth; ++i)
        if (left->hub_address[i] != right->hub_address[i] ||
            left->hub_port[i] != right->hub_port[i]) return false;
    return true;
}

DaliUsbQemuXhciRuntime::DeviceRecord *
DaliUsbQemuXhciRuntime::findDevice(const struct usb_host_route *route)
{
    if (route == nullptr || mDevices == nullptr) return nullptr;
    for (uint8_t i = 0; i < mDeviceCapacity; ++i) {
        DeviceRecord *record = &mDevices[i];
        if (record->in_use && same_host_route(&record->route, route)) return record;
    }
    return nullptr;
}

DaliUsbQemuXhciRuntime::DeviceRecord *
DaliUsbQemuXhciRuntime::findRootDevice(uint8_t root_port)
{
    if (mDevices == nullptr || root_port == 0) return nullptr;
    for (uint8_t i = 0; i < mDeviceCapacity; ++i) {
        DeviceRecord *record = &mDevices[i];
        if (record->in_use && record->route.root_port == root_port &&
            record->route.depth == 0) return record;
    }
    return nullptr;
}

DaliUsbQemuXhciRuntime::DeviceRecord *
DaliUsbQemuXhciRuntime::allocateDevice(const struct usb_host_route *route)
{
    if (route == nullptr || mDevices == nullptr) return nullptr;
    for (uint8_t i = 0; i < mDeviceCapacity; ++i) {
        DeviceRecord *record = &mDevices[i];
        if (record->in_use) continue;
        *record = {};
        record->route = *route;
        record->in_use = true;
        return record;
    }
    return nullptr;
}

int
DaliUsbQemuXhciRuntime::publishDeviceServices(DeviceRecord *record)
{
    if (record == nullptr || !record->in_use ||
        record->host.state != USB_HOST_CONFIGURED) return USB_DISCONNECTED;

    for (uint8_t index = 0; index < record->host.hid_count; ++index) {
        if (record->hid_services[index] != nullptr) continue;
        IOService *service = dali_usb_hid_publish(this, &record->host, index);
        if (service == nullptr) return USB_NO_MEMORY;
        record->hid_services[index] = service;
        record->hid_quiesced[index] = false;
        IOLog("DaliUsbQemuXhciRuntime: published USB HID interface %u at address %u\n",
              record->host.hid[index].interface_number, record->host.address);
    }

    if (record->host.has_bot && record->storage_service == nullptr) {
        const int capacity = usb_host_start_storage(&record->host);
        if (capacity != USB_OK) return capacity;
        IOService *service = dali_usb_storage_publish(this, &record->host.bot);
        if (service == nullptr) return USB_NO_MEMORY;
        record->storage_service = service;
        record->storage_quiesced = false;
        IOLog("DaliUsbQemuXhciRuntime: published USB BOT storage at address %u (%llu blocks, %u-byte sectors)\n",
              record->host.address, record->host.bot.blocks, record->host.bot.block_size);
    }

    if (record->host.hub_interface.present && !record->hub_started) {
        const int started = usb_host_hub_start(&record->host, &record->hub);
        record->hub_started = record->hub.initialized;
        if (started != USB_OK) return started;
        IOLog("DaliUsbQemuXhciRuntime: USB hub at address %u exposes %u ports\n",
              record->host.address, record->hub.port_count);
    }
    return USB_OK;
}

int
DaliUsbQemuXhciRuntime::enumerateDevice(const struct usb_host_route *route,
                                        enum usb_host_speed speed)
{
    if (!mHostAdapterReady || route == nullptr) return USB_AGAIN;
    DeviceRecord *record = findDevice(route);
    if (record != nullptr && record->host.state == USB_HOST_CONFIGURED)
        return publishDeviceServices(record);
    if (record != nullptr && record->host.state != USB_HOST_EMPTY) {
        const int stopped = stopDevice(record);
        if (stopped != USB_OK) return stopped;
        record = nullptr;
    }
    if (record == nullptr) record = allocateDevice(route);
    if (record == nullptr) return USB_NO_MEMORY;

    const uint8_t preferred_address = static_cast<uint8_t>(record - mDevices + 1);
    const int result = usb_host_enumerate(&record->host, &mHostOps, route, speed,
                                          preferred_address);
    if (result != USB_OK) {
        if (record->host.state == USB_HOST_EMPTY) *record = {};
        else IOLog("DaliUsbQemuXhciRuntime: USB enumeration retained a faulted route (port %u depth %u, error %d)\n",
                   route->root_port, route->depth, result);
        return result;
    }
    IOLog("DaliUsbQemuXhciRuntime: enumerated USB device at root port %u depth %u address %u speed %u\n",
          route->root_port, route->depth, record->host.address, speed);
    return publishDeviceServices(record);
}

int
DaliUsbQemuXhciRuntime::handleHubEvent(enum usb_host_hub_event_kind kind,
                                       const struct usb_host_route *route,
                                       enum usb_host_speed speed)
{
    if (route == nullptr) return USB_INVALID;
    if (kind == USB_HOST_HUB_ATTACH) return enumerateDevice(route, speed);
    if (kind != USB_HOST_HUB_DETACH && kind != USB_HOST_HUB_OVERCURRENT)
        return USB_UNSUPPORTED;
    DeviceRecord *record = findDevice(route);
    return record == nullptr ? USB_OK : stopDevice(record);
}

int
DaliUsbQemuXhciRuntime::quiesceDeviceServices(DeviceRecord *record)
{
    if (record == nullptr || !record->in_use) return USB_OK;
    for (uint8_t index = 0; index < USB_HOST_MAX_HID_INTERFACES; ++index) {
        if (record->hid_services[index] == nullptr || record->hid_quiesced[index]) continue;
        const int result = dali_usb_hid_quiesce(record->hid_services[index]);
        if (result != USB_OK) return result;
        record->hid_quiesced[index] = true;
    }
    if (record->storage_service != nullptr && !record->storage_quiesced) {
        const int result = dali_usb_storage_quiesce(record->storage_service);
        if (result != USB_OK) return result;
        record->storage_quiesced = true;
    }
    return USB_OK;
}

int
DaliUsbQemuXhciRuntime::stopDevice(DeviceRecord *record)
{
    if (record == nullptr || !record->in_use) return USB_OK;
    int result = quiesceDeviceServices(record);
    if (result != USB_OK) return result;

    if (record->hub_started && record->hub.initialized &&
        record->host.state == USB_HOST_CONFIGURED) {
        result = usb_host_hub_stop(&record->host, &record->hub, hubEvent, this);
        if (result != USB_OK) return result;
        record->hub_started = false;
    }
    if (record->host.state != USB_HOST_EMPTY) {
        result = usb_host_stop_device(&record->host);
        if (result != USB_OK) return result;
    }

    for (uint8_t index = 0; index < USB_HOST_MAX_HID_INTERFACES; ++index) {
        IOService *service = record->hid_services[index];
        if (service == nullptr) continue;
        result = dali_usb_hid_detach(service, true);
        if (result != USB_OK) return result;
        result = dali_usb_hid_terminate(service);
        if (result != USB_OK) return result;
        service->release();
        record->hid_services[index] = nullptr;
    }
    if (record->storage_service != nullptr) {
        result = dali_usb_storage_detach(record->storage_service, true);
        if (result != USB_OK) return result;
        result = dali_usb_storage_terminate(record->storage_service);
        if (result != USB_OK) return result;
        record->storage_service->release();
        record->storage_service = nullptr;
    }
    if (record->host.state != USB_HOST_EMPTY) {
        result = usb_host_release_stopped_device(&record->host, nullptr, nullptr, nullptr);
        if (result != USB_OK) return result;
    }
    *record = {};
    return USB_OK;
}

bool
DaliUsbQemuXhciRuntime::quiesceAllDeviceServices()
{
    if (mDevices == nullptr) return true;
    for (uint8_t index = 0; index < mDeviceCapacity; ++index) {
        if (!mDevices[index].in_use) continue;
        if (quiesceDeviceServices(&mDevices[index]) != USB_OK) return false;
    }
    return true;
}

bool
DaliUsbQemuXhciRuntime::stopAllDevicesGracefully()
{
    if (mDevices == nullptr) return true;
    for (int depth = USB_HOST_MAX_ROUTE_DEPTH; depth >= 0; --depth) {
        for (uint8_t index = 0; index < mDeviceCapacity; ++index) {
            DeviceRecord *record = &mDevices[index];
            if (!record->in_use || record->route.depth != static_cast<uint8_t>(depth)) continue;
            if (stopDevice(record) != USB_OK) return false;
        }
    }
    return true;
}

bool
DaliUsbQemuXhciRuntime::releaseDevicesAfterControllerStop()
{
    if (!mHostAdapterReady) return true;
    if (mDevices == nullptr || mHostDevices == nullptr) return false;
    for (uint8_t index = 0; index < mDeviceCapacity; ++index) {
        DeviceRecord *record = &mDevices[index];
        if (record->in_use && record->host.state != USB_HOST_EMPTY &&
            usb_host_confirm_controller_stopped(&record->host) != USB_OK) return false;
    }
    if (usb_xhci_host_adapter_release_stopped(&mHostAdapter) != USB_OK) return false;

    for (int depth = USB_HOST_MAX_ROUTE_DEPTH; depth >= 0; --depth) {
        for (uint8_t index = 0; index < mDeviceCapacity; ++index) {
            DeviceRecord *record = &mDevices[index];
            if (!record->in_use || record->route.depth != static_cast<uint8_t>(depth)) continue;
            for (uint8_t hid = 0; hid < USB_HOST_MAX_HID_INTERFACES; ++hid) {
                IOService *service = record->hid_services[hid];
                if (service == nullptr) continue;
                if (dali_usb_hid_detach(service, true) != USB_OK ||
                    dali_usb_hid_terminate(service) != USB_OK) return false;
                service->release();
                record->hid_services[hid] = nullptr;
            }
            if (record->storage_service != nullptr) {
                if (dali_usb_storage_detach(record->storage_service, true) != USB_OK ||
                    dali_usb_storage_terminate(record->storage_service) != USB_OK) return false;
                record->storage_service->release();
                record->storage_service = nullptr;
            }
            if (record->host.state != USB_HOST_EMPTY &&
                usb_host_release_stopped_device(&record->host, nullptr, nullptr, nullptr) != USB_OK)
                return false;
            *record = {};
        }
    }
    mHostAdapterReady = false;
    mHostOps = {};
    return true;
}

int
DaliUsbQemuXhciRuntime::drainControllerEvents()
{
    if (!mHostAdapterReady) return USB_AGAIN;
    for (unsigned count = 0; count < 64u; ++count) {
        struct usb_xhci_event event = {};
        const int result = usb_xhci_host_adapter_next_event(&mHostAdapter, &event);
        if (result == USB_AGAIN) return USB_OK;
        if (result != USB_OK) return result;
        if (event.kind != USB_XHCI_EVENT_PORT_STATUS_CHANGE ||
            event.data.port_status.port_id == 0) return USB_IO;
        const uint8_t port = event.data.port_status.port_id;
        if (port > mCapabilities.max_ports) return USB_RANGE;
        const uint16_t index = static_cast<uint16_t>(port - 1u);
        if (!(mCapabilities.usb2_ports[index / 8u] & (1u << (index % 8u)))) {
            /* This owner exposes USB2 root-port APIs only. Fail closed without
             * sending an unsupported USB3 port ID to the USB2 status/ack path. */
            IOLog("DaliUsbQemuXhciRuntime: unsupported non-USB2 port event %u\n",
                  port);
            return USB_UNSUPPORTED;
        }
        struct usb_xhci_root_port_status status = {};
        const int read = usb_xhci_host_adapter_root_port_get_status(
            &mHostAdapter, port, &status);
        if (read != USB_OK) return read;
        if (status.changes & USB_XHCI_ROOT_PORT_CHANGE_CONNECT)
            mRootConnectionEvent[port] = true;
        if (status.changes) {
            const int ack = usb_xhci_host_adapter_root_port_ack_changes(
                &mHostAdapter, port, status.changes);
            if (ack != USB_OK) return ack;
        }
    }
    return USB_RANGE;
}

bool
DaliUsbQemuXhciRuntime::releaseControllerOwnership()
{
    if (mHostAdapterReady) {
        if (!quiesceAllDeviceServices()) {
            IOLog("DaliUsbQemuXhciRuntime: child quiesce failed; controller and DMA retained\n");
            return false;
        }
        /* Per-slot shutdown is preferred. If a downstream hub or slot no longer
         * responds, the children are already quiesced, so a successful
         * controller-wide halt is the fallback proof for every retained slot. */
        (void)stopAllDevicesGracefully();
    }

    if (mController != nullptr && mController->state != USB_XHCI_CONTROLLER_EMPTY &&
        usb_xhci_controller_stop(mController) != USB_OK) return false;
    mStartedController = false;
    if (!releaseDevicesAfterControllerStop()) return false;

    if (mDma.active && !dali_usb_xhci_dma_stop(&mDma)) return false;
    if (mMmioBound && !mPciOwner->releaseGuardedMmio(true)) return false;
    mMmioBound = false;
    mGuardActive = false;
    return true;
}

void
DaliUsbQemuXhciRuntime::releaseLocalObjects()
{
    if (mTimerAdded || mHostAdapterReady) return;
    if (mPollTimer != nullptr) {
        mPollTimer->release();
        mPollTimer = nullptr;
    }
    if (mWorkLoop != nullptr) {
        mWorkLoop->release();
        mWorkLoop = nullptr;
    }
    if (mSnapshotLock != nullptr) {
        IOLockFree(mSnapshotLock);
        mSnapshotLock = nullptr;
    }
    if (mController != nullptr) {
        IOFree(mController, sizeof(struct usb_xhci_controller));
        mController = nullptr;
    }
    if (mHostDevices != nullptr) {
        IOFree(mHostDevices, sizeof(*mHostDevices) * mDeviceCapacity);
        mHostDevices = nullptr;
    }
    if (mDevices != nullptr) {
        IOFree(mDevices, sizeof(*mDevices) * mDeviceCapacity);
        mDevices = nullptr;
    }
    if (mAdapterLock != nullptr) {
        IOLockFree(mAdapterLock);
        mAdapterLock = nullptr;
    }
    mDeviceCapacity = 0;
    mHostAdapter = {};
    for (size_t port = 0; port < sizeof(mRootRetryPending); ++port) {
        mRootRetryPending[port] = false;
        mRootRetryAfterMs[port] = 0;
        mRootConnectionEvent[port] = false;
    }
    mDmaOps = {};
    mCapabilities = {};
    mMmio = {};
    mPciDescription = {};
}

void
DaliUsbQemuXhciRuntime::stop(IOService *provider)
{
    if (!stopTimer() || !releaseControllerOwnership()) {
        mRetained = true;
        IOLog("DaliUsbQemuXhciRuntime: retaining controller, DMA and BAR leases for stop retry\n");
        return;
    }
    mRetained = false;
    releaseLocalObjects();
    DaliUsbQemuPciOwner *owner = mPciOwner;
    mPciOwner = nullptr;
    if (owner != nullptr) owner->release();
    IOService::stop(provider);
}

void
DaliUsbQemuXhciRuntime::free()
{
    if (mController != nullptr || mPciOwner != nullptr || mDma.active ||
        mMmioBound || mTimerAdded || mRetained || mHostAdapterReady ||
        mHostDevices != nullptr || mDevices != nullptr || mAdapterLock != nullptr) {
        panic("DaliUsbQemuXhciRuntime: freeing live QEMU xHCI controller owner");
        return;
    }
    releaseLocalObjects();
    IOService::free();
}

void
DaliUsbQemuXhciRuntime::pollTimerAction(OSObject *owner, IOTimerEventSource *)
{
    auto *runtime = OSDynamicCast(DaliUsbQemuXhciRuntime, owner);
    if (runtime == nullptr || runtime->mTopologyFaulted || runtime->mRetained) return;
    runtime->pollRootPorts();
    if (!runtime->mTopologyFaulted) runtime->pollUsbDevices();
    if (!runtime->mTopologyFaulted && runtime->mPollTimer != nullptr &&
        runtime->mPollTimer->setTimeoutMS(kPollIntervalMs) != kIOReturnSuccess)
        runtime->latchRuntimeFault("USB2 polling timer rearm failed");
}

void
DaliUsbQemuXhciRuntime::latchRuntimeFault(const char *reason)
{
    (void)reason;
    mTopologyFaulted = true;
    if (mSnapshotLock != nullptr) {
        IOLockLock(mSnapshotLock);
        mSnapshot.faulted = true;
        if (mController == nullptr || mController->state != USB_XHCI_CONTROLLER_RUNNING)
            mSnapshot.controller_running = false;
        IOLockUnlock(mSnapshotLock);
    }
    IOLog("DaliUsbQemuXhciRuntime: %s; retaining USB owner leases\n",
          reason ? reason : "USB topology fault");
}

void
DaliUsbQemuXhciRuntime::pollRootPorts()
{
    if (mController == nullptr || !mStartedController || mRetained || mSnapshotLock == nullptr)
        return;
    if (!mHostAdapterReady) {
        latchRuntimeFault("USB host adapter is unavailable");
        return;
    }

    for (uint16_t port = 1; port <= mCapabilities.max_ports; ++port) {
        const uint16_t index = static_cast<uint16_t>(port - 1u);
        if (!(mCapabilities.usb2_ports[index / 8u] & (1u << (index % 8u)))) continue;
        struct usb_xhci_root_port_status status = {};
        const int read = usb_xhci_host_adapter_root_port_get_status(
            &mHostAdapter, static_cast<uint8_t>(port), &status);
        if (read != USB_OK) {
            latchRuntimeFault("USB2 root-port status read failed");
            return;
        }

        bool logChange = false;
        bool wasConnected = false;
        bool hadSnapshot = false;
        IOLockLock(mSnapshotLock);
        hadSnapshot = (mSnapshot.valid_ports[index / 8u] &
                       static_cast<uint8_t>(1u << (index % 8u))) != 0;
        wasConnected = hadSnapshot && mSnapshot.ports[port].connected;
        if (!hadSnapshot ||
            mSnapshot.ports[port].connected != status.connected ||
            mSnapshot.ports[port].powered != status.powered ||
            mSnapshot.ports[port].overcurrent != status.overcurrent ||
            status.changes != 0) logChange = true;
        mSnapshot.valid_ports[index / 8u] |= static_cast<uint8_t>(1u << (index % 8u));
        mSnapshot.ports[port] = status;
        IOLockUnlock(mSnapshotLock);

        if (logChange) {
            IOLog("DaliUsbQemuXhciRuntime: USB2 port %u connected=%u powered=%u overcurrent=%u speed=%u changes=0x%x\n",
                  port, status.connected, status.powered, status.overcurrent,
                  status.speed_id, status.changes);
        }

        const bool connectionChanged =
            (status.changes & USB_XHCI_ROOT_PORT_CHANGE_CONNECT) != 0 ||
            mRootConnectionEvent[port] || (hadSnapshot && wasConnected != status.connected);
        const bool rootStateTransition = hadSnapshot && wasConnected != status.connected;
        const bool freshConnectionEvent = rootStateTransition || mRootConnectionEvent[port];
        const uint32_t now_ms = uptimeMilliseconds(nullptr);
        if (freshConnectionEvent) {
            mRootRetryPending[port] = false;
            mRootRetryAfterMs[port] = 0;
            mRootConnectionEvent[port] = false;
        }
        DeviceRecord *record = findRootDevice(static_cast<uint8_t>(port));
        if (status.overcurrent || !status.connected || connectionChanged) {
            if (record != nullptr) {
                if (mRootRetryPending[port] && !freshConnectionEvent &&
                    deadline_pending(now_ms, mRootRetryAfterMs[port])) continue;
                const int stopped = stopDevice(record);
                if (stopped != USB_OK) {
                    if (mController->state != USB_XHCI_CONTROLLER_RUNNING) {
                        latchRuntimeFault("USB device teardown faulted the controller");
                        return;
                    }
                    mRootRetryPending[port] = true;
                    mRootRetryAfterMs[port] = now_ms + kTopologyRetryDelayMs;
                    IOLog("DaliUsbQemuXhciRuntime: USB2 root-port %u teardown failed (%d); retry scheduled\n",
                          port, stopped);
                    continue;
                }
                record = nullptr;
                mRootRetryPending[port] = false;
                mRootRetryAfterMs[port] = 0;
            }
            if (freshConnectionEvent) {
                mRootRetryPending[port] = false;
                mRootRetryAfterMs[port] = 0;
            }
        }

        if (status.changes && usb_xhci_host_adapter_root_port_ack_changes(
                &mHostAdapter, static_cast<uint8_t>(port), status.changes) != USB_OK) {
            latchRuntimeFault("USB2 root-port change acknowledgement failed");
            return;
        }
        mRootConnectionEvent[port] = false;

        if (!status.connected || status.overcurrent || !status.powered || status.resetting)
            continue;

        record = findRootDevice(static_cast<uint8_t>(port));
        if (record != nullptr && record->host.state == USB_HOST_CONFIGURED) continue;
        if (mRootRetryPending[port] && !rootStateTransition &&
            deadline_pending(now_ms, mRootRetryAfterMs[port])) continue;
        if (record != nullptr && record->host.state != USB_HOST_EMPTY) {
            const int stopped = stopDevice(record);
            if (stopped != USB_OK) {
                if (mController->state != USB_XHCI_CONTROLLER_RUNNING) {
                    latchRuntimeFault("faulted USB device could not be stopped before retry");
                    return;
                }
                mRootRetryPending[port] = true;
                mRootRetryAfterMs[port] = now_ms + kTopologyRetryDelayMs;
                continue;
            }
            mRootRetryPending[port] = false;
            mRootRetryAfterMs[port] = 0;
        }

        const int drained = drainControllerEvents();
        if (drained != USB_OK) {
            latchRuntimeFault("unexpected xHCI event before root-port reset");
            return;
        }
        enum usb_host_speed speed = USB_HOST_FULL_SPEED;
        const int reset = usb_xhci_host_adapter_reset_root_port(
            &mHostAdapter, static_cast<uint8_t>(port), kRootPortResetTimeoutMs, &speed);
        if (reset == USB_BUSY || reset == USB_DISCONNECTED) {
            /* USB_BUSY can mean an in-progress reset or a just-arrived event;
             * retry from a fresh PORTSC snapshot on the next timer tick. */
            if (reset == USB_BUSY) {
                const int pending = drainControllerEvents();
                if (pending != USB_OK) {
                    latchRuntimeFault("xHCI event drain failed while waiting for root reset");
                    return;
                }
            }
            continue;
        }
        if (reset != USB_OK) {
            if (mController->state != USB_XHCI_CONTROLLER_RUNNING) {
                latchRuntimeFault("USB2 root-port reset faulted the controller");
                return;
            }
            mRootRetryPending[port] = true;
            mRootRetryAfterMs[port] = uptimeMilliseconds(nullptr) + kTopologyRetryDelayMs;
            IOLog("DaliUsbQemuXhciRuntime: USB2 root-port %u reset refused (%d)\n",
                  port, reset);
            continue;
        }

        const int reset_events = drainControllerEvents();
        if (reset_events != USB_OK) {
            latchRuntimeFault("xHCI reset completion event drain failed");
            return;
        }
        struct usb_host_route route = {};
        route.root_port = static_cast<uint8_t>(port);
        const int enumeration = enumerateDevice(&route, speed);
        if (enumeration != USB_OK && mController->state != USB_XHCI_CONTROLLER_RUNNING) {
            latchRuntimeFault("USB device enumeration faulted the controller");
            return;
        }
        DeviceRecord *enumerated = findRootDevice(static_cast<uint8_t>(port));
        if (enumeration != USB_OK &&
            (enumerated == nullptr || enumerated->host.state != USB_HOST_CONFIGURED)) {
            mRootRetryPending[port] = true;
            mRootRetryAfterMs[port] = uptimeMilliseconds(nullptr) + kTopologyRetryDelayMs;
            IOLog("DaliUsbQemuXhciRuntime: USB2 root-port %u device rejected (%d); retry scheduled\n",
                  port, enumeration);
        } else if (enumeration == USB_OK) {
            mRootRetryPending[port] = false;
            mRootRetryAfterMs[port] = 0;
        }
    }

    const int drained = drainControllerEvents();
    if (drained != USB_OK) {
        latchRuntimeFault("unexpected xHCI event while polling root ports");
    }
}

void
DaliUsbQemuXhciRuntime::pollUsbDevices()
{
    if (!mHostAdapterReady || mDevices == nullptr || mTopologyFaulted) return;
    for (uint8_t index = 0; index < mDeviceCapacity; ++index) {
        DeviceRecord *record = &mDevices[index];
        if (!record->in_use || record->host.state != USB_HOST_CONFIGURED) continue;

        const int publish = publishDeviceServices(record);
        if (publish != USB_OK) {
            if (mController == nullptr || mController->state != USB_XHCI_CONTROLLER_RUNNING)
                latchRuntimeFault("USB child publication failed after controller fault");
            continue;
        }
        if (!record->hub_started || !record->hub.initialized) continue;
        const uint32_t now_ms = uptimeMilliseconds(nullptr);
        if (record->hub_retry_pending &&
            deadline_pending(now_ms, record->hub_retry_after_ms)) continue;

        /* The current xHCI interrupt helper is synchronous and fail-stops on an
         * idle interrupt endpoint timeout. Until an async interrupt owner exists,
         * scan hub port status through class control requests instead. */
        record->hub.scan_all_ports = true;
        const int result = usb_host_hub_poll(&record->host, &record->hub,
                                             kHubPollTimeoutMs, hubEvent, this);
        if (result == USB_OK || result == USB_AGAIN) {
            record->hub_retry_pending = false;
            record->hub_retry_after_ms = 0;
            continue;
        }
        if (mController == nullptr || mController->state != USB_XHCI_CONTROLLER_RUNNING ||
            record->hub.faulted) {
            latchRuntimeFault("USB hub polling faulted the controller");
            return;
        }
        record->hub_retry_pending = true;
        record->hub_retry_after_ms = now_ms + kTopologyRetryDelayMs;
        IOLog("DaliUsbQemuXhciRuntime: USB hub at address %u poll failed (%d); retry scheduled\n",
              record->host.address, result);
    }
}

bool
DaliUsbQemuXhciRuntime::copyRuntimeSnapshot(
    struct dali_qemu_xhci_runtime_snapshot *output) const
{
    if (output == nullptr || mSnapshotLock == nullptr) return false;
    IOLockLock(mSnapshotLock);
    *output = mSnapshot;
    IOLockUnlock(mSnapshotLock);
    return true;
}
