/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_I2C_CLOCKS_H
#define DALI_USB_I2C_CLOCKS_H
#include "DaliUsbRepeater.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_i2c_clocks {
    struct usb_mmio impw, perao;
    struct dali_repeater_guard guard;
    uint8_t initially_gated, owned;
    bool initialized, on, faulted;
};
/* Exact IMPW131b0000/PERAO16640000 mappings; caller holds stable powered
 * selected parents and exclusive physical gate ownership for this lifetime.
 * PERAO bit26 is shared with UART/APDMA users, not just this I2C controller.
 * Guard must stop every affected consumer before mutations and cleanup.
 * No parent/TOP/PLL/HWV changes. Initial enabled gates remain enabled.
 * Zeroed persistent object, serialized caller. Init only reads stable bits.
 * An attempted write is owned before issue; failures retain all dependencies.
 * After fault only explicit disable/destroy may retry owned cleanup. */
int dali_i2c_clocks_init(struct dali_i2c_clocks *, const struct usb_mmio *impw,
    const struct usb_mmio *perao, const struct dali_repeater_guard *, bool qualified);
int dali_i2c_clocks_enable(struct dali_i2c_clocks *);
int dali_i2c_clocks_disable(struct dali_i2c_clocks *);
int dali_i2c_clocks_destroy(struct dali_i2c_clocks *);
#ifdef __cplusplus
}
#endif
#endif
