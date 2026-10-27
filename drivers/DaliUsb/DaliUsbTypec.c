/* SPDX-License-Identifier: GPL-2.0-only */
/* Register sequences from MediaTek tcpc_mt6379.c/std_tcpci_v10.h and
 * mt6379-charger.c/.h, Copyright (C) 2020-2023 MediaTek Inc. Fixed reference:
 * MiCode/MTK_kernel_device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbTypec.h"

static int read8(struct dali_usb_typec *t, uint16_t offset, uint8_t *value)
{ return dali_mt6379_read(t->pmif, (uint16_t)(0x400 + offset), value, 1); }
static int read16(struct dali_usb_typec *t, uint16_t offset, uint16_t *value)
{
    uint8_t bytes[2];
    int result = dali_mt6379_read(t->pmif, (uint16_t)(0x400 + offset), bytes, 2);
    if (!result) *value = (uint16_t)((uint16_t)bytes[0] | ((uint16_t)bytes[1] << 8));
    return result;
}
static int write8(struct dali_usb_typec *t, uint16_t offset, uint8_t value)
{ return dali_mt6379_write(t->pmif, (uint16_t)(0x400 + offset), &value, 1); }
static int write16(struct dali_usb_typec *t, uint16_t offset, uint16_t value)
{
    const uint8_t bytes[2] = {(uint8_t)value, (uint8_t)(value >> 8)};
    return dali_mt6379_write(t->pmif, (uint16_t)(0x400 + offset), bytes, 2);
}
static int update8(struct dali_usb_typec *t, uint16_t offset, uint8_t mask, uint8_t value)
{ return dali_mt6379_update8(t->pmif, (uint16_t)(0x400 + offset), mask, value); }
static int fault(struct dali_usb_typec *t, int result)
{ t->faulted = true; return result; }
static void delay(struct dali_usb_typec *t, uint32_t us)
{ t->pmif->registers.delay_us(t->pmif->registers.context, us); }
static int boost_off(struct dali_usb_typec *t)
{
    uint8_t value;
    int result = dali_mt6379_read(t->pmif, 0x108, &value, 1);
    return result ? result : value & 1 ? USB_BUSY : USB_OK;
}
static int select_mode(struct dali_usb_typec *t, enum dali_cc_mode mode)
{
    uint8_t value = mode == DALI_CC_DRP ? 0x4a : mode == DALI_CC_DEVICE ? 0x0a : mode == DALI_CC_HOST ? 0x05 : 0x0f;
    int result = write8(t, 0x1a, value);
    if (!result && mode == DALI_CC_DRP) {
        result = update8(t, 0x91, 2, 0); /* Reference masks safe0V alert while toggling. */
        if (!result) { delay(t, 30); result = write8(t, 0x23, 0x99); }
    }
    if (!result) {
        t->mode = mode; t->have_candidate = false;
        t->published = (struct dali_typec_sample){0};
    }
    return result;
}
int dali_usb_typec_probe(struct dali_usb_typec *t, struct dali_usb_pmif *p, const struct dali_typec_ops *ops)
{
    if (!t || !p || !ops || !ops->quiesce || !p->initialized || !p->identified) return USB_INVALID;
    if (t->initialized) return USB_BUSY;
    struct dali_usb_typec candidate = {0}; candidate.pmif = p; candidate.ops = *ops;
    uint16_t vendor, product, device;
    int result = read16(&candidate, 0, &vendor);
    if (!result) result = read16(&candidate, 2, &product);
    if (!result) result = read16(&candidate, 4, &device);
    if (result) return result;
    if (vendor != 0x29cf || product != 0x6379) return USB_UNSUPPORTED;
    candidate.device_id = device; candidate.initialized = true; *t = candidate;
    return USB_OK;
}
int dali_usb_typec_start(struct dali_usb_typec *t, enum dali_cc_mode mode)
{
    if (!t || !t->initialized || mode < DALI_CC_OPEN || mode > DALI_CC_DRP) return USB_INVALID;
    if (t->faulted) return USB_IO;
    if (t->started) return USB_BUSY;
    int result = t->ops.quiesce(t->ops.context);
    if (!result) result = boost_off(t);
    if (result) return result; /* No TCPC write before ownership/quiesce checks. */
    result = write8(t, 0xb0, 1); /* TCPC-only software reset, not a PMIC reset. */
    if (result) return fault(t, result);
    delay(t, 2000);
    result = write16(t, 0x12, 0); /* Polling owns only TCPC-local masks. */
    for (uint16_t reg = 0x91; !result && reg <= 0x97; ++reg) result = write8(t, reg, 0);
    if (!result) result = write8(t, 0x14, 0);
    if (!result) result = write8(t, 0x15, 0);
    if (!result) result = write8(t, 0x16, 0);
    if (!result) result = write8(t, 0x2f, 0); /* PD RX disabled. No PD messages sent. */
    if (!result) result = write8(t, 0x1a, 0x0a);
    const uint8_t settings[][2] = {
        {0xb1, 0x0a}, {0xb2, 0}, {0x80, 0x74}, {0x81, 0x3a},
        {0x82, 0x82}, {0x86, 0x36}, {0xac, 0x3c}, {0x8c, 0x51},
        {0xc8, 0xaa}, {0xbb, 0xd8}
    };
    for (unsigned i = 0; !result && i < sizeof(settings) / sizeof(settings[0]); ++i)
        result = write8(t, settings[i][0], settings[i][1]);
    if (!result) result = write16(t, 0xb3, 308); /* TCPC_NORMAL_RP_DUTY (30%). */
    if (!result) result = update8(t, 0xca, 0x10, 0x10);
    /* CORECTRL2 is logical PMU0x106 -> physical0x006, not TCPCI0x506. */
    if (!result) result = dali_mt6379_update8(t->pmif, 0x006, 0x10, 0x10);
    if (!result) result = update8(t, 0x19, 0x42, 0x40); /* Look4Connection alert, no BIST. */
    if (!result) result = update8(t, 0xbe, 0x40, 0x40);
    if (!result) result = update8(t, 0x1c, 0x09, 0); /* No bleed discharge or VCONN. */
    if (!result) result = update8(t, 0x8f, 0xaf, 0xa8); /* IRQ path, shipping off, autoidle. */
    if (!result) result = update8(t, 0x90, 0x0b, 3); /* Wake BMC clock and VBUS detector. */
    if (!result) result = select_mode(t, mode);
    if (result) return fault(t, result);
    delay(t, 1000); t->started = true;
    return USB_OK;
}
int dali_usb_typec_mode(struct dali_usb_typec *t, enum dali_cc_mode mode)
{
    if (!t || !t->initialized || !t->started || mode < DALI_CC_OPEN || mode > DALI_CC_DRP) return USB_INVALID;
    if (t->faulted) return USB_IO;
    int result = t->ops.quiesce(t->ops.context);
    if (!result) result = boost_off(t);
    if (result) return result;
    result = select_mode(t, mode);
    return result ? fault(t, result) : USB_OK;
}
int dali_usb_typec_orientation(struct dali_usb_typec *t, uint8_t polarity)
{
    if (!t || !t->initialized || !t->started || polarity > 1) return USB_INVALID;
    if (t->faulted) return USB_IO;
    int result = t->ops.quiesce(t->ops.context);
    if (result) return result;
    result = update8(t, 0x19, 1, polarity);
    return result ? fault(t, result) : USB_OK;
}
int dali_usb_typec_read(struct dali_usb_typec *t, struct dali_typec_sample *out)
{
    if (!t || !out || !t->initialized || !t->started) return USB_INVALID;
    if (t->faulted) return USB_IO;
    uint8_t cc, role, orientation, power, faults, again;
    uint16_t voltage;
    int result = read8(t, 0x1d, &cc);
    if (!result) result = read8(t, 0x1a, &role);
    if (!result) result = read8(t, 0x19, &orientation);
    if (!result) result = read8(t, 0x9f, &power);
    if (!result) result = read8(t, 0x1f, &faults);
    if (!result) result = read16(t, 0x70, &voltage);
    if (!result) result = read8(t, 0x1d, &again);
    if (result) return result == USB_BUSY ? result : fault(t, result);
    if (cc != again) return USB_AGAIN;
    if (faults || (power & 4)) return fault(t, USB_IO); /* Fault status / Type-C overtemperature. */
    if (voltage & 0x0c00) return fault(t, USB_UNSUPPORTED); /* Unconfigured measurement scaling. */
    struct dali_typec_sample s = {0};
    s.millivolts = (uint16_t)((voltage & 0x3ff) * 25);
    if (s.millivolts > 5500) return fault(t, USB_RANGE);
    s.cc1 = cc & 3; s.cc2 = (cc >> 2) & 3;
    s.vbus_present = (power & 0x20) != 0; s.safe0v = (power & 2) != 0; s.toggling = (cc & 0x20) != 0;
    s.rearm_required = s.toggling && !(role & 0x40);
    if (!s.toggling && t->mode != DALI_CC_OPEN) {
        unsigned active_term = (role >> ((orientation & 1) ? 2 : 0)) & 3;
        bool sink = (role & 0x40) ? (cc & 0x10) != 0 : active_term == 2;
        if (sink) {
            if ((s.cc1 != 0) != (s.cc2 != 0)) {
                s.polarity = s.cc2 != 0 ? 1 : 0;
                if (s.vbus_present && s.millivolts >= 4850) s.role = USB_ROLE_DEVICE;
            } else if (s.cc1 || s.cc2) s.unsupported = true;
        } else if ((role & 0x40) || active_term == 1) {
            if ((s.cc1 == 2 && s.cc2 <= 1) || (s.cc2 == 2 && s.cc1 <= 1)) {
                s.role = USB_ROLE_HOST; s.polarity = s.cc2 == 2 ? 1 : 0;
            } else if (s.cc1 || s.cc2) s.unsupported = true;
        }
    }
    *out = s; return USB_OK;
}
static bool same_attachment(const struct dali_typec_sample *a, const struct dali_typec_sample *b)
{ return a->role == b->role && a->polarity == b->polarity && a->unsupported == b->unsupported; }
int dali_usb_typec_latch_attachment(struct dali_usb_typec *t)
{
    if (!t || !t->initialized || !t->started) return USB_INVALID;
    if (t->faulted) return USB_IO;
    if (t->published.role != USB_ROLE_DEVICE && t->published.role != USB_ROLE_HOST) return USB_AGAIN;
    struct dali_typec_sample s;
    int result = dali_usb_typec_read(t, &s);
    if (!result && !same_attachment(&s, &t->published)) result = USB_DISCONNECTED;
    if (!result) result = t->ops.quiesce(t->ops.context);
    if (!result) result = boost_off(t);
    if (result) return result;
    result = update8(t, 0x19, 1, s.polarity);
    uint8_t term = s.role == USB_ROLE_DEVICE ? 0x0a : s.polarity ? 0x06 : 0x09;
    if (!result) result = write8(t, 0x1a, term);
    if (result) return fault(t, result);
    t->mode = s.role == USB_ROLE_DEVICE ? DALI_CC_DEVICE : DALI_CC_HOST;
    result = dali_usb_typec_read(t, &s);
    if (result || !same_attachment(&s, &t->published)) return fault(t, result ? result : USB_DISCONNECTED);
    t->candidate = t->published = s;
    return USB_OK;
}
int dali_usb_typec_poll(struct dali_usb_typec *t, uint64_t now, struct dali_typec_sample *out)
{
    if (!t || !out || !t->initialized || !t->started || now < t->last_us) return USB_INVALID;
    if (t->have_poll && now - t->last_us > 5000) t->have_candidate = false;
    t->last_us = now; t->have_poll = true;
    struct dali_typec_sample s;
    int result = dali_usb_typec_read(t, &s);
    if (result) { t->have_candidate = false; return result; }
    if (s.rearm_required) {
        t->have_candidate = false;
        if (t->mode != DALI_CC_DRP || t->published.role != USB_ROLE_STOPPED) return fault(t, USB_IO);
        result = t->ops.quiesce(t->ops.context);
        if (!result) result = boost_off(t);
        uint8_t role;
        if (!result) result = read8(t, 0x1a, &role);
        /* Reference lost-toggle workaround: pulse DRP, restoring the original
         * termination. Do not change source power or retry an uncertain write. */
        if (!result) result = write8(t, 0x1a, (uint8_t)(role | 0x40));
        if (!result) result = write8(t, 0x1a, role);
        return result ? fault(t, result) : USB_AGAIN;
    }
    if (!t->have_candidate || !same_attachment(&s, &t->candidate)) {
        t->candidate = s; t->candidate_since = now; t->have_candidate = true;
    }
    if (same_attachment(&s, &t->published)) { t->published = s; *out = s; return USB_OK; }
    uint64_t interval = t->published.role != USB_ROLE_STOPPED || s.role == USB_ROLE_STOPPED ? 15000 : 120000;
    if (now - t->candidate_since < interval) return USB_AGAIN;
    if (t->published.role != USB_ROLE_STOPPED) {
        /* Always publish a stopped boundary before an attachment/orientation
         * change. The caller must quiesce the real role and disable its boost. */
        t->published = s; t->published.role = USB_ROLE_STOPPED;
        t->candidate_since = now;
    } else t->published = s;
    *out = t->published; return USB_OK;
}
int dali_usb_typec_stop(struct dali_usb_typec *t)
{
    if (!t || !t->initialized) return USB_INVALID;
    int result = t->ops.quiesce(t->ops.context);
    if (!result && t->pmif->faulted) result = dali_usb_pmif_recover(t->pmif);
    if (!result) result = boost_off(t);
    if (!result) result = write8(t, 0x1a, 0x0f);
    if (!result) result = write16(t, 0x12, 0);
    if (!result) result = update8(t, 0x1c, 1, 0);
    if (result) return fault(t, result);
    t->mode = DALI_CC_OPEN;
    t->started = false; t->have_candidate = false; t->published = (struct dali_typec_sample){0};
    return USB_OK;
}
int dali_usb_typec_destroy(struct dali_usb_typec *t)
{
    int result = dali_usb_typec_stop(t);
    if (!result) *t = (struct dali_usb_typec){0};
    return result;
}

static int vbus_fault(struct dali_usb_vbus *v, int result)
{ v->faulted = true; v->state = DALI_VBUS_UNKNOWN; return result; }
static int test_mode(struct dali_usb_vbus *v, bool enable)
{
    static const uint8_t pass[4] = {0x69, 0x96, 0x63, 0x79};
    if (!enable) { const uint8_t zero = 0; return dali_mt6379_write(v->typec->pmif, 7, &zero, 1); }
    int result = dali_mt6379_write(v->typec->pmif, 7, pass, 2);
    return result ? result : dali_mt6379_write(v->typec->pmif, 9, pass + 2, 2);
}
int dali_usb_vbus_init(struct dali_usb_vbus *v, struct dali_usb_typec *t, bool owned)
{
    if (!v || !t || !t->initialized || !t->pmif->identified) return USB_INVALID;
    if (v->initialized) return USB_BUSY;
    if (!owned || !t->ops.connector_ok) return USB_UNSUPPORTED;
    int result = boost_off(t);
    if (result) return result;
    *v = (struct dali_usb_vbus){0}; v->typec = t; v->initialized = true;
    return USB_OK;
}
static int source_attachment(struct dali_usb_vbus *v, bool off)
{
    struct dali_typec_sample s;
    int result = dali_usb_typec_read(v->typec, &s);
    if (result) return result;
    if (v->typec->mode != DALI_CC_HOST || v->typec->published.role != USB_ROLE_HOST || s.role != USB_ROLE_HOST ||
        s.polarity != v->typec->published.polarity) return USB_DISCONNECTED;
    if (off && (s.vbus_present || !s.safe0v || s.millivolts > 800)) return USB_BUSY;
    if (!off) {
        if (!s.vbus_present || s.millivolts < 4850 || s.safe0v) return USB_IO;
        uint8_t enabled;
        result = dali_mt6379_read(v->typec->pmif, 0x108, &enabled, 1);
        if (result || !(enabled & 1)) return result ? result : USB_IO;
    }
    return USB_OK;
}
int dali_usb_vbus_enable(struct dali_usb_vbus *v)
{
    if (!v || !v->initialized) return USB_INVALID;
    if (v->faulted || v->state == DALI_VBUS_UNKNOWN) return USB_IO;
    if (v->state == DALI_VBUS_ON) {
        int checked = source_attachment(v, false);
        return checked ? vbus_fault(v, checked) : USB_OK;
    }
    struct dali_usb_typec *t = v->typec;
    int result = source_attachment(v, true);
    if (!result) result = t->ops.quiesce(t->ops.context);
    if (!result) result = t->ops.connector_ok(t->ops.context);
    if (!result) result = boost_off(t);
    if (result) return result;
    const uint8_t five_volts[2] = {0, 26}; /* BE16 selector:20+(5000000-4850000)/25000. */
    result = dali_mt6379_write(t->pmif, 0x11c, five_volts, 2);
    if (!result) result = dali_mt6379_update8(t->pmif, 0x11e, 7, 0); /* 500mA. */
    uint8_t voltage[2], current;
    if (!result) result = dali_mt6379_read(t->pmif, 0x11c, voltage, 2);
    if (!result) result = dali_mt6379_read(t->pmif, 0x11e, &current, 1);
    if (!result && (voltage[0] != 0 || voltage[1] != 26 || (current & 7))) result = USB_IO;
    if (!result) result = test_mode(v, true);
    if (!result) result = dali_mt6379_update8(t->pmif, 0x135, 0x20, 0);
    /* Always try to close TM if its transaction did not poison PMIF. An
     * uncertain write instead requires explicit disable/recovery, never retry. */
    if (!t->pmif->faulted) { int closed = test_mode(v, false); if (!result) result = closed; }
    if (result) return vbus_fault(v, result);
    result = source_attachment(v, true); /* CC/VBUS may have changed during setup. */
    if (result) return vbus_fault(v, result);
    v->state = DALI_VBUS_UNKNOWN;
    result = dali_mt6379_update8(t->pmif, 0x108, 1, 1);
    if (result) return vbus_fault(v, result);
    for (unsigned elapsed = 0;; ++elapsed) {
        struct dali_typec_sample s;
        result = dali_usb_typec_read(t, &s);
        if (result) return vbus_fault(v, result);
        if (s.role != USB_ROLE_HOST || s.polarity != t->published.polarity) return vbus_fault(v, USB_DISCONNECTED);
        if (s.vbus_present && s.millivolts >= 4850 && s.millivolts <= 5500) {
            uint8_t enabled;
            result = dali_mt6379_read(t->pmif, 0x108, &enabled, 1);
            if (result || !(enabled & 1)) return vbus_fault(v, result ? result : USB_IO);
            v->state = DALI_VBUS_ON; return USB_OK;
        }
        if (elapsed == 500) return vbus_fault(v, USB_TIMEOUT);
        delay(t, 1000);
    }
}
int dali_usb_vbus_disable(struct dali_usb_vbus *v)
{
    if (!v || !v->initialized) return USB_INVALID;
    if (v->state == DALI_VBUS_OFF) return USB_OK;
    struct dali_usb_typec *t = v->typec;
    int result = t->ops.quiesce(t->ops.context);
    if (result) return result; /* No power change with uncertain DMA. */
    if (t->pmif->faulted) result = dali_usb_pmif_recover(t->pmif);
    if (!result) result = test_mode(v, true);
    if (!result) result = dali_mt6379_update8(t->pmif, 0x135, 0x20, 0x20);
    if (!t->pmif->faulted) { int closed = test_mode(v, false); if (!result) result = closed; }
    if (!result) result = dali_mt6379_update8(t->pmif, 0x108, 1, 0);
    uint8_t enabled;
    if (!result) result = dali_mt6379_read(t->pmif, 0x108, &enabled, 1);
    if (!result && (enabled & 1)) result = USB_IO;
    if (result) return vbus_fault(v, result);
    v->state = DALI_VBUS_OFF;
    return USB_OK;
}
int dali_usb_vbus_destroy(struct dali_usb_vbus *v)
{
    int result = dali_usb_vbus_disable(v);
    if (!result) *v = (struct dali_usb_vbus){0};
    return result;
}
