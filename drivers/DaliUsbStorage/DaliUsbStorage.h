/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_STORAGE_H
#define DALI_USB_STORAGE_H

#include "UsbCore.h"

#ifdef __cplusplus
class IOService;
extern "C" {

/* The USB host owner keeps bot and its transport alive until detach succeeds.
 * The returned service carries one retained reference owned by the caller.
 */
IOService *dali_usb_storage_publish(IOService *provider, struct usb_bot *bot);

/* Call before stopping/changing the host controller. This closes new storage
 * requests and tries SYNCHRONIZE CACHE, but does not prove DMA has stopped.
 */
int dali_usb_storage_quiesce(IOService *service);

/* Call only after the controller owner has independently proved the slot and
 * endpoint DMA stopped. Successful detach invalidates this BOT object.
 */
int dali_usb_storage_detach(IOService *service, bool controller_stopped);

/* Termination is accepted only after controller-stop-proven detach. The caller
 * remains responsible for releasing the reference returned by publish().
 */
int dali_usb_storage_terminate(IOService *service);

}
#endif
#endif
