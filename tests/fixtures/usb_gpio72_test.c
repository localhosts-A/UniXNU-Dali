/* Actual GPIO72 and NXP C; MMIO registers and I2C bytes are modeled only. */
#include "DaliUsbGpio.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model {
    struct dali_usb_gpio72 gpio;
    struct dali_nxp_repeater nxp;
    uint32_t mode, direction, output;
    uint32_t events[32];
    unsigned operations, fail_operation, writes, barriers, event_count, quiesces, i2c_reads;
    uint64_t time, low_time, high_time;
    bool quiesce_fail, applied_error, ignore_output, ignore_direction;
};
static int operation(struct model *m) { return ++m->operations == m->fail_operation ? USB_IO : USB_OK; }
static int read32(void *context, uint32_t reg, uint32_t *value)
{
    struct model *m = context;
    int result = operation(m); if (result) return result;
    switch (reg) {
    case 0x390: *value = m->mode; break;
    case 0x20: *value = m->direction; break;
    case 0x120: *value = m->output; break;
    default: assert(0 && "out-of-scope GPIO read");
    }
    return USB_OK;
}
static int write32(void *context, uint32_t reg, uint32_t value)
{
    struct model *m = context;
    assert(value == 0x100 && m->barriers == m->writes * 2 + 1 && m->event_count < 32);
    ++m->writes; m->events[m->event_count++] = reg;
    int result = operation(m); if (result) return result;
    switch (reg) {
    case 0x124: if (!m->ignore_output) m->output |= value; m->high_time = m->time; break;
    case 0x128: if (!m->ignore_output) m->output &= ~value; m->low_time = m->time; break;
    case 0x24: if (!m->ignore_direction) m->direction |= value; break;
    default: assert(0 && "out-of-scope GPIO write");
    }
    if (m->applied_error) { m->applied_error = false; return USB_IO; }
    return USB_OK;
}
static void barrier(void *context) { ++((struct model *)context)->barriers; }
static void delay(void *context, uint32_t us) { ((struct model *)context)->time += us; }
static int quiesce(void *context)
{ struct model *m = context; ++m->quiesces; return m->quiesce_fail ? USB_TIMEOUT : USB_OK; }
static void prepare(struct model *m)
{
    memset(m, 0, sizeof *m);
    m->mode = 0x76543210; m->direction = 0xdeadbeef & ~0x100u; m->output = 0x12345778;
}
static int init(struct model *m, bool qualified)
{
    struct usb_mmio mmio = {m, 0x1000, read32, write32, barrier, delay};
    struct dali_repeater_guard g = {m, quiesce};
    return dali_usb_gpio72_init(&m->gpio, &mmio, &g, qualified);
}
static int enable(void *context, bool value)
{ return dali_usb_gpio72_set(&((struct model *)context)->gpio, value); }
static int i2c_read(void *context, uint8_t reg, uint8_t *value)
{
    struct model *m = context; ++m->i2c_reads;
    assert(m->gpio.level_known && m->gpio.enabled && (m->direction & m->output & 0x100));
    assert(m->time - m->high_time >= 4000);
    assert(reg == 0x15 || reg == 0x14); *value = reg == 0x15 ? 0x32 : 0x22; return USB_OK;
}
static int i2c_write(void *context, uint8_t reg, uint8_t value)
{ (void)context; (void)reg; (void)value; assert(0 && "unused I2C mode path"); return USB_IO; }
static int recover(void *context) { (void)context; return USB_OK; }
static void test_nxp_composition(void)
{
    struct model m; prepare(&m); assert(init(&m, true) == USB_OK && !m.writes && !m.gpio.level_known);
    struct dali_nxp_repeater_bus bus = {&m, i2c_read, i2c_write, recover, enable, delay};
    struct dali_repeater_guard g = {&m, quiesce};
    uint32_t old_direction = m.direction, old_output = m.output;
    assert(dali_nxp_repeater_probe(&m.nxp, &bus, &g, true) == USB_OK);
    assert(m.high_time - m.low_time == 10 && m.time == 4010 && m.i2c_reads == 2);
    assert(m.gpio.enabled && m.gpio.level_known && !m.gpio.faulted);
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !m.gpio.enabled && m.gpio.level_known);
    assert(dali_usb_gpio72_destroy(&m.gpio) == USB_OK && !m.gpio.initialized);
    assert(m.mode == 0x76543210 && (m.direction & ~0x100u) == old_direction &&
        (m.output & ~0x100u) == (old_output & ~0x100u) && !(m.output & 0x100));
    const uint32_t order[] = {0x128,0x24,0x124,0x24,0x128,0x24,0x128,0x24};
    assert(m.event_count == sizeof order / sizeof order[0] && !memcmp(m.events, order, sizeof order));
    assert(m.barriers == m.writes * 2);
}
static void test_init_guards(void)
{
    struct model m; prepare(&m);
    assert(init(&m, false) == USB_UNSUPPORTED && !m.operations && !m.writes);
    m.mode |= 1;
    assert(init(&m, true) == USB_UNSUPPORTED && !m.gpio.initialized && !m.writes);
    for (unsigned i = 1; i <= 3; ++i) {
        prepare(&m); m.fail_operation = i;
        assert(init(&m, true) == USB_IO && !m.gpio.initialized && !m.writes);
    }
    prepare(&m); assert(init(&m, true) == USB_OK);
    assert(init(&m, true) == USB_BUSY);
    m.quiesce_fail = true; unsigned ops = m.operations;
    assert(dali_usb_gpio72_set(&m.gpio, false) == USB_TIMEOUT && m.operations == ops && !m.writes);
    assert(dali_usb_gpio72_destroy(&m.gpio) == USB_TIMEOUT && m.gpio.initialized);
    m.quiesce_fail = false; m.mode |= 1;
    assert(dali_usb_gpio72_set(&m.gpio, true) == USB_UNSUPPORTED && !m.writes && m.gpio.faulted);
    assert(dali_usb_gpio72_destroy(&m.gpio) == USB_UNSUPPORTED && m.gpio.initialized && !m.writes);
}
static void test_faults(void)
{
    for (unsigned i = 1; i <= 6; ++i) {
        struct model m; prepare(&m); assert(init(&m, true) == USB_OK);
        m.fail_operation = m.operations + i;
        assert(dali_usb_gpio72_set(&m.gpio, true) == USB_IO && m.gpio.faulted && !m.gpio.level_known);
        unsigned writes = m.writes;
        assert(dali_usb_gpio72_set(&m.gpio, true) == USB_IO && m.writes == writes);
        m.quiesce_fail = true;
        assert(dali_usb_gpio72_destroy(&m.gpio) == USB_TIMEOUT && m.gpio.initialized && m.writes == writes);
        m.quiesce_fail = false;
        assert(dali_usb_gpio72_set(&m.gpio, false) == USB_OK && m.gpio.faulted && m.gpio.level_known && !m.gpio.enabled);
        assert(dali_usb_gpio72_set(&m.gpio, true) == USB_IO);
        assert(dali_usb_gpio72_destroy(&m.gpio) == USB_OK && !m.gpio.initialized && !(m.output & 0x100));
    }
    struct model m; prepare(&m); assert(init(&m, true) == USB_OK); m.applied_error = true;
    assert(dali_usb_gpio72_set(&m.gpio, false) == USB_IO && !(m.output & 0x100) && !m.gpio.level_known);
    assert(dali_usb_gpio72_destroy(&m.gpio) == USB_OK);
    prepare(&m); assert(init(&m, true) == USB_OK); m.ignore_output = true;
    assert(dali_usb_gpio72_set(&m.gpio, false) == USB_IO && m.gpio.faulted);
    m.ignore_output = false; assert(dali_usb_gpio72_destroy(&m.gpio) == USB_OK);
    prepare(&m); assert(init(&m, true) == USB_OK); m.ignore_direction = true;
    assert(dali_usb_gpio72_set(&m.gpio, true) == USB_IO && m.gpio.faulted);
    m.ignore_direction = false; assert(dali_usb_gpio72_destroy(&m.gpio) == USB_OK);
}
int main(void)
{
    test_nxp_composition(); test_init_guards(); test_faults();
    puts("PASS: production GPIO72/NXP composed reset, neighbors, alias ordering, guards and faults; register/I2C model only");
    return 0;
}
