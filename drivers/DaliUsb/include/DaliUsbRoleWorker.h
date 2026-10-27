/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_ROLE_WORKER_H
#define DALI_USB_ROLE_WORKER_H
#include "DaliUsbTypec.h"
#ifdef __cplusplus
extern "C" {
#endif

struct dali_usb_role_worker {
    struct dali_usb_typec *typec;
    struct dali_usb_vbus *vbus;
    struct usb_role_ops platform_ops;
    struct usb_role_ops dispatch_ops;
    struct usb_role_state role;
    int stop_error;
    bool dma_stopped, initialized, faulted;
};

/* Zero-initialized persistent object. The caller must serialize start/poll/stop
 * in application context and retain every PMIF, clock, domain, PHY, controller,
 * IRQ and DMA lease until stop succeeds. Type-C must already be running in DRP
 * with VBUS initialized and OFF.
 *
 * platform_ops supplies the real host/gadget owner:
 *  - stop_submissions closes host and gadget request admission;
 *  - sync_storage flushes the external filesystem and must fail closed;
 *  - cancel_requests cancels outstanding non-storage work;
 *  - stop_dma succeeds only after every USB controller and queue is halted;
 *  - select_role configures/releases the qualified power, clock, HWRSC, PHY and
 *    IPPC resources, but never starts submissions; STOPPED is a retryable safe
 *    teardown after DMA proof;
 *  - start starts only the selected host/gadget engine. It is called after
 *    Type-C orientation is latched and, for HOST, after 5V VBUS is verified.
 * Callback pairs must be serialized and idempotent for explicit stop retries.
 *
 * This layer does not supply board qualifications, a polling timer, or a host /
 * gadget provider. It cannot turn a model callback into hardware qualification. */
int dali_usb_role_worker_init(struct dali_usb_role_worker *, struct dali_usb_typec *,
    struct dali_usb_vbus *, const struct usb_role_ops *platform_ops);
/* Call at <=5ms cadence. USB_AGAIN means debounce is incomplete. The output is
 * only written when USB_OK is returned. A role/orientation change always passes
 * through STOPPED, filesystem sync, request cancellation and DMA-stop proof. */
int dali_usb_role_worker_poll(struct dali_usb_role_worker *, uint64_t now_us,
    struct dali_typec_sample *published);
/* Explicitly retry safe teardown after a fault. A failure retains this object
 * and its referenced dependencies; success closes Type-C and clears the worker. */
int dali_usb_role_worker_stop(struct dali_usb_role_worker *);

#ifdef __cplusplus
}
#endif
#endif
