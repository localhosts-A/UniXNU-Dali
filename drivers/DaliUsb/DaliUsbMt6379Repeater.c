/* SPDX-License-Identifier: GPL-2.0-only */
/* Register sequences from MediaTek (C)2023 phy-mtk-mt6379-eusb2-repeater.c,
 * MiCode/MTK_kernel_device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbRepeater.h"
const struct dali_mt6379_repeater_config dali_mt6379_dali_tuning = {
    {2, 0, 15, 5, 4, 2, 4}, {2, 0, 13, 2, 4, 3, -2}, 7, 0
};
struct step { uint8_t reg, mask, value; uint16_t wait; };
static int update(struct dali_mt6379_repeater *r, uint8_t reg, uint8_t mask, uint8_t value)
{ return dali_mt6379_update8(r->pmif, (uint16_t)(0xd00 + reg), mask, value); }
static int read_byte(struct dali_mt6379_repeater *r, uint16_t reg, uint8_t *value)
{ return dali_mt6379_read(r->pmif, reg, value, 1); }
static void delay(struct dali_mt6379_repeater *r, uint32_t us)
{ r->pmif->registers.delay_us(r->pmif->registers.context, us); }
static int sequence(struct dali_mt6379_repeater *r, const struct step *steps, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        int result = update(r, steps[i].reg, steps[i].mask, steps[i].value);
        if (result) return result;
        if (steps[i].wait) delay(r, steps[i].wait);
    }
    return USB_OK;
}
#define RUN(r, steps) sequence(r, steps, sizeof(steps) / sizeof((steps)[0]))
static int fault(struct dali_mt6379_repeater *r, int result)
{ r->faulted = true; r->power = DALI_REPEATER_UNKNOWN; return result; }
static bool valid_tuning(const struct dali_repeater_tuning *t)
{ return t->vrt <= 7 && t->squelch <= 15 && t->disconnect <= 15 && t->emphasis <= 7 && t->equalization <= 15; }
int dali_mt6379_repeater_probe(struct dali_mt6379_repeater *r, struct dali_usb_pmif *p,
    const struct dali_repeater_guard *g, const struct dali_mt6379_repeater_config *config, bool qualified)
{
    if (!r || !p || !g || !config || !g->quiesce || !p->initialized || !p->identified) return USB_INVALID;
    if (r->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (!valid_tuning(&config->device) || !valid_tuning(&config->host) || config->fs_slew > 7 || config->fs_hysteresis > 3) return USB_RANGE;
    struct dali_mt6379_repeater candidate = {0}; candidate.pmif = p; candidate.guard = *g; candidate.config = *config;
    uint8_t info, intr, term, lp = 0;
    int result = read_byte(&candidate, 0, &info);
    if (!result && ((info & 0xf0) != 0x70 || (info & 15) != p->revision)) result = USB_UNSUPPORTED;
    if (!result) result = read_byte(&candidate, 0xd11, &intr);
    if (!result) result = read_byte(&candidate, 0xd1a, &term);
    if (!result && (info & 15) >= 3) result = read_byte(&candidate, 0xd91, &lp);
    if (!result && (lp & 1)) {
        /* Adopt only the complete documented disconnected LP state. */
        const struct step expected[] = {{0x02,0xc0,0xc0,0},{0x27,0x0c,0x0c,0},{0x8e,1,1,0},
            {0x19,6,6,0},{0x16,8,8,0},{0x89,0x80,0x80,0},{0,1,0,0},{3,3,0,0},{0x12,0x20,0,0}};
        for (size_t i = 0; !result && i < sizeof expected / sizeof expected[0]; ++i) {
            uint8_t value;
            result = read_byte(&candidate, (uint16_t)(0xd00 + expected[i].reg), &value);
            if (!result && (value & expected[i].mask) != expected[i].value) result = USB_UNSUPPORTED;
        }
    }
    if (result) return result;
    candidate.intr = intr & 63; candidate.term = term & 15; candidate.revision = info & 15;
    candidate.low_power = (lp & 1) != 0; candidate.initialized = true; *r = candidate;
    return USB_OK;
}
static int cold_off(struct dali_mt6379_repeater *r)
{
    const struct step steps[] = {{0x8b,0x20,0x20,0},{3,1,1,0},{0,1,0,0},{3,2,0,0},{0x12,0x20,0,300}};
    return RUN(r, steps);
}
static int wake_lp(struct dali_mt6379_repeater *r)
{
    const struct step steps[] = {{0x16,8,0,0},{2,0xc0,0,0},{0x27,0x0c,4,0},{0x8e,1,0,0},
        {0x19,6,4,0},{0x89,0x80,0x80,0},{3,1,0,0},{0,1,1,30},
        {0x34,1,1,0},{3,2,2,10},{3,2,0,30},{0x34,1,0,0},{3,2,2,100},
        {0x12,0x20,0x20,0},{0x91,1,0,0},{0x89,0x80,0,300}};
    int result = RUN(r, steps);
    if (!result) r->low_power = false;
    return result;
}
static int efuse(struct dali_mt6379_repeater *r)
{
    uint8_t a, b, value, osc, fs, ls;
    int result = read_byte(r, 0x381, &a);
    if (!result) result = read_byte(r, 0x382, &b);
    if (result) return result;
    /* RSV81/82 are one-bit additive patches: zero efuse bits do not clear
     * unrelated/inherited hardware fields. The source uses mask=value. */
    const struct step patches[] = {
        {0x22, (uint8_t)((a & 0x40) >> 6), (uint8_t)((a & 0x40) >> 6),0},
        {0x22, (uint8_t)((a & 0x20) >> 2), (uint8_t)((a & 0x20) >> 2),0},
        {0x22, (uint8_t)((a & 0x10) >> 2), (uint8_t)((a & 0x10) >> 2),0},
        {0x13, (uint8_t)((a & 15) << 3), (uint8_t)((a & 15) << 3),0},
        {0x21, (uint8_t)((b & 0x10) << 3), (uint8_t)((b & 0x10) << 3),0},
        {0x22, (uint8_t)((b & 15) << 4), (uint8_t)((b & 15) << 4),0}};
    for (size_t i = 0; !result && i < sizeof patches / sizeof patches[0]; ++i)
        if (patches[i].mask) result = sequence(r, patches + i, 1);
    if (!result) result = update(r, 0x89, 0x40, 0x40); /* SW efuse select != PHY select bit7. */
    if (!result) result = read_byte(r, 0x323, &value);
    if (!result && (value & 63)) { result = update(r, 0x11, 63, value & 63); if (!result) r->intr = value & 63; }
    if (!result) result = read_byte(r, 0x324, &value);
    if (!result && (value & 15)) { result = update(r, 0x1a, 15, value & 15); if (!result) r->term = value & 15; }
    if (!result) result = read_byte(r, 0x325, &value);
    if (!result && (value & 0xf8)) result = update(r, 0x29, 31, value >> 3);
    if (!result) result = read_byte(r, 0x326, &value);
    if (!result && (value & 0xf8)) result = update(r, 0x2a, 31, value >> 3);
    if (!result) result = read_byte(r, 0x324, &value);
    if (!result && (value & 0xf0)) result = update(r, 0x28, 0xf0, value & 0xf0);
    if (!result) result = read_byte(r, 0x327, &osc);
    if (!result) result = read_byte(r, 0x325, &fs);
    if (!result) result = read_byte(r, 0x326, &ls);
    if (!result && ((osc & 127) || (fs & 7) || (ls & 7))) {
        result = update(r, 0x30, 127, osc & 127);
        if (!result) result = update(r, 0x24, 7, fs & 7);
        if (!result) result = update(r, 0x24, 0x70, (uint8_t)((ls & 7) << 4));
    }
    return result;
}
int dali_mt6379_repeater_power_on(struct dali_mt6379_repeater *r)
{
    if (!r || !r->initialized) return USB_INVALID;
    if (r->faulted) return USB_IO;
    if (r->power == DALI_REPEATER_ON) return USB_BUSY;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    /* Never inherit a device pull-up from a prior owner into STOPPED/HOST. */
    result = update(r, 0x88, 0x48, 0);
    if (!result) result = update(r, 0x17, 3, 0); /* PHYA_U2_CR1_3, NOT FS slew +0x14. */
    if (!result) result = update(r, 0x92, 7, 7);
    if (!result && r->revision >= 3) result = update(r, 0x94, 2, 2);
    if (!result && r->low_power) result = wake_lp(r);
    if (!result) result = cold_off(r);
    if (!result) result = efuse(r);
    if (!result) result = update(r, 0x18, 0xf0, 0x60);
    if (!result && r->revision <= 2) {
        result = update(r, 0x19, 4, 0);
        if (!result) result = update(r, 0xa5, 0x10, 0x10);
    }
    if (!result) result = update(r, 0x14, 7, r->config.fs_slew);
    if (!result) result = update(r, 0x27, 0xc0, (uint8_t)(r->config.fs_hysteresis << 6));
    if (!result && r->revision >= 3) {
        const struct step e3[] = {{0x20,4,4,0},{0x8a,1,1,0},{0x8a,2,2,0},{0x8a,4,0,0}};
        result = RUN(r, e3);
    }
    const struct step on[] = {{0x35,6,6,0},{0x24,0x10,0x10,0},{0x31,0xff,0x52,0},
        {0x12,0x20,0x20,0},{0x89,0x80,0x80,0},{0x8b,0x20,0,0},{0,1,1,100},
        {3,1,0,50},{0x34,1,1,0},{3,2,2,10},{3,2,0,30},{0x34,1,0,0},{3,2,2,100},{0x89,0x80,0,0}};
    if (!result) result = RUN(r, on);
    if (result) return fault(r, result);
    r->power = DALI_REPEATER_ON; r->role = USB_ROLE_STOPPED;
    return USB_OK;
}
static uint8_t adjusted(uint8_t base, int16_t offset, int mask)
{
    int value = (int)base + offset;
    return offset < -mask || offset > mask || value < 0 || value > mask ? base : (uint8_t)value;
}
int dali_mt6379_repeater_mode(struct dali_mt6379_repeater *r, enum usb_role role)
{
    if (!r || !r->initialized || (role != USB_ROLE_DEVICE && role != USB_ROLE_HOST)) return USB_INVALID;
    if (r->faulted) return USB_IO;
    if (r->power != DALI_REPEATER_ON) return USB_DISCONNECTED;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    const struct dali_repeater_tuning *t = role == USB_ROLE_HOST ? &r->config.host : &r->config.device;
    const struct step steps[] = {{0x10,0x70,(uint8_t)(t->vrt << 4),0},{0x18,15,t->squelch,0},
        {0x18,0xf0,(uint8_t)(t->disconnect << 4),0},{0x1b,0x70,(uint8_t)(t->emphasis << 4),0},
        {0x1b,15,t->equalization,0},{0x11,63,adjusted(r->intr,t->intr_offset,63),0},
        {0x1a,15,adjusted(r->term,t->term_offset,15),0}};
    /* Role changes leave the port detached until an explicit device pullup. */
    result = update(r, 0x88, 0x48, 0);
    if (!result) result = RUN(r, steps);
    if (result) return fault(r, result);
    r->role = role; return USB_OK;
}
int dali_mt6379_repeater_pullup(struct dali_mt6379_repeater *r, bool enabled)
{
    if (!r || !r->initialized) return USB_INVALID;
    if (r->faulted) return USB_IO;
    if (r->power != DALI_REPEATER_ON || (enabled && r->role != USB_ROLE_DEVICE)) return USB_DISCONNECTED;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    result = update(r, 0x88, 0x48, enabled ? 0x48 : 0);
    return result ? fault(r, result) : USB_OK;
}
int dali_mt6379_repeater_idle(struct dali_mt6379_repeater *r)
{
    if (!r || !r->initialized) return USB_INVALID;
    if (r->faulted) return USB_IO;
    if (r->revision <= 2) return USB_UNSUPPORTED;
    if (r->power != DALI_REPEATER_ON) return USB_DISCONNECTED;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    const struct step steps[] = {{0x88,0x48,0,0},{0x91,1,1,0},{0,1,0,0},{3,2,0,0},{3,1,0,0},
        {0x12,0x20,0,0},{0x89,0x80,0x80,0},{2,0xc0,0xc0,0},{0x27,0x0c,0x0c,0},
        {0x8e,1,1,0},{0x19,6,6,0},{0x16,8,8,0}};
    r->low_power = true;
    result = RUN(r, steps);
    if (result) return fault(r, result);
    r->power = DALI_REPEATER_IDLE; r->role = USB_ROLE_STOPPED; return USB_OK;
}
int dali_mt6379_repeater_power_off(struct dali_mt6379_repeater *r)
{
    if (!r || !r->initialized) return USB_INVALID;
    if (r->power == DALI_REPEATER_OFF) return USB_OK;
    int result = r->guard.quiesce(r->guard.context);
    if (result) return result;
    if (r->pmif->faulted) result = dali_usb_pmif_recover(r->pmif);
    if (!result && r->low_power) result = wake_lp(r);
    if (!result) result = update(r, 0x88, 0x48, 0);
    if (!result) result = cold_off(r);
    if (result) return fault(r, result);
    r->power = DALI_REPEATER_OFF; r->role = USB_ROLE_STOPPED; return USB_OK;
}
int dali_mt6379_repeater_destroy(struct dali_mt6379_repeater *r)
{
    int result = dali_mt6379_repeater_power_off(r);
    if (!result) *r = (struct dali_mt6379_repeater){0};
    return result;
}
