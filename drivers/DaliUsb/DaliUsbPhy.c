/* SPDX-License-Identifier: GPL-2.0-only */
/* USB2 sequences from MediaTek phy-mtk-xsphy.c, fixed MiCode device_modules
 * e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee; Dali PHY0 FDT properties. */
#include "DaliUsbPhy.h"
struct step { uint32_t reg, mask, value, wait; };
static int update(struct dali_usb_phy *p, uint32_t reg, uint32_t mask, uint32_t value)
{
    uint32_t old;
    int result = usb_mmio_read(&p->registers, reg, &old);
    return result ? result : usb_mmio_write(&p->registers, reg, (old & ~mask) | value);
}
static int sequence(struct dali_usb_phy *p, const struct step *steps, size_t count)
{
    for (size_t n = 0; n < count; ++n) {
        int result = update(p, steps[n].reg, steps[n].mask, steps[n].value);
        if (result) return result;
        if (steps[n].wait) p->registers.delay_us(p->registers.context, steps[n].wait);
    }
    return USB_OK;
}
#define RUN(p,s) sequence(p,s,sizeof(s)/sizeof((s)[0]))
static int fault(struct dali_usb_phy *p, int result)
{ p->faulted = true; p->power = DALI_REPEATER_UNKNOWN; return result; }
int dali_usb_phy_init(struct dali_usb_phy *p, const struct usb_mmio *m,
    struct dali_nxp_repeater *nxp, struct dali_mt6379_repeater *mt,
    const struct dali_xsphy_calibration *cal, const struct dali_repeater_guard *guard, bool qualified)
{
    if (!p || !m || !nxp || !mt || !cal || !guard || !guard->quiesce ||
        !m->read32 || !m->write32 || !m->barrier || !m->delay_us) return USB_INVALID;
    if (p->initialized) return USB_BUSY;
    if (!qualified || !cal->qualified) return USB_UNSUPPORTED;
    if (m->size < 0x370) return USB_RANGE;
    if (!nxp->initialized || !nxp->identified || !nxp->enabled || !mt->initialized) return USB_INVALID;
    if (nxp->faulted || mt->faulted || !mt->pmif || mt->pmif->faulted) return USB_IO;
    if (mt->power == DALI_REPEATER_ON || mt->power == DALI_REPEATER_IDLE) return USB_BUSY;
    const uint32_t regs[] = {0x100,0x110,0x314,0x330,0x334};
    uint32_t v[5];
    for (size_t n = 0; n < 5; ++n) {
        int result = usb_mmio_read(m, regs[n], &v[n]);
        if (result) return result;
    }
    if ((v[0] & 0x1000000) || (v[1] & 0x100) || (v[2] & 0x8000)) return USB_BUSY;
    *p = (struct dali_usb_phy){.registers=*m, .guard=*guard, .calibration=*cal, .nxp=nxp, .mt=mt,
        .intr=(uint8_t)((v[3] >> 24) & 63), .term=(uint8_t)((v[4] >> 8) & 15), .initialized=true};
    return USB_OK;
}
static int props(struct dali_usb_phy *p, enum usb_role role)
{
    int result = update(p, 0x330, 0x3f000000, (uint32_t)p->intr << 24);
    if (!result) result = update(p, 0x334, 0xf00, (uint32_t)p->term << 8);
    /* Dali has no SoC eye/host overrides or intr/term offsets. */
    if (!result && role == USB_ROLE_DEVICE && !p->calibration.sw_ver) result = update(p, 0x340, 0x70, 0x70);
    if (!result && role == USB_ROLE_DEVICE) result = update(p, 0x310, 0x700, 0x700);
    return result;
}
static int efuse(struct dali_usb_phy *p)
{
    uint32_t term = (p->calibration.efuse_1d4 >> 20) & 15;
    uint32_t n = (p->calibration.efuse_1d8 >> 10) & 31;
    uint32_t v = (p->calibration.efuse_1d8 >> 15) & 31;
    int result = term ? update(p, 0x340, 0xf0000, term << 16) : USB_OK;
    if (!result && n) result = update(p, 0x340, 0x1f00000, n << 20);
    if (!result && v) result = update(p, 0x344, 0x1f00, v << 8);
    return result;
}
static int fm_stop(struct dali_usb_phy *p)
{
    const struct step s[] = {{0x100,0x1000000,0,0},{0x110,0x100,0,0},{0x314,0x8000,0,0}};
    int result = RUN(p,s);
    if (!result) p->fm_owned = false;
    return result;
}
static int calibrate(struct dali_usb_phy *p)
{
    p->fm_owned = true; /* Set before possibly-applied oscillator write. */
    const struct step s[] = {{0x314,0x8000,0x8000,1},{0x110,0x100,0x100,0},
        {0x100,0xffffff,1024,0},{0x100,0x1000000,0x1000000,0}};
    int result = RUN(p,s);
    if (!result) result = usb_mmio_poll(&p->registers, 0x110, 1, 1, 200, 10);
    uint32_t measured = 0;
    if (!result) result = usb_mmio_read(&p->registers, 0x10c, &measured);
    uint32_t slew = measured ? ((26u * 17u * 1024u / measured) + 500u) / 1000u : 0;
    if (!result && !measured) result = USB_IO;
    if (!result && slew > 7) result = USB_RANGE;
    /* Reference order: meter/free-clock off, slew write, oscillator off.
     * Always attempt cleanup on errors and retain ownership if it fails. */
    const struct step stop[] = {{0x100,0x1000000,0,0},{0x110,0x100,0,0}};
    int stopped = RUN(p,stop);
    if (!result) result = stopped;
    if (!result) result = update(p, 0x314, 0x7000, slew << 12);
    int oscillator = update(p, 0x314, 0x8000, 0);
    if (!stopped && !oscillator) p->fm_owned = false;
    if (!result) result = oscillator;
    return result;
}
static int set_mode(struct dali_usb_phy *p, enum usb_role role)
{
    /* Detach DEVICE before even the first repeater changes role. */
    int result = dali_mt6379_repeater_pullup(p->mt, false);
    if (!result) result = dali_nxp_repeater_mode(p->nxp, role);
    if (!result) result = dali_mt6379_repeater_mode(p->mt, role);
    if (!result) result = props(p, role);
    if (!result) result = update(p, 0x36c, 0x202, role == USB_ROLE_HOST ? 0x200 : 0x202);
    if (!result) p->role = role;
    return result;
}
int dali_usb_phy_power_on(struct dali_usb_phy *p, enum usb_role role)
{
    if (!p || !p->initialized || (role != USB_ROLE_HOST && role != USB_ROLE_DEVICE)) return USB_INVALID;
    if (p->faulted) return USB_IO;
    if (p->power == DALI_REPEATER_ON) return USB_BUSY;
    int result = p->guard.quiesce(p->guard.context);
    if (result) return result;
    p->power = DALI_REPEATER_UNKNOWN;
    const struct step init[] = {{0x318,0x800000,0,0},{0x300,0x20,0x20,0}};
    result = RUN(p,init);
    if (!result) result = props(p, USB_ROLE_DEVICE);
    if (!result) result = dali_mt6379_repeater_power_on(p->mt); /* NXP power_on has no Dali tuning writes. */
    if (!result) result = efuse(p);
    const struct step on[] = {{0x344,0x20,0x20,250},{0x368,0x40000,0x40000,0},{0x368,8,0,0},
        {0x368,8,8,30},{0x368,0x40000,0,0},{0x368,8,0,0},{0x368,0x4000000,0,0},
        {0x36c,0x10000,0,0},{0x320,0x300,0,0},{0x368,0x40000,0,0},{0x368,0xbe3cf4,0,0},
        {0x318,0x800000,0,0},{0x318,0x100000,0x100000,0},{0x36c,0x34,0x24,0},
        {0x300,0x7000000,0,0},{0x318,0xc2000000,0,0},{0x318,0x40000000,0x40000000,800},
        {0x364,0xc0000,0x40000,0},{4,0x10000,0x10000,0},{0,0xffffff00,0x1e1e1900,0}};
    if (!result) result = RUN(p,on);
    if (!result && !p->calibration.sw_ver) result = update(p, 0x300, 2, 0);
    if (!result) result = calibrate(p);
    if (!result) result = set_mode(p, role);
    if (result) return fault(p,result);
    p->power = DALI_REPEATER_ON; return USB_OK;
}
int dali_usb_phy_mode(struct dali_usb_phy *p, enum usb_role role)
{
    if (!p || !p->initialized || (role != USB_ROLE_HOST && role != USB_ROLE_DEVICE)) return USB_INVALID;
    if (p->faulted) return USB_IO;
    if (p->power != DALI_REPEATER_ON) return USB_DISCONNECTED;
    int result = p->guard.quiesce(p->guard.context);
    if (result) return result;
    result = set_mode(p,role);
    return result ? fault(p,result) : USB_OK;
}
int dali_usb_phy_pullup(struct dali_usb_phy *p, bool enabled)
{
    if (!p || !p->initialized) return USB_INVALID;
    if (p->faulted) return USB_IO;
    if (p->power != DALI_REPEATER_ON || (enabled && p->role != USB_ROLE_DEVICE)) return USB_DISCONNECTED;
    int result = dali_mt6379_repeater_pullup(p->mt,enabled);
    return result ? fault(p,result) : USB_OK;
}
int dali_usb_phy_power_off(struct dali_usb_phy *p)
{
    if (!p || !p->initialized) return USB_INVALID;
    if (p->power == DALI_REPEATER_OFF) return USB_OK;
    int result = p->guard.quiesce(p->guard.context);
    if (result) return result;
    result = dali_mt6379_repeater_power_off(p->mt); /* NXP power_off is a no-op. */
    if (!result && p->fm_owned) result = fm_stop(p);
    const struct step off[] = {{0x368,0x4000000,0,0},{0x36c,0x10000,0,0},{0x320,0x300,0,0},
        {0x318,0x800000,0,0},{0x318,0x100000,0,0},{0x36c,0x34,0x10,0},
        {0x368,0x40008,0x40008,2000},{0x368,0x3c00,0,0},{0x368,0xbe00d4,0xbe00d4,0},
        {0x318,0x40000000,0x40000000,800},{0x368,8,0,1}};
    if (!result) result = RUN(p,off);
    if (result) return fault(p,result);
    p->power = DALI_REPEATER_OFF; p->role = USB_ROLE_STOPPED; return USB_OK;
}
int dali_usb_phy_destroy(struct dali_usb_phy *p)
{
    int result = dali_usb_phy_power_off(p);
    if (!result) *p = (struct dali_usb_phy){0};
    return result;
}
