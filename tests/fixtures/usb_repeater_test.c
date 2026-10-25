/* Production repeater C, modeled PMIC byte API/I2C/GPIO only. Real PMIF has its
 * own transport and combined UEFI fixture; these results do not qualify a port. */
#include "DaliUsbRepeater.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct update_event { uint16_t address; uint8_t mask, value; uint64_t time; };
struct model {
    struct dali_usb_pmif pmif;
    struct dali_mt6379_repeater mt;
    struct dali_nxp_repeater nxp;
    uint8_t regs[0xe00], i2c[0x20];
    struct update_event updates[512];
    uint64_t time, low_time, high_time;
    unsigned operations, fail_operation, writes, update_count, quiesces, recoveries, pin_writes, i2c_reads, i2c_writes;
    bool quiesce_fail, recover_fail, uncertain_write, uncertain_pin, enabled;
};
static int bus_operation(struct model *m)
{
    if (++m->operations == m->fail_operation) { m->pmif.faulted = true; return USB_IO; }
    return USB_OK;
}
int dali_mt6379_read(struct dali_usb_pmif *p, uint16_t address, void *bytes, size_t length)
{
    struct model *m = p->registers.context;
    assert(p->initialized && p->identified && bytes && length && length <= 2 && address + length <= sizeof m->regs);
    if (p->faulted) return USB_IO;
    int result = bus_operation(m);
    if (!result) memcpy(bytes, m->regs + address, length);
    return result;
}
int dali_mt6379_update8(struct dali_usb_pmif *p, uint16_t address, uint8_t mask, uint8_t value)
{
    struct model *m = p->registers.context; uint8_t old = 0;
    assert(!(value & (uint8_t)~mask) && m->update_count < 512);
    m->updates[m->update_count++] = (struct update_event){address,mask,value,m->time};
    int result = dali_mt6379_read(p, address, &old, 1);
    uint8_t next = (uint8_t)((old & (uint8_t)~mask) | value);
    if (!result && next != old) {
        result = bus_operation(m);
        if (!result) {
            m->regs[address] = next; ++m->writes;
            if (m->uncertain_write) { m->uncertain_write = false; p->faulted = p->write_uncertain = p->pending_owned = true; result = USB_IO; }
        }
    }
    return result;
}
int dali_usb_pmif_recover(struct dali_usb_pmif *p)
{
    struct model *m = p->registers.context; ++m->recoveries;
    if (m->recover_fail) return USB_TIMEOUT;
    p->faulted = p->pending_owned = false; return USB_OK;
}
static void delay(void *context, uint32_t us) { ((struct model *)context)->time += us; }
static int quiesce(void *context)
{ struct model *m = context; ++m->quiesces; return m->quiesce_fail ? USB_TIMEOUT : USB_OK; }
static int nxp_read(void *context, uint8_t reg, uint8_t *value)
{
    struct model *m = context; assert(m->enabled && reg < sizeof m->i2c); ++m->i2c_reads;
    int result = bus_operation(m); if (!result) *value = m->i2c[reg]; return result;
}
static int nxp_write(void *context, uint8_t reg, uint8_t value)
{
    struct model *m = context; assert(m->enabled && reg == 2); ++m->i2c_writes;
    int result = bus_operation(m);
    if (!result) {
        m->i2c[reg] = value;
        if (m->uncertain_write) { m->uncertain_write = false; result = USB_IO; }
    }
    return result;
}
static int nxp_recover(void *context)
{ struct model *m = context; ++m->recoveries; return m->recover_fail ? USB_TIMEOUT : USB_OK; }
static int nxp_enable(void *context, bool enable)
{
    struct model *m = context; ++m->pin_writes;
    int result = bus_operation(m);
    if (!result) {
        m->enabled = enable; if (enable) m->high_time = m->time; else m->low_time = m->time;
        if (m->uncertain_pin) { m->uncertain_pin = false; result = USB_IO; }
    }
    return result;
}
static void prepare(struct model *m, uint8_t revision)
{
    memset(m, 0, sizeof *m); m->pmif.initialized = m->pmif.identified = true; m->pmif.revision = revision;
    m->pmif.registers.context = m; m->pmif.registers.delay_us = delay;
    m->regs[0] = (uint8_t)(0x70 | revision);
    m->regs[0xd11] = 0xe0; m->regs[0xd1a] = 0xf5; m->regs[0xd17] = 0xff; m->regs[0xd19] = 0xff;
    m->regs[0xd22] = 2; m->regs[0xd89] = 0x25;
    m->regs[0x323] = 40; m->regs[0x324] = 0x97; m->regs[0x325] = 0x99; m->regs[0x326] = 0x73;
    m->regs[0x327] = 0x2a; m->regs[0x381] = 0x7f; m->regs[0x382] = 0x1f;
    m->i2c[0x15] = 0x32; m->i2c[0x14] = 0x22; m->i2c[2] = 0xcf;
}
static int probe_mt(struct model *m, bool qualified)
{
    struct dali_repeater_guard g = {m, quiesce};
    return dali_mt6379_repeater_probe(&m->mt, &m->pmif, &g, &dali_mt6379_dali_tuning, qualified);
}
static int probe_nxp(struct model *m, bool qualified)
{
    struct dali_repeater_guard g = {m, quiesce};
    struct dali_nxp_repeater_bus bus = {m, nxp_read, nxp_write, nxp_recover, nxp_enable, delay};
    return dali_nxp_repeater_probe(&m->nxp, &bus, &g, qualified);
}
static void test_mt_revision(uint8_t revision)
{
    struct model m; prepare(&m, revision);
    assert(probe_mt(&m, false) == USB_UNSUPPORTED && !m.operations && !m.writes);
    assert(probe_mt(&m, true) == USB_OK && !m.writes && m.mt.intr == 32 && m.mt.term == 5);
    m.regs[0xd88] = 0xff; /* A prior firmware owner may have left D+ attached. */
    assert(dali_mt6379_repeater_power_on(&m.mt) == USB_OK && m.mt.power == DALI_REPEATER_ON && m.time == 590);
    assert(m.regs[0xd88] == 0xb7 && m.mt.role == USB_ROLE_STOPPED);
    assert((m.regs[0xd17] & 3) == 0 && (m.regs[0xd14] & 7) == 7);
    assert((m.regs[0xd89] & 0xc0) == 0x40); /* efuse bit6 retained, PHY bit7 cleared. */
    assert((m.regs[0xd00] & 1) && (m.regs[0xd03] & 3) == 2 && (m.regs[0xd12] & 0x20));
    assert(m.mt.intr == 40 && m.mt.term == 7 && (m.regs[0xd11] & 0xc0) == 0xc0 && (m.regs[0xd1a] & 0xf0) == 0xf0);
    assert(m.regs[0xd22] == 0xff && (m.regs[0xd13] & 0x78) == 0x78 && (m.regs[0xd21] & 0x80));
    assert((m.regs[0xd29] & 31) == 19 && (m.regs[0xd2a] & 31) == 14 && (m.regs[0xd28] & 0xf0) == 0x90);
    assert((m.regs[0xd30] & 127) == 42 && (m.regs[0xd24] & 7) == 1 && (m.regs[0xd24] & 0x70) == 0x30 && m.regs[0xd31] == 0x52);
    if (revision <= 2) assert(!(m.regs[0xd19] & 4) && (m.regs[0xda5] & 0x10) && !m.regs[0xd94]);
    else assert((m.regs[0xd94] & 2) && (m.regs[0xd20] & 4) && (m.regs[0xd8a] & 7) == 3);
    assert(dali_mt6379_repeater_mode(&m.mt, USB_ROLE_DEVICE) == USB_OK);
    assert((m.regs[0xd18] & 0xff) == 0xf0 && (m.regs[0xd1b] & 0x7f) == 0x54);
    assert((m.regs[0xd11] & 63) == 42 && (m.regs[0xd1a] & 15) == 11);
    assert(dali_mt6379_repeater_pullup(&m.mt, true) == USB_OK && (m.regs[0xd88] & 0x48) == 0x48);
    assert(dali_mt6379_repeater_pullup(&m.mt, false) == USB_OK && !(m.regs[0xd88] & 0x48));
    assert(dali_mt6379_repeater_pullup(&m.mt, true) == USB_OK);
    assert(dali_mt6379_repeater_mode(&m.mt, USB_ROLE_HOST) == USB_OK);
    assert(m.regs[0xd88] == 0xb7); /* Direct DEVICE-with-pullup -> HOST detaches first. */
    assert(m.regs[0xd18] == 0xd0 && (m.regs[0xd1b] & 0x7f) == 0x24 && (m.regs[0xd11] & 63) == 43 && (m.regs[0xd1a] & 15) == 5);
    assert(dali_mt6379_repeater_pullup(&m.mt, true) == USB_DISCONNECTED);
    unsigned writes = m.writes;
    m.quiesce_fail = true;
    assert(dali_mt6379_repeater_power_off(&m.mt) == USB_TIMEOUT && m.writes == writes && m.mt.power == DALI_REPEATER_ON);
    m.quiesce_fail = false;
    if (revision >= 3) {
        assert(dali_mt6379_repeater_idle(&m.mt) == USB_OK && m.mt.low_power && m.mt.power == DALI_REPEATER_IDLE);
        uint64_t time = m.time;
        assert(dali_mt6379_repeater_power_on(&m.mt) == USB_OK && !m.mt.low_power && m.time - time == 1060);
        assert(!(m.regs[0xd91] & 1) && (m.regs[0xd27] & 0x0c) == 4 && (m.regs[0xd89] & 0xc0) == 0x40);
        assert(dali_mt6379_repeater_idle(&m.mt) == USB_OK);
    } else assert(dali_mt6379_repeater_idle(&m.mt) == USB_UNSUPPORTED);
    assert(dali_mt6379_repeater_power_off(&m.mt) == USB_OK && m.mt.power == DALI_REPEATER_OFF && !(m.regs[0xd00] & 1) && !(m.regs[0xd12] & 0x20));
    writes = m.writes;
    assert(dali_mt6379_repeater_power_off(&m.mt) == USB_OK && m.writes == writes);
    assert(dali_mt6379_repeater_destroy(&m.mt) == USB_OK && !m.mt.initialized);
    assert(probe_mt(&m, true) == USB_OK); /* LP exit leaves reprobeable normal state. */
}
static void test_mt_lp_and_calibration(void)
{
    struct model m; prepare(&m, 3); assert(probe_mt(&m, true) == USB_OK);
    assert(dali_mt6379_repeater_power_on(&m.mt) == USB_OK);
    assert(dali_mt6379_repeater_idle(&m.mt) == USB_OK);
    m.mt = (struct dali_mt6379_repeater){0}; /* Simulate a new qualified owner of complete LP state. */
    assert(probe_mt(&m, true) == USB_OK && m.mt.low_power && !m.mt.faulted);
    assert(dali_mt6379_repeater_power_off(&m.mt) == USB_OK && !m.mt.low_power && !(m.regs[0xd91] & 1));
    prepare(&m, 3); m.regs[0xd91] = 1;
    assert(probe_mt(&m, true) == USB_UNSUPPORTED && !m.mt.initialized && !m.writes);
    prepare(&m, 3); assert(probe_mt(&m, true) == USB_OK);
    memset(m.regs + 0x323, 0, 5); m.regs[0x381] = m.regs[0x382] = 0;
    m.regs[0xd30] = 0x55; m.regs[0xd24] = 0x45;
    assert(dali_mt6379_repeater_power_on(&m.mt) == USB_OK && m.mt.intr == 32 && m.mt.term == 5);
    assert(m.regs[0xd30] == 0x55 && m.regs[0xd24] == 0x55); /* All-zero trio: retain oscillator/FS/LS, except fixed bias bit4. */
    m.mt.config.host.intr_offset = 100; m.mt.config.host.term_offset = -100;
    assert(dali_mt6379_repeater_mode(&m.mt, USB_ROLE_HOST) == USB_OK && (m.regs[0xd11] & 63) == 32 && (m.regs[0xd1a] & 15) == 5);
}
static void test_mt_failures(void)
{
    struct model m; prepare(&m, 3); assert(probe_mt(&m, true) == USB_OK);
    unsigned base = m.operations;
    assert(dali_mt6379_repeater_power_on(&m.mt) == USB_OK);
    unsigned operations = m.operations - base;
    for (unsigned i = 1; i <= operations; ++i) {
        prepare(&m, 3); assert(probe_mt(&m, true) == USB_OK); m.fail_operation = m.operations + i;
        assert(dali_mt6379_repeater_power_on(&m.mt) == USB_IO && m.mt.faulted && m.mt.power == DALI_REPEATER_UNKNOWN);
        unsigned writes = m.writes, recoveries = m.recoveries;
        assert(dali_mt6379_repeater_power_on(&m.mt) == USB_IO && m.writes == writes);
        m.quiesce_fail = true;
        assert(dali_mt6379_repeater_power_off(&m.mt) == USB_TIMEOUT && m.writes == writes && m.recoveries == recoveries);
        m.quiesce_fail = false;
        assert(dali_mt6379_repeater_power_off(&m.mt) == USB_OK && m.mt.power == DALI_REPEATER_OFF && m.mt.faulted);
        assert(dali_mt6379_repeater_power_on(&m.mt) == USB_IO);
        assert(dali_mt6379_repeater_destroy(&m.mt) == USB_OK);
    }
    prepare(&m, 2); assert(probe_mt(&m, true) == USB_OK); m.uncertain_write = true;
    assert(dali_mt6379_repeater_power_on(&m.mt) == USB_IO && m.pmif.write_uncertain);
    m.recover_fail = true;
    assert(dali_mt6379_repeater_destroy(&m.mt) == USB_TIMEOUT && m.mt.initialized);
    m.recover_fail = false;
    assert(dali_mt6379_repeater_destroy(&m.mt) == USB_OK && m.pmif.write_uncertain);
}
enum mt_action { MODE_DEVICE, MODE_HOST, PULLUP, IDLE, WAKE, OFF, LP_OFF };
static void prepare_action(struct model *m, enum mt_action action)
{
    prepare(m, 3); assert(probe_mt(m, true) == USB_OK);
    assert(dali_mt6379_repeater_power_on(&m->mt) == USB_OK);
    assert(dali_mt6379_repeater_mode(&m->mt, USB_ROLE_DEVICE) == USB_OK);
    if (action != PULLUP) assert(dali_mt6379_repeater_pullup(&m->mt, true) == USB_OK);
    if (action == WAKE || action == LP_OFF) assert(dali_mt6379_repeater_idle(&m->mt) == USB_OK);
}
static int run_action(struct model *m, enum mt_action action)
{
    switch (action) {
    case MODE_DEVICE: return dali_mt6379_repeater_mode(&m->mt, USB_ROLE_DEVICE);
    case MODE_HOST: return dali_mt6379_repeater_mode(&m->mt, USB_ROLE_HOST);
    case PULLUP: return dali_mt6379_repeater_pullup(&m->mt, true);
    case IDLE: return dali_mt6379_repeater_idle(&m->mt);
    case WAKE: return dali_mt6379_repeater_power_on(&m->mt);
    case OFF: case LP_OFF: return dali_mt6379_repeater_power_off(&m->mt);
    }
    return USB_INVALID;
}
static void test_mt_lifecycle_faults(void)
{
    for (enum mt_action action = MODE_DEVICE; action <= LP_OFF; ++action) {
        struct model m; prepare_action(&m, action);
        unsigned base = m.operations;
        m.quiesce_fail = true;
        assert(run_action(&m, action) == USB_TIMEOUT && m.operations == base && !m.mt.faulted);
        m.quiesce_fail = false;
        assert(run_action(&m, action) == USB_OK);
        unsigned operations = m.operations - base;
        assert(operations);
        for (unsigned i = 1; i <= operations; ++i) {
            prepare_action(&m, action); m.fail_operation = m.operations + i;
            assert(run_action(&m, action) == USB_IO && m.mt.faulted && m.mt.power == DALI_REPEATER_UNKNOWN);
            unsigned writes = m.writes;
            assert(dali_mt6379_repeater_power_on(&m.mt) == USB_IO && m.writes == writes);
            m.recover_fail = true;
            assert(dali_mt6379_repeater_destroy(&m.mt) == USB_TIMEOUT && m.mt.initialized && m.writes == writes);
            m.recover_fail = false;
            assert(dali_mt6379_repeater_destroy(&m.mt) == USB_OK && !m.mt.initialized);
            assert(!(m.regs[0xd88] & 0x48) && !(m.regs[0xd00] & 1) && !(m.regs[0xd91] & 1));
        }
    }
}
static void test_nxp(void)
{
    struct model m; prepare(&m, 3);
    assert(probe_nxp(&m, false) == USB_UNSUPPORTED && !m.pin_writes && !m.i2c_reads);
    assert(probe_nxp(&m, true) == USB_OK && m.enabled && m.nxp.identified && m.high_time - m.low_time == 10 && m.time == 4010);
    assert(m.i2c_reads == 2 && !m.i2c_writes);
    assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_HOST) == USB_OK && m.i2c[2] == 0xdf);
    assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_DEVICE) == USB_OK && m.i2c[2] == 0xef);
    assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_STOPPED) == USB_OK && m.i2c[2] == 0xcf);
    m.quiesce_fail = true; unsigned pins = m.pin_writes;
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_TIMEOUT && m.enabled && m.pin_writes == pins);
    m.quiesce_fail = false; m.recover_fail = true;
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_TIMEOUT && m.nxp.initialized && m.enabled);
    m.recover_fail = false;
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !m.nxp.initialized && !m.enabled);
    for (unsigned fail = 1; fail <= 4; ++fail) {
        prepare(&m, 3); m.fail_operation = fail;
        assert(probe_nxp(&m, true) == USB_IO && m.nxp.initialized && m.nxp.faulted);
        assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !m.enabled);
    }
    prepare(&m, 3); m.i2c[0x15] = 0;
    assert(probe_nxp(&m, true) == USB_UNSUPPORTED && m.nxp.faulted && !m.nxp.identified);
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK);
    for (unsigned fail = 1; fail <= 2; ++fail) {
        prepare(&m, 3); assert(probe_nxp(&m, true) == USB_OK); m.fail_operation = m.operations + fail;
        unsigned writes = m.i2c_writes;
        assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_HOST) == USB_IO && m.nxp.faulted);
        assert(m.i2c_writes == writes + fail - 1 && m.i2c[2] == 0xcf);
        assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_HOST) == USB_IO);
        assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !m.enabled);
    }
    prepare(&m, 3); assert(probe_nxp(&m, true) == USB_OK); m.uncertain_write = true;
    assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_HOST) == USB_IO && m.nxp.faulted && m.i2c[2] == 0xdf);
    unsigned writes = m.i2c_writes;
    assert(dali_nxp_repeater_mode(&m.nxp, USB_ROLE_DEVICE) == USB_IO && m.i2c_writes == writes);
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !m.enabled);
    for (unsigned uncertain = 0; uncertain < 2; ++uncertain) {
        prepare(&m, 3); assert(probe_nxp(&m, true) == USB_OK);
        if (uncertain) m.uncertain_pin = true; else m.fail_operation = m.operations + 1;
        assert(dali_nxp_repeater_destroy(&m.nxp) == USB_IO && m.nxp.initialized && m.nxp.faulted);
        assert(m.enabled == !uncertain); /* An error may have asserted reset already. */
        assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !m.nxp.initialized && !m.enabled);
    }
}
int main(void)
{
    test_mt_revision(0); test_mt_revision(2); test_mt_revision(3); test_mt_revision(15);
    test_mt_lp_and_calibration(); test_mt_failures(); test_mt_lifecycle_faults(); test_nxp();
    puts("PASS: production MT6379/NXP USB2 repeater revisions/efuse/roles/LP/stop/faults; byte/I2C/GPIO model only");
    return 0;
}
