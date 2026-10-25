/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_GPIO_H
#define DALI_USB_GPIO_H
#include "DaliUsbRepeater.h"
#ifdef __cplusplus
extern "C" {
#endif

struct dali_usb_gpio72 {
    struct usb_mmio registers;
    struct dali_repeater_guard guard;
    bool initialized, faulted, level_known, enabled;
};
/* Zeroed persistent storage, serialized worker. qualified requires validated
 * Dali pinctrl/FDT GPIO72 function0/output-high/output-low states, GPIO MMIO at
 * 0x1002d000 and exclusive pin ownership. No other GPIO owner may change pin72.
 * Init only reads. An inherited non-GPIO mux is refused, never overridden. */
int dali_usb_gpio72_init(struct dali_usb_gpio72 *, const struct usb_mmio *,
    const struct dali_repeater_guard *, bool qualified);
/* Stop USB before changing DO then DIR via race-free bit8 SET/CLR aliases.
 * Readback confirms registers only, not electrical level. Errors retain the
 * object and unknown output; only reset-low and destroy remain allowed after
 * a fault. Successful reset-low does not authorize enable/restart. */
int dali_usb_gpio72_set(struct dali_usb_gpio72 *, bool enabled);
/* Explicitly assert reset low before releasing this pin's software context.
 * Caller destroys dependent NXP/controller state first. No prior mux/level is
 * restored and no neighboring pins, pulls or drive strengths are modified. */
int dali_usb_gpio72_destroy(struct dali_usb_gpio72 *);

#ifdef __cplusplus
}
#endif
#endif
