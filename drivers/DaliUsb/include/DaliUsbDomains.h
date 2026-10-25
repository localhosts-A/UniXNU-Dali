/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_DOMAINS_H
#define DALI_USB_DOMAINS_H
#include "UsbCore.h"
#ifdef __cplusplus
extern "C" {
#endif
enum dali_domain_power { DALI_DOMAIN_UNKNOWN, DALI_DOMAIN_OFF, DALI_DOMAIN_ON };
enum dali_domain_phase { DALI_DOMAIN_IDLE, DALI_DOMAIN_STARTING, DALI_DOMAIN_STOPPING };
struct dali_domain_step {
    enum dali_domain_power power;
    enum dali_domain_phase phase;
    uint8_t cursor;
    bool restore;
};
struct dali_domain_guard {
    void *context;
    /* Prove ALL USB0 consumers/PHY stopped and all DMA drained. Do not access
     * powered-off MAC/PHY windows or recurse into this domain API. The caller
     * stops controllers/PHY before this check, and keeps that proof valid. */
    int (*quiesce)(void *);
};
struct dali_usb_domains {
    struct usb_mmio controller, protection;
    struct dali_domain_guard guard;
    /* [0]=SSUSB_P0 parent, [1]=SSUSB_DP_PHY_P0 child. */
    struct dali_domain_step domain[2];
    bool initialized, faulted;
};
/* Zeroed persistent object, serialized application/worker context (not IRQ).
 * qualified requires validated Dali provider/caps and exclusive ownership of
 * BOTH complete USB0 domains and all their consumers, held supplies/clocks,
 * native MMIO at 1c004000 and SPM protection at 1c00d000. Ownership must hold
 * throughout retained errors. External reset or mutation invalidates
 * a retained transition; the platform must re-establish ownership/state before
 * constructing a fresh object, never resume old RAM cursors after such a reset.
 * No optional domain supply/clock override may be unhandled. Shared protection uses only bit6/7
 * SET/CLR aliases; no global voter/SMC/other domain register is changed.
 * Init reads only, accepting coherent ON/OFF states and parent dependency.
 * Destroy explicitly turns off even inherited ON domains: ownership is required. */
int dali_usb_domains_init(struct dali_usb_domains *, const struct usb_mmio *,
    const struct usb_mmio *, const struct dali_domain_guard *, bool qualified);
int dali_usb_domains_power_on(struct dali_usb_domains *);
/* On failure retain cursor/context/clocks and prohibit new startup. Explicit
 * shutdown resumes owned steps; an incomplete own power-on is completed with
 * bus protection retained, then stopped. Reads/writes are never applied to an
 * unrecognized foreign idle transition. Retained steps rely on the exclusive
 * no-external-reset contract above. Polls use10us/1s source deadlines. Faults
 * remain latched until successful destroy/reinit. */
int dali_usb_domains_power_off(struct dali_usb_domains *);
int dali_usb_domains_destroy(struct dali_usb_domains *);
#ifdef __cplusplus
}
#endif
#endif
