/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed MT6991 ordinary setclr gates, clk-mt6991-peri.c / clk-gate.c. */
#include "DaliUsbI2cClocks.h"
static const uint32_t status_reg[2] = {0xe00, 0x14};
static const uint32_t enable_reg[2] = {0xe04, 0x30};
static const uint32_t disable_reg[2] = {0xe08, 0x2c};
static const uint32_t masks[2] = {4, 1u << 26};
static const struct usb_mmio *bank(struct dali_i2c_clocks *c, unsigned n)
{ return n ? &c->perao : &c->impw; }
static int snapshot(struct dali_i2c_clocks *c, uint8_t *out)
{
    uint8_t value[2] = {0};
    for (unsigned pass = 0; pass < 2; ++pass) for (unsigned n = 0; n < 2; ++n) {
        uint32_t word;
        int result = usb_mmio_read(bank(c,n), status_reg[n], &word);
        if (result) return result;
        if (word & masks[n]) value[pass] |= (uint8_t)(1u << n);
    }
    if (value[0] != value[1]) return USB_BUSY;
    *out = value[0]; return USB_OK;
}
static int fault(struct dali_i2c_clocks *c, int result)
{ c->faulted = true; c->on = false; return result; }
int dali_i2c_clocks_init(struct dali_i2c_clocks *c, const struct usb_mmio *impw,
    const struct usb_mmio *perao, const struct dali_repeater_guard *guard, bool qualified)
{
    if (!c || !impw || !perao || !guard || !guard->quiesce || !impw->read32 || !perao->read32 ||
        !impw->write32 || !perao->write32 || !impw->barrier || !perao->barrier ||
        !impw->delay_us || !perao->delay_us) return USB_INVALID;
    if (c->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (impw->size < 0xe0c || perao->size < 0x34) return USB_RANGE;
    struct dali_i2c_clocks value = {.impw=*impw,.perao=*perao,.guard=*guard};
    int result = guard->quiesce(guard->context);
    if (!result) result = snapshot(&value,&value.initially_gated);
    if (result) return result;
    value.initialized = true; *c = value; return USB_OK;
}
int dali_i2c_clocks_enable(struct dali_i2c_clocks *c)
{
    if (!c || !c->initialized) return USB_INVALID;
    if (c->faulted) return USB_IO;
    int result = c->guard.quiesce(c->guard.context);
    uint8_t gates = 0;
    if (!result) result = snapshot(c,&gates);
    if (!result && gates != (c->on ? 0 : c->initially_gated)) result = USB_BUSY;
    for (unsigned n = 0; !result && n < 2; ++n) if (gates & (1u << n)) {
        c->owned |= (uint8_t)(1u << n);
        result = usb_mmio_write(bank(c,n),enable_reg[n],masks[n]);
        if (!result) result = usb_mmio_poll(bank(c,n),status_reg[n],masks[n],0,100,1);
    }
    if (!result) result = snapshot(c,&gates);
    if (!result && gates) result = USB_IO;
    if (result) return fault(c,result);
    c->on = true; return USB_OK;
}
int dali_i2c_clocks_disable(struct dali_i2c_clocks *c)
{
    if (!c || !c->initialized) return USB_INVALID;
    int result = c->guard.quiesce(c->guard.context);
    uint8_t gates = 0;
    if (!result) result = snapshot(c,&gates);
    /* Unowned gates must still match their inherited state. An owned write may
     * have completed before an error, so its current bit alone is not foreign. */
    if (!result && ((gates ^ c->initially_gated) & (uint8_t)~c->owned)) result = USB_BUSY;
    for (unsigned remaining = 2; !result && remaining; --remaining) {
        unsigned n = remaining - 1;
        if (!(c->owned & (1u << n))) continue;
        if (!(gates & (1u << n))) {
            result = usb_mmio_write(bank(c,n),disable_reg[n],masks[n]);
            if (!result) result = usb_mmio_poll(bank(c,n),status_reg[n],masks[n],masks[n],100,1);
        }
        if (!result) c->owned &= (uint8_t)~(1u << n);
    }
    if (!result) result = snapshot(c,&gates);
    if (!result && gates != c->initially_gated) result = USB_IO;
    if (result) return fault(c,result);
    c->on = false; return USB_OK;
}
int dali_i2c_clocks_destroy(struct dali_i2c_clocks *c)
{
    int result = dali_i2c_clocks_disable(c);
    if (!result) *c = (struct dali_i2c_clocks){0};
    return result;
}
