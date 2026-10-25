/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed MT6373 VUSB descriptor/table and Dali always-on/ramp policy from
 * MiCode device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbVusb.h"
struct snapshot { uint8_t enabled, lp, trim, selector; };
static int read_snapshot(struct dali_usb_pmif *p, struct snapshot *out)
{
    uint8_t control = 0, trim = 0, selector = 0;
    int result = dali_mt6373_vusb_read8(p, 0x1b95, &control);
    if (!result) result = dali_mt6373_vusb_read8(p, 0x1e0c, &trim);
    if (!result) result = dali_mt6373_vusb_read8(p, 0x1e0d, &selector);
    if (!result) *out = (struct snapshot){control & 1, control & 2, trim & 15, selector & 15};
    return result;
}
static int stable(struct dali_usb_pmif *p, struct snapshot *out)
{
    struct snapshot a = {0}, b = {0};
    int result = read_snapshot(p, &a);
    if (!result) result = read_snapshot(p, &b);
    if (!result && (a.enabled != b.enabled || a.lp != b.lp || a.trim != b.trim || a.selector != b.selector)) result = USB_BUSY;
    if (!result) *out = a;
    return result;
}
static bool same_config(const struct dali_usb_vusb *v, const struct snapshot *s)
{ return v->selector == s->selector && v->trim == s->trim && v->lp == s->lp; }
static int fault(struct dali_usb_vusb *v, int result)
{ if (result) { v->faulted = true; v->held = false; } return result; }
int dali_usb_vusb_init(struct dali_usb_vusb *v, struct dali_usb_pmif *p,
    const struct dali_repeater_guard *guard, bool qualified)
{
    if (!v || !p || !guard || !guard->quiesce) return USB_INVALID;
    if (v->initialized) return USB_BUSY;
    if (!qualified || !p->initialized || !p->identified || p->sid != 5) return USB_UNSUPPORTED;
    if (p->faulted || p->pending_owned) return USB_IO;
    struct snapshot s = {0};
    int result = stable(p, &s);
    if (result) return result;
    static const uint32_t table[] = {1200000,1300000,1500000,1700000,1800000,2000000,2100000,2200000,
        2700000,2800000,2900000,3000000,3100000,3300000,3400000,3500000};
    *v = (struct dali_usb_vusb){.pmif=p, .guard=*guard, .nominal_uv=table[s.selector],
        .selector=s.selector, .trim=s.trim, .lp=s.lp, .initialized=true};
    return USB_OK;
}
int dali_usb_vusb_acquire(struct dali_usb_vusb *v)
{
    if (!v || !v->initialized) return USB_INVALID;
    if (v->faulted) return USB_IO;
    int result = v->guard.quiesce(v->guard.context);
    struct snapshot s = {0};
    if (!result) result = stable(v->pmif, &s);
    if (!result && (!same_config(v, &s) || (v->held && !s.enabled))) result = USB_BUSY;
    if (result) return fault(v, result);
    if (!s.enabled) {
        v->enable_attempted = true;
        result = dali_mt6373_vusb_enable(v->pmif);
    }
    if (!result) {
        v->pmif->registers.delay_us(v->pmif->registers.context, 720);
        result = stable(v->pmif, &s);
    }
    if (!result && (!same_config(v, &s) || !s.enabled)) result = USB_IO;
    if (!result) v->held = true;
    return fault(v, result);
}
int dali_usb_vusb_release(struct dali_usb_vusb *v)
{
    if (!v || !v->initialized) return USB_INVALID;
    int result = v->guard.quiesce(v->guard.context);
    if (!result && (v->pmif->faulted || v->pmif->pending_owned)) result = dali_usb_pmif_recover(v->pmif);
    // No power-down write: vusb33 is an always-on board rail shared by policy.
    if (!result) *v = (struct dali_usb_vusb){0};
    return result ? fault(v, result) : USB_OK;
}
