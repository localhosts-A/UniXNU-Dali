/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_CALIBRATION_H
#define DALI_USB_CALIBRATION_H
#include "UsbCore.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_xsphy_calibration {
    /* Validated Dali chosen chipid.sw_ver and nvmem words1d4/1d8. Acquired
     * zero fields mean no software override, not failed acquisition. */
    uint32_t sw_ver, efuse_1d4, efuse_1d8;
    bool qualified;
};
/* Decode bounded immutable ATAG property bytes, not DT big-endian cells.
 * Fixed MT6991 little-endian boot format: chipid five words/20 bytes with
 * legacy size word24, hw_code1357, sw_ver A0/B0; devinfo count then that many
 * LE32 words. Source's400-word warning is enforced here. The native FDT/ADT
 * adapter must validate the MT6991 graph, enabled provider and actual USB cell
 * references/masks before supplying these properties. This function validates
 * payload format/bounds only; it neither discovers nor authenticates hardware.
 * No allocation, MMIO, SMC or retained pointers. Output changes only on success;
 * callers must check status. FDT publication must use a stable boot snapshot. */
int dali_usb_calibration_decode(const void *chipid, size_t chipid_size,
    const void *devinfo, size_t devinfo_size, struct dali_xsphy_calibration *);
#ifdef __cplusplus
}
#endif
#endif
