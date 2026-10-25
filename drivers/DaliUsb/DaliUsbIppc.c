/* SPDX-License-Identifier: GPL-2.0-only */
/* MT6991 USB0 IPPC sequences from MediaTek mtu3_{plat,dr,core,host}.c,
 * fixed device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbIppc.h"
static bool valid(const struct usb_mmio *m)
{ return m && m->read32 && m->write32 && m->barrier && m->delay_us; }
static int update(const struct usb_mmio *m, uint32_t reg, uint32_t mask, uint32_t value)
{
    uint32_t v, check;
    int r=usb_mmio_read(m,reg,&v);
    if (!r) r=usb_mmio_write(m,reg,(v & ~mask) | value);
    if (!r) r=usb_mmio_read(m,reg,&check);
    return !r && (check & mask) != value ? USB_IO : r;
}
static int fault(struct dali_usb_ippc *p, int r)
{ p->faulted=true; p->stopped=false; p->role=USB_ROLE_STOPPED; return r; }
static int idle(struct dali_usb_ippc *p)
{
    /* Three bounded 1s core waits implement source MBIST's 3s deadline. */
    for (unsigned second=0;second<3;++second) {
        int r=usb_mmio_poll(&p->mbist,0x34,1,1,1000000,100000);
        if (r != USB_TIMEOUT) return r;
    }
    return USB_TIMEOUT;
}
static int deassert_owned(struct dali_usb_ippc *p)
{
    int r=USB_OK;
    if (p->ao_reset_owned) {
        /* The assertion may have applied even when its write returned error. */
        p->ao.delay_us(p->ao.context,1000);
        r=update(&p->ao,0,0x13,0);
        if (!r) p->ao_reset_owned=false;
    }
    if (!r && p->ip_reset_owned) {
        p->registers.delay_us(p->registers.context,1);
        r=update(&p->registers,0,1,0);
        if (!r) p->ip_reset_owned=false;
    }
    return r;
}
static int off_snapshot(struct dali_usb_ippc *p)
{
    const struct usb_mmio *m=&p->registers;
    uint32_t v;
    int r=usb_mmio_read(&p->ao,0,&v);
    if (!r && (v & 0x13)) r=USB_BUSY;
    if (!r) r=usb_mmio_read(m,0,&v);
    if (!r && (v & 1)) r=USB_BUSY;
    for (unsigned n=0;!r && n<2;++n) {
        r=usb_mmio_read(m,4+4*n,&v);
        if (!r && !(v & 1)) r=USB_BUSY;
    }
    for (unsigned n=0;!r && n<p->u3_ports;++n) {
        r=usb_mmio_read(m,0x30+8*n,&v);
        if (!r && (v & 0x83) != 3) r=USB_BUSY;
    }
    for (unsigned n=0;!r && n<p->u2_ports;++n) {
        r=usb_mmio_read(m,0x50+8*n,&v);
        if (!r && (v & 0x83) != 3) r=USB_BUSY;
    }
    return r;
}
static int wait_sleep(struct dali_usb_ippc *p)
{
    int r=usb_mmio_poll(&p->registers,0x10,0x40000000,0x40000000,100000,100);
    if (!r) p->registers.delay_us(p->registers.context,100);
    return r;
}
static int power_down(struct dali_usb_ippc *p)
{
    int r=USB_OK;
    for (unsigned n=0;!r && n<p->u3_ports;++n) {
        r=update(&p->registers,0x30+8*n,3,3);
        if (!r) r=update(&p->registers,0x30+8*n,0x80,0);
    }
    for (unsigned n=0;!r && n<p->u2_ports;++n) {
        r=update(&p->registers,0x50+8*n,3,3);
        if (!r) r=update(&p->registers,0x50+8*n,0x80,0);
    }
    if (!r) r=update(&p->registers,8,1,1); /* Device first, including inherited BROM state. */
    if (!r) r=update(&p->registers,4,1,1);
    if (!r) r=wait_sleep(p);
    if (!r) { p->stopped=true; p->role=USB_ROLE_STOPPED; }
    return r;
}
int dali_usb_ippc_init(struct dali_usb_ippc *p, const struct usb_mmio *ippc,
    const struct usb_mmio *ao, const struct usb_mmio *mbist,
    const struct dali_domain_guard *guard, bool qualified)
{
    if (!p || !valid(ippc) || !valid(ao) || !valid(mbist) || !guard || !guard->quiesce) return USB_INVALID;
    if (p->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (ippc->size < 0xa0 || ao->size < 4 || mbist->size < 0x38) return USB_RANGE;
    uint32_t v, caps;
    int r=usb_mmio_read(ao,0,&v);
    if (!r && (v & 0x13)) r=USB_BUSY;
    if (!r) r=usb_mmio_read(ippc,0,&v);
    if (!r && (v & 1)) r=USB_BUSY;
    if (!r) r=usb_mmio_read(ippc,0x24,&caps);
    if (r) return r;
    unsigned u2=(caps>>8) & 255, u3=caps & 255;
    if (!u2 || u2>2 || u3>4) return USB_UNSUPPORTED;
    *p=(struct dali_usb_ippc){.registers=*ippc,.ao=*ao,.mbist=*mbist,.guard=*guard,
        .u2_ports=(uint8_t)u2,.u3_ports=(uint8_t)u3,.initialized=true};
    return USB_OK;
}
int dali_usb_ippc_ao_reset(struct dali_usb_ippc *p)
{
    if (!p || !p->initialized) return USB_INVALID;
    if (p->faulted) return USB_IO;
    if (p->role != USB_ROLE_STOPPED || p->reset_done) return USB_BUSY;
    int r=p->guard.quiesce(p->guard.context); if (r) return r;
    r=idle(p);
    if (!r) {
        p->stopped=false; p->ao_reset_owned=true;
        r=update(&p->ao,0,0x13,0x13);
    }
    if (!r) r=deassert_owned(p);
    return r ? fault(p,r) : USB_OK;
}
int dali_usb_ippc_reset(struct dali_usb_ippc *p)
{
    if (!p || !p->initialized) return USB_INVALID;
    if (p->faulted) return USB_IO;
    if (p->role != USB_ROLE_STOPPED) return USB_BUSY;
    int r=p->guard.quiesce(p->guard.context); if (r) return r;
    r=idle(p);
    if (!r) {
        p->stopped=false; p->reset_done=false; p->ip_reset_owned=true;
        r=update(&p->registers,0,1,1);
    }
    if (!r) r=deassert_owned(p);
    if (!r) r=power_down(p);
    if (!r) p->reset_done=true;
    return r ? fault(p,r) : USB_OK;
}
int dali_usb_ippc_enable(struct dali_usb_ippc *p, enum usb_role role)
{
    if (!p || !p->initialized || (role != USB_ROLE_DEVICE && role != USB_ROLE_HOST)) return USB_INVALID;
    if (p->faulted) return USB_IO;
    if (!p->stopped || !p->reset_done) return USB_BUSY;
    int r=p->guard.quiesce(p->guard.context); if (r) return r;
    r=off_snapshot(p);
    if (!r) r=idle(p);
    if (!r) {
        p->stopped=false;
        uint32_t width=p->u2_ports == 2 ? 0x18u : 8u;
        r=update(&p->registers,0x9c,width,width);
    }
    if (!r) r=update(&p->registers,role == USB_ROLE_HOST ? 4u : 8u,1,0);
    /* off_snapshot proves PDN|DIS before changing HOST_SEL/OTG_SEL. */
    if (!r) r=update(&p->registers,0x50,0x287,role == USB_ROLE_HOST ? 0x204u : 0u);
    /* Device reference enables the port first, then sets OTG_SEL separately. */
    if (!r && role == USB_ROLE_DEVICE) r=update(&p->registers,0x50,0x80,0x80);
    uint32_t stable=0x501u | (role == USB_ROLE_HOST ? 0x800u : 0u);
    if (!r) r=usb_mmio_poll(&p->registers,0x10,stable,stable,20000,100);
    if (!r) r=usb_mmio_poll(&p->registers,0x14,1,1,10000,100);
    if (!r) p->role=role;
    return r ? fault(p,r) : USB_OK;
}
int dali_usb_ippc_stop(struct dali_usb_ippc *p)
{
    if (!p || !p->initialized) return USB_INVALID;
    int r=p->guard.quiesce(p->guard.context); if (r) return r;
    r=deassert_owned(p);
    if (!r) r=idle(p);
    if (!r && p->stopped) { r=off_snapshot(p); if (!r) r=wait_sleep(p); }
    else if (!r) r=power_down(p);
    return r ? fault(p,r) : USB_OK;
}
int dali_usb_ippc_destroy(struct dali_usb_ippc *p)
{ int r=dali_usb_ippc_stop(p); if (!r) *p=(struct dali_usb_ippc){0}; return r; }
