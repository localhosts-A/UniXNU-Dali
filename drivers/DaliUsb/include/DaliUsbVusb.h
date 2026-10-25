/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_VUSB_H
#define DALI_USB_VUSB_H
#include "DaliUsbPmif.h"
#include "DaliUsbRepeater.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_usb_vusb {
    struct dali_usb_pmif *pmif;
    struct dali_repeater_guard guard;
    uint32_t nominal_uv;
    uint8_t selector, trim, lp;
    bool initialized, held, enable_attempted, faulted;
};
/* Zeroed persistent single USB resource owner, serialized worker. qualified
 * validates exact enabled USB0 vusb33 -> MT6373 SID5/regulators/VUSB, declared
 * always-on policy,1200000..3500000uV/720us and CID tuple73,9,8, native PMIF
 * readiness/M-route/shared SWINF2 arbitration, and a platform policy excluding
 * competing voltage/trim/LP/enable changes for the lease. Guard stops USB/PHY
 * consumers before acquiring or releasing the supply. Init only reads, preserves
 * failed output/context and captures two stable masked snapshots.
 * The returned nominal voltage is the selector table, not a measured/trim-corrected
 * voltage or proof of power-good. PHY consumer suitability is a separate gate. */
int dali_usb_vusb_init(struct dali_usb_vusb *, struct dali_usb_pmif *,
    const struct dali_repeater_guard *, bool qualified);
/* Atomically set only enable bit0 if off, leave voltage/LP/trim untouched, wait
 * the declared720us even when inherited on, and verify stable configuration.
 * No automatic replay after a failure; enable_attempted is set before writing.
 * Repeated acquire rechecks the held rail without cycling power. */
int dali_usb_vusb_acquire(struct dali_usb_vusb *);
/* Quiesce consumers, recover only this PMIF client's outstanding command,
 * then release software context. Never disable an always-on rail, even if we
 * enabled it. Failed recovery/guard retains the object for explicit retry.
 * Clock/source/connector/DMA prerequisites must be handled independently. */
int dali_usb_vusb_release(struct dali_usb_vusb *);
#ifdef __cplusplus
}
#endif
#endif
