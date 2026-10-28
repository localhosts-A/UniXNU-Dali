/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_QEMU_XHCI_RUNTIME_HPP
#define DALI_USB_QEMU_XHCI_RUNTIME_HPP

#include <IOKit/IOService.h>
#include <IOKit/IOLocks.h>
#include "DaliUsbQemuPciOwner.hpp"
#include "DaliUsbXhciDma.hpp"
#include "UsbHost.h"
#include "UsbXhci.h"
#include "UsbXhciHost.h"

class IOTimerEventSource;
class IOWorkLoop;

#define DALI_QEMU_XHCI_RUNTIME_SNAPSHOT_VERSION 1u
struct dali_qemu_xhci_runtime_snapshot {
    uint32_t version;
    uint8_t max_ports;
    bool controller_running;
    bool faulted;
    struct usb_xhci_root_port_status ports[256];
    uint8_t valid_ports[USB_XHCI_PORT_BITMAP_BYTES];
};

/* QEMU-only PCI xHCI controller owner. The workloop serializes root-port reset,
 * USB enumeration, child publication and hub status scans. HID report polling
 * remains unwired until the controller exposes asynchronous interrupt I/O. */
class DaliUsbQemuXhciRuntime : public IOService {
    OSDeclareDefaultStructors(DaliUsbQemuXhciRuntime)

    DaliUsbQemuPciOwner *mPciOwner = nullptr;
    IOWorkLoop *mWorkLoop = nullptr;
    IOTimerEventSource *mPollTimer = nullptr;
    IOLock *mSnapshotLock = nullptr;
    struct usb_xhci_controller *mController = nullptr;
    struct dali_usb_xhci_dma_context mDma = {};
    struct usb_dma_ops mDmaOps = {};
    struct usb_xhci_capabilities mCapabilities = {};
    struct usb_mmio mMmio = {};
    struct dali_qemu_pci_xhci mPciDescription = {};
    struct dali_qemu_xhci_runtime_snapshot mSnapshot = {};
    struct DeviceRecord {
        struct usb_host_device host = {};
        struct usb_host_hub hub = {};
        struct usb_host_route route = {};
        IOService *hid_services[USB_HOST_MAX_HID_INTERFACES] = {};
        bool hid_quiesced[USB_HOST_MAX_HID_INTERFACES] = {};
        IOService *storage_service = nullptr;
        bool storage_quiesced = false;
        bool in_use = false;
        bool hub_started = false;
        bool hub_retry_pending = false;
        uint32_t hub_retry_after_ms = 0;
    };
    struct usb_xhci_host_adapter mHostAdapter = {};
    struct usb_host_ops mHostOps = {};
    struct usb_xhci_host_device *mHostDevices = nullptr;
    struct DeviceRecord *mDevices = nullptr;
    IOLock *mAdapterLock = nullptr;
    uint8_t mDeviceCapacity = 0;
    bool mGuardActive = false;
    bool mMmioBound = false;
    bool mTimerAdded = false;
    bool mStartedController = false;
    bool mHostAdapterReady = false;
    bool mTopologyFaulted = false;
    bool mRetained = false;
    bool mRootRetryPending[256] = {};
    uint32_t mRootRetryAfterMs[256] = {};
    bool mRootConnectionEvent[256] = {};

    static int controllerGuard(void *);
    static int adapterLock(void *);
    static void adapterUnlock(void *);
    static uint32_t uptimeMilliseconds(void *);
    static int hubEvent(void *, enum usb_host_hub_event_kind, uint8_t,
                        const struct usb_host_route *, enum usb_host_speed);
    static void pollTimerAction(OSObject *, IOTimerEventSource *);
    void latchRuntimeFault(const char *reason);
    void pollRootPorts();
    void pollUsbDevices();
    int drainControllerEvents();
    int enumerateDevice(const struct usb_host_route *, enum usb_host_speed);
    int publishDeviceServices(DeviceRecord *);
    int handleHubEvent(enum usb_host_hub_event_kind, const struct usb_host_route *,
                       enum usb_host_speed);
    int quiesceDeviceServices(DeviceRecord *);
    int stopDevice(DeviceRecord *);
    bool quiesceAllDeviceServices();
    bool stopAllDevicesGracefully();
    bool releaseDevicesAfterControllerStop();
    DeviceRecord *findDevice(const struct usb_host_route *);
    DeviceRecord *findRootDevice(uint8_t root_port);
    DeviceRecord *allocateDevice(const struct usb_host_route *);
    bool stopTimer();
    bool releaseControllerOwnership();
    void releaseLocalObjects();
    bool failStart(IOService *provider, const char *reason);

public:
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    void free() override;

    /* Returns a consistent cached snapshot. Port IDs are one-based. */
    bool copyRuntimeSnapshot(struct dali_qemu_xhci_runtime_snapshot *output) const;
};

#endif
