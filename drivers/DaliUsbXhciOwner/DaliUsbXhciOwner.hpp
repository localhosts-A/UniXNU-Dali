/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_XHCI_OWNER_HPP
#define DALI_USB_XHCI_OWNER_HPP

#include <IOKit/IOService.h>
#include <IOKit/IOLocks.h>
#include "DaliUsbXhciResourceV1.h"
#include "UsbCore.h"

class IOMemoryMap;

/* Owns the CPU mapping of the validated xhci0 register window. It deliberately
 * does not read or write registers until separate power, clock, IRQ and DMA
 * leases are implemented and qualified. */
class DaliUsbXhciOwner : public IOService {
    OSDeclareDefaultStructors(DaliUsbXhciOwner)

    struct dali_usb_xhci_resource_v1 mResources = {};
    IOMemoryMap *mRegisterMap = nullptr;
    IOLock *mRegisterLock = nullptr;
    int (*mRegisterGuard)(void *) = nullptr;
    void *mRegisterGuardContext = nullptr;
    bool mMapped = false;
    bool mRegisterOpsBound = false;
    bool mRegisterRefHeld = false;

    static int registerRead32(void *, uint32_t, uint32_t *);
    static int registerWrite32(void *, uint32_t, uint32_t);
    static bool validRegisterAccess(const DaliUsbXhciOwner *, uint32_t);
    static void registerBarrier(void *);
    static void registerDelay(void *, uint32_t);
    void releaseRegisterLock();

public:
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    void free() override;
    bool hasMappedWindow() const { return mMapped && mRegisterMap != nullptr; }

    /* Bind bounded xHCI MMIO callbacks only while the caller holds a platform
     * lease whose guard proves exclusive controller/MMIO/power ownership.
     * The guard must be nonblocking and non-reentrant. This method validates the
     * guard but does not access the mapped registers; the caller retains its
     * guard context until releaseGuardedMmio succeeds. */
    bool bindGuardedMmio(int (*guard)(void *), void *guard_context,
                         struct usb_mmio *out);
    /* Revoke callbacks only after a fresh controller stop proof, while the
     * platform lease still permits its guard. stop() retains the mapping if
     * callbacks remain bound. */
    bool releaseGuardedMmio(bool controller_stopped);
};

#endif
