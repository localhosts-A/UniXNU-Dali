/* SPDX-License-Identifier: GPL-2.0-only */
/* MT6991 GPIO104/105 fields and EH selector semantics from pinned MediaTek
 * pinctrl-mt6991.c / pinctrl-mtk-common-v2.c, device_modules e45c3199537f. */
#include "DaliUsbI2cPins.h"
static const uint32_t offsets[] = {0x3d0, 0x20, 0x50, 0xc0, 0x90, 0x80, 0, 0x10, 0xb0};
static const uint32_t masks[] = {0xff, (7u << 6) | (7u << 15), (1u << 11) | (1u << 14),
    (1u << 8) | (1u << 11), (1u << 11) | (1u << 14), (1u << 11) | (1u << 14),
    7u << 24, 7u << 3, (7u << 6) | (7u << 15)};
static int capture(const struct usb_mmio *gpio, const struct usb_mmio *iocfg, uint32_t *values)
{
    gpio->barrier(gpio->context); iocfg->barrier(iocfg->context);
    for (size_t n = 0; n < sizeof offsets / sizeof offsets[0]; ++n) {
        uint32_t value;
        int result = usb_mmio_read(n ? iocfg : gpio, offsets[n], &value);
        if (result) return result;
        values[n] = value & masks[n];
    }
    gpio->barrier(gpio->context); iocfg->barrier(iocfg->context);
    return USB_OK;
}
int dali_usb_i2c6_pins_read(const struct usb_mmio *gpio, const struct usb_mmio *iocfg,
    struct dali_i2c6_pin_snapshot *out)
{
    if (!gpio || !iocfg || !out || !gpio->read32 || !iocfg->read32 ||
        !gpio->barrier || !iocfg->barrier) return USB_INVALID;
    if (gpio->size < 0x3d4 || iocfg->size < 0xc4) return USB_RANGE;
    uint32_t first[9], second[9];
    int result = capture(gpio, iocfg, first);
    if (!result) result = capture(gpio, iocfg, second);
    if (result) return result;
    for (size_t n = 0; n < 9; ++n) if (first[n] != second[n]) return USB_BUSY;
    if (first[0] != 0x11 || (first[1] & ((1u << 6) | (1u << 15))) != ((1u << 6) | (1u << 15)))
        return USB_UNSUPPORTED;
    struct dali_i2c6_pin_snapshot value = {0};
    for (unsigned n = 0; n < 2; ++n) {
        struct dali_i2c6_pin_state *pin = n ? &value.sda : &value.scl;
        unsigned eh = n ? 15u : 6u, input = n ? 14u : 11u, smt = n ? 11u : 8u;
        *pin = (struct dali_i2c6_pin_state){1, (uint8_t)((first[1] >> eh) & 7),
            (uint8_t)((first[2] >> input) & 1), (uint8_t)((first[3] >> smt) & 1),
            (uint8_t)((first[4] >> input) & 1), (uint8_t)((first[5] >> input) & 1),
            (uint8_t)((first[n ? 7 : 6] >> (n ? 3 : 24)) & 7), (uint8_t)((first[8] >> eh) & 7)};
    }
    *out = value;
    return USB_OK;
}
