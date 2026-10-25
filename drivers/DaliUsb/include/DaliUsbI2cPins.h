/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_I2C_PINS_H
#define DALI_USB_I2C_PINS_H
#include "UsbCore.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_i2c6_pin_state {
    uint8_t mode, enhanced_drive, input_enable, schmitt;
    uint8_t pull_up, pull_down, drive, resistance;
};
struct dali_i2c6_pin_snapshot { struct dali_i2c6_pin_state scl, sda; };
/* Read only the real GPIO104/105 MODE and IOCFG_LB2 configuration fields.
 * Caller supplies validated powered native device mappings, serialized access
 * and exclusion of pin writers throughout the capture. Two masked captures
 * detect observed changes, not transient/ABA changes or future writers.
 * Require MODE1 and EH selector bit0 for SCL6/SDA6, preserving/reporting both
 * higher EH drive bits. Source does not establish unique IES/SMT/PU/PD/DRV/RSEL
 * requirements; report these inherited settings without guessing a policy.
 * This is not electrical, clock, ownership or I2C-ready qualification. No write,
 * allocation, retained state or pin mux change; failure leaves Out unchanged.
 * GPIO is based at1002d000, IOCFG_LB2 at13110000, at least3d4/c4 bytes.
 * Out is distinct writable storage; both MMIO callback contexts remain valid. */
int dali_usb_i2c6_pins_read(const struct usb_mmio *gpio, const struct usb_mmio *iocfg_lb2,
    struct dali_i2c6_pin_snapshot *out);
#ifdef __cplusplus
}
#endif
#endif
