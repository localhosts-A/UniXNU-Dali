/* SPDX-License-Identifier: GPL-2.0-only */
/* Ordinary SCPSYS/NON_CPU_RTFF and MT6991 USB domain tables from MediaTek,
 * fixed device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbDomains.h"
static const uint32_t control[] = {0xe1c,0xe18};
static const uint32_t protect[] = {0x80,0x40};
static int update(struct dali_usb_domains *d, unsigned n, uint32_t mask, uint32_t value)
{
    uint32_t old, check;
    int r = usb_mmio_read(&d->controller,control[n],&old);
    if (!r) r = usb_mmio_write(&d->controller,control[n],(old & ~mask) | value);
    if (!r) r = usb_mmio_read(&d->controller,control[n],&check);
    return !r && (check & mask) != value ? USB_IO : r;
}
static int poll(struct dali_usb_domains *d, unsigned n, uint32_t mask, uint32_t value)
{ return usb_mmio_poll(&d->controller,control[n],mask,value,1000000,10); }
static int fault(struct dali_usb_domains *d, int result) { d->faulted = true; return result; }
static int snapshot(struct dali_usb_domains *d, unsigned n, enum dali_domain_power *out)
{
    uint32_t v, en, sta;
    int r = usb_mmio_read(&d->controller,control[n],&v);
    if (!r) r = usb_mmio_read(&d->protection,0xd8,&en);
    if (!r) r = usb_mmio_read(&d->protection,0x208,&sta);
    if (r) return r;
    if (v & 0x5000000) return USB_BUSY; /* Incomplete RTFF save/clock pulse. */
    uint32_t mask = 0xc000001f | (n == 0 ? 0x1100u : 0u);
    if ((v & mask) == 0xc000000d && !(v & 0x8000000) && !(en & protect[n])) *out = DALI_DOMAIN_ON;
    else if ((v & mask) == (0x12u | (n == 0 ? 0x1100u : 0u)) &&
        (en & protect[n]) && (sta & protect[n])) *out = DALI_DOMAIN_OFF;
    else return USB_BUSY;
    return USB_OK;
}
static int preflight(struct dali_usb_domains *d)
{
    for (unsigned n = 0; n < 2; ++n) {
        if (d->domain[n].phase != DALI_DOMAIN_IDLE) continue; /* Only our retained transition. */
        enum dali_domain_power actual;
        int r = snapshot(d,n,&actual);
        if (r) return r;
        if (actual != d->domain[n].power) return USB_BUSY;
    }
    return USB_OK;
}
static int bus_set(struct dali_usb_domains *d, unsigned n)
{
    for (unsigned retry = 0; retry <= 10; ++retry) {
        uint32_t v;
        int r = usb_mmio_write(&d->protection,0xdc,protect[n]);
        if (!r) r = usb_mmio_read(&d->protection,0xd8,&v);
        if (r) return r;
        if (v & protect[n]) return USB_OK;
    }
    return USB_IO; /* Unlike source, don't accept stale ACK after all SETs failed. */
}
static int start(struct dali_usb_domains *d, unsigned n, bool expose_bus)
{
    struct dali_domain_step *s = &d->domain[n];
    if (s->phase == DALI_DOMAIN_IDLE) {
        if (s->power == DALI_DOMAIN_ON) return USB_OK;
        s->phase = DALI_DOMAIN_STARTING; s->cursor = 0; s->power = DALI_DOMAIN_UNKNOWN;
    }
    if (s->phase != DALI_DOMAIN_STARTING) return USB_BUSY;
    while (s->cursor < 18) {
        int r = USB_OK;
        switch (s->cursor) {
        case 0: r=update(d,n,4,4); break;
        case 1: r=poll(d,n,0x40000000,0x40000000); break;
        case 2: d->controller.delay_us(d->controller.context,50); break;
        case 3: r=update(d,n,8,8); break;
        case 4: r=poll(d,n,0x80000000,0x80000000); break;
        case 5: r=update(d,n,0x10,0); break;
        case 6: r=update(d,n,2,0); break;
        case 7: r=update(d,n,1,1); break;
        case 8: {
            uint32_t v; r=usb_mmio_read(&d->controller,control[n],&v);
            if (!r) s->restore = (v & 0x8000000) != 0;
            break;
        }
        case 9: if (s->restore) r=update(d,n,0x8000000,0); break;
        case 10: if (s->restore) r=update(d,n,0x4000000,0x4000000); break;
        case 11: if (s->restore) r=update(d,n,0x2000000,0); break;
        case 12: if (s->restore) r=update(d,n,0x2000000,0x2000000); break;
        case 13: if (s->restore) r=update(d,n,0x4000000,0); break;
        case 14: if (!n) r=update(d,n,0x100,0); break;
        case 15: if (!n) r=poll(d,n,0x1000,0); break;
        case 16: if (expose_bus) r=usb_mmio_write(&d->protection,0xe0,protect[n]); break;
        case 17: if (expose_bus) r=usb_mmio_poll(&d->protection,0xd8,protect[n],0,1000000,10); break;
        }
        if (r) return r;
        ++s->cursor;
    }
    s->phase=DALI_DOMAIN_IDLE; s->power=DALI_DOMAIN_ON; s->cursor=0;
    return USB_OK;
}
static int stop(struct dali_usb_domains *d, unsigned n)
{
    struct dali_domain_step *s = &d->domain[n];
    if (s->phase == DALI_DOMAIN_STARTING) {
        /* Complete only our retained startup, with no new unprotect request. */
        int r=start(d,n,false); if (r) return r;
    }
    if (s->phase == DALI_DOMAIN_IDLE) {
        if (s->power == DALI_DOMAIN_OFF) return USB_OK;
        s->phase=DALI_DOMAIN_STOPPING; s->cursor=0; s->power=DALI_DOMAIN_UNKNOWN;
    }
    if (s->phase != DALI_DOMAIN_STOPPING) return USB_BUSY;
    while (s->cursor < 16) {
        int r=USB_OK;
        switch (s->cursor) {
        case 0: r=bus_set(d,n); break;
        case 1: r=usb_mmio_poll(&d->protection,0x208,protect[n],protect[n],1000000,10); break;
        case 2: if (!n) r=update(d,n,0x100,0x100); break;
        case 3: if (!n) r=poll(d,n,0x1000,0x1000); break;
        case 4: r=update(d,n,0x4000000,0x4000000); break;
        case 5: r=update(d,n,0x1000000,0x1000000); break;
        case 6: r=update(d,n,0x1000000,0); break;
        case 7: r=update(d,n,0x4000000,0); break;
        case 8: r=update(d,n,0x8000000,0x8000000); break;
        case 9: r=update(d,n,2,2); break;
        case 10: r=update(d,n,1,0); break;
        case 11: r=update(d,n,0x10,0x10); break;
        case 12: r=update(d,n,4,0); break;
        case 13: r=poll(d,n,0x40000000,0); break;
        case 14: r=update(d,n,8,0); break;
        case 15: r=poll(d,n,0x80000000,0); break;
        }
        if (r) return r;
        ++s->cursor;
    }
    s->phase=DALI_DOMAIN_IDLE; s->power=DALI_DOMAIN_OFF; s->cursor=0;
    return USB_OK;
}
int dali_usb_domains_init(struct dali_usb_domains *d, const struct usb_mmio *c,
    const struct usb_mmio *p, const struct dali_domain_guard *guard, bool qualified)
{
    if (!d || !c || !p || !guard || !guard->quiesce || !c->read32 || !c->write32 || !c->barrier || !c->delay_us ||
        !p->read32 || !p->write32 || !p->barrier || !p->delay_us) return USB_INVALID;
    if (d->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (c->size < 0xe20 || p->size < 0x20c) return USB_RANGE;
    struct dali_usb_domains candidate={.controller=*c,.protection=*p,.guard=*guard};
    for (unsigned n=0; n<2; ++n) {
        int r=snapshot(&candidate,n,&candidate.domain[n].power); if (r) return r;
    }
    if (candidate.domain[1].power == DALI_DOMAIN_ON && candidate.domain[0].power != DALI_DOMAIN_ON) return USB_BUSY;
    candidate.initialized=true; *d=candidate; return USB_OK;
}
int dali_usb_domains_power_on(struct dali_usb_domains *d)
{
    if (!d || !d->initialized) return USB_INVALID;
    if (d->faulted) return USB_IO;
    int r=d->guard.quiesce(d->guard.context); if (r) return r;
    r=preflight(d);
    for (unsigned n=0; !r && n<2; ++n) r=start(d,n,true);
    return r ? fault(d,r) : USB_OK;
}
int dali_usb_domains_power_off(struct dali_usb_domains *d)
{
    if (!d || !d->initialized) return USB_INVALID;
    int r=d->guard.quiesce(d->guard.context); if (r) return r;
    r=preflight(d);
    for (unsigned n=2; !r && n>0; --n) r=stop(d,n-1);
    return r ? fault(d,r) : USB_OK;
}
int dali_usb_domains_destroy(struct dali_usb_domains *d)
{
    int r=dali_usb_domains_power_off(d);
    if (!r) *d=(struct dali_usb_domains){0};
    return r;
}
