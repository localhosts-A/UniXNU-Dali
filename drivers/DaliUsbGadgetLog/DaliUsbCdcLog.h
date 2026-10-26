/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_CDC_LOG_H
#define DALI_USB_CDC_LOG_H

#include "UsbMtu3Device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bind only an initialized, non-faulted gadget after its provider owns the
 * device object. Logging may begin before USB configuration/DTR; the portable
 * ring buffers it until the gadget worker can transmit it.
 */
int dali_usb_cdc_log_bind(struct usb_mtu3_device *device);

/* Unbind before stop/destroy. USB_BUSY means another lifecycle call is active or
 * a producer still holds a transient device reference; keep the object alive and
 * retry until USB_OK.
 */
int dali_usb_cdc_log_unbind(struct usb_mtu3_device *device);

/* Nonblocking producer entry point. Returns bytes accepted by the CDC ring.
 * With no active binding or during a lifecycle transition, it drops the bytes.
 */
size_t dali_usb_cdc_log_write(const void *bytes, size_t length);

/* Bytes rejected because no sink was bound or a lifecycle transition raced a
 * producer. Ring-full and ring-lock losses remain in usb_log_dropped().
 */
uint64_t dali_usb_cdc_log_dropped(void);

#ifdef __cplusplus
}
#endif
#endif
