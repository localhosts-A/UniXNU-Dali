/* Actual XS-PHY and both repeater C implementations. SoC registers, PMIC
 * byte API and NXP I2C/GPIO are modeled; PMIF/I2C transports test separately. */
#include "DaliUsbPhy.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model {
    struct dali_usb_phy phy;
    struct dali_usb_pmif pmif;
    struct dali_mt6379_repeater mt;
    struct dali_nxp_repeater nxp;
    uint32_t soc[0x400/4];
    uint8_t pmic[0xe00], chip[32];
    unsigned operations, fail_operation, writes, fm_reads, recoveries, nxp_modes;
    uint64_t time;
    bool quiesce_fail, recover_fail, meter_timeout, enabled;
};
static int operation(struct model *m) { return ++m->operations == m->fail_operation ? USB_IO : USB_OK; }
static void delay(void *c, uint32_t us) { ((struct model *)c)->time += us; }
static void barrier(void *c) { (void)c; }
static int quiesce(void *c) { return ((struct model *)c)->quiesce_fail ? USB_TIMEOUT : USB_OK; }
static int read32(void *c, uint32_t reg, uint32_t *value)
{
    struct model *m = c; assert(reg < 0x400);
    int result = operation(m); if (result) return result;
    *value = m->soc[reg/4];
    if (reg == 0x110 && (m->soc[0x100/4] & 0x1000000) && !m->meter_timeout && ++m->fm_reads >= 2) *value |= 1;
    return USB_OK;
}
static int write32(void *c, uint32_t reg, uint32_t value)
{
    struct model *m = c; assert(reg < 0x400);
    int result = operation(m); if (result) return result;
    if (reg == 0x314 && ((value ^ m->soc[reg/4]) & 0x7000)) {
        assert((m->soc[reg/4] & 0x8000) && !(m->soc[0x100/4] & 0x1000000) && !(m->soc[0x110/4] & 0x100));
    }
    if (reg == 0x100 && !(value & 0x1000000)) m->fm_reads = 0;
    m->soc[reg/4] = reg == 0x110 ? value & ~1u : value; ++m->writes;
    return USB_OK;
}
int dali_mt6379_read(struct dali_usb_pmif *p, uint16_t reg, void *bytes, size_t size)
{
    struct model *m = p->registers.context; assert(reg + size <= sizeof m->pmic && size == 1);
    if (p->faulted) return USB_IO;
    int result = operation(m); if (result) p->faulted = true; else memcpy(bytes,m->pmic+reg,size);
    return result;
}
int dali_mt6379_update8(struct dali_usb_pmif *p, uint16_t reg, uint8_t mask, uint8_t value)
{
    struct model *m = p->registers.context; uint8_t old;
    int result = dali_mt6379_read(p,reg,&old,1);
    if (result) return result;
    uint8_t next = (uint8_t)((old & (uint8_t)~mask) | value);
    if (next != old) {
        result = operation(m);
        if (result) p->faulted = true; else { m->pmic[reg] = next; ++m->writes; }
    }
    return result;
}
int dali_usb_pmif_recover(struct dali_usb_pmif *p)
{
    struct model *m = p->registers.context; ++m->recoveries;
    if (m->recover_fail) return USB_TIMEOUT;
    p->faulted = false; return USB_OK;
}
static int nxp_read(void *c, uint8_t reg, uint8_t *value)
{ struct model *m = c; assert(m->enabled && reg < 32); int r = operation(m); if (!r) *value = m->chip[reg]; return r; }
static int nxp_write(void *c, uint8_t reg, uint8_t value)
{
    struct model *m = c; assert(m->enabled && reg == 2 && !(m->pmic[0xd88] & 0x48));
    int result = operation(m); if (!result) { m->chip[reg] = value; ++m->writes; ++m->nxp_modes; } return result;
}
static int nxp_recover(void *c) { return ((struct model *)c)->recover_fail ? USB_TIMEOUT : USB_OK; }
static int nxp_enable(void *c, bool enable) { ((struct model *)c)->enabled = enable; return USB_OK; }
static void prepare(struct model *m)
{
    memset(m,0,sizeof *m);
    m->pmif.initialized = m->pmif.identified = true; m->pmif.revision = 3;
    m->pmif.registers.context = m; m->pmif.registers.delay_us = delay;
    m->pmic[0] = 0x73; m->pmic[0xd11] = 0x20; m->pmic[0xd1a] = 5;
    m->chip[0x15] = 0x32; m->chip[0x14] = 0x22; m->chip[2] = 0xcf;
    m->soc[0x100/4] = 0x80000000; m->soc[0x110/4] = 0x40000000; m->soc[0x10c/4] = 100;
    m->soc[0x300/4] = 0xffffffff; m->soc[0x304/4] = 0x12345678; m->soc[0x310/4] = 0xaabb0011;
    m->soc[0x314/4] = 0x11110222; m->soc[0x318/4] = m->soc[0x320/4] = 0xffffffff;
    m->soc[0x330/4] = 0xa5abcdef; m->soc[0x334/4] = 0x12345a78;
    m->soc[0x340/4] = 0x87654321; m->soc[0x344/4] = 0x12345678;
    m->soc[0x364/4] = 0xaaaaaaaa; m->soc[0x368/4] = m->soc[0x36c/4] = 0xffffffff;
    struct dali_repeater_guard guard = {m,quiesce};
    struct dali_nxp_repeater_bus bus = {m,nxp_read,nxp_write,nxp_recover,nxp_enable,delay};
    assert(dali_nxp_repeater_probe(&m->nxp,&bus,&guard,true) == USB_OK);
    assert(dali_mt6379_repeater_probe(&m->mt,&m->pmif,&guard,&dali_mt6379_dali_tuning,true) == USB_OK);
}
static int init(struct model *m, uint32_t sw, bool qualified, bool cal_valid)
{
    struct usb_mmio mmio = {m,0x400,read32,write32,barrier,delay};
    struct dali_repeater_guard guard = {m,quiesce};
    struct dali_xsphy_calibration cal = {sw,9u<<20,(13u<<10)|(21u<<15),cal_valid};
    return dali_usb_phy_init(&m->phy,&mmio,&m->nxp,&m->mt,&cal,&guard,qualified);
}
static void finish(struct model *m)
{
    assert(dali_usb_phy_destroy(&m->phy) == USB_OK && !m->phy.initialized);
    assert(dali_mt6379_repeater_destroy(&m->mt) == USB_OK);
    assert(dali_nxp_repeater_destroy(&m->nxp) == USB_OK && !m->enabled);
}
static void normal(uint32_t sw, enum usb_role role)
{
    struct model m; prepare(&m); unsigned writes = m.writes;
    assert(init(&m,sw,true,true) == USB_OK && m.writes == writes && m.phy.intr == 37 && m.phy.term == 10);
    uint64_t time = m.time;
    assert(dali_usb_phy_power_on(&m.phy,role) == USB_OK && m.phy.power == DALI_REPEATER_ON && m.phy.role == role);
    assert(m.time - time == 1681 && m.nxp_modes == 1 && !(m.pmic[0xd88] & 0x48));
    assert((m.soc[0x340/4] & 0x1ff0000) == ((13u<<20)|(9u<<16)) && (m.soc[0x344/4] & 0x1f00) == (21u<<8));
    assert(m.soc[0x304/4] == 0x12345678 && (m.soc[0x330/4] & ~0x3f000000u) == (0xa5abcdef & ~0x3f000000u));
    assert((m.soc[0x334/4] & ~0xf00u) == (0x12345a78 & ~0xf00u));
    assert((m.soc[0x314/4] & 0xf000) == 0x5000 && !(m.soc[0x100/4] & 0x1000000) && !(m.soc[0x110/4] & 0x100));
    assert((m.soc[0x364/4] & 0xc0000) == 0x40000 && (m.soc[4/4] & 0x10000) && m.soc[0] == 0x1e1e1900);
    assert((m.soc[0x36c/4] & 0x202) == (role == USB_ROLE_HOST ? 0x200u : 0x202u));
    if (!sw) assert(!(m.soc[0x300/4] & 2) && (m.soc[0x340/4] & 0x70) == 0x70);
    else assert((m.soc[0x300/4] & 2) && (m.soc[0x340/4] & 0x70) == 0x20);
    assert(dali_usb_phy_mode(&m.phy,USB_ROLE_DEVICE) == USB_OK);
    assert(dali_usb_phy_pullup(&m.phy,true) == USB_OK && (m.pmic[0xd88] & 0x48) == 0x48);
    assert(dali_usb_phy_mode(&m.phy,USB_ROLE_HOST) == USB_OK && !(m.pmic[0xd88] & 0x48));
    assert(dali_usb_phy_pullup(&m.phy,true) == USB_DISCONNECTED);
    writes = m.writes; m.quiesce_fail = true;
    assert(dali_usb_phy_power_off(&m.phy) == USB_TIMEOUT && m.writes == writes && m.phy.power == DALI_REPEATER_ON);
    m.quiesce_fail = false; time = m.time;
    assert(dali_usb_phy_power_off(&m.phy) == USB_OK && m.time - time == 3101 && m.phy.power == DALI_REPEATER_OFF);
    assert(!(m.soc[0x318/4] & 0x900000) && (m.soc[0x36c/4] & 0x34) == 0x10 && !(m.soc[0x368/4] & 8));
    writes = m.writes; assert(dali_usb_phy_power_off(&m.phy) == USB_OK && m.writes == writes);
    assert(dali_usb_phy_power_on(&m.phy,role == USB_ROLE_HOST ? USB_ROLE_DEVICE : USB_ROLE_HOST) == USB_OK);
    finish(&m);
}
static void guards_and_calibration(void)
{
    struct model m; prepare(&m); unsigned base = m.operations;
    assert(init(&m,0,false,true) == USB_UNSUPPORTED && m.operations == base);
    assert(init(&m,0,true,false) == USB_UNSUPPORTED && m.operations == base);
    for (unsigned n = 1; n <= 5; ++n) {
        prepare(&m); unsigned writes = m.writes; m.fail_operation = m.operations + n;
        assert(init(&m,0,true,true) == USB_IO && !m.phy.initialized && m.writes == writes);
    }
    for (unsigned n = 0; n < 3; ++n) {
        prepare(&m); m.soc[(n == 0 ? 0x100 : n == 1 ? 0x110 : 0x314)/4] |= n == 0 ? 0x1000000 : n == 1 ? 0x100 : 0x8000;
        unsigned writes = m.writes;
        assert(init(&m,0,true,true) == USB_BUSY && !m.phy.initialized && m.writes == writes);
    }
    for (unsigned n = 0; n < 3; ++n) {
        prepare(&m); assert(init(&m,0,true,true) == USB_OK);
        if (!n) m.meter_timeout = true; else m.soc[0x10c/4] = n == 1 ? 0 : 1;
        assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_DEVICE) == (!n ? USB_TIMEOUT : n == 1 ? USB_IO : USB_RANGE));
        assert(m.phy.faulted && !m.phy.fm_owned && !(m.soc[0x100/4] & 0x1000000) && !(m.soc[0x314/4] & 0x8000));
        finish(&m);
    }
    prepare(&m); assert(init(&m,1,true,true) == USB_OK); m.phy.calibration.efuse_1d4 = m.phy.calibration.efuse_1d8 = 0;
    uint32_t term_n = m.soc[0x340/4] & 0x1ff0000, imp_p = m.soc[0x344/4] & 0x1f00;
    assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_HOST) == USB_OK);
    assert((m.soc[0x340/4] & 0x1ff0000) == term_n && (m.soc[0x344/4] & 0x1f00) == imp_p);
    finish(&m);
}
static void faults(void)
{
    struct model m; prepare(&m); assert(init(&m,0,true,true) == USB_OK);
    unsigned base = m.operations;
    assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_DEVICE) == USB_OK);
    unsigned total = m.operations - base, retained_fm = 0;
    for (unsigned n = 1; n <= total; ++n) {
        prepare(&m); assert(init(&m,0,true,true) == USB_OK); m.fail_operation = m.operations + n;
        assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_DEVICE) == USB_IO && m.phy.faulted && m.phy.power == DALI_REPEATER_UNKNOWN);
        unsigned writes = m.writes;
        assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_HOST) == USB_IO && m.writes == writes);
        m.quiesce_fail = true;
        assert(dali_usb_phy_destroy(&m.phy) == USB_TIMEOUT && m.phy.initialized && m.writes == writes);
        m.quiesce_fail = false;
        if (m.phy.fm_owned) {
            ++retained_fm;
            assert(dali_usb_phy_power_off(&m.phy) == USB_OK && !m.phy.fm_owned && m.phy.faulted);
            assert(!(m.soc[0x100/4] & 0x1000000) && !(m.soc[0x110/4] & 0x100) && !(m.soc[0x314/4] & 0x8000));
        }
        finish(&m);
    }
    assert(retained_fm); /* Fault matrix must include failed calibration cleanup. */
}
static int lifecycle_action(struct model *m, unsigned action)
{
    if (!action) return dali_usb_phy_mode(&m->phy,USB_ROLE_HOST);
    if (action == 1) return dali_usb_phy_pullup(&m->phy,true);
    return dali_usb_phy_power_off(&m->phy);
}
static void setup_action(struct model *m, unsigned action)
{
    prepare(m); assert(init(m,0,true,true) == USB_OK);
    assert(dali_usb_phy_power_on(&m->phy,USB_ROLE_DEVICE) == USB_OK);
    if (action != 1) assert(dali_usb_phy_pullup(&m->phy,true) == USB_OK);
}
static void lifecycle_faults(void)
{
    for (unsigned action = 0; action < 3; ++action) {
        struct model m; setup_action(&m,action); unsigned base = m.operations;
        assert(lifecycle_action(&m,action) == USB_OK);
        unsigned total = m.operations - base;
        for (unsigned n = 1; n <= total; ++n) {
            setup_action(&m,action); m.fail_operation = m.operations + n;
            assert(lifecycle_action(&m,action) == USB_IO && m.phy.faulted && m.phy.power == DALI_REPEATER_UNKNOWN);
            unsigned writes = m.writes;
            assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_HOST) == USB_IO && m.writes == writes);
            m.quiesce_fail = true;
            assert(dali_usb_phy_destroy(&m.phy) == USB_TIMEOUT && m.phy.initialized && m.writes == writes);
            m.quiesce_fail = false; finish(&m);
        }
    }
    struct model m; prepare(&m); assert(init(&m,0,true,true) == USB_OK);
    unsigned ops = m.operations; m.quiesce_fail = true;
    assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_DEVICE) == USB_TIMEOUT && m.operations == ops && !m.phy.faulted);
    m.quiesce_fail = false;
    assert(dali_usb_phy_power_on(&m.phy,USB_ROLE_DEVICE) == USB_OK);
    ops = m.operations; m.quiesce_fail = true;
    assert(dali_usb_phy_mode(&m.phy,USB_ROLE_HOST) == USB_TIMEOUT && m.operations == ops && !m.phy.faulted);
    m.quiesce_fail = false;
    m.pmif.faulted = true; m.recover_fail = true;
    assert(dali_usb_phy_destroy(&m.phy) == USB_TIMEOUT && m.phy.initialized);
    m.recover_fail = false; finish(&m);
}
int main(void)
{
    normal(0,USB_ROLE_DEVICE); normal(1,USB_ROLE_HOST); guards_and_calibration(); faults(); lifecycle_faults();
    puts("PASS: production Dali XS-PHY/both-repeaters power, roles, eFuse, checked slew and failures; register/byte/I2C model only");
    return 0;
}
