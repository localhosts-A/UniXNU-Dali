/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_I2C_H
#define DALI_USB_I2C_H
#include "DaliUsbGpio.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_i2c6_io {
    struct usb_mmio controller, dma;
    /* DATA_PORT is byte-wide even on MT6991's 32-bit register interface.
     * Same context as controller; offset is always0. Ordered native accesses. */
    int (*read8)(void *, uint32_t, uint8_t *);
    int (*write8)(void *, uint32_t, uint8_t);
};
struct dali_i2c6_timing { uint32_t high, low, ext, divider; };
struct dali_usb_i2c6 {
    struct dali_i2c6_io io;
    struct dali_repeater_guard guard;
    struct dali_i2c6_timing timing;
    bool initialized, faulted, dirty, pending_owned, write_uncertain;
};
/* Vendor speed_v2 duty/factor search with compensation disabled: never exceed
 * 400kHz. parent_hz must be the proven, enabled, stable controller clock rate. */
int dali_i2c6_timing_400k(uint32_t parent_hz, struct dali_i2c6_timing *);
/* Zeroed persistent object, serialized worker; qualified requires validated
 * Dali FDT, exclusive WHOLE I2C6@13170000/APDMA@16410000 ownership (all remote
 * clients stopped), native MMIO, main/dma clocks and GPIO104/105 I2C pin states.
 * DaliUsbI2cPins reports inherited MODE/EH/electrical fields but is not itself
 * a persistent exclusion lease or proof of the board's electrical policy.
 * Only channel0/open-drain/stretching is supported, matching saved Dali FDT.
 * Reject active/foreign status before any writes. Retain partial-init context.
 * APDMA local reset follows the reference, but this FIFO-only driver never
 * enables DMA, programs data buffers or changes IOMMU routing. */
int dali_usb_i2c6_init(struct dali_usb_i2c6 *, const struct dali_i2c6_io *,
    const struct dali_repeater_guard *, uint32_t parent_hz, bool qualified);
/* Fixed NXP slave4fh byte-register reads (combined WRRD) and2-byte writes.
 * Caller quiesces USB before changing PHY registers. Completion polling is
 * bounded5ms, followed by at most100us to confirm START cleared;
 * failed reads leave output untouched. Any error after configuration retains
 * owned state, latches fault and prohibits new transfers. No automatic replay. */
int dali_usb_i2c6_read8(struct dali_usb_i2c6 *, uint8_t, uint8_t *);
int dali_usb_i2c6_write8(struct dali_usb_i2c6 *, uint8_t, uint8_t);
/* Stop USB, recover/reset only owned work. Foreign activity/DMA is never reset.
 * A fault stays latched after recovery; destroy/reinit is required to restart.
 * write_uncertain persists until destroy; inspect it before releasing context. */
int dali_usb_i2c6_recover(struct dali_usb_i2c6 *);
int dali_usb_i2c6_destroy(struct dali_usb_i2c6 *);
/* Bind initialized production I2C and GPIO backends into NXP's actual bus API.
 * All objects/context are persistent; do not rebind a live NXP instance.
 * Destroy NXP, then I2C/GPIO dependencies. */
struct dali_nxp_io { struct dali_usb_i2c6 *i2c; struct dali_usb_gpio72 *gpio; };
int dali_nxp_io_bind(struct dali_nxp_io *, struct dali_usb_i2c6 *,
    struct dali_usb_gpio72 *, struct dali_nxp_repeater_bus *);
#ifdef __cplusplus
}
#endif
#endif
