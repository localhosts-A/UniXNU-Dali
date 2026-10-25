/* Actual USB0 SCPSYS C against register/ACK/RTFF event model only. */
#include "DaliUsbDomains.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model;
struct window { struct model *model; bool protection; };
struct model {
    struct dali_usb_domains domains;
    struct window ctrl_window, prot_window;
    uint32_t ctrl[2], enable, status, blocked[2];
    unsigned operations, fail_operation, writes, restores[2], flag_clear_op[2], set_attempts, drop_set;
    uint64_t time;
    bool quiesce_fail, applied_error, drop_clear;
};
static const uint32_t bp[] = {0x80,0x40};
static int operation(struct model *m) { return ++m->operations == m->fail_operation ? USB_IO : USB_OK; }
static void refresh(struct model *m, unsigned n)
{
    uint32_t value = (m->ctrl[n] & 4 ? 0x40000000u : 0) | (m->ctrl[n] & 8 ? 0x80000000u : 0);
    uint32_t mask = 0xc0000000;
    if (!n) { value |= m->ctrl[n] & 0x100 ? 0x1000u : 0; mask |= 0x1000; }
    mask &= ~m->blocked[n];
    m->ctrl[n] = (m->ctrl[n] & ~mask) | (value & mask);
}
static int read32(void *context, uint32_t reg, uint32_t *value)
{
    struct window *w=context; struct model *m=w->model;
    int r=operation(m); if (r) return r;
    if (w->protection) {
        assert(reg == 0xd8 || reg == 0x208); *value = reg == 0xd8 ? m->enable : m->status;
    } else {
        assert(reg == 0xe1c || reg == 0xe18); unsigned n=reg == 0xe1c ? 0u : 1u;
        refresh(m,n); *value=m->ctrl[n];
    }
    return USB_OK;
}
static int write32(void *context, uint32_t reg, uint32_t v)
{
    struct window *w=context; struct model *m=w->model; ++m->writes;
    int r=operation(m); if (r && !m->applied_error) return r;
    if (w->protection) {
        assert((reg == 0xdc || reg == 0xe0) && (v == 0x40 || v == 0x80));
        unsigned n=v == 0x80 ? 0u : 1u;
        if (reg == 0xdc) {
            if (++m->set_attempts > m->drop_set) { m->enable |= v; m->status |= v; }
        } else if (!m->drop_clear) {
            uint32_t mask=0xc000001f | (!n ? 0x1100u : 0u);
            assert((m->ctrl[n] & mask) == 0xc000000d && !(m->ctrl[n] & 0xd000000));
            m->enable &= ~v; /* ACK intentionally stays high: source IGN clear semantics. */
        }
    } else {
        assert(reg == 0xe1c || reg == 0xe18); unsigned n=reg == 0xe1c ? 0u : 1u;
        uint32_t old=m->ctrl[n], ro=0xc0000000 | (!n ? 0x1000u : 0u);
        uint32_t allowed=0xcf00001f | (!n ? 0x100u : 0u);
        assert((v & ~(allowed|ro)) == (old & ~(allowed|ro))); /* Neighbors/reserved untouched. */
        if (!(old & 8) && (v & 8)) assert(old & 0x40000000);
        if ((old & 2) && !(v & 2)) assert((old & 0xc0000010) == 0xc0000000);
        if (!(old & 1) && (v & 1)) assert((old & 0xc0000012) == 0xc0000000);
        if (!n && (old & 0x100) && !(v & 0x100)) assert((old & 0xc0000013) == 0xc0000001);
        if (!(old & 0x1000000) && (v & 0x1000000))
            assert((old & 0xc4000000) == 0xc4000000 && (m->enable & bp[n]) && (m->status & bp[n]));
        if ((old & 0x8000000) && !(v & 0x8000000)) m->flag_clear_op[n]=m->operations;
        if (!(old & 0x2000000) && (v & 0x2000000)) {
            assert((old & 0xc4000001) == 0xc4000001 && !(old & 0x8000000)); ++m->restores[n];
        }
        if ((old & 1) && !(v & 1)) assert((old & 0x8000002) == 0x8000002);
        if ((old & 4) && !(v & 4)) {
            assert((old & 0x13) == 0x12 && (m->enable & bp[n]) && (m->status & bp[n]));
            if (!n) assert(!(m->ctrl[1] & 0xc000000c)); /* Parent never drops before child. */
        }
        if ((old & 8) && !(v & 8)) assert(!(old & 0x40000000));
        if (n && !(old & 4) && (v & 4)) assert((m->ctrl[0] & 0xc000001f) == 0xc000000d);
        m->ctrl[n]=(v & ~ro) | (old & ro); refresh(m,n);
    }
    return r;
}
static void barrier(void *context) { (void)context; }
static void delay(void *context, uint32_t us) { ((struct window *)context)->model->time += us; }
static int quiesce(void *context) { return ((struct model *)context)->quiesce_fail ? USB_TIMEOUT : USB_OK; }
static void prepare(struct model *m, bool parent_on, bool child_on)
{
    memset(m,0,sizeof *m); m->ctrl_window=(struct window){m,false}; m->prot_window=(struct window){m,true};
    m->enable=0xa55a0000; m->status=0x5aa500c0;
    for (unsigned n=0;n<2;++n) {
        bool on=n ? child_on : parent_on;
        m->ctrl[n]=0x20804000 | (on ? 0xc000000du : (0x8000012u | (!n ? 0x1100u : 0u)));
        if (!on) m->enable |= bp[n];
    }
}
static int init(struct model *m, bool qualified)
{
    struct usb_mmio c={&m->ctrl_window,0x1000,read32,write32,barrier,delay};
    struct usb_mmio p={&m->prot_window,0x1000,read32,write32,barrier,delay};
    struct dali_domain_guard g={m,quiesce};
    return dali_usb_domains_init(&m->domains,&c,&p,&g,qualified);
}
static void assert_off(struct model *m)
{
    for (unsigned n=0;n<2;++n) {
        assert(m->domains.domain[n].power == DALI_DOMAIN_OFF && m->domains.domain[n].phase == DALI_DOMAIN_IDLE);
        assert(!(m->ctrl[n] & 0xc000000d) && (m->ctrl[n] & 0x8000012) == 0x8000012);
    }
    assert((m->enable & 0xc0) == 0xc0 && (m->status & 0xc0) == 0xc0);
}
static void normal(bool parent_on, bool child_on)
{
    struct model m; prepare(&m,parent_on,child_on); assert(init(&m,true) == USB_OK && !m.writes);
    assert(dali_usb_domains_power_on(&m.domains) == USB_OK);
    assert(m.domains.domain[0].power == DALI_DOMAIN_ON && m.domains.domain[1].power == DALI_DOMAIN_ON);
    assert(m.restores[0] == !parent_on && m.restores[1] == !child_on && !(m.enable & 0xc0) && (m.status & 0xc0) == 0xc0);
    unsigned writes=m.writes;
    assert(dali_usb_domains_power_on(&m.domains) == USB_OK && m.writes == writes);
    m.quiesce_fail=true; unsigned ops=m.operations;
    assert(dali_usb_domains_destroy(&m.domains) == USB_TIMEOUT && m.operations == ops && m.domains.initialized);
    m.quiesce_fail=false;
    assert(dali_usb_domains_power_off(&m.domains) == USB_OK); assert_off(&m);
    assert((m.enable & ~0xc0u) == 0xa55a0000 && (m.status & ~0xc0u) == 0x5aa50000);
    writes=m.writes; assert(dali_usb_domains_power_off(&m.domains) == USB_OK && m.writes == writes);
    assert(dali_usb_domains_power_on(&m.domains) == USB_OK && m.restores[0] == (unsigned)!parent_on+1 && m.restores[1] == (unsigned)!child_on+1);
    assert(dali_usb_domains_destroy(&m.domains) == USB_OK && !m.domains.initialized);
}
static void guards(void)
{
    struct model m; prepare(&m,false,false); assert(init(&m,false) == USB_UNSUPPORTED && !m.operations);
    prepare(&m,false,true); assert(init(&m,true) == USB_BUSY && !m.writes);
    const uint32_t invalid[]={4,0x40000000,0x1000000,0x4000000};
    for (unsigned n=0;n<4;++n) {
        prepare(&m,false,false); m.ctrl[0] ^= invalid[n];
        m.blocked[0] = invalid[n] & 0xc0001000; /* Preserve deliberately inconsistent ACK. */
        assert(init(&m,true) == USB_BUSY && !m.writes);
    }
    for (unsigned n=1;n<=6;++n) {
        prepare(&m,false,false); m.fail_operation=n;
        assert(init(&m,true) == USB_IO && !m.domains.initialized && !m.writes);
    }
    prepare(&m,false,false); assert(init(&m,true) == USB_OK); m.quiesce_fail=true; unsigned ops=m.operations;
    assert(dali_usb_domains_power_on(&m.domains) == USB_TIMEOUT && m.operations == ops && !m.writes && !m.domains.faulted);
    m.quiesce_fail=false; m.ctrl[0] |= 4;
    assert(dali_usb_domains_power_on(&m.domains) == USB_BUSY && !m.writes && m.domains.faulted);
    assert(dali_usb_domains_destroy(&m.domains) == USB_BUSY && m.domains.initialized && !m.writes);
    prepare(&m,true,true); assert(init(&m,true) == USB_OK); m.ctrl[1] |= 0x1000000;
    assert(dali_usb_domains_power_off(&m.domains) == USB_BUSY && !m.writes && m.domains.faulted);
    assert(dali_usb_domains_destroy(&m.domains) == USB_BUSY && m.domains.initialized && !m.writes);
}
static void failures(bool turning_on, bool applied)
{
    struct model m; prepare(&m,!turning_on,!turning_on); assert(init(&m,true) == USB_OK); unsigned base=m.operations;
    assert((turning_on ? dali_usb_domains_power_on(&m.domains) : dali_usb_domains_power_off(&m.domains)) == USB_OK);
    unsigned total=m.operations-base;
    for (unsigned n=1;n<=total;++n) {
        prepare(&m,!turning_on,!turning_on); assert(init(&m,true) == USB_OK);
        m.fail_operation=m.operations+n; m.applied_error=applied;
        assert((turning_on ? dali_usb_domains_power_on(&m.domains) : dali_usb_domains_power_off(&m.domains)) == USB_IO && m.domains.faulted);
        unsigned writes=m.writes;
        assert(dali_usb_domains_power_on(&m.domains) == USB_IO && m.writes == writes);
        m.quiesce_fail=true;
        assert(dali_usb_domains_destroy(&m.domains) == USB_TIMEOUT && m.domains.initialized && m.writes == writes);
        m.quiesce_fail=false;
        assert(dali_usb_domains_power_off(&m.domains) == USB_OK && m.domains.faulted); assert_off(&m);
        assert(dali_usb_domains_destroy(&m.domains) == USB_OK && !m.domains.initialized);
    }
}
static void timeouts_and_restore(void)
{
    struct model m;
    const uint32_t blocked[]={0x40000000,0x80000000,0x1000};
    for (unsigned target=0;target<2;++target) for (unsigned direction=0;direction<2;++direction)
    for (unsigned n=0;n<(target ? 2u : 3u);++n) {
        prepare(&m,direction != 0,direction != 0); assert(init(&m,true) == USB_OK); m.blocked[target]=blocked[n];
        uint64_t time=m.time;
        assert((direction ? dali_usb_domains_power_off(&m.domains) : dali_usb_domains_power_on(&m.domains)) == USB_TIMEOUT && m.time-time >= 1000000);
        assert(m.domains.domain[target].phase != DALI_DOMAIN_IDLE && m.domains.initialized);
        m.blocked[target]=0;
        assert(dali_usb_domains_power_off(&m.domains) == USB_OK); assert_off(&m);
    }
    prepare(&m,true,true); assert(init(&m,true) == USB_OK); m.drop_set=11;
    assert(dali_usb_domains_power_off(&m.domains) == USB_IO && m.set_attempts == 11 && (m.ctrl[1] & 1));
    m.drop_set=0; assert(dali_usb_domains_destroy(&m.domains) == USB_OK);
    prepare(&m,false,false); assert(init(&m,true) == USB_OK); m.drop_clear=true;
    assert(dali_usb_domains_power_on(&m.domains) == USB_TIMEOUT);
    assert(dali_usb_domains_destroy(&m.domains) == USB_OK); /* Own recovery never needs to unprotect. */
    for (unsigned target=0;target<2;++target) {
        prepare(&m,false,false); assert(init(&m,true) == USB_OK); assert(dali_usb_domains_power_on(&m.domains) == USB_OK);
        unsigned at=m.flag_clear_op[target]; assert(at);
        prepare(&m,false,false); assert(init(&m,true) == USB_OK); m.fail_operation=at; m.applied_error=true;
        assert(dali_usb_domains_power_on(&m.domains) == USB_IO && !(m.ctrl[target] & 0x8000000));
        assert(dali_usb_domains_power_off(&m.domains) == USB_OK && m.restores[target] == 1); assert_off(&m);
    }
}
int main(void)
{
    normal(false,false); normal(true,false); normal(true,true); guards();
    failures(true,false); failures(true,true); failures(false,false); failures(false,true); timeouts_and_restore();
    puts("PASS: production MT6991 USB domains, parent/child/RTFF, bus aliases, ACKs and retained failure cursors; register model only");
    return 0;
}
