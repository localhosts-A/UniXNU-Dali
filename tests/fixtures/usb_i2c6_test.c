/* Actual I2C6/GPIO72/NXP C; hardware register/event model, no physical I/O. */
#include "DaliUsbI2c.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model {
    struct dali_usb_i2c6 i2c;
    struct dali_usb_gpio72 gpio;
    struct dali_nxp_repeater nxp;
    struct dali_nxp_io binding;
    uint32_t c[0x100/4], d[0x20/4], mode, direction, output;
    uint8_t chip[256], tx[2], rx;
    unsigned tx_count, rx_count, operations, fail_operation, writes, starts, resets, polls, gpio_writes, complete_after;
    uint64_t time, low_time, high_time;
    uint32_t inject_status, drop_reg, start_command;
    bool running, finished, timeout, stuck_start, short_read, late_error, applied_error, quiesce_fail, fail_slave_write;
};
static int op(struct model *m) { return ++m->operations == m->fail_operation ? USB_IO : USB_OK; }
static void barrier(void *c) { (void)c; }
static void delay(void *c, uint32_t us) { ((struct model *)c)->time += us; }
static int quiesce(void *c) { return ((struct model *)c)->quiesce_fail ? USB_TIMEOUT : USB_OK; }
static void complete(struct model *m)
{
    assert(m->c[0x94/4] == 0x9e && !(m->c[0x10/4] & 0x304) && !m->c[8/4]);
    if (m->c[0x10/4] & 0x10) {
        assert(m->tx_count == 1 && m->c[0x14/4] == 1 && m->c[0x44/4] == 1 && m->c[0x18/4] == 2);
        assert((m->c[0x10/4] & 0x12) == 0x12 && m->start_command == 1);
        m->rx = m->chip[m->tx[0]]; m->rx_count = m->short_read ? 0 : 1;
    } else {
        assert(m->tx_count == 2 && m->c[0x14/4] == 2 && m->c[0x18/4] == 1 && m->start_command == 0x4001);
        m->chip[m->tx[0]] = m->tx[1];
    }
    m->tx_count = 0; m->running = false; m->finished = true; m->c[0xc/4] |= 1;
    if (!m->stuck_start) m->c[0x24/4] &= ~1u;
}
static int ctrl_read(void *context, uint32_t reg, uint32_t *value)
{
    struct model *m = context; assert(reg && reg < 0x100);
    int result = op(m); if (result) return result;
    if (reg == 0xc && m->running) {
        if (m->fail_slave_write && !(m->c[0x10/4] & 0x10)) m->c[reg/4] |= 2;
        else if (m->inject_status) m->c[reg/4] |= m->inject_status;
        else if (!m->timeout && ++m->polls >= m->complete_after) complete(m);
    }
    *value = reg == 0xf4 ? m->tx_count + m->rx_count : m->c[reg/4];
    return USB_OK;
}
static int ctrl_write(void *context, uint32_t reg, uint32_t value)
{
    struct model *m = context; assert(reg && reg < 0x100); ++m->writes;
    int result = op(m); if (result && !m->applied_error) return result;
    if (reg == m->drop_reg) return result;
    if (reg == 0xc) {
        assert(!(value & ~0x21fu)); m->c[reg/4] &= ~value;
        if (m->finished && (value & 1) && m->late_error) m->c[reg/4] |= 2;
    } else if (reg == 0x38) {
        assert(value == 1); m->tx_count = m->rx_count = 0;
    } else {
        m->c[reg/4] = value;
        if (reg == 0x24 && (value & 1)) { ++m->starts; m->start_command = value; m->running = true; m->finished = false; m->polls = 0; }
        if (reg == 0x50 && value) {
            assert(value == 0x21); ++m->resets; m->c[0x24/4] = m->c[0xc/4] = 0;
            m->running = m->finished = false; m->tx_count = m->rx_count = 0;
        }
    }
    return result;
}
static int dma_read(void *context, uint32_t reg, uint32_t *value)
{ struct model *m = context; assert(reg == 8); int result = op(m); if (!result) *value = m->d[reg/4]; return result; }
static int dma_write(void *context, uint32_t reg, uint32_t value)
{
    struct model *m = context; assert(reg == 0xc || (reg == 0x1c && value == 0x1000));
    int result = op(m); ++m->writes; if (!result || m->applied_error) m->d[reg/4] = value; return result;
}
static int data_read(void *context, uint32_t reg, uint8_t *value)
{
    struct model *m = context; assert(!reg && m->rx_count == 1 && m->finished);
    int result = op(m); if (!result) { *value = m->rx; --m->rx_count; } return result;
}
static int data_write(void *context, uint32_t reg, uint8_t value)
{
    struct model *m = context; assert(!reg && m->tx_count < 2 && !m->running);
    int result = op(m); if (!result || m->applied_error) m->tx[m->tx_count++] = value; return result;
}
static int gpio_read(void *context, uint32_t reg, uint32_t *value)
{
    struct model *m = context;
    switch (reg) { case 0x390: *value = m->mode; break; case 0x20: *value = m->direction; break;
    case 0x120: *value = m->output; break; default: assert(0); }
    return USB_OK;
}
static int gpio_write(void *context, uint32_t reg, uint32_t value)
{
    struct model *m = context; assert(value == 0x100); ++m->gpio_writes;
    switch (reg) { case 0x124: m->output |= value; m->high_time = m->time; break;
    case 0x128: m->output &= ~value; m->low_time = m->time; break;
    case 0x24: m->direction |= value; break; default: assert(0); }
    return USB_OK;
}
static void prepare(struct model *m)
{
    memset(m, 0, sizeof *m); m->drop_reg = 0xffffffff; m->complete_after = 2;
    m->chip[0x15] = 0x32; m->chip[0x14] = 0x22; m->chip[2] = 0xcf;
}
static int init(struct model *m, bool qualified)
{
    struct dali_i2c6_io io = {{m,0x20000,ctrl_read,ctrl_write,barrier,delay},
        {m,0x10000,dma_read,dma_write,barrier,delay},data_read,data_write};
    struct dali_repeater_guard g = {m,quiesce};
    return dali_usb_i2c6_init(&m->i2c, &io, &g, 130000000, qualified);
}
static void timing(void)
{
    struct dali_i2c6_timing t;
    assert(dali_i2c6_timing_400k(26000000,&t) == USB_OK && t.high == 28 && t.low == 36 && t.ext == 0x2001 && !t.divider);
    assert(dali_i2c6_timing_400k(130000000,&t) == USB_OK && t.high == 0x22f && t.low == 0x199 && t.ext == 0xa201 && !t.divider);
    assert(dali_i2c6_timing_400k(208000000,&t) == USB_OK && t.high == 0x226 && t.low == 0xaf && t.ext == 0x8201 && t.divider == 0x101);
    for (uint32_t hz = 4000000; hz <= 1000000000; hz += 1000003) {
        assert(dali_i2c6_timing_400k(hz,&t) == USB_OK);
        uint32_t h = ((t.high >> 8) + 1) * ((t.high & 63) + 1);
        uint32_t l = ((t.low >> 6) + 1) * ((t.low & 63) + 1);
        uint32_t div = (t.divider & 31) + 1;
        assert((uint64_t)(h+l) * div * 400000 >= hz && h*1000/(h+l) >= 410 && h*1000/(h+l) < 490);
        assert((t.high >> 8) < 8 && (t.low >> 6) < 8 && (t.ext >> 8) <= 255 && div <= 32);
    }
    assert(dali_i2c6_timing_400k(0,&t) == USB_INVALID);
    assert(dali_i2c6_timing_400k(UINT32_MAX,&t) == USB_OK); /* Wide intermediate arithmetic. */
}
static void composed(bool inject_fault)
{
    struct model m; prepare(&m); assert(init(&m,true) == USB_OK && m.resets == 1 && !m.i2c.dirty);
    struct usb_mmio gpio = {&m,0x1000,gpio_read,gpio_write,barrier,delay};
    struct dali_repeater_guard guard = {&m,quiesce};
    struct dali_nxp_repeater_bus bus;
    assert(dali_usb_gpio72_init(&m.gpio,&gpio,&guard,true) == USB_OK);
    assert(dali_nxp_io_bind(&m.binding,&m.i2c,&m.gpio,&bus) == USB_OK);
    assert(dali_nxp_repeater_probe(&m.nxp,&bus,&guard,true) == USB_OK && m.nxp.identified);
    assert(m.high_time - m.low_time == 10 && m.time - m.high_time >= 4000);
    if (inject_fault) {
        m.fail_slave_write = true;
        assert(dali_nxp_repeater_mode(&m.nxp,USB_ROLE_HOST) == USB_IO && m.nxp.faulted && m.i2c.write_uncertain);
        unsigned starts = m.starts;
        assert(dali_nxp_repeater_mode(&m.nxp,USB_ROLE_DEVICE) == USB_IO && m.starts == starts);
    } else {
        assert(dali_nxp_repeater_mode(&m.nxp,USB_ROLE_HOST) == USB_OK && m.chip[2] == 0xdf);
        assert(dali_nxp_repeater_mode(&m.nxp,USB_ROLE_DEVICE) == USB_OK && m.chip[2] == 0xef);
    }
    assert(dali_nxp_repeater_destroy(&m.nxp) == USB_OK && !(m.output & 0x100));
    if (inject_fault) assert(m.i2c.faulted && m.i2c.write_uncertain && !m.i2c.pending_owned);
    assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK && !m.i2c.initialized);
    assert(dali_usb_gpio72_destroy(&m.gpio) == USB_OK && !m.gpio.initialized);
}
static void guards_and_init_faults(void)
{
    struct model m; prepare(&m); assert(init(&m,false) == USB_UNSUPPORTED && !m.operations);
    for (unsigned which = 0; which < 6; ++which) {
        prepare(&m);
        if (!which) m.d[8/4] = 1; else if (which == 1) m.c[0x8c/4] = 2;
        else if (which == 2) m.c[0x24/4] = 1; else if (which == 3) m.c[8/4] = 1;
        else if (which == 4) m.c[0xc/4] = 1; else m.rx_count = 1;
        assert(init(&m,true) == (which == 1 ? USB_UNSUPPORTED : USB_BUSY) && !m.i2c.initialized && !m.writes);
    }
    prepare(&m); m.quiesce_fail = true;
    assert(init(&m,true) == USB_TIMEOUT && !m.i2c.initialized && !m.writes);
    prepare(&m); assert(init(&m,true) == USB_OK); unsigned total = m.operations;
    for (unsigned n = 1; n <= total; ++n) {
        prepare(&m); m.fail_operation = n;
        assert(init(&m,true) == USB_IO);
        if (m.i2c.initialized) {
            assert(m.i2c.faulted); assert(dali_usb_i2c6_recover(&m.i2c) == USB_OK && m.i2c.faulted);
            assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK);
        } else assert(!m.writes);
    }
    prepare(&m); m.drop_reg = 0x20;
    assert(init(&m,true) == USB_IO && m.i2c.initialized && m.i2c.faulted);
    m.drop_reg = 0xffffffff; assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK);
}
static void runtime_foreign_state(void)
{
    for (unsigned which = 0; which < 6; ++which) {
        struct model m; prepare(&m); assert(init(&m,true) == USB_OK);
        if (!which) m.d[8/4] = 1; else if (which == 1) m.c[0x8c/4] = 2;
        else if (which == 2) m.c[0x24/4] = 1; else if (which == 3) m.c[8/4] = 1;
        else if (which == 4) m.c[0xc/4] = 1; else m.rx_count = 1;
        uint32_t controller[0x100/4], dma[0x20/4];
        memcpy(controller,m.c,sizeof controller); memcpy(dma,m.d,sizeof dma);
        unsigned writes = m.writes, starts = m.starts, resets = m.resets;
        uint8_t value = 0xaa; int expected = which == 1 ? USB_UNSUPPORTED : USB_BUSY;
        assert(dali_usb_i2c6_read8(&m.i2c,0x15,&value) == expected && value == 0xaa);
        assert(dali_usb_i2c6_write8(&m.i2c,2,0xdf) == expected);
        assert(dali_usb_i2c6_destroy(&m.i2c) == expected && m.i2c.initialized);
        assert(m.writes == writes && m.starts == starts && m.resets == resets && !m.i2c.dirty && !m.i2c.pending_owned);
        assert(!memcmp(controller,m.c,sizeof controller) && !memcmp(dma,m.d,sizeof dma));
    }
}
static void transaction_faults(bool reading)
{
    struct model m; prepare(&m); assert(init(&m,true) == USB_OK); unsigned base = m.operations;
    uint8_t value = 0xaa;
    assert((reading ? dali_usb_i2c6_read8(&m.i2c,0x15,&value) : dali_usb_i2c6_write8(&m.i2c,2,0xdf)) == USB_OK);
    unsigned total = m.operations - base;
    for (unsigned n = 1; n <= total; ++n) {
        prepare(&m); assert(init(&m,true) == USB_OK); value = 0xaa; m.fail_operation = m.operations + n;
        assert((reading ? dali_usb_i2c6_read8(&m.i2c,0x15,&value) : dali_usb_i2c6_write8(&m.i2c,2,0xdf)) == USB_IO);
        assert(value == 0xaa);
        if (m.i2c.dirty) {
            assert(m.i2c.faulted); unsigned starts = m.starts, writes = m.writes;
            assert(dali_usb_i2c6_write8(&m.i2c,2,0xef) == USB_IO && m.starts == starts && m.writes == writes);
            m.quiesce_fail = true;
            assert(dali_usb_i2c6_destroy(&m.i2c) == USB_TIMEOUT && m.i2c.initialized && m.writes == writes);
            m.quiesce_fail = false;
        }
        assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK && !m.i2c.initialized);
    }
}
static void bus_faults(void)
{
    struct model m; uint8_t value;
    const uint32_t errors[] = {2,4,8,0x10,0x200};
    for (unsigned n = 0; n < sizeof errors / sizeof errors[0]; ++n) {
        prepare(&m); assert(init(&m,true) == USB_OK); m.inject_status = errors[n];
        assert(dali_usb_i2c6_write8(&m.i2c,2,0xdf) == USB_IO && m.i2c.pending_owned && m.i2c.write_uncertain);
        assert(dali_usb_i2c6_recover(&m.i2c) == USB_OK && m.i2c.write_uncertain && m.i2c.faulted && !m.i2c.pending_owned);
    }
    prepare(&m); assert(init(&m,true) == USB_OK); m.timeout = true; uint64_t start = m.time;
    assert(dali_usb_i2c6_write8(&m.i2c,2,0xdf) == USB_TIMEOUT && m.time - start == 5000 && m.i2c.write_uncertain);
    m.d[8/4] = 1; unsigned resets = m.resets;
    assert(dali_usb_i2c6_destroy(&m.i2c) == USB_BUSY && m.i2c.initialized && m.resets == resets);
    m.d[8/4] = 0; assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK);
    for (unsigned n = 0; n < 3; ++n) {
        prepare(&m); assert(init(&m,true) == USB_OK); value = 0xaa;
        if (!n) m.stuck_start = true; else if (n == 1) m.short_read = true; else m.late_error = true;
        assert(dali_usb_i2c6_read8(&m.i2c,0x15,&value) == (!n ? USB_TIMEOUT : (n == 1 ? USB_IO : USB_BUSY)) && value == 0xaa && m.i2c.faulted);
        assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK);
    }
    prepare(&m); assert(init(&m,true) == USB_OK); m.complete_after = 1001; m.stuck_start = true;
    value = 0xaa; start = m.time;
    assert(dali_usb_i2c6_read8(&m.i2c,0x15,&value) == USB_TIMEOUT && value == 0xaa && m.time - start == 5100);
    assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK);
    /* Error returned after START may already have put bytes on the wire. */
    prepare(&m); assert(init(&m,true) == USB_OK); unsigned base = m.operations;
    assert(dali_usb_i2c6_write8(&m.i2c,2,0xdf) == USB_OK);
    /* Preflight6 + config8 + DATA2 + START is operation17. */
    prepare(&m); assert(init(&m,true) == USB_OK); assert(m.operations == base);
    m.fail_operation = m.operations + 17; m.applied_error = true;
    assert(dali_usb_i2c6_write8(&m.i2c,2,0xdf) == USB_IO && m.running && m.i2c.write_uncertain);
    assert(dali_usb_i2c6_destroy(&m.i2c) == USB_OK);
}
int main(void)
{
    timing(); composed(false); composed(true); guards_and_init_faults(); runtime_foreign_state(); transaction_faults(true); transaction_faults(false); bus_faults();
    puts("PASS: production MT6991 I2C6/GPIO72/NXP timing, FIFO WRRD, native bytes, ownership and faults; register model only");
    return 0;
}
