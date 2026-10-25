/* Actual IPPC production C; modeled registers/pulse timing/idle only. */
#include "DaliUsbIppc.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model;
struct window { struct model *model; unsigned id; };
struct model {
    struct dali_usb_ippc ippc;
    struct window window[3];
    uint32_t regs[64], ao, blocked1, blocked2;
    unsigned u2, u3, operations, fail_operation, writes, guards;
    uint64_t time, ao_assert_at, ip_assert_at, idle_at, status_at, read_fail_at;
    bool applied_error, quiesce_fail, busy, ignore_write;
};
static bool all_ports_off(struct model *m)
{
    for (unsigned n=0;n<m->u3;++n) if ((m->regs[(0x30+8*n)/4] & 3) != 3) return false;
    for (unsigned n=0;n<m->u2;++n) if ((m->regs[(0x50+8*n)/4] & 3) != 3) return false;
    return true;
}
static bool idle_now(struct model *m) { return !m->busy && m->time >= m->idle_at; }
static int operation(struct model *m) { return ++m->operations == m->fail_operation ? USB_IO : USB_OK; }
static int read32(void *context, uint32_t reg, uint32_t *v)
{
    struct window *w=context; struct model *m=w->model;
    int r=operation(m); if (r) return r;
    if (m->read_fail_at && m->time >= m->read_fail_at) { m->read_fail_at=0; return USB_IO; }
    if (w->id == 1) { assert(!reg); *v=m->ao; }
    else if (w->id == 2) { assert(reg == 0x34); *v=0xaaff0080 | (uint32_t)idle_now(m); }
    else {
        assert(!(m->ao & 0x13)); assert(reg < 0xa0 && !(reg & 3));
        if (reg == 0x10 || reg == 0x14) {
            bool asleep=all_ports_off(m) && (m->regs[1] & 1) && (m->regs[2] & 1);
            bool active=(m->regs[0x50/4] & 3) == 0 && (!(m->regs[1] & 1) || !(m->regs[2] & 1));
            if (reg == 0x10) *v=0x12000 | (asleep ? 0x40000000u : 0u) |
                (active && m->time >= m->status_at ? 0x501u | (!(m->regs[1] & 1) ? 0x800u : 0u) : 0u);
            else *v=0xa5a50000 | (uint32_t)(active && m->time >= m->status_at);
            *v &= ~(reg == 0x10 ? m->blocked1 : m->blocked2);
        } else *v=m->regs[reg/4];
    }
    return USB_OK;
}
static int write32(void *context, uint32_t reg, uint32_t v)
{
    struct window *w=context; struct model *m=w->model; ++m->writes;
    int r=operation(m); if ((r && !m->applied_error) || m->ignore_write) return r;
    assert(!m->quiesce_fail && m->guards && idle_now(m));
    if (w->id == 1) {
        assert(!reg && (v & ~0x13u) == (m->ao & ~0x13u));
        if (!(m->ao & 0x13) && (v & 0x13)) m->ao_assert_at=m->time;
        if ((m->ao & 0x13) && !(v & 0x13)) assert(m->time-m->ao_assert_at >= 1000);
        m->ao=v;
    } else {
        assert(!w->id && !(m->ao & 0x13) && !(reg & 3)); uint32_t old=m->regs[reg/4];
        uint32_t allowed=0;
        if (!reg || reg == 4 || reg == 8) allowed=1;
        else if (reg == 0x9c) { allowed=m->u2 == 2 ? 0x18u : 8u; assert(all_ports_off(m) && (m->regs[1] & 1) && (m->regs[2] & 1)); }
        else {
            bool u2=reg >= 0x50; unsigned n=(reg-(u2 ? 0x50u : 0x30u))/8;
            assert(reg == (u2 ? 0x50u : 0x30u)+8*n && n<(u2 ? m->u2 : m->u3));
            allowed=u2 && !n ? 0x287u : 0x83u;
            if (u2 && !n && ((old ^ v) & 0x204)) assert((old & 3) == 3); /* HOST_SEL only while off. */
            if ((old ^ v) & 0x80) {
                if (u2 && !n && (v & 0x80)) { /* Device: OTG after enabling, per source. */
                    assert(!(old & 0x287) && !(m->regs[2] & 1) && (m->regs[1] & 1));
                } else assert((old & 3) == 3);
            }
            if ((v & 3) != 3) {
                assert(u2 && !n); /* No USB3/secondary port activation. */
                if ((old & 3) != 3) assert(!(old & 0x287) && (v & 0x287) == 0x80);
                else assert(!(v & 0x287) || (v & 0x287) == 0x204);
            }
        }
        assert(allowed && (v & ~allowed) == (old & ~allowed));
        if (!reg) {
            if (!(old & 1) && (v & 1)) m->ip_assert_at=m->time;
            if ((old & 1) && !(v & 1)) assert(m->time-m->ip_assert_at >= 1);
        }
        if ((reg == 4 || reg == 8) && !(v & 1)) assert(m->regs[(reg == 4 ? 8 : 4)/4] & 1);
        if ((reg == 4 || reg == 8) && (v & 1)) assert(all_ports_off(m));
        m->regs[reg/4]=v;
    }
    return r;
}
static void barrier(void *context) { (void)context; }
static void delay(void *context, uint32_t us) { ((struct window *)context)->model->time += us; }
static int quiesce(void *context)
{ struct model *m=context; ++m->guards; return m->quiesce_fail ? USB_BUSY : USB_OK; }
static struct usb_mmio mmio(struct model *m, unsigned id)
{ return (struct usb_mmio){&m->window[id],0x1000,read32,write32,barrier,delay}; }
static void prepare(struct model *m, unsigned u2, unsigned u3)
{
    memset(m,0,sizeof *m); m->u2=u2; m->u3=u3;
    for (unsigned n=0;n<3;++n) m->window[n]=(struct window){m,n};
    for (unsigned n=0;n<64;++n) m->regs[n]=0xa55a4000;
    m->regs[0x24/4]=(u2<<8) | u3; m->ao=0xb5b50000;
    /* Inherited enabled ports/IPs, already externally drained. */
    for (unsigned n=0;n<u3;++n) m->regs[(0x30+8*n)/4] |= 0x80;
    for (unsigned n=0;n<u2;++n) m->regs[(0x50+8*n)/4] |= 0x80;
}
static int init(struct model *m, bool qualified)
{
    struct usb_mmio regs=mmio(m,0), ao=mmio(m,1), mbist=mmio(m,2); struct dali_domain_guard g={m,quiesce};
    return dali_usb_ippc_init(&m->ippc,&regs,&ao,&mbist,&g,qualified);
}
static void assert_stopped(struct model *m)
{
    assert(all_ports_off(m) && (m->regs[1] & 1) && (m->regs[2] & 1));
    assert(!(m->ao & 0x13) && !(m->regs[0] & 1));
    assert(!m->ippc.ao_reset_owned && !m->ippc.ip_reset_owned);
    assert(m->ippc.stopped && m->ippc.role == USB_ROLE_STOPPED);
}
static void cold(struct model *m)
{ assert(init(m,true) == USB_OK); assert(dali_usb_ippc_ao_reset(&m->ippc) == USB_OK); assert(dali_usb_ippc_reset(&m->ippc) == USB_OK); }
static void normal(void)
{
    struct model m;
    for (unsigned u2=1;u2<=2;++u2) for (unsigned u3=0;u3<=4;++u3) {
        prepare(&m,u2,u3); assert(init(&m,true) == USB_OK && !m.writes);
        assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_DEVICE) == USB_BUSY && !m.writes);
        assert(dali_usb_ippc_ao_reset(&m.ippc) == USB_OK && m.time == 1000);
        assert(dali_usb_ippc_reset(&m.ippc) == USB_OK && m.time == 1101); assert_stopped(&m);
        for (unsigned n=0;n<2;++n) {
            enum usb_role role=n ? USB_ROLE_HOST : USB_ROLE_DEVICE;
            assert(dali_usb_ippc_enable(&m.ippc,role) == USB_OK && m.ippc.role == role && !m.ippc.stopped);
            assert((m.regs[0x50/4] & 0x287) == (n ? 0x204u : 0x80u));
            assert((m.regs[0x9c/4] & (u2 == 2 ? 0x18u : 8u)) == (u2 == 2 ? 0x18u : 8u));
            unsigned writes=m.writes;
            assert(dali_usb_ippc_enable(&m.ippc,n ? USB_ROLE_DEVICE : USB_ROLE_HOST) == USB_BUSY && m.writes == writes);
            assert(dali_usb_ippc_ao_reset(&m.ippc) == USB_BUSY && dali_usb_ippc_reset(&m.ippc) == USB_BUSY && m.writes == writes);
            m.quiesce_fail=true; unsigned ops=m.operations;
            assert(dali_usb_ippc_destroy(&m.ippc) == USB_BUSY && m.ippc.initialized && m.operations == ops && m.writes == writes);
            m.quiesce_fail=false; assert(dali_usb_ippc_stop(&m.ippc) == USB_OK); assert_stopped(&m);
            writes=m.writes;
            assert(dali_usb_ippc_stop(&m.ippc) == USB_OK && m.writes == writes); assert_stopped(&m);
        }
        assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK && !m.ippc.initialized);
        assert((m.ao & ~0x13u) == 0xb5b50000 && (m.regs[0] & ~1u) == 0xa55a4000);
    }
}
static void guards(void)
{
    struct model m; prepare(&m,1,1); assert(init(&m,false) == USB_UNSUPPORTED && !m.operations);
    for (unsigned n=1;n<=3;++n) { prepare(&m,1,1); m.fail_operation=n; assert(init(&m,true) == USB_IO && !m.ippc.initialized && !m.writes); }
    prepare(&m,1,1); m.ao |= 1; assert(init(&m,true) == USB_BUSY && m.operations == 1 && !m.writes);
    prepare(&m,1,1); m.regs[0] |= 1; assert(init(&m,true) == USB_BUSY && !m.writes);
    for (unsigned n=0;n<3;++n) {
        prepare(&m,1,1); m.regs[0x24/4]=n == 0 ? 1u : (n == 1 ? 0x301u : 0x105u);
        assert(init(&m,true) == USB_UNSUPPORTED && !m.writes);
    }
    prepare(&m,1,1); assert(init(&m,true) == USB_OK); m.quiesce_fail=true; unsigned ops=m.operations;
    assert(dali_usb_ippc_ao_reset(&m.ippc) == USB_BUSY && dali_usb_ippc_reset(&m.ippc) == USB_BUSY &&
        dali_usb_ippc_stop(&m.ippc) == USB_BUSY && m.operations == ops && !m.writes);
    prepare(&m,1,1); cold(&m); m.regs[0x50/4] &= ~1u; unsigned writes=m.writes;
    assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_HOST) == USB_BUSY && m.writes == writes && m.ippc.faulted);
}
static int action(struct model *m, unsigned which)
{
    switch (which) {
    case 0: return dali_usb_ippc_ao_reset(&m->ippc);
    case 1: return dali_usb_ippc_reset(&m->ippc);
    case 2: return dali_usb_ippc_enable(&m->ippc,USB_ROLE_DEVICE);
    case 3: return dali_usb_ippc_enable(&m->ippc,USB_ROLE_HOST);
    case 4: return dali_usb_ippc_stop(&m->ippc);
    default: return dali_usb_ippc_destroy(&m->ippc);
    }
}
static void before(struct model *m, unsigned which)
{
    prepare(m,2,4); assert(init(m,true) == USB_OK);
    if (which>=2) assert(dali_usb_ippc_reset(&m->ippc) == USB_OK);
    if (which>=4) assert(dali_usb_ippc_enable(&m->ippc,USB_ROLE_DEVICE) == USB_OK);
}
static void faults(unsigned which, bool applied)
{
    struct model m; before(&m,which); unsigned base=m.operations; assert(action(&m,which) == USB_OK); unsigned total=m.operations-base;
    for (unsigned n=1;n<=total;++n) {
        before(&m,which); m.fail_operation=m.operations+n; m.applied_error=applied;
        assert(action(&m,which) == USB_IO && m.ippc.initialized && m.ippc.faulted);
        unsigned writes=m.writes;
        assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_HOST) == USB_IO && m.writes == writes);
        m.quiesce_fail=true;
        assert(dali_usb_ippc_destroy(&m.ippc) == USB_BUSY && m.writes == writes && m.ippc.initialized);
        m.quiesce_fail=false;
        assert(dali_usb_ippc_stop(&m.ippc) == USB_OK && m.ippc.faulted); assert_stopped(&m);
        assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK && !m.ippc.initialized);
    }
}
static void deadlines(void)
{
    struct model m;
    prepare(&m,1,1); assert(init(&m,true) == USB_OK); m.busy=true;
    assert(dali_usb_ippc_reset(&m.ippc) == USB_TIMEOUT && m.time == 3000000 && !m.writes && m.ippc.initialized);
    m.busy=false; assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    prepare(&m,1,1); cold(&m); assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_HOST) == USB_OK);
    m.busy=true; unsigned writes=m.writes; uint64_t time=m.time;
    assert(dali_usb_ippc_stop(&m.ippc) == USB_TIMEOUT && m.time-time == 3000000 && m.writes == writes && !(m.regs[1] & 1));
    m.busy=false; assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    const uint32_t clocks[]={1,0x100,0x400,0x800,0};
    for (unsigned role=0;role<2;++role) for (unsigned n=0;n<5;++n) {
        if (!role && n == 3) continue; /* XHCI reset bit belongs only to host. */
        prepare(&m,2,1); cold(&m); m.blocked1=clocks[n]; m.blocked2=n == 4 ? 1u : 0u; time=m.time;
        assert(dali_usb_ippc_enable(&m.ippc,role ? USB_ROLE_HOST : USB_ROLE_DEVICE) == USB_TIMEOUT && m.time-time == (n == 4 ? 10000u : 20000u));
        m.blocked1=m.blocked2=0; assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    }
    prepare(&m,1,1); cold(&m); assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_DEVICE) == USB_OK); m.blocked1=0x40000000; time=m.time;
    assert(dali_usb_ippc_destroy(&m.ippc) == USB_TIMEOUT && m.time-time == 100000 && m.ippc.initialized && !m.ippc.stopped);
    m.blocked1=0; assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    prepare(&m,1,1); cold(&m); m.blocked1=0x40000000; writes=m.writes; time=m.time;
    assert(dali_usb_ippc_stop(&m.ippc) == USB_TIMEOUT && m.time-time == 100000 && m.writes == writes); /* Fresh EBS retry sleep proof. */
    m.blocked1=0; assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    prepare(&m,1,1); assert(init(&m,true) == USB_OK); m.idle_at=200000;
    assert(dali_usb_ippc_reset(&m.ippc) == USB_OK && m.time == 200101);
    m.status_at=m.time+500; assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_DEVICE) == USB_OK && m.time == 200601);
    assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    prepare(&m,1,1); cold(&m); m.status_at=m.time+1000; m.read_fail_at=m.time+200; time=m.time;
    assert(dali_usb_ippc_enable(&m.ippc,USB_ROLE_DEVICE) == USB_IO && m.time-time == 200);
    assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    for (unsigned n=0;n<2;++n) {
        prepare(&m,1,1); assert(init(&m,true) == USB_OK); m.ignore_write=true;
        assert(action(&m,n) == USB_IO && (n ? m.ippc.ip_reset_owned : m.ippc.ao_reset_owned));
        m.ignore_write=false; assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    }
}
static void repeated_reset_faults(void)
{
    struct model m;
    for (unsigned which=0;which<2;++which) {
        prepare(&m,1,1); assert(init(&m,true) == USB_OK);
        m.applied_error=true; m.fail_operation=m.operations+3; /* Assertion write applied. */
        assert(action(&m,which) == USB_IO && (which ? m.ippc.ip_reset_owned : m.ippc.ao_reset_owned));
        assert(which ? (m.regs[0] & 1) != 0 : (m.ao & 0x13) == 0x13);
        m.fail_operation=m.operations+2; /* Deassertion applied but its error retains ownership. */
        assert(dali_usb_ippc_stop(&m.ippc) == USB_IO && m.ippc.initialized &&
            (which ? m.ippc.ip_reset_owned : m.ippc.ao_reset_owned));
        assert(which ? !(m.regs[0] & 1) : !(m.ao & 0x13));
        m.fail_operation=m.operations+1; /* Next cleanup read also fails. */
        assert(dali_usb_ippc_destroy(&m.ippc) == USB_IO && m.ippc.initialized &&
            (which ? m.ippc.ip_reset_owned : m.ippc.ao_reset_owned));
        assert(dali_usb_ippc_stop(&m.ippc) == USB_OK); assert_stopped(&m);
        assert(dali_usb_ippc_destroy(&m.ippc) == USB_OK);
    }
}
int main(void)
{
    normal(); guards();
    for (unsigned which=0;which<6;++which) { faults(which,false); faults(which,true); }
    deadlines(); repeated_reset_faults();
    puts("PASS: production MT6991 IPPC/AO reset, USB2 role/port, MBIST/clock/sleep gates and retained cleanup; register model only");
    return 0;
}
