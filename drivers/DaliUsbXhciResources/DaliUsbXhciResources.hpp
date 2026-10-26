/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_XHCI_RESOURCES_HPP
#define DALI_USB_XHCI_RESOURCES_HPP

#include <IOKit/IOService.h>
#include "DaliUsbXhciResourceV1.h"

/*
 * Validates the xhci0 device-tree description and provides a copied resource
 * tuple to a future controller owner. The tuple is descriptive only; clients
 * must acquire separate MMIO, power, clock, IRQ, role, and DMA qualifications.
 */
class DaliUsbXhciResources : public IOService {
    OSDeclareDefaultStructors(DaliUsbXhciResources)

    struct dali_usb_xhci_resource_v1 mResources = {};
    bool mValid = false;

public:
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    virtual bool copyResourceDescription(struct dali_usb_xhci_resource_v1 *output) const;
};

#endif
