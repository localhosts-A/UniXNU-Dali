/* SPDX-License-Identifier: GPL-2.0-only */
/* MT6991 v2 register/reset and speed_v2 algorithm from MediaTek i2c-mt65xx.c,
 * MiCode device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbI2c.h"
#define IRQ_BITS 0x21fu
#define IRQ_ERRORS 0x21eu
static bool factor(uint32_t n, uint32_t *sample, uint32_t *step)
{
    for (uint32_t s = 1; s <= 8; ++s) {
        uint32_t t = n / s;
        if (t < s) break;
        if (!(n % s) && t <= 64) { *sample = s; *step = t; return true; }
    }
    return false;
}
static bool duty(uint32_t total, uint32_t h, uint32_t l, uint32_t *hs, uint32_t *ht, uint32_t *ls, uint32_t *lt)
{ return h >= 2 && l >= 2 && h * 1000 / total >= 410 && h * 1000 / total < 490 &&
    factor(h, hs, ht) && factor(l, ls, lt); }
int dali_i2c6_timing_400k(uint32_t parent, struct dali_i2c6_timing *out)
{
    if (!parent || !out) return USB_INVALID;
    for (uint32_t div = 1; div <= 32; ++div) {
        uint64_t ext = ((uint64_t)parent / div) * 5 / 4000000;
        if (ext > 255) continue;
        uint32_t total = (uint32_t)(((uint64_t)parent + div * 400000u - 1) / (div * 400000u)) + 1;
        uint32_t high = total * 45 / 100, low = total - high;
        for (uint32_t delta = 0; high + delta <= (total + 1) / 2; ++delta) {
            uint32_t hs, ht, ls, lt;
            if ((low > delta && duty(total, high + delta, low - delta, &hs, &ht, &ls, &lt)) ||
                (high > delta && duty(total, high - delta, low + delta, &hs, &ht, &ls, &lt))) {
                *out = (struct dali_i2c6_timing){((hs - 1) << 8) | (ht - 1),
                    ((ls - 1) << 6) | (lt - 1), (uint32_t)(ext << 8) | 1,
                    ((div - 1) << 8) | (div - 1)};
                return USB_OK;
            }
        }
    }
    return USB_RANGE;
}
static int read_reg(struct dali_usb_i2c6 *i, uint32_t reg, uint32_t *v)
{ return usb_mmio_read(&i->io.controller, reg, v); }
static int write_reg(struct dali_usb_i2c6 *i, uint32_t reg, uint32_t v)
{ return usb_mmio_write(&i->io.controller, reg, v); }
static int fault(struct dali_usb_i2c6 *i, int result)
{ i->faulted = true; return result; }
static int dma_idle(struct dali_usb_i2c6 *i)
{
    uint32_t value;
    int result = usb_mmio_read(&i->io.dma, 8, &value);
    if (!result && (value & 1)) result = USB_BUSY;
    if (!result) result = read_reg(i, 0x8c, &value);
    if (!result && (value & 2)) result = USB_UNSUPPORTED; /* Shared/shadow channel mode. */
    return result;
}
static int idle(struct dali_usb_i2c6 *i)
{
    int result = dma_idle(i);
    const uint32_t regs[] = {0x24, 8, 0xc, 0xf4};
    const uint32_t masks[] = {3, IRQ_BITS, IRQ_BITS, 31};
    for (size_t n = 0; !result && n < 4; ++n) {
        uint32_t value;
        result = read_reg(i, regs[n], &value);
        if (!result && (value & masks[n])) result = USB_BUSY;
    }
    return result;
}
static int reset_owned(struct dali_usb_i2c6 *i)
{
    int result = dma_idle(i); /* This driver never started DMA; never stop foreign DMA. */
    if (result) return result;
    result = write_reg(i, 8, 0);
    if (!result) result = write_reg(i, 0x24, 0);
    uint32_t status;
    if (!result) result = read_reg(i, 0xc, &status);
    if (!result) result = write_reg(i, 0xc, status & IRQ_BITS);
    const uint32_t dma_values[] = {1,0,6,0};
    for (size_t n = 0; !result && n < 4; ++n) {
        result = usb_mmio_write(&i->io.dma, 0xc, dma_values[n]);
        if (!result && n == 2) result = write_reg(i, 0x50, 0x21);
        if (!result && n < 3) i->io.controller.delay_us(i->io.controller.context, 2);
    }
    if (!result) result = write_reg(i, 0x50, 0);
    /* Reference DMA-v2 handshake sentinel, never an enabled data buffer. */
    if (!result) result = usb_mmio_write(&i->io.dma, 0x1c, 0x1000);
    const uint32_t reg[] = {0x34,0x30,0x2c,0x28,0x48,0x90,0x3c,0x20,0x10,0x1c,0x38};
    const uint32_t val[] = {3,0,i->timing.low,i->timing.ext,i->timing.divider,0,0,i->timing.high,0x28,2,1};
    for (size_t n = 0; !result && n < sizeof reg / sizeof reg[0]; ++n) result = write_reg(i, reg[n], val[n]);
    /* Reject dropped/partial reset and timing writes instead of starting a bus
     * with unknown rate. No global reset, pin bitbang or automatic retry. */
    for (size_t n = 0; !result && n < sizeof reg / sizeof reg[0] - 1; ++n) {
        uint32_t value;
        result = read_reg(i, reg[n], &value);
        if (!result && value != val[n]) result = USB_IO;
    }
    if (!result) result = idle(i);
    if (!result) i->dirty = i->pending_owned = false;
    return result;
}
int dali_usb_i2c6_init(struct dali_usb_i2c6 *i, const struct dali_i2c6_io *io,
    const struct dali_repeater_guard *guard, uint32_t parent, bool qualified)
{
    if (!i || !io || !guard || !guard->quiesce || !io->read8 || !io->write8 ||
        !io->controller.read32 || !io->controller.write32 || !io->controller.barrier || !io->controller.delay_us ||
        !io->dma.read32 || !io->dma.write32 || !io->dma.barrier) return USB_INVALID;
    if (i->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (io->controller.size < 0xfc || io->dma.size < 0x20) return USB_RANGE;
    struct dali_usb_i2c6 candidate = {.io=*io, .guard=*guard};
    int result = dali_i2c6_timing_400k(parent, &candidate.timing);
    if (!result) result = idle(&candidate);
    if (!result) result = guard->quiesce(guard->context);
    if (result) return result;
    candidate.initialized = candidate.dirty = true; *i = candidate;
    result = reset_owned(i);
    return result ? fault(i, result) : USB_OK;
}
static int transfer(struct dali_usb_i2c6 *i, uint8_t reg, uint8_t *byte, bool reading)
{
    if (!i || !byte || !i->initialized) return USB_INVALID;
    if (i->faulted) return USB_IO;
    int result = idle(i);
    if (result) return result; /* Foreign state is never adopted/acknowledged. */
    i->dirty = true;
    const uint32_t regs[] = {0x10,0x94,0xc,0x38,8,0x14,0x44,0x18};
    const uint32_t vals[] = {reading ? 0x3a : 0x28,0x9e,IRQ_BITS,1,0,reading ? 1u : 2u,reading ? 1u : 0u,reading ? 2u : 1u};
    for (size_t n = 0; !result && n < sizeof regs / sizeof regs[0]; ++n) result = write_reg(i, regs[n], vals[n]);
    if (!result) {
        i->io.controller.barrier(i->io.controller.context);
        result = i->io.write8(i->io.controller.context, 0, reg);
        if (!result && !reading) result = i->io.write8(i->io.controller.context, 0, *byte);
        i->io.controller.barrier(i->io.controller.context);
    }
    if (!result) { i->pending_owned = true; result = write_reg(i, 0x24, reading ? 1u : 0x4001u); }
    uint32_t status = 0;
    for (uint32_t elapsed = 0; !result; elapsed += 5) {
        result = read_reg(i, 0xc, &status);
        if (result) break;
        if (status & IRQ_ERRORS) { result = USB_IO; break; }
        if (status & 1) break;
        if (elapsed == 5000) { result = USB_TIMEOUT; break; }
        i->io.controller.delay_us(i->io.controller.context, 5);
    }
    if (!result) result = usb_mmio_poll(&i->io.controller, 0x24, 1, 0, 100, 2);
    uint32_t count;
    if (!result) result = read_reg(i, 0xf4, &count);
    if (!result && (count & 31) != (reading ? 1u : 0u)) result = USB_IO;
    uint8_t value = 0;
    if (!result && reading) result = i->io.read8(i->io.controller.context, 0, &value);
    if (!result) result = write_reg(i, 0xc, status & IRQ_BITS);
    if (!result) result = idle(i);
    if (result) {
        if (!reading && i->pending_owned) i->write_uncertain = true;
        return fault(i, result);
    }
    i->dirty = i->pending_owned = false;
    if (reading) *byte = value;
    return USB_OK;
}
int dali_usb_i2c6_read8(struct dali_usb_i2c6 *i, uint8_t reg, uint8_t *value)
{ return transfer(i, reg, value, true); }
int dali_usb_i2c6_write8(struct dali_usb_i2c6 *i, uint8_t reg, uint8_t value)
{ return transfer(i, reg, &value, false); }
int dali_usb_i2c6_recover(struct dali_usb_i2c6 *i)
{
    if (!i || !i->initialized) return USB_INVALID;
    int result = i->guard.quiesce(i->guard.context);
    if (!result) result = i->dirty ? reset_owned(i) : idle(i);
    return result ? fault(i, result) : USB_OK;
}
int dali_usb_i2c6_destroy(struct dali_usb_i2c6 *i)
{
    int result = dali_usb_i2c6_recover(i);
    if (!result) *i = (struct dali_usb_i2c6){0};
    return result;
}
static int nxp_read(void *c, uint8_t reg, uint8_t *v) { return dali_usb_i2c6_read8(((struct dali_nxp_io *)c)->i2c, reg, v); }
static int nxp_write(void *c, uint8_t reg, uint8_t v) { return dali_usb_i2c6_write8(((struct dali_nxp_io *)c)->i2c, reg, v); }
static int nxp_recover(void *c) { return dali_usb_i2c6_recover(((struct dali_nxp_io *)c)->i2c); }
static int nxp_enable(void *c, bool v) { return dali_usb_gpio72_set(((struct dali_nxp_io *)c)->gpio, v); }
static void nxp_delay(void *c, uint32_t us)
{ struct usb_mmio *m = &((struct dali_nxp_io *)c)->i2c->io.controller; m->delay_us(m->context, us); }
int dali_nxp_io_bind(struct dali_nxp_io *c, struct dali_usb_i2c6 *i, struct dali_usb_gpio72 *g, struct dali_nxp_repeater_bus *bus)
{
    if (!c || !i || !g || !bus || !i->initialized || !g->initialized) return USB_INVALID;
    if (i->faulted || g->faulted) return USB_IO;
    *c = (struct dali_nxp_io){i,g};
    *bus = (struct dali_nxp_repeater_bus){c,nxp_read,nxp_write,nxp_recover,nxp_enable,nxp_delay};
    return USB_OK;
}
