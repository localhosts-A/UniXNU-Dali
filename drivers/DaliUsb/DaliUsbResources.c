/* SPDX-License-Identifier: GPL-2.0-only */
/* MT6991 mux gate/fence and MTU3 HWRSC v2 sequences from MediaTek's fixed
 * device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbResources.h"
static bool valid(const struct usb_mmio *m)
{ return m && m->read32 && m->write32 && m->barrier && m->delay_us; }
static int change(const struct usb_mmio *m, uint32_t reg, uint32_t mask, uint32_t value)
{
    uint32_t old, check;
    int r=usb_mmio_read(m,reg,&old);
    if (!r) r=usb_mmio_write(m,reg,(old & ~mask) | value);
    if (!r) r=usb_mmio_read(m,reg,&check);
    return !r && (check & mask) != value ? USB_IO : r;
}
static const uint32_t cfg[]={0x90,0x90,0xb0};
static const uint32_t gate[]={0x8000,0x800000,0x80000000};
static const uint32_t parent_mask[]={0x100,0x10000,0x1000000};
static const uint32_t fence_reg[]={0x3a0,0x3a0,0x274};
static const uint32_t fence_bit[]={0x40000000,0x20000000,0x100000};
static const struct usb_mmio *clock_mmio(struct dali_usb_clocks *c, unsigned n)
{ return n == 2 ? &c->top : &c->vlp; }
static int clock_fence(const struct usb_mmio *m, unsigned n)
{
    /* Shared MMIO polls cap a single wait at 1s; the source mux permits 5s. */
    for (unsigned second=0;second<5;++second) {
        int r=usb_mmio_poll(m,fence_reg[n],fence_bit[n],fence_bit[n],1000000,1);
        if (r != USB_TIMEOUT) return r;
    }
    return USB_TIMEOUT;
}
static int clock_snapshot(struct dali_usb_clocks *c, unsigned n, uint32_t *v)
{
    int r=usb_mmio_read(clock_mmio(c,n),cfg[n],v);
    if (!r && (*v & parent_mask[n]) != c->parent[n]) r=USB_BUSY;
    return r;
}
static int clock_preflight(struct dali_usb_clocks *c)
{
    for (unsigned n=0;n<3;++n) {
        uint32_t v;
        int r=clock_snapshot(c,n,&v); if (r) return r;
        if (!(c->owned & (1u<<n)) && ((v & gate[n]) != 0) != ((c->initially_gated & (1u<<n)) != 0)) return USB_BUSY;
    }
    return USB_OK;
}
int dali_usb_clocks_init(struct dali_usb_clocks *c, const struct usb_mmio *vlp,
    const struct usb_mmio *top, const struct dali_domain_guard *guard, bool qualified)
{
    if (!c || !valid(vlp) || !valid(top) || !guard || !guard->quiesce) return USB_INVALID;
    if (c->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (vlp->size < 0x3a4 || top->size < 0x278) return USB_RANGE;
    struct dali_usb_clocks candidate={.vlp=*vlp,.top=*top,.guard=*guard};
    for (unsigned n=0;n<3;++n) {
        uint32_t v; int r=usb_mmio_read(clock_mmio(&candidate,n),cfg[n],&v); if (r) return r;
        candidate.parent[n]=v & parent_mask[n];
        if (v & gate[n]) candidate.initially_gated |= (uint8_t)(1u<<n);
    }
    candidate.initialized=true; *c=candidate; return USB_OK;
}
int dali_usb_clocks_enable(struct dali_usb_clocks *c)
{
    if (!c || !c->initialized) return USB_INVALID;
    if (c->faulted) return USB_IO;
    int r=c->guard.quiesce(c->guard.context); if (r) return r;
    r=clock_preflight(c);
    for (unsigned n=0;!r && n<3;++n) {
        const struct usb_mmio *m=clock_mmio(c,n);
        if ((c->initially_gated & (1u<<n)) && !(c->owned & (1u<<n))) {
            c->owned |= (uint8_t)(1u<<n); /* Before potentially applied-but-failed write. */
            r=usb_mmio_write(m,cfg[n]+8,gate[n]);
        }
        uint32_t v;
        if (!r) r=clock_snapshot(c,n,&v);
        if (!r && (v & gate[n])) r=USB_IO;
        if (!r) r=clock_fence(m,n);
    }
    if (r) c->faulted=true;
    else c->on=true;
    return r;
}
int dali_usb_clocks_disable(struct dali_usb_clocks *c)
{
    if (!c || !c->initialized) return USB_INVALID;
    int r=c->guard.quiesce(c->guard.context); if (r) return r;
    r=clock_preflight(c);
    for (unsigned i=3;!r && i>0;--i) {
        unsigned n=i-1; uint8_t bit=(uint8_t)(1u<<n);
        if (!(c->owned & bit)) continue;
        r=usb_mmio_write(clock_mmio(c,n),cfg[n]+4,gate[n]);
        uint32_t v;
        if (!r) r=clock_snapshot(c,n,&v);
        if (!r && !(v & gate[n])) r=USB_IO;
        if (!r) c->owned &= (uint8_t)~bit;
    }
    if (r) c->faulted=true;
    else c->on=false;
    return r;
}
int dali_usb_clocks_destroy(struct dali_usb_clocks *c)
{ int r=dali_usb_clocks_disable(c); if (!r) *c=(struct dali_usb_clocks){0}; return r; }

#define REQUEST 0x5fu
#define FORCE 0x7f00u
static int request_snapshot(struct dali_usb_hwrsc *h, uint32_t *v)
{ return usb_mmio_read(&h->ippc,0xe0,v); }
static int request_preflight(struct dali_usb_hwrsc *h)
{
    uint32_t v; int r=request_snapshot(h,&v); if (r) return r;
    if (!h->request_owned && (v & (REQUEST|FORCE)) != h->initial_request) return USB_BUSY;
    for (unsigned n=0;n<8;++n) {
        r=usb_mmio_read(&h->ao,0x70+4*n,&v); if (r) return r;
        uint32_t field=v & 15u;
        if (field != h->gate_initial[n] && (!(h->gates_owned & (1u<<n)) || field != 15)) return USB_BUSY;
    }
    return USB_OK;
}
static int request_set(struct dali_usb_hwrsc *h, bool on)
{
    h->request_owned=true;
    int r=change(&h->ippc,0xe0,REQUEST|FORCE,on ? REQUEST : 0);
    if (!r) r=usb_mmio_poll(&h->ippc,0xe8,REQUEST,on ? REQUEST : 0,20000,100);
    if (!r) h->ippc.delay_us(h->ippc.context,2000);
    return r;
}
int dali_usb_hwrsc_init(struct dali_usb_hwrsc *h, const struct usb_mmio *ippc,
    const struct usb_mmio *ao, const struct dali_domain_guard *guard,
    enum dali_usb_resource_provider provider, bool qualified)
{
    if (!h || !valid(ippc) || !valid(ao) || !guard || !guard->quiesce) return USB_INVALID;
    if (h->initialized) return USB_BUSY;
    if (!qualified || provider != DALI_USB_RESOURCE_NATIVE_HWRSC_V2) return USB_UNSUPPORTED;
    if (ippc->size < 0xec || ao->size < 0x90) return USB_RANGE;
    struct dali_usb_hwrsc candidate={.ippc=*ippc,.ao=*ao,.guard=*guard};
    uint32_t ctrl, ack;
    int r=request_snapshot(&candidate,&ctrl);
    if (!r) r=usb_mmio_read(ippc,0xe8,&ack);
    if (r) return r;
    uint32_t req=ctrl & REQUEST;
    if ((req != 0 && req != REQUEST) || (ack & REQUEST) != req) return USB_BUSY;
    candidate.initial_request=ctrl & (REQUEST|FORCE);
    candidate.request_owned=candidate.initial_request != 0;
    candidate.on=req == REQUEST && !(ctrl & FORCE);
    for (unsigned n=0;n<8;++n) {
        uint32_t v; r=usb_mmio_read(ao,0x70+4*n,&v); if (r) return r;
        candidate.gate_initial[n]=(uint8_t)(v & 15);
    }
    candidate.initialized=true; *h=candidate; return USB_OK;
}
int dali_usb_hwrsc_enable(struct dali_usb_hwrsc *h)
{
    if (!h || !h->initialized) return USB_INVALID;
    if (h->faulted) return USB_IO;
    int r=h->guard.quiesce(h->guard.context); if (r) return r;
    r=request_preflight(h);
    for (unsigned n=0;!r && n<8;++n) {
        if (h->gate_initial[n] == 15 || (h->gates_owned & (1u<<n))) continue;
        h->gates_owned |= (uint8_t)(1u<<n);
        r=change(&h->ao,0x70+4*n,15,15);
    }
    if (!r) r=request_set(h,true);
    if (r) h->faulted=true;
    else h->on=true;
    return r;
}
int dali_usb_hwrsc_disable(struct dali_usb_hwrsc *h)
{
    if (!h || !h->initialized) return USB_INVALID;
    int r=h->guard.quiesce(h->guard.context); if (r) return r;
    r=request_preflight(h);
    if (!r && h->request_owned) {
        r=request_set(h,false);
        if (!r) { h->request_owned=false; h->initial_request=0; }
    }
    for (unsigned i=8;!r && i>0;--i) {
        unsigned n=i-1; uint8_t bit=(uint8_t)(1u<<n);
        if (!(h->gates_owned & bit)) continue;
        r=change(&h->ao,0x70+4*n,15,h->gate_initial[n]);
        if (!r) h->gates_owned &= (uint8_t)~bit;
    }
    if (r) h->faulted=true;
    else h->on=false;
    return r;
}
int dali_usb_hwrsc_destroy(struct dali_usb_hwrsc *h)
{ int r=dali_usb_hwrsc_disable(h); if (!r) *h=(struct dali_usb_hwrsc){0}; return r; }
