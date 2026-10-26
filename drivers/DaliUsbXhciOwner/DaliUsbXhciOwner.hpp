/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_XHCI_OWNER_HPP
#define DALI_USB_XHCI_OWNER_HPP

#include <IOKit/IOService.h>
#include "DaliUsbXhciResourceV1.h"

class IOMemoryMap;

/* Owns the CPU mapping of the validated xhci0 register window. It deliberately
 * does not read or write registers until separate power, clock, IRQ and DMA
 * leases are implemented and qualified. */
class DaliUsbXhciOwner : public IOService {
    OSDeclareDefaultStructors(DaliUsbXhciOwner)

    struct dali_usb_xhci_resource_v1 mResources = {};
    IOMemoryMap *mRegisterMap = nullptr;
    bool mMapped = false;

public:
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    bool hasMappedWindow() const { return mMapped && mRegisterMap != nullptr; }
};

#endif
