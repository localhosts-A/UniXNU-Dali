/* SPDX-License-Identifier: GPL-2.0-only */
/* MT6991 chosen ATAG consumers: phy-mtk-xsphy.c and mtk-devinfo.c,
 * fixed device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbCalibration.h"
static uint32_t le32(const uint8_t *p)
{ return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }
int dali_usb_calibration_decode(const void *chipid, size_t chipid_size,
    const void *devinfo, size_t devinfo_size, struct dali_xsphy_calibration *out)
{
    if (!chipid || !devinfo || !out) return USB_INVALID;
    if (chipid_size != 20 || devinfo_size < 4) return USB_RANGE;
    const uint8_t *chip=chipid, *info=devinfo;
    /* Dali's observed legacy tag reports24 even though its property is20 bytes. */
    if (le32(chip) != 24 || le32(chip+4) != 0x1357 || le32(chip+16)>1) return USB_UNSUPPORTED;
    uint32_t words=le32(info);
    if (words<119 || words>400 || devinfo_size != 4+(size_t)words*4) return USB_RANGE;
    *out=(struct dali_xsphy_calibration){.sw_ver=le32(chip+16),
        .efuse_1d4=le32(info+4+0x1d4),.efuse_1d8=le32(info+4+0x1d8),.qualified=true};
    return USB_OK;
}
