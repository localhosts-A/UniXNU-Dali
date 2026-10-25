#include "DaliUsbPmif.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>

#define ACC 0x880u
#define WDATA 0x884u
#define RDATA 0x894u
#define VLD 0x8a4u
#define STA 0x8a8u
#define ERROR (1u << 18)
struct model {
    uint32_t registers[0x900 / 4], command, staged;
    uint8_t pmic[0xe00];
    uint64_t now, complete_at;
    unsigned commands, clears, reads, writes, acquired, released, spacings, fail_read;
    uint32_t fail_write;
    bool locked, busy, hang, pending, nack, command_write, uncertain_command, reserved;
    uint16_t address;
    size_t length;
};
static void advance(struct model *m)
{
    if (!m->pending || m->hang || m->now < m->complete_at) return;
    m->pending = false;
    if (m->command_write) {
        if (!m->nack) for (size_t i = 0; i < m->length; ++i) m->pmic[m->address + i] = (uint8_t)(m->staged >> (8u * (unsigned)i));
        m->registers[STA / 4] = m->nack ? ERROR : 0;
    } else {
        uint32_t value = 0;
        for (size_t i = 0; i < m->length; ++i) value |= (uint32_t)m->pmic[m->address + i] << (8u * (unsigned)i);
        m->registers[RDATA / 4] = value;
        m->registers[STA / 4] = 12u | (m->nack ? ERROR : 0);
    }
}
static int read32(void *context, uint32_t offset, uint32_t *value)
{
    struct model *m = context; assert(m->locked);
    if (++m->reads == m->fail_read) return USB_IO;
    advance(m); *value = m->registers[offset / 4]; return USB_OK;
}
static int write32(void *context, uint32_t offset, uint32_t value)
{
    struct model *m = context; assert(m->locked); ++m->writes;
    if (offset == m->fail_write) { m->fail_write = UINT32_MAX; return USB_IO; }
    if (offset == ACC) {
        assert(((m->registers[STA / 4] >> 1) & 7) == 0 && !m->pending);
        assert((value & 0xdf000000u) == 0xce000000u); /* Type3, SID14, no unknown fields. */
        assert(!(value & 0xfe0000u)); /* At most 2 bytes. */
        m->command = value; m->address = (uint16_t)value;
        m->length = ((value >> 16) & 1) + 1; m->command_write = (value & (1u << 29)) != 0;
        assert(m->address + m->length <= sizeof(m->pmic));
        m->staged = m->registers[WDATA / 4]; m->registers[STA / 4] = 2;
        m->pending = true; m->complete_at = m->now + 10; ++m->commands;
        if (m->uncertain_command) { m->uncertain_command = false; return USB_IO; }
    } else if (offset == VLD) {
        assert(value == 1 && ((m->registers[STA / 4] >> 1) & 7) == 6);
        m->registers[STA / 4] = 0; ++m->clears;
    } else { assert(offset == WDATA); m->registers[offset / 4] = value; }
    return USB_OK;
}
static void barrier(void *context) { assert(((struct model *)context)->locked); }
static void delay(void *context, uint32_t us)
{
    struct model *m = context; assert(m->locked); m->now += us;
    if (us == 8) ++m->spacings;
}
static int acquire(void *context)
{
    struct model *m = context;
    if (m->busy || m->locked) return USB_BUSY;
    m->locked = true; ++m->acquired; return USB_OK;
}
static void release(void *context, bool pending_owned)
{ struct model *m = context; assert(m->locked); m->reserved = pending_owned; m->locked = false; ++m->released; }
static int init(struct model *m, struct dali_usb_pmif *p, bool qualified, uint16_t mask)
{
    struct usb_mmio mmio = {m, sizeof(m->registers), read32, write32, barrier, delay};
    struct dali_pmif_lock lock = {m, acquire, release};
    return dali_usb_pmif_init(p, &mmio, &lock, mask, qualified);
}
static void prepare(struct model *m, struct dali_usb_pmif *p)
{
    memset(m, 0, sizeof(*m)); memset(p, 0, sizeof(*p));
    m->registers[0] = 1; m->fail_write = UINT32_MAX;
    assert(init(m, p, true, 0x85c0) == USB_OK && !m->writes && !m->locked);
    m->pmic[0] = 0x73;
    assert(dali_mt6379_identify(p) == USB_OK && p->identified && p->revision == 3);
    /* Count each test's transactions after the independent identity read. */
    m->commands = m->clears = m->reads = m->writes = m->acquired = m->released = 0;
}
static void test_qualification(void)
{
    struct model m = {0}; struct dali_usb_pmif p = {0}; m.registers[0] = 1;
    assert(init(&m, &p, false, 0x85c0) == USB_UNSUPPORTED && !m.reads && !m.writes);
    assert(init(&m, &p, true, 0xc5c0) == USB_UNSUPPORTED && !m.reads);
    m.registers[0] = 0; assert(init(&m, &p, true, 0x85c0) == USB_UNSUPPORTED && !m.writes);
    m.registers[0] = 1; m.registers[STA / 4] = 12;
    assert(init(&m, &p, true, 0x85c0) == USB_BUSY && !m.writes && !m.clears);
    m.registers[STA / 4] = 0; assert(init(&m, &p, true, 0x85c0) == USB_OK);
    assert(init(&m, &p, true, 0x85c0) == USB_BUSY);
    uint8_t byte = 1;
    assert(dali_mt6379_write(&p, 0x108, &byte, 1) == USB_UNSUPPORTED && !m.commands);
    assert(dali_mt6379_update8(&p, 0x108, 1, 1) == USB_UNSUPPORTED && !m.commands);
    assert(dali_mt6379_identify(&p) == USB_UNSUPPORTED && !p.identified);
    m.pmic[0] = 0x72;
    assert(dali_mt6379_identify(&p) == USB_OK && p.identified && p.revision == 2);
}
static void test_data_and_atomic_update(void)
{
    struct model m; struct dali_usb_pmif p; prepare(&m, &p);
    m.pmic[0x400] = 0xcf; m.pmic[0x401] = 0x29;
    uint8_t bytes[2] = {0};
    assert(dali_mt6379_read(&p, 0x400, bytes, 2) == USB_OK && bytes[0] == 0xcf && bytes[1] == 0x29);
    assert(m.command == 0xce010400 && m.clears == 1 && !p.pending_owned);
    const uint8_t write[2] = {0x34, 0x12};
    assert(dali_mt6379_write(&p, 0x108, write, 2) == USB_OK && m.command == 0xee010108 && !p.pending_owned);
    assert(m.pmic[0x108] == 0x34 && m.pmic[0x109] == 0x12 && m.registers[WDATA / 4] == 0x1234);
    assert(dali_mt6379_read(&p, 0x109, bytes, 1) == USB_OK && bytes[0] == 0x12 && m.spacings == 1);
    unsigned locks = m.acquired, commands = m.commands;
    assert(dali_mt6379_update8(&p, 0x109, 0x0f, 5) == USB_OK && m.pmic[0x109] == 0x15);
    assert(m.acquired == locks + 1 && m.commands == commands + 2 && m.acquired == m.released && !m.locked);
    commands = m.commands;
    assert(dali_mt6379_update8(&p, 0x109, 0x0f, 5) == USB_OK && m.commands == commands + 1);
    assert(dali_mt6379_update8(&p, 0x109, 3, 4) == USB_RANGE);
    assert(dali_mt6379_read(&p, 0xdff, bytes, 1) == USB_OK && m.command == 0xce000dff);
    assert(dali_mt6379_read(&p, 0xdff, bytes, 2) == USB_RANGE);
    assert(dali_mt6379_read(&p, 0xe00, bytes, 1) == USB_RANGE);
    assert(dali_mt6379_read(&p, 0, bytes, 0) == USB_RANGE);
    assert(dali_mt6379_write(&p, 0, bytes, 3) == USB_RANGE);
    assert(dali_mt6379_write(&p, 0, NULL, 1) == USB_RANGE);
}
static void test_foreign_and_locked(void)
{
    struct model m; struct dali_usb_pmif p; prepare(&m, &p);
    uint8_t byte = 0xaa;
    m.registers[STA / 4] = 12;
    assert(dali_mt6379_read(&p, 0x400, &byte, 1) == USB_BUSY && byte == 0xaa && !m.writes && !m.clears && !p.faulted);
    assert(dali_usb_pmif_recover(&p) == USB_BUSY && !m.clears);
    m.registers[STA / 4] = 0; m.busy = true; unsigned reads = m.reads;
    assert(dali_mt6379_read(&p, 0, &byte, 1) == USB_BUSY && m.reads == reads);
    m.busy = false; p.locked = 1;
    assert(dali_mt6379_read(&p, 0, &byte, 1) == USB_BUSY && m.reads == reads);
    p.locked = 0; assert(m.acquired == m.released);
}
static void test_nack_and_late_response(void)
{
    struct model m; struct dali_usb_pmif p; prepare(&m, &p);
    uint8_t bytes[2] = {0xa5, 0x5a}; m.nack = true;
    assert(dali_mt6379_read(&p, 0x400, bytes, 2) == USB_IO && bytes[0] == 0xa5 && bytes[1] == 0x5a);
    assert(p.faulted && !p.pending_owned && m.clears == 1);
    assert(dali_usb_pmif_recover(&p) == USB_OK && !p.faulted);
    m.nack = false; m.hang = true;
    uint64_t before = m.now;
    assert(dali_mt6379_read(&p, 0x400, bytes, 2) == USB_TIMEOUT && m.now - before == 100000);
    assert(p.faulted && p.pending_owned && p.pending_read && m.reserved && m.clears == 1 && bytes[0] == 0xa5);
    unsigned commands = m.commands, reads = m.reads;
    assert(dali_mt6379_read(&p, 0x400, bytes, 2) == USB_IO && m.commands == commands && m.reads == reads);
    m.hang = false;
    assert(dali_usb_pmif_recover(&p) == USB_OK && !p.faulted && !p.pending_owned && !m.reserved && m.clears == 2 && m.commands == commands);
    assert(bytes[0] == 0xa5 && bytes[1] == 0x5a); /* Recovery discards, never returns a stale response. */
    assert(dali_mt6379_read(&p, 0x400, bytes, 2) == USB_OK);
    assert(m.acquired == m.released);
}
static void test_write_uncertainty_and_io_failures(void)
{
    struct model m; struct dali_usb_pmif p; prepare(&m, &p);
    uint8_t byte = 1; m.uncertain_command = true;
    assert(dali_mt6379_write(&p, 0x108, &byte, 1) == USB_IO && p.faulted && p.pending_owned && p.write_uncertain && m.commands == 1);
    assert(dali_usb_pmif_recover(&p) == USB_OK && m.pmic[0x108] == 1 && m.commands == 1 && p.write_uncertain);
    m.nack = true;
    assert(dali_mt6379_write(&p, 0x108, &byte, 1) == USB_IO && !p.pending_owned && p.write_uncertain);
    assert(dali_usb_pmif_recover(&p) == USB_OK);
    m.nack = false; m.hang = true;
    assert(dali_mt6379_write(&p, 0x108, &byte, 1) == USB_TIMEOUT && p.pending_owned && !p.pending_read);
    assert(dali_usb_pmif_recover(&p) == USB_TIMEOUT && p.pending_owned && m.reserved);
    m.hang = false;
    assert(dali_usb_pmif_recover(&p) == USB_OK && !p.pending_owned && !m.reserved && p.write_uncertain);
    prepare(&m, &p); m.fail_write = WDATA;
    assert(dali_mt6379_write(&p, 0x108, &byte, 1) == USB_IO && !m.commands && !p.pending_owned && !p.write_uncertain);
    assert(dali_usb_pmif_recover(&p) == USB_OK);
    prepare(&m, &p); m.fail_write = VLD;
    assert(dali_mt6379_read(&p, 0x400, &byte, 1) == USB_IO && p.pending_owned && !m.clears && byte == 1);
    assert(dali_usb_pmif_recover(&p) == USB_OK && m.clears == 1);
    for (unsigned step = 1; step < 9; ++step) {
        prepare(&m, &p); m.fail_read = m.reads + step; byte = 0xa5;
        int result = dali_mt6379_read(&p, 0x400, &byte, 1);
        if (m.reads >= m.fail_read) {
            assert(result == USB_IO && p.faulted && byte == 0xa5);
            m.fail_read = 0; assert(dali_usb_pmif_recover(&p) == USB_OK);
        }
        assert(m.acquired == m.released);
    }
    prepare(&m, &p); m.nack = true;
    assert(dali_mt6379_update8(&p, 0x108, 1, 1) == USB_IO && m.commands == 1 && !m.command_write);
}
struct init_thread { struct model *model; struct dali_usb_pmif *pmif; int result; };
static void *initialize_concurrently(void *context)
{
    struct init_thread *t = context;
    t->result = init(t->model, t->pmif, true, 0x85c0); return NULL;
}
static void test_concurrent_initialization(void)
{
    for (unsigned i = 0; i < 100; ++i) {
        struct model m = {0}; struct dali_usb_pmif p = {0}; m.registers[0] = 1;
        struct init_thread a = {&m, &p, USB_IO}, b = {&m, &p, USB_IO};
        pthread_t first, second;
        assert(!pthread_create(&first, NULL, initialize_concurrently, &a));
        assert(!pthread_create(&second, NULL, initialize_concurrently, &b));
        assert(!pthread_join(first, NULL) && !pthread_join(second, NULL));
        assert((a.result == USB_OK && b.result == USB_BUSY) || (b.result == USB_OK && a.result == USB_BUSY));
        assert(p.initialized && !p.locked && m.acquired == 1 && m.released == 1 && !m.writes);
    }
}
int main(void)
{
    test_qualification(); test_data_and_atomic_update(); test_foreign_and_locked();
    test_nack_and_late_response(); test_write_uncertainty_and_io_failures();
    test_concurrent_initialization();
    puts("PASS: MT6991 SID14 PMIF production transport/state/spacing/error/ownership/recovery tests; model only");
    return 0;
}
