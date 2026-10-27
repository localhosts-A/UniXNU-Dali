/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_XHCI_DMA_HPP
#define DALI_USB_XHCI_DMA_HPP

#include <IOKit/IOService.h>
#include "UsbCore.h"

class IOMapper;
struct dali_usb_xhci_dma_allocation;

/* A per-device IOKit DMA mapping context. The platform owner must supply the
 * xHCI service that IOMapper recognizes; this layer never assumes that a CPU
 * physical address is also a device-visible bus address. */
struct dali_usb_xhci_dma_context {
    IOMapper *mapper = nullptr;
    struct dali_usb_xhci_dma_allocation *allocations = nullptr;
    bool active = false;
    bool faulted = false;
};

extern "C" {
bool dali_usb_xhci_dma_init(struct dali_usb_xhci_dma_context *, IOService *device);
bool dali_usb_xhci_dma_get_ops(struct dali_usb_xhci_dma_context *, struct usb_dma_ops *);
/* Only after the controller has been stopped and every DMA allocation released. */
bool dali_usb_xhci_dma_stop(struct dali_usb_xhci_dma_context *);
}

#endif
