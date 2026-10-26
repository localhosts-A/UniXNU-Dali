/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_HID_H
#define DALI_USB_HID_H
#include "UsbHost.h"

#ifdef __cplusplus
class IOService;
extern "C" {
/* Snapshot descriptor/identity from a configured Boot-protocol interface.
 * No host pointer or DMA buffer is retained. The caller owns the returned ref
 * and serializes publish/poll/quiesce/detach/terminate on its host worker. */
IOService *dali_usb_hid_publish(IOService *, const struct usb_host_device *, uint8_t hid_index);
/* Consumes a synchronous copy from usb_host_read_boot_report. No Report ID;
 * exactly 8 keyboard or 3 mouse bytes. Reentrant submissions return BUSY. */
int dali_usb_hid_submit(IOService *, const uint8_t *, size_t);
/* Closes admission and sends a neutral report after the current report drains.
 * BUSY means an in-flight report still owns the service; retry on the worker,
 * never wait inside an IOHID report callback. */
int dali_usb_hid_quiesce(IOService *);
/* Only the controller owner may supply true after endpoint/slot DMA stop.
 * Failed proof or an in-flight report retains the service's lifetime lease. */
int dali_usb_hid_detach(IOService *, bool controller_stopped);
int dali_usb_hid_terminate(IOService *);
}
#endif
#endif
