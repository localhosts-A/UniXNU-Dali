/* SPDX-License-Identifier: GPL-2.0-only */
/* MediaTek (C)2023 phy-mtk-nxp-eusb2-repeater.c register/reset contract,
 * fixed MiCode device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbRepeater.h"
static int fault(struct dali_nxp_repeater *r, int result)
{ r->faulted = true; return result; }
int dali_nxp_repeater_probe(struct dali_nxp_repeater *r, const struct dali_nxp_repeater_bus *bus,
    const struct dali_repeater_guard *guard, bool qualified)
{
    if (!r || !bus || !guard || !guard->quiesce || !bus->read8 || !bus->write8 || !bus->recover || !bus->enable || !bus->delay_us) return USB_INVALID;
    if (r->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    int result = guard->quiesce(guard->context);
    if (result) return result;
    r->bus = *bus; r->guard = *guard; r->initialized = true;
    result = bus->enable(bus->context, false);
    if (!result) { bus->delay_us(bus->context, 10); result = bus->enable(bus->context, true); }
    if (result) return fault(r, result);
    r->enabled = true; bus->delay_us(bus->context, 4000);
    uint8_t high, low;
    result = bus->read8(bus->context, 0x15, &high);
    if (!result) result = bus->read8(bus->context, 0x14, &low);
    if (!result && (high != 0x32 || low != 0x22)) result = USB_UNSUPPORTED;
    if (result) return fault(r, result);
    r->identified = true; return USB_OK;
}
int dali_nxp_repeater_mode(struct dali_nxp_repeater *r, enum usb_role role)
{
    if (!r || !r->initialized || role < USB_ROLE_STOPPED || role > USB_ROLE_HOST) return USB_INVALID;
    if (r->faulted) return USB_IO;
    if (!r->enabled || !r->identified) return USB_DISCONNECTED;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    uint8_t value = 0;
    result = r->bus.read8(r->bus.context, 2, &value);
    uint8_t mode = role == USB_ROLE_HOST ? 0x10 : role == USB_ROLE_DEVICE ? 0x20 : 0;
    if (!result) result = r->bus.write8(r->bus.context, 2, (uint8_t)((value & 0xcf) | mode));
    if (result) return fault(r, result);
    r->role = role; return USB_OK;
}
int dali_nxp_repeater_destroy(struct dali_nxp_repeater *r)
{
    if (!r || !r->initialized) return USB_INVALID;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    result = r->bus.recover(r->bus.context);
    if (!result) result = r->bus.enable(r->bus.context, false);
    if (result) return fault(r, result);
    *r = (struct dali_nxp_repeater){0}; return USB_OK;
}
