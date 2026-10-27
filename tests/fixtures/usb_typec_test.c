/* Production TCPC/VBUS code against a byte-register PMIC model. The PMIF bus
 * seam is modeled here; its real SWINF implementation has a separate fixture. */
#include "DaliUsbTypec.h"
#include "DaliUsbRoleWorker.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

struct write_event { uint16_t address; uint8_t bytes[2], length; uint64_t time; };
struct model {
    struct dali_usb_pmif pmif;
    struct dali_usb_typec typec;
    struct dali_usb_vbus vbus;
    uint8_t regs[0xe00];
    struct write_event events[512];
    unsigned event_count, operations, fail_operation, quiesces, connector_checks, recoveries, cc_reads;
    uint64_t time, role_time, look_time;
    bool quiesce_fail, connector_fail, recover_fail, auto_vbus, external_vbus, tm_open;
    bool fail_after_write, corrupt_voltage, unstable_snapshot, busy_next;
    unsigned pass_stage;
};
enum role_event {
    ROLE_STOP_SUBMISSIONS, ROLE_SYNC_STORAGE, ROLE_CANCEL_REQUESTS, ROLE_STOP_DMA,
    ROLE_SELECT_STOPPED, ROLE_SELECT_DEVICE, ROLE_SELECT_HOST, ROLE_START_DEVICE,
    ROLE_START_HOST
};
struct role_model {
    struct model *usb;
    enum role_event events[64];
    unsigned event_count;
    int stop_submissions_result, sync_result, cancel_result, stop_dma_result;
    int select_stopped_result, select_device_result, select_host_result;
    int start_device_result, start_host_result;
    bool dma_stopped;
    bool fail_vbus_disable, fail_typec_stop;
};
static struct model *model_of(struct dali_usb_pmif *p) { return p->registers.context; }
static void set_voltage(struct model *m, uint16_t millivolts, bool present, bool safe)
{
    uint16_t value = millivolts / 25;
    m->regs[0x470] = (uint8_t)value; m->regs[0x471] = (uint8_t)(value >> 8);
    m->regs[0x49f] = (uint8_t)((present ? 0x20 : 0) | (safe ? 2 : 0));
}
static int begin_bus(struct dali_usb_pmif *p, uint16_t address, const void *bytes, size_t length)
{
    struct model *m = model_of(p);
    assert(p->initialized && p->identified && length && length <= 2 && bytes && address + length <= sizeof(m->regs));
    if (p->faulted) return USB_IO;
    if (m->busy_next) { m->busy_next = false; return USB_BUSY; }
    if (++m->operations == m->fail_operation) { p->faulted = true; return USB_IO; }
    return USB_OK;
}
int dali_mt6379_read(struct dali_usb_pmif *p, uint16_t address, void *bytes, size_t length)
{
    int result = begin_bus(p, address, bytes, length);
    if (!result) {
        struct model *m = model_of(p);
        memcpy(bytes, m->regs + address, length);
        if (address == 0x41d && (++m->cc_reads % 2) == 0 && m->unstable_snapshot) {
            *(uint8_t *)bytes ^= 1; m->unstable_snapshot = false;
        }
    }
    return result;
}
int dali_mt6379_write(struct dali_usb_pmif *p, uint16_t address, const void *bytes, size_t length)
{
    struct model *m = model_of(p);
    int result = begin_bus(p, address, bytes, length);
    if (result) return result;
    assert(m->event_count < 512);
    struct write_event *event = &m->events[m->event_count++];
    event->address = address; event->length = (uint8_t)length; event->time = m->time;
    memcpy(event->bytes, bytes, length); memcpy(m->regs + address, bytes, length);
    if (address == 0x4b0) { /* TCPC reset leaves read-only identity and external bus untouched. */
        memset(m->regs + 0x410, 0, 0xf0); set_voltage(m, m->external_vbus ? 5000 : 0, m->external_vbus, !m->external_vbus);
    }
    if (address == 0x41a) m->role_time = m->time;
    if (address == 0x423) { assert(m->regs[0x423] == 0x99); m->look_time = m->time; }
    if (address == 7) {
        if (length == 1 && m->regs[7] == 0) { m->tm_open = false; m->pass_stage = 0; }
        else { assert(length == 2 && m->regs[7] == 0x69 && m->regs[8] == 0x96); m->pass_stage = 2; }
    }
    if (address == 9) {
        assert(length == 2 && m->pass_stage == 2 && m->regs[9] == 0x63 && m->regs[10] == 0x79);
        m->pass_stage = 4; m->tm_open = true;
    }
    if (address == 0x135) assert(m->tm_open);
    if (address == 0x11c && m->corrupt_voltage) m->regs[0x11c] = 1;
    if (address == 0x108) {
        assert(!m->tm_open);
        if (m->regs[0x108] & 1) {
            assert(m->regs[0x11c] == 0 && m->regs[0x11d] == 26 && !(m->regs[0x11e] & 7) && !(m->regs[0x135] & 0x20));
            if (m->auto_vbus) set_voltage(m, 5000, true, false);
        } else if (m->auto_vbus && !m->external_vbus) set_voltage(m, 0, false, true);
    }
    if (m->fail_after_write) { m->fail_after_write = false; p->faulted = p->write_uncertain = true; return USB_IO; }
    return USB_OK;
}
int dali_mt6379_update8(struct dali_usb_pmif *p, uint16_t address, uint8_t mask, uint8_t value)
{
    assert(!(value & (uint8_t)~mask)); uint8_t old;
    int result = dali_mt6379_read(p, address, &old, 1);
    if (result) return result;
    uint8_t next = (uint8_t)((old & (uint8_t)~mask) | value);
    return old == next ? USB_OK : dali_mt6379_write(p, address, &next, 1);
}
int dali_usb_pmif_recover(struct dali_usb_pmif *p)
{
    struct model *m = model_of(p); ++m->recoveries;
    if (m->recover_fail) return USB_TIMEOUT;
    p->faulted = false; p->pending_owned = false; return USB_OK;
}
static void delay(void *context, uint32_t us) { ((struct model *)context)->time += us; }
static int quiesce(void *context)
{ struct model *m = context; ++m->quiesces; return m->quiesce_fail ? USB_TIMEOUT : USB_OK; }
static int connector_ok(void *context)
{ struct model *m = context; ++m->connector_checks; return m->connector_fail ? USB_IO : USB_OK; }
static void prepare(struct model *m)
{
    memset(m, 0, sizeof(*m));
    m->pmif.initialized = m->pmif.identified = true;
    m->pmif.registers.context = m; m->pmif.registers.delay_us = delay;
    m->regs[0] = 0x73; m->regs[0x400] = 0xcf; m->regs[0x401] = 0x29;
    m->regs[0x402] = 0x79; m->regs[0x403] = 0x63; m->regs[0x404] = 0x34; m->regs[0x405] = 0x12;
    m->regs[0x135] = 0x20; m->auto_vbus = true;
    set_voltage(m, 0, false, true);
}
static int probe(struct model *m)
{
    struct dali_typec_ops ops = {m, quiesce, connector_ok};
    return dali_usb_typec_probe(&m->typec, &m->pmif, &ops);
}
static void start(struct model *m, enum dali_cc_mode mode)
{
    assert(probe(m) == USB_OK && !m->event_count && m->typec.device_id == 0x1234);
    assert(dali_usb_typec_start(&m->typec, mode) == USB_OK);
    assert(dali_usb_vbus_init(&m->vbus, &m->typec, true) == USB_OK);
}
/* Exercise every documented <=5ms polling interval, including boundary checks. */
static int poll_through(struct model *m, uint64_t until, struct dali_typec_sample *sample)
{
    for (uint64_t next = m->typec.last_us + 5000; next < until; next += 5000)
        assert(dali_usb_typec_poll(&m->typec, next, sample) == USB_AGAIN);
    return dali_usb_typec_poll(&m->typec, until, sample);
}
static void role_record(struct role_model *r, enum role_event event)
{ assert(r->event_count < 64); r->events[r->event_count++] = event; }
static int role_stop_submissions(void *context)
{
    struct role_model *r = context; role_record(r, ROLE_STOP_SUBMISSIONS);
    return r->stop_submissions_result;
}
static int role_sync_storage(void *context)
{
    struct role_model *r = context; role_record(r, ROLE_SYNC_STORAGE); return r->sync_result;
}
static int role_cancel_requests(void *context)
{
    struct role_model *r = context; role_record(r, ROLE_CANCEL_REQUESTS);
    return r->cancel_result;
}
static int role_stop_dma(void *context)
{
    struct role_model *r = context; role_record(r, ROLE_STOP_DMA);
    if (r->fail_vbus_disable) {
        /* VBUS-off follows stop-DMA immediately; fail its first PMIF access. */
        r->usb->fail_operation = r->usb->operations + 1;
        r->fail_vbus_disable = false;
    }
    r->dma_stopped = r->stop_dma_result == USB_OK; return r->stop_dma_result;
}
static int role_select(void *context, enum usb_role role)
{
    struct role_model *r = context;
    if (role == USB_ROLE_STOPPED) {
        role_record(r, ROLE_SELECT_STOPPED);
        assert(r->dma_stopped && r->usb->vbus.state == DALI_VBUS_OFF);
        if (r->fail_typec_stop) {
            /* dali_usb_typec_stop() starts with this next PMIF transaction. */
            r->usb->fail_operation = r->usb->operations + 1;
            r->fail_typec_stop = false;
        }
    } else if (role == USB_ROLE_DEVICE) {
        role_record(r, ROLE_SELECT_DEVICE);
        assert(r->usb->typec.mode == DALI_CC_DEVICE && r->usb->vbus.state == DALI_VBUS_OFF);
    } else {
        role_record(r, ROLE_SELECT_HOST);
        assert(role == USB_ROLE_HOST && r->usb->typec.mode == DALI_CC_HOST &&
            r->usb->vbus.state == DALI_VBUS_OFF && !(r->usb->regs[0x108] & 1));
    }
    return role == USB_ROLE_STOPPED ? r->select_stopped_result :
        role == USB_ROLE_DEVICE ? r->select_device_result : r->select_host_result;
}
static int role_start(void *context, enum usb_role role)
{
    struct role_model *r = context;
    if (role == USB_ROLE_HOST) {
        role_record(r, ROLE_START_HOST);
        assert(r->usb->typec.mode == DALI_CC_HOST && r->usb->vbus.state == DALI_VBUS_ON);
    } else {
        role_record(r, ROLE_START_DEVICE);
        assert(role == USB_ROLE_DEVICE && r->usb->typec.mode == DALI_CC_DEVICE &&
            r->usb->vbus.state == DALI_VBUS_OFF && r->usb->external_vbus);
    }
    return role == USB_ROLE_HOST ? r->start_host_result : r->start_device_result;
}
static struct usb_role_ops role_ops(struct role_model *r)
{
    return (struct usb_role_ops){r, role_stop_submissions, role_sync_storage,
        role_cancel_requests, role_stop_dma, 0, role_select, role_start};
}
static int worker_poll_to_host(struct model *m, struct dali_usb_role_worker *worker,
    struct dali_typec_sample *sample)
{
    m->regs[0x41d] = 2; set_voltage(m, 0, false, true);
    int result = dali_usb_role_worker_poll(worker, 0, sample);
    if (result != USB_AGAIN) return result;
    for (uint64_t now = 5000; now < 120000; now += 5000) {
        result = dali_usb_role_worker_poll(worker, now, sample);
        if (result != USB_AGAIN) return result;
    }
    return dali_usb_role_worker_poll(worker, 120000, sample);
}
static int worker_detach_host(struct model *m, struct dali_usb_role_worker *worker,
    struct dali_typec_sample *sample)
{
    m->regs[0x41d] = 0; set_voltage(m, 0, false, true);
    assert(dali_usb_role_worker_poll(worker, 125000, sample) == USB_AGAIN);
    assert(dali_usb_role_worker_poll(worker, 130000, sample) == USB_AGAIN);
    assert(dali_usb_role_worker_poll(worker, 135000, sample) == USB_AGAIN);
    return dali_usb_role_worker_poll(worker, 140000, sample);
}
static void host_attach(struct model *m, uint8_t polarity)
{
    struct dali_typec_sample sample;
    m->regs[0x41d] = polarity ? 8 : 2; set_voltage(m, 0, false, true);
    assert(dali_usb_typec_poll(&m->typec, 0, &sample) == USB_AGAIN);
    assert(poll_through(m, 120000, &sample) == USB_OK && sample.role == USB_ROLE_HOST && sample.polarity == polarity);
    assert(dali_usb_vbus_enable(&m->vbus) == USB_DISCONNECTED); /* DRP must be frozen before sourcing. */
    assert(dali_usb_typec_latch_attachment(&m->typec) == USB_OK && m->typec.mode == DALI_CC_HOST);
    assert(m->regs[0x41a] == (polarity ? 6 : 9) && (m->regs[0x419] & 1) == polarity);
}
static void test_probe_and_setup(void)
{
    struct model m; prepare(&m); m.regs[0x400] = 0;
    assert(probe(&m) == USB_UNSUPPORTED && !m.event_count);
    prepare(&m); assert(probe(&m) == USB_OK);
    m.quiesce_fail = true;
    assert(dali_usb_typec_start(&m.typec, DALI_CC_DRP) == USB_TIMEOUT && !m.event_count);
    m.quiesce_fail = false; m.regs[0x108] = 1;
    assert(dali_usb_typec_start(&m.typec, DALI_CC_DRP) == USB_BUSY && !m.event_count);
    m.regs[0x108] = 0;
    assert(dali_usb_typec_start(&m.typec, DALI_CC_DRP) == USB_OK);
    assert(m.regs[0x41a] == 0x4a && m.look_time >= m.role_time + 30);
    assert(m.regs[0x4b3] == 0x34 && m.regs[0x4b4] == 1); /*308 LE16. */
    assert((m.regs[0x006] & 0x10) && !m.regs[0x506]);
    assert(!(m.regs[0x41c] & 9) && !m.regs[0x42f] && (m.regs[0x490] & 0x0b) == 3);
    for (unsigned i = 0x491; i <= 0x497; ++i) assert(!m.regs[i]);
    assert(!m.regs[0x412] && !m.regs[0x413] && !m.regs[0x108]);
    for (unsigned i = 0; i < m.event_count; ++i) assert(m.events[i].address >= 0x400 || m.events[i].address == 6);
    assert(dali_usb_typec_orientation(&m.typec, 1) == USB_OK && (m.regs[0x419] & 1));
    assert(dali_usb_typec_orientation(&m.typec, 2) == USB_INVALID);
    assert(dali_usb_typec_mode(&m.typec, DALI_CC_DEVICE) == USB_OK && m.regs[0x41a] == 0x0a);
    assert(dali_usb_typec_mode(&m.typec, DALI_CC_HOST) == USB_OK && m.regs[0x41a] == 5);
    assert(dali_usb_typec_stop(&m.typec) == USB_OK && m.regs[0x41a] == 15);
    assert(dali_usb_typec_destroy(&m.typec) == USB_OK && !m.typec.initialized &&
        m.typec.mode == DALI_CC_OPEN);
}
static void test_sample_debounce(void)
{
    struct model m; prepare(&m); start(&m, DALI_CC_DRP);
    struct dali_typec_sample sample = {.millivolts = 1234};
    m.regs[0x41d] = 0x11; set_voltage(&m, 5000, true, false); /* DRP result=sink, Rp CC1. */
    assert(dali_usb_typec_poll(&m.typec, 0, &sample) == USB_AGAIN && sample.millivolts == 1234);
    assert(poll_through(&m, 119999, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 120000, &sample) == USB_OK && sample.role == USB_ROLE_DEVICE && !sample.polarity);
    m.regs[0x41d] = 8; set_voltage(&m, 0, false, true);
    assert(dali_usb_typec_poll(&m.typec, 120005, &sample) == USB_AGAIN);
    assert(poll_through(&m, 135004, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 135005, &sample) == USB_OK && sample.role == USB_ROLE_STOPPED);
    assert(poll_through(&m, 255004, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 255005, &sample) == USB_OK && sample.role == USB_ROLE_HOST && sample.polarity == 1);
    assert(dali_usb_typec_poll(&m.typec, 2, &sample) == USB_INVALID);
    m.regs[0x41d] = 0x0a;
    assert(dali_usb_typec_read(&m.typec, &sample) == USB_OK && sample.unsupported && sample.role == USB_ROLE_STOPPED);
    m.regs[0x41d] = 0x20;
    assert(dali_usb_typec_read(&m.typec, &sample) == USB_OK && sample.toggling && sample.role == USB_ROLE_STOPPED);
    m.regs[0x41d] = 0x12; set_voltage(&m, 0, false, true);
    assert(dali_usb_typec_read(&m.typec, &sample) == USB_OK && sample.role == USB_ROLE_STOPPED);
    set_voltage(&m, 5500, true, false);
    assert(dali_usb_typec_read(&m.typec, &sample) == USB_OK && sample.role == USB_ROLE_DEVICE);
    set_voltage(&m, 5525, true, false); sample.millivolts = 1234;
    assert(dali_usb_typec_read(&m.typec, &sample) == USB_RANGE && m.typec.faulted && sample.millivolts == 1234);
}
static void test_debounce_restarts(void)
{
    struct model m; prepare(&m); start(&m, DALI_CC_DRP);
    struct dali_typec_sample sample = {.millivolts = 1234};
    m.regs[0x41d] = 2;
    assert(dali_usb_typec_poll(&m.typec, 0, &sample) == USB_AGAIN);
    assert(poll_through(&m, 115000, &sample) == USB_AGAIN);
    m.unstable_snapshot = true;
    assert(dali_usb_typec_poll(&m.typec, 120000, &sample) == USB_AGAIN && !m.typec.have_candidate && sample.millivolts == 1234);
    assert(dali_usb_typec_poll(&m.typec, 125000, &sample) == USB_AGAIN);
    assert(poll_through(&m, 244999, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 245000, &sample) == USB_OK && sample.role == USB_ROLE_HOST);
    prepare(&m); start(&m, DALI_CC_DRP); m.regs[0x41d] = 2;
    assert(dali_usb_typec_poll(&m.typec, 0, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 120000, &sample) == USB_AGAIN); /* Gap restarts, not an attachment. */
    assert(poll_through(&m, 239999, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 240000, &sample) == USB_OK && sample.role == USB_ROLE_HOST);
    prepare(&m); start(&m, DALI_CC_DRP); m.regs[0x41d] = 2;
    assert(dali_usb_typec_poll(&m.typec, 0, &sample) == USB_AGAIN);
    assert(poll_through(&m, 115000, &sample) == USB_AGAIN);
    m.busy_next = true;
    assert(dali_usb_typec_poll(&m.typec, 120000, &sample) == USB_BUSY && !m.typec.have_candidate);
    assert(dali_usb_typec_poll(&m.typec, 125000, &sample) == USB_AGAIN);
    assert(poll_through(&m, 244999, &sample) == USB_AGAIN);
    assert(dali_usb_typec_poll(&m.typec, 245000, &sample) == USB_OK && sample.role == USB_ROLE_HOST);
}
static void test_lost_toggle(void)
{
    struct model m; prepare(&m); start(&m, DALI_CC_DRP);
    struct dali_typec_sample sample = {.millivolts = 1234};
    m.regs[0x41a] = 0x0a; m.regs[0x41d] = 0x20;
    unsigned before = m.event_count;
    assert(dali_usb_typec_poll(&m.typec, 0, &sample) == USB_AGAIN && sample.millivolts == 1234);
    assert(m.event_count == before + 2 && m.events[before].address == 0x41a && m.events[before].bytes[0] == 0x4a);
    assert(m.events[before + 1].address == 0x41a && m.events[before + 1].bytes[0] == 0x0a && !m.typec.have_candidate);
    m.quiesce_fail = true; before = m.event_count;
    assert(dali_usb_typec_poll(&m.typec, 5000, &sample) == USB_TIMEOUT && m.event_count == before && m.typec.faulted);
    prepare(&m); start(&m, DALI_CC_DRP); m.regs[0x41a] = 0x0a; m.regs[0x41d] = 0x20; m.regs[0x108] = 1;
    before = m.event_count;
    assert(dali_usb_typec_poll(&m.typec, 0, &sample) == USB_BUSY && m.event_count == before && m.typec.faulted);
    prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0);
    m.regs[0x41d] = 0x20; before = m.event_count;
    assert(dali_usb_typec_poll(&m.typec, 125000, &sample) == USB_IO && m.event_count == before && m.typec.faulted);
}
static void test_vbus(void)
{
    struct model m; prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 1);
    unsigned before = m.event_count;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_OK && m.vbus.state == DALI_VBUS_ON && !m.tm_open && (m.regs[0x108] & 1));
    assert(m.quiesces >= 2 && m.connector_checks == 1 && m.regs[0x11c] == 0 && m.regs[0x11d] == 26);
    const uint16_t expected[] = {0x11c, 7, 9, 0x135, 7, 0x108};
    assert(m.event_count - before == sizeof(expected) / sizeof(expected[0]));
    for (unsigned i = 0; i < sizeof(expected) / sizeof(expected[0]); ++i) assert(m.events[before + i].address == expected[i]);
    before = m.event_count;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_OK && m.event_count == before);
    m.quiesce_fail = true;
    assert(dali_usb_vbus_disable(&m.vbus) == USB_TIMEOUT && m.event_count == before && (m.regs[0x108] & 1));
    m.quiesce_fail = false; m.external_vbus = true;
    assert(dali_usb_vbus_disable(&m.vbus) == USB_OK && m.vbus.state == DALI_VBUS_OFF && !(m.regs[0x108] & 1) && (m.regs[0x135] & 0x20));
    assert(m.regs[0x49f] & 0x20); /* Confirming our source off does not discharge another source. */
    before = m.event_count;
    assert(dali_usb_vbus_disable(&m.vbus) == USB_OK && m.event_count == before);
    assert(dali_usb_vbus_destroy(&m.vbus) == USB_OK && !m.vbus.initialized);
    assert(dali_usb_typec_destroy(&m.typec) == USB_OK);
}
static void test_power_guards_and_failures(void)
{
    struct model m; prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0);
    unsigned before = m.event_count;
    m.quiesce_fail = true;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_TIMEOUT && m.event_count == before);
    m.quiesce_fail = false; m.connector_fail = true;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_IO && m.event_count == before);
    m.connector_fail = false; set_voltage(&m, 5000, true, false);
    assert(dali_usb_vbus_enable(&m.vbus) == USB_BUSY && m.event_count == before);
    set_voltage(&m, 1000, false, true);
    assert(dali_usb_vbus_enable(&m.vbus) == USB_BUSY && m.event_count == before);
    set_voltage(&m, 0, false, true); m.regs[0x41d] = 0;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_DISCONNECTED && m.event_count == before);
    m.regs[0x41d] = 2; m.corrupt_voltage = true;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_IO && m.vbus.state == DALI_VBUS_UNKNOWN && !(m.regs[0x108] & 1));
    assert(dali_usb_vbus_enable(&m.vbus) == USB_IO);
    m.corrupt_voltage = false;
    assert(dali_usb_vbus_disable(&m.vbus) == USB_OK && !m.tm_open);
    prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0); m.auto_vbus = false;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_TIMEOUT && m.vbus.state == DALI_VBUS_UNKNOWN && (m.regs[0x108] & 1));
    assert(dali_usb_vbus_disable(&m.vbus) == USB_OK && !(m.regs[0x108] & 1));
    prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0);
    unsigned base = m.operations;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_OK);
    unsigned operations = m.operations - base;
    for (unsigned i = 1; i <= operations; ++i) {
        prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0);
        m.fail_operation = m.operations + i;
        int result = dali_usb_vbus_enable(&m.vbus);
        assert(result == USB_IO && m.pmif.faulted);
        if (m.vbus.state == DALI_VBUS_UNKNOWN) {
            assert(dali_usb_vbus_disable(&m.vbus) == USB_OK && m.vbus.state == DALI_VBUS_OFF && !(m.regs[0x108] & 1) && !m.tm_open);
        }
    }
    prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0); m.fail_after_write = true;
    assert(dali_usb_vbus_enable(&m.vbus) == USB_IO && m.pmif.write_uncertain && m.vbus.state == DALI_VBUS_UNKNOWN);
    m.recover_fail = true; before = m.event_count;
    assert(dali_usb_vbus_destroy(&m.vbus) == USB_TIMEOUT && m.vbus.initialized && m.event_count == before);
    m.recover_fail = false;
    assert(dali_usb_vbus_destroy(&m.vbus) == USB_OK && m.pmif.write_uncertain && !m.tm_open);
}
static void test_lost_output(void)
{
    for (unsigned failure = 0; failure < 4; ++failure) {
        struct model m; prepare(&m); start(&m, DALI_CC_DRP); host_attach(&m, 0);
        assert(dali_usb_vbus_enable(&m.vbus) == USB_OK);
        if (failure == 0) set_voltage(&m, 0, false, true);
        if (failure == 1) set_voltage(&m, 4825, true, false);
        if (failure == 2) m.regs[0x108] = 0; /* External voltage cannot stand in for our boost. */
        if (failure == 3) m.regs[0x41d] = 0;
        unsigned before = m.event_count;
        assert(dali_usb_vbus_enable(&m.vbus) == (failure == 3 ? USB_DISCONNECTED : USB_IO));
        assert(m.vbus.faulted && m.vbus.state == DALI_VBUS_UNKNOWN && m.event_count == before);
        assert(dali_usb_vbus_enable(&m.vbus) == USB_IO && m.event_count == before);
        assert(dali_usb_vbus_disable(&m.vbus) == USB_OK && !(m.regs[0x108] & 1));
        assert(dali_usb_vbus_enable(&m.vbus) == USB_IO); /* No automatic power replay. */
    }
}
static void test_role_worker_host_device_and_shutdown(void)
{
    struct model m; prepare(&m); start(&m, DALI_CC_DRP);
    struct role_model roles = {.usb = &m};
    struct usb_role_ops ops = role_ops(&roles);
    struct dali_usb_role_worker worker = {0};
    struct dali_typec_sample sample = {.millivolts = 1234};
    assert(dali_usb_role_worker_init(&worker, &m.typec, &m.vbus, &ops) == USB_OK);
    assert(worker_poll_to_host(&m, &worker, &sample) == USB_OK);
    assert(sample.role == USB_ROLE_HOST && worker.role.role == USB_ROLE_HOST &&
        m.vbus.state == DALI_VBUS_ON && roles.event_count == 2);
    assert(roles.events[0] == ROLE_SELECT_HOST && roles.events[1] == ROLE_START_HOST);

    /* A detach is a STOPPED boundary; only after sync/cancel/DMA proof may the
     * source turn off and the TCPC return to DRP to discover a different role. */
    assert(worker_detach_host(&m, &worker, &sample) == USB_OK);
    assert(sample.role == USB_ROLE_STOPPED && worker.role.role == USB_ROLE_STOPPED &&
        m.vbus.state == DALI_VBUS_OFF && m.typec.mode == DALI_CC_DRP);
    const enum role_event host_stop[] = {ROLE_STOP_SUBMISSIONS, ROLE_SYNC_STORAGE,
        ROLE_CANCEL_REQUESTS, ROLE_STOP_DMA, ROLE_SELECT_STOPPED};
    assert(roles.event_count == 7 &&
        memcmp(roles.events + 2, host_stop, sizeof(host_stop)) == 0);

    /* A sink appears after DRP is restored. It must never enable source VBUS. */
    m.external_vbus = true; m.regs[0x41d] = 0x11; set_voltage(&m, 5000, true, false);
    assert(dali_usb_role_worker_poll(&worker, 145000, &sample) == USB_AGAIN);
    for (uint64_t now = 150000; now < 265000; now += 5000)
        assert(dali_usb_role_worker_poll(&worker, now, &sample) == USB_AGAIN);
    assert(dali_usb_role_worker_poll(&worker, 265000, &sample) == USB_OK);
    assert(sample.role == USB_ROLE_DEVICE && worker.role.role == USB_ROLE_DEVICE &&
        m.typec.mode == DALI_CC_DEVICE && m.vbus.state == DALI_VBUS_OFF &&
        (m.regs[0x108] & 1) == 0);
    assert(roles.events[7] == ROLE_SELECT_DEVICE && roles.events[8] == ROLE_START_DEVICE);

    assert(dali_usb_role_worker_stop(&worker) == USB_OK);
    assert(!worker.initialized && !m.typec.started && m.vbus.state == DALI_VBUS_OFF &&
        m.typec.mode == DALI_CC_OPEN);
    const enum role_event final_stop[] = {ROLE_STOP_SUBMISSIONS, ROLE_SYNC_STORAGE,
        ROLE_CANCEL_REQUESTS, ROLE_STOP_DMA, ROLE_SELECT_STOPPED};
    assert(roles.event_count == 14 &&
        memcmp(roles.events + 9, final_stop, sizeof(final_stop)) == 0);
}
static void test_role_worker_retains_source_until_sync_and_stop_proof(void)
{
    for (unsigned failure = 0; failure < 2; ++failure) {
        struct model m; prepare(&m); start(&m, DALI_CC_DRP);
        struct role_model roles = {.usb = &m};
        struct usb_role_ops ops = role_ops(&roles);
        struct dali_usb_role_worker worker = {0};
        struct dali_typec_sample sample = {0};
        assert(dali_usb_role_worker_init(&worker, &m.typec, &m.vbus, &ops) == USB_OK);
        assert(worker_poll_to_host(&m, &worker, &sample) == USB_OK &&
            m.vbus.state == DALI_VBUS_ON);

        if (failure == 0) roles.sync_result = USB_TIMEOUT;
        else roles.stop_dma_result = USB_TIMEOUT;
        assert(worker_detach_host(&m, &worker, &sample) == USB_TIMEOUT);
        assert(worker.faulted && worker.role.faulted && m.vbus.state == DALI_VBUS_ON &&
            (m.regs[0x108] & 1) && m.typec.mode == DALI_CC_HOST);

        /* Explicit teardown retries the proof and sync; no opposite role starts. */
        roles.sync_result = USB_OK; roles.stop_dma_result = USB_OK;
        assert(dali_usb_role_worker_stop(&worker) == USB_OK);
        assert(!worker.initialized && !m.typec.started && m.vbus.state == DALI_VBUS_OFF &&
            !(m.regs[0x108] & 1));
        for (unsigned i = 0; i < roles.event_count; ++i)
            assert(roles.events[i] != ROLE_SELECT_DEVICE && roles.events[i] != ROLE_START_DEVICE);
    }
}
static void test_role_worker_stop_callback_failures_retain_vbus(void)
{
    for (unsigned failure = 0; failure < 4; ++failure) {
        struct model m; prepare(&m); start(&m, DALI_CC_DRP);
        struct role_model roles = {.usb = &m};
        struct usb_role_ops ops = role_ops(&roles);
        struct dali_usb_role_worker worker = {0};
        struct dali_typec_sample sample = {0};
        assert(dali_usb_role_worker_init(&worker, &m.typec, &m.vbus, &ops) == USB_OK);
        assert(worker_poll_to_host(&m, &worker, &sample) == USB_OK);
        if (failure == 0) roles.stop_submissions_result = USB_TIMEOUT;
        if (failure == 1) roles.sync_result = USB_TIMEOUT;
        if (failure == 2) roles.cancel_result = USB_TIMEOUT;
        if (failure == 3) roles.stop_dma_result = USB_TIMEOUT;

        assert(worker_detach_host(&m, &worker, &sample) == USB_TIMEOUT);
        assert(worker.faulted && worker.role.faulted && m.vbus.state == DALI_VBUS_ON &&
            (m.regs[0x108] & 1) && m.typec.mode == DALI_CC_HOST);
        assert(roles.event_count >= 6 && roles.events[2] == ROLE_STOP_SUBMISSIONS &&
            roles.events[3] == ROLE_SYNC_STORAGE && roles.events[4] == ROLE_CANCEL_REQUESTS &&
            roles.events[5] == ROLE_STOP_DMA);

        roles.stop_submissions_result = USB_OK;
        roles.sync_result = USB_OK;
        roles.cancel_result = USB_OK;
        roles.stop_dma_result = USB_OK;
        assert(dali_usb_role_worker_stop(&worker) == USB_OK);
        assert(!worker.initialized && m.vbus.state == DALI_VBUS_OFF &&
            !(m.regs[0x108] & 1) && !m.typec.started);
        for (unsigned i = 0; i < roles.event_count; ++i)
            assert(roles.events[i] != ROLE_SELECT_DEVICE && roles.events[i] != ROLE_START_DEVICE);
    }
}
static void test_role_worker_late_cleanup_failures_are_retryable(void)
{
    for (unsigned failure = 0; failure < 3; ++failure) {
        struct model m; prepare(&m); start(&m, DALI_CC_DRP);
        struct role_model roles = {.usb = &m};
        struct usb_role_ops ops = role_ops(&roles);
        struct dali_usb_role_worker worker = {0};
        struct dali_typec_sample sample = {0};
        assert(dali_usb_role_worker_init(&worker, &m.typec, &m.vbus, &ops) == USB_OK);
        assert(worker_poll_to_host(&m, &worker, &sample) == USB_OK);

        if (failure == 0) roles.fail_vbus_disable = true;
        if (failure == 1) roles.select_stopped_result = USB_IO;
        if (failure == 2) roles.fail_typec_stop = true;
        int result = failure == 2 ? dali_usb_role_worker_stop(&worker) :
            worker_detach_host(&m, &worker, &sample);
        assert(result != USB_OK && worker.initialized && worker.faulted && worker.role.faulted);
        if (failure == 0) {
            assert(m.vbus.state == DALI_VBUS_UNKNOWN && m.pmif.faulted && (m.regs[0x108] & 1));
        } else if (failure == 1) {
            assert(m.vbus.state == DALI_VBUS_OFF && !(m.regs[0x108] & 1) &&
                m.typec.mode == DALI_CC_HOST);
            roles.select_stopped_result = USB_OK;
        } else {
            assert(m.vbus.state == DALI_VBUS_OFF && m.typec.started && m.pmif.faulted);
        }
        m.fail_operation = 0;
        assert(dali_usb_role_worker_stop(&worker) == USB_OK);
        assert(!worker.initialized && m.vbus.state == DALI_VBUS_OFF &&
            !(m.regs[0x108] & 1) && !m.typec.started && m.typec.mode == DALI_CC_OPEN);
    }
}
static void test_role_worker_start_failures_cleanup_and_retain(void)
{
    for (unsigned failure = 0; failure < 2; ++failure) {
        struct model m; prepare(&m); start(&m, DALI_CC_DRP);
        struct role_model roles = {.usb = &m};
        struct usb_role_ops ops = role_ops(&roles);
        struct dali_usb_role_worker worker = {0};
        struct dali_typec_sample sample = {0};
        assert(dali_usb_role_worker_init(&worker, &m.typec, &m.vbus, &ops) == USB_OK);
        if (failure == 0) roles.select_host_result = USB_IO;
        else roles.start_host_result = USB_IO;
        assert(worker_poll_to_host(&m, &worker, &sample) == USB_IO);
        assert(worker.faulted && worker.role.faulted && m.vbus.state == DALI_VBUS_OFF &&
            !(m.regs[0x108] & 1) && m.typec.mode == DALI_CC_DRP);
        if (failure == 0) roles.select_host_result = USB_OK;
        else roles.start_host_result = USB_OK;
        assert(dali_usb_role_worker_stop(&worker) == USB_OK);
        assert(!worker.initialized && !m.typec.started && m.vbus.state == DALI_VBUS_OFF);
    }
}
int main(void)
{
    test_probe_and_setup(); test_sample_debounce(); test_debounce_restarts(); test_lost_toggle();
    test_vbus(); test_power_guards_and_failures(); test_lost_output();
    test_role_worker_host_device_and_shutdown();
    test_role_worker_retains_source_until_sync_and_stop_proof();
    test_role_worker_stop_callback_failures_retain_vbus();
    test_role_worker_late_cleanup_failures_are_retryable();
    test_role_worker_start_failures_cleanup_and_retain();
    puts("PASS: MT6379 production TCPC/VBUS/role worker; PMIC/controller callbacks modeled only");
    return 0;
}
