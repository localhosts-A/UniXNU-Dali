/* Production clock/HWRSC C against register/fence/ACK model only. */
#include "DaliUsbResources.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model;
struct window { struct model *model; unsigned id; };
struct model {
    struct dali_usb_clocks clocks;
    struct dali_usb_hwrsc hwrsc;
    struct window window[4];
    uint32_t cfg[2], fence_block[2], request, ack, ack_block, ao[8];
    uint32_t initial_cfg[2], initial_ao[8];
    unsigned operations, fail_operation, writes, clear_order[3], clear_count;
    uint64_t time, fence_at[3], ack_at, read_fail_at;
    bool applied_error, quiesce_fail, ignore_write;
};
static const uint32_t gate[]={0x8000,0x800000,0x80000000};
static int operation(struct model *m) { return ++m->operations == m->fail_operation ? USB_IO : USB_OK; }
static void refresh_ack(struct model *m)
{ uint32_t mask=m->time >= m->ack_at ? 0x5f & ~m->ack_block : 0; m->ack=(m->ack & ~mask) | (m->request & mask); }
static int read32(void *context, uint32_t reg, uint32_t *v)
{
    struct window *w=context; struct model *m=w->model;
    int r=operation(m); if (r) return r;
    if (m->read_fail_at && m->time >= m->read_fail_at) { m->read_fail_at=0; return USB_IO; }
    if (w->id < 2) {
        if (reg == (w->id ? 0xb0u : 0x90u)) *v=m->cfg[w->id];
        else {
            assert(reg == (w->id ? 0x274u : 0x3a0u));
            *v=0x180000;
            if (w->id) { *v=0x4567; if (!(m->cfg[1] & gate[2])) *v |= 0x100000; }
            else { if (!(m->cfg[0] & gate[0])) *v |= 0x40000000; if (!(m->cfg[0] & gate[1])) *v |= 0x20000000; }
            if (w->id) { if (m->time < m->fence_at[2]) *v &= ~0x100000u; }
            else { if (m->time < m->fence_at[0]) *v &= ~0x40000000u; if (m->time < m->fence_at[1]) *v &= ~0x20000000u; }
            *v &= ~m->fence_block[w->id];
        }
    } else if (w->id == 2) {
        assert(reg == 0xe0 || reg == 0xe8); refresh_ack(m); *v=reg == 0xe0 ? m->request : m->ack;
    } else {
        assert(reg >= 0x70 && reg <= 0x8c && !(reg & 3)); *v=m->ao[(reg-0x70)/4];
    }
    return USB_OK;
}
static int write32(void *context, uint32_t reg, uint32_t v)
{
    struct window *w=context; struct model *m=w->model; ++m->writes;
    int r=operation(m); if ((r && !m->applied_error) || m->ignore_write) return r;
    if (w->id < 2) {
        uint32_t set=w->id ? 0xb4u : 0x94u;
        assert((reg == set || reg == set+4) && (w->id ? v == gate[2] : (v == gate[0] || v == gate[1])));
        if (reg == set) {
            m->cfg[w->id] |= v;
            if (m->clear_count < 3) m->clear_order[m->clear_count++]=w->id ? 2u : (v == gate[0] ? 0u : 1u);
        } else m->cfg[w->id] &= ~v;
    } else if (w->id == 2) {
        assert(reg == 0xe0 && (v & ~0x7f5fu) == (m->request & ~0x7f5fu));
        assert((v & 0x5f) == 0 || (v & 0x5f) == 0x5f);
        if (v & 0x5f) for (unsigned n=0;n<8;++n) assert((m->ao[n] & 15) == 15);
        assert(!(v & 0x7f00)); m->request=v; refresh_ack(m);
    } else {
        assert(reg >= 0x70 && reg <= 0x8c && !(reg & 3)); unsigned n=(reg-0x70)/4;
        assert((v & ~15u) == (m->ao[n] & ~15u));
        if ((v & 15) != 15) { refresh_ack(m); assert(!(m->request & 0x7f5f) && !(m->ack & 0x5f)); }
        m->ao[n]=v;
    }
    return r;
}
static void barrier(void *context) { (void)context; }
static void delay(void *context, uint32_t us) { ((struct window *)context)->model->time += us; }
static int quiesce(void *context) { return ((struct model *)context)->quiesce_fail ? USB_BUSY : USB_OK; }
static struct usb_mmio mmio(struct model *m, unsigned id)
{ return (struct usb_mmio){&m->window[id],0x1000,read32,write32,barrier,delay}; }
static void prepare(struct model *m, unsigned gated, uint32_t req)
{
    memset(m,0,sizeof *m);
    for (unsigned n=0;n<4;++n) m->window[n]=(struct window){m,n};
    m->cfg[0]=0x11010111; m->cfg[1]=0x01203040;
    for (unsigned n=0;n<3;++n) if (gated & (1u<<n)) m->cfg[n == 2 ? 1 : 0] |= gate[n];
    memcpy(m->initial_cfg,m->cfg,sizeof m->cfg);
    for (unsigned n=0;n<8;++n) m->ao[n]=0xcafe1000+n*2;
    m->ao[7] |= 15; /* Inherited open gate: never acquire or rewrite it. */
    memcpy(m->initial_ao,m->ao,sizeof m->ao);
    m->request=0x800080a0 | req; m->ack=0x400080a0 | (req & 0x5f);
}
static int clocks_init(struct model *m, bool qualified)
{
    struct usb_mmio vlp=mmio(m,0), top=mmio(m,1); struct dali_domain_guard g={m,quiesce};
    return dali_usb_clocks_init(&m->clocks,&vlp,&top,&g,qualified);
}
static int hwrsc_init(struct model *m, bool qualified)
{
    struct usb_mmio ippc=mmio(m,2), ao=mmio(m,3); struct dali_domain_guard g={m,quiesce};
    return dali_usb_hwrsc_init(&m->hwrsc,&ippc,&ao,&g,DALI_USB_RESOURCE_NATIVE_HWRSC_V2,qualified);
}
static void clocks_restored(struct model *m)
{ assert(!memcmp(m->cfg,m->initial_cfg,sizeof m->cfg) && !m->clocks.owned && !m->clocks.on); }
static void resources_released(struct model *m)
{
    assert(!(m->request & 0x7f5f) && !(m->ack & 0x5f));
    assert((m->request & ~0x7f5fu) == 0x800080a0);
    assert(!memcmp(m->ao,m->initial_ao,sizeof m->ao) && !m->hwrsc.gates_owned && !m->hwrsc.request_owned && !m->hwrsc.on);
}
static void normal(void)
{
    struct model m;
    for (unsigned parents=0;parents<8;++parents) for (unsigned gated=0;gated<8;++gated) {
        prepare(&m,gated,0);
        const uint32_t parent[]={0x100,0x10000,0x1000000};
        for (unsigned n=0;n<3;++n) if (parents & (1u<<n)) m.cfg[n == 2 ? 1 : 0] ^= parent[n];
        memcpy(m.initial_cfg,m.cfg,sizeof m.cfg);
        assert(clocks_init(&m,true) == USB_OK && !m.writes);
        assert(dali_usb_clocks_enable(&m.clocks) == USB_OK && m.clocks.on && m.clocks.owned == gated);
        unsigned writes=m.writes;
        assert(dali_usb_clocks_enable(&m.clocks) == USB_OK && m.writes == writes);
        assert(dali_usb_clocks_disable(&m.clocks) == USB_OK); clocks_restored(&m);
        if (gated == 7) assert(m.clear_count == 3 && m.clear_order[0] == 2 && m.clear_order[1] == 1 && m.clear_order[2] == 0);
        writes=m.writes; assert(dali_usb_clocks_disable(&m.clocks) == USB_OK && m.writes == writes);
        assert(dali_usb_clocks_enable(&m.clocks) == USB_OK);
        assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK && !m.clocks.initialized); clocks_restored(&m);
    }
    const uint32_t inherited[]={0,0x7f00,0x5f,0x7f5f};
    for (unsigned i=0;i<4;++i) {
        prepare(&m,7,inherited[i]); assert(hwrsc_init(&m,true) == USB_OK && !m.writes);
        assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_OK && m.hwrsc.on && m.time == 2000);
        assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_OK && m.time == 4000);
        assert(dali_usb_hwrsc_disable(&m.hwrsc) == USB_OK && m.time == 6000); resources_released(&m);
        unsigned writes=m.writes;
        assert(dali_usb_hwrsc_disable(&m.hwrsc) == USB_OK && m.writes == writes);
        assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_OK);
        assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK && !m.hwrsc.initialized); resources_released(&m);
        prepare(&m,7,inherited[i]); assert(hwrsc_init(&m,true) == USB_OK);
        assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); resources_released(&m);
    }
}
static void guards(void)
{
    struct model m; prepare(&m,7,0);
    assert(clocks_init(&m,false) == USB_UNSUPPORTED && hwrsc_init(&m,false) == USB_UNSUPPORTED && !m.operations);
    struct usb_mmio ippc=mmio(&m,2),ao=mmio(&m,3); struct dali_domain_guard g={&m,quiesce};
    assert(dali_usb_hwrsc_init(&m.hwrsc,&ippc,&ao,&g,DALI_USB_RESOURCE_UNQUALIFIED,true) == USB_UNSUPPORTED && !m.operations);
    for (unsigned i=1;i<=10;++i) { prepare(&m,7,0); m.fail_operation=i; assert(hwrsc_init(&m,true) == USB_IO && !m.hwrsc.initialized && !m.writes); }
    for (unsigned i=1;i<=3;++i) { prepare(&m,7,0); m.fail_operation=i; assert(clocks_init(&m,true) == USB_IO && !m.clocks.initialized && !m.writes); }
    prepare(&m,7,1); assert(hwrsc_init(&m,true) == USB_BUSY && !m.writes);
    prepare(&m,7,0); m.ack=0x5f; m.ack_block=0x5f; assert(hwrsc_init(&m,true) == USB_BUSY && !m.writes);
    prepare(&m,7,0); assert(clocks_init(&m,true) == USB_OK && hwrsc_init(&m,true) == USB_OK);
    m.quiesce_fail=true; unsigned ops=m.operations;
    assert(dali_usb_clocks_enable(&m.clocks) == USB_BUSY && dali_usb_hwrsc_enable(&m.hwrsc) == USB_BUSY && m.operations == ops && !m.writes);
    assert(dali_usb_clocks_destroy(&m.clocks) == USB_BUSY && dali_usb_hwrsc_destroy(&m.hwrsc) == USB_BUSY && m.operations == ops);
    m.quiesce_fail=false; m.cfg[0] ^= 0x100;
    assert(dali_usb_clocks_enable(&m.clocks) == USB_BUSY && dali_usb_clocks_destroy(&m.clocks) == USB_BUSY && !m.writes && m.clocks.initialized);
    m.request |= 1;
    assert(dali_usb_hwrsc_disable(&m.hwrsc) == USB_BUSY && dali_usb_hwrsc_destroy(&m.hwrsc) == USB_BUSY && !m.writes && m.hwrsc.initialized);
    prepare(&m,7,0); assert(hwrsc_init(&m,true) == USB_OK); m.ao[0] |= 8;
    assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_BUSY && !m.writes && m.hwrsc.faulted);
}
static int call(struct model *m, bool clocks, bool on)
{ return clocks ? (on ? dali_usb_clocks_enable(&m->clocks) : dali_usb_clocks_disable(&m->clocks)) :
    (on ? dali_usb_hwrsc_enable(&m->hwrsc) : dali_usb_hwrsc_disable(&m->hwrsc)); }
static void failures(bool clocks, bool on, bool applied)
{
    struct model m; prepare(&m,7,0); assert((clocks ? clocks_init(&m,true) : hwrsc_init(&m,true)) == USB_OK);
    if (!on) assert(call(&m,clocks,true) == USB_OK);
    unsigned base=m.operations; assert(call(&m,clocks,on) == USB_OK); unsigned total=m.operations-base;
    for (unsigned i=1;i<=total;++i) {
        prepare(&m,7,0); assert((clocks ? clocks_init(&m,true) : hwrsc_init(&m,true)) == USB_OK);
        if (!on) assert(call(&m,clocks,true) == USB_OK);
        m.fail_operation=m.operations+i; m.applied_error=applied;
        assert(call(&m,clocks,on) == USB_IO && (clocks ? m.clocks.faulted : m.hwrsc.faulted));
        unsigned writes=m.writes; assert(call(&m,clocks,true) == USB_IO && m.writes == writes);
        m.quiesce_fail=true;
        assert((clocks ? dali_usb_clocks_destroy(&m.clocks) : dali_usb_hwrsc_destroy(&m.hwrsc)) == USB_BUSY && m.writes == writes);
        assert(clocks ? m.clocks.initialized : m.hwrsc.initialized); m.quiesce_fail=false;
        assert(call(&m,clocks,false) == USB_OK);
        if (clocks) { clocks_restored(&m); assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK); }
        else { resources_released(&m); assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); }
    }
}
static void timeouts(void)
{
    struct model m;
    prepare(&m,7,0); assert(clocks_init(&m,true) == USB_OK); m.fence_at[0]=4000001;
    assert(dali_usb_clocks_enable(&m.clocks) == USB_OK && m.time == 4000001);
    assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK); clocks_restored(&m);
    prepare(&m,7,0); assert(hwrsc_init(&m,true) == USB_OK); m.ack_at=5000;
    assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_OK && m.time == 7000);
    m.ack_at=m.time+5000;
    assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK && m.time == 14000); resources_released(&m);
    prepare(&m,7,0); assert(clocks_init(&m,true) == USB_OK); m.fence_at[0]=100; m.read_fail_at=50;
    assert(dali_usb_clocks_enable(&m.clocks) == USB_IO && m.time == 50 && m.clocks.owned);
    assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK); clocks_restored(&m);
    prepare(&m,7,0); assert(hwrsc_init(&m,true) == USB_OK); m.ack_at=5000; m.read_fail_at=200;
    assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_IO && m.time == 200 && m.hwrsc.request_owned);
    assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); resources_released(&m);
    const uint32_t fence[]={0x40000000,0x20000000,0x100000};
    for (unsigned n=0;n<3;++n) {
        prepare(&m,7,0); assert(clocks_init(&m,true) == USB_OK); m.fence_block[n == 2 ? 1 : 0]=fence[n];
        assert(dali_usb_clocks_enable(&m.clocks) == USB_TIMEOUT && m.time == 5000000 && m.clocks.owned);
        assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK); clocks_restored(&m);
    }
    for (unsigned direction=0;direction<2;++direction) for (unsigned bit=0;bit<7;++bit) {
        if (bit == 5) continue;
        prepare(&m,7,0); assert(hwrsc_init(&m,true) == USB_OK);
        if (direction) assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_OK);
        m.ack_block=1u<<bit; uint64_t time=m.time;
        assert(call(&m,false,!direction) == USB_TIMEOUT && m.time-time == 20000 && m.hwrsc.request_owned);
        m.ack_block=0; assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); resources_released(&m);
    }
    prepare(&m,7,0); assert(clocks_init(&m,true) == USB_OK); m.ignore_write=true;
    assert(dali_usb_clocks_enable(&m.clocks) == USB_IO && m.clocks.owned);
    m.ignore_write=false; assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK); clocks_restored(&m);
    prepare(&m,7,0); assert(clocks_init(&m,true) == USB_OK && dali_usb_clocks_enable(&m.clocks) == USB_OK);
    m.ignore_write=true;
    assert(dali_usb_clocks_destroy(&m.clocks) == USB_IO && m.clocks.initialized && m.clocks.owned);
    m.ignore_write=false; assert(dali_usb_clocks_destroy(&m.clocks) == USB_OK); clocks_restored(&m);
    prepare(&m,7,0); assert(hwrsc_init(&m,true) == USB_OK); m.ignore_write=true;
    assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_IO && m.hwrsc.gates_owned);
    m.ignore_write=false; assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); resources_released(&m);
    prepare(&m,7,0);
    for (unsigned n=0;n<8;++n) m.ao[n] |= 15;
    memcpy(m.initial_ao,m.ao,sizeof m.ao);
    assert(hwrsc_init(&m,true) == USB_OK); m.ignore_write=true;
    assert(dali_usb_hwrsc_enable(&m.hwrsc) == USB_IO && m.hwrsc.request_owned && !m.hwrsc.gates_owned);
    m.ignore_write=false; assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); resources_released(&m);
    prepare(&m,7,0); assert(hwrsc_init(&m,true) == USB_OK && dali_usb_hwrsc_enable(&m.hwrsc) == USB_OK);
    m.ignore_write=true;
    assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_IO && m.hwrsc.initialized && m.hwrsc.request_owned && m.hwrsc.gates_owned);
    m.ignore_write=false; assert(dali_usb_hwrsc_destroy(&m.hwrsc) == USB_OK); resources_released(&m);
}
int main(void)
{
    normal(); guards();
    for (unsigned clocks=0;clocks<2;++clocks) for (unsigned on=0;on<2;++on) for (unsigned applied=0;applied<2;++applied)
        failures(clocks != 0,on != 0,applied != 0);
    timeouts();
    puts("PASS: production MT6991 USB clock gates/fences and explicit native HWRSC v2; register model only");
    return 0;
}
