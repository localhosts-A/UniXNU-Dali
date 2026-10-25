/* SPDX-License-Identifier: GPL-2.0-only */
/* MT6991 channel/command/status definitions from MediaTek spmi-mtk-pmif-core.c;
 * MT6379 spacing/transfer contract from mt6379-spmi.c (MediaTek, 2023).
 * Fixed reference: MiCode/MTK_kernel_device_modules
 * e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbPmif.h"

#define INIT_DONE 0u
#define ACC 0x880u
#define WDATA 0x884u
#define RDATA 0x894u
#define VLD_CLR 0x8a4u
#define STA 0x8a8u
#define ERROR (1u << 18)
#define SID14_READ 0xce000000u
#define WRITE (1u << 29)
#define MT6379_MAX_REG 0xdffu

static unsigned state(uint32_t value) { return (value >> 1) & 7; }
static bool valid_range(uint16_t address, const void *data, size_t length)
{ return data && length && length <= 2 && address <= MT6379_MAX_REG && length - 1 <= MT6379_MAX_REG - address; }
static int enter(struct dali_usb_pmif *p)
{
    if (!p) return USB_INVALID;
    if (__atomic_test_and_set(&p->locked, __ATOMIC_ACQUIRE)) return USB_BUSY;
    if (!p->initialized) { __atomic_clear(&p->locked, __ATOMIC_RELEASE); return USB_INVALID; }
    int result = p->lock.try_acquire(p->lock.context);
    if (result) __atomic_clear(&p->locked, __ATOMIC_RELEASE);
    return result;
}
static void leave(struct dali_usb_pmif *p)
{ p->lock.release(p->lock.context, p->pending_owned); __atomic_clear(&p->locked, __ATOMIC_RELEASE); }
static int poll_state(struct dali_usb_pmif *p, unsigned expected, uint32_t *last)
{
    for (uint32_t elapsed = 0;; elapsed += 10) {
        int result = usb_mmio_read(&p->registers, STA, last);
        if (result) return result;
        if (state(*last) == expected) return USB_OK;
        if (elapsed == 100000) return USB_TIMEOUT;
        p->registers.delay_us(p->registers.context, 10);
    }
}
static int ready(struct dali_usb_pmif *p)
{
    if (p->faulted) return USB_IO;
    uint32_t value;
    int result = usb_mmio_read(&p->registers, INIT_DONE, &value);
    if (!result && !(value & 1)) result = USB_UNSUPPORTED;
    if (result) { p->faulted = true; return result; }
    result = usb_mmio_read(&p->registers, STA, &value);
    if (result) { p->faulted = true; return result; }
    /* Never clear or consume a response that was present before our command. */
    return state(value) == 0 ? USB_OK : USB_BUSY;
}
static void spacing(struct dali_usb_pmif *p, uint16_t address, size_t size)
{
    if (p->last_write_size && p->last_write_address < address + size &&
        address < p->last_write_address + p->last_write_size)
        p->registers.delay_us(p->registers.context, 8);
}
static int transfer(struct dali_usb_pmif *p, uint16_t address, void *bytes, size_t length, bool write)
{
    int result = ready(p);
    if (result) return result;
    spacing(p, address, length);
    uint32_t value = 0;
    if (write) {
        const uint8_t *input = bytes;
        for (size_t i = 0; i < length; ++i) value |= (uint32_t)input[i] << (8u * (unsigned)i);
        result = usb_mmio_write(&p->registers, WDATA, value);
        if (result) { p->faulted = true; return result; }
        p->last_write_address = address; p->last_write_size = (uint8_t)length;
    }
    /* An MMIO write reporting failure may nevertheless have reached PMIF. Keep
     * ownership metadata before publishing ACC so recovery cannot steal a reply. */
    p->pending_owned = true; p->pending_read = !write;
    result = usb_mmio_write(&p->registers, ACC,
        SID14_READ | (write ? WRITE : 0) | ((uint32_t)(length - 1) << 16) | address);
    uint32_t status = 0;
    if (!result) result = poll_state(p, write ? 0 : 6, &status);
    if (!result && !write) {
        result = usb_mmio_read(&p->registers, RDATA, &value);
        if (!result) result = usb_mmio_write(&p->registers, VLD_CLR, 1);
        uint32_t idle;
        if (!result) result = poll_state(p, 0, &idle);
    }
    if (!result) {
        p->pending_owned = false;
        if (status & ERROR) result = USB_IO;
    }
    if (result) {
        p->faulted = true;
        if (write) p->write_uncertain = true;
        return result;
    }
    if (!write) {
        uint8_t *output = bytes;
        for (size_t i = 0; i < length; ++i) output[i] = (uint8_t)(value >> (8u * (unsigned)i));
    }
    return USB_OK;
}
int dali_usb_pmif_init(struct dali_usb_pmif *p, const struct usb_mmio *mmio,
                       const struct dali_pmif_lock *lock, uint16_t mask, bool qualified)
{
    if (!p || !mmio || !lock || !lock->try_acquire || !lock->release) return USB_INVALID;
    if (!qualified || (mask & (1u << 14)) || !mmio->read32 || !mmio->write32 || !mmio->barrier || !mmio->delay_us)
        return USB_UNSUPPORTED;
    if (mmio->size < STA + 4) return USB_RANGE;
    if (__atomic_test_and_set(&p->locked, __ATOMIC_ACQUIRE)) return USB_BUSY;
    if (p->initialized) { __atomic_clear(&p->locked, __ATOMIC_RELEASE); return USB_BUSY; }
    int result = lock->try_acquire(lock->context);
    if (result) { __atomic_clear(&p->locked, __ATOMIC_RELEASE); return result; }
    uint32_t value;
    result = usb_mmio_read(mmio, INIT_DONE, &value);
    if (!result && !(value & 1)) result = USB_UNSUPPORTED;
    if (!result) result = usb_mmio_read(mmio, STA, &value);
    if (!result && state(value) != 0) result = USB_BUSY;
    lock->release(lock->context, false);
    /* Preserve the acquired local flag until the complete configuration is
     * published. A whole-object assignment would momentarily unlock init. */
    if (!result) { p->registers = *mmio; p->lock = *lock; p->initialized = true; }
    __atomic_clear(&p->locked, __ATOMIC_RELEASE);
    return result;
}
int dali_mt6379_read(struct dali_usb_pmif *p, uint16_t address, void *bytes, size_t length)
{
    if (!valid_range(address, bytes, length)) return USB_RANGE;
    int result = enter(p);
    if (result) return result;
    result = transfer(p, address, bytes, length, false); leave(p); return result;
}
int dali_mt6379_identify(struct dali_usb_pmif *p)
{
    int result = enter(p);
    if (result) return result;
    p->identified = false;
    uint8_t info = 0;
    result = transfer(p, 0, &info, 1, false);
    if (!result && (info & 0xf0) != 0x70) result = USB_UNSUPPORTED;
    if (!result) { p->identified = true; p->revision = info & 15; }
    leave(p); return result;
}
int dali_mt6379_write(struct dali_usb_pmif *p, uint16_t address, const void *bytes, size_t length)
{
    if (!valid_range(address, bytes, length)) return USB_RANGE;
    int result = enter(p);
    if (result) return result;
    if (!p->identified) { leave(p); return USB_UNSUPPORTED; }
    result = transfer(p, address, (void *)bytes, length, true); leave(p); return result;
}
int dali_mt6379_update8(struct dali_usb_pmif *p, uint16_t address, uint8_t mask, uint8_t value)
{
    if ((value & (uint8_t)~mask) || address > MT6379_MAX_REG) return USB_RANGE;
    int result = enter(p);
    if (result) return result;
    if (!p->identified) { leave(p); return USB_UNSUPPORTED; }
    uint8_t old = 0;
    result = transfer(p, address, &old, 1, false);
    uint8_t next = (uint8_t)((old & (uint8_t)~mask) | value);
    if (!result && old != next) result = transfer(p, address, &next, 1, true);
    leave(p); return result;
}
int dali_usb_pmif_recover(struct dali_usb_pmif *p)
{
    int result = enter(p);
    if (result) return result;
    uint32_t value;
    result = usb_mmio_read(&p->registers, INIT_DONE, &value);
    if (!result && !(value & 1)) result = USB_UNSUPPORTED;
    if (!result) result = usb_mmio_read(&p->registers, STA, &value);
    if (!result && state(value) != 0) {
        if (!p->pending_owned) result = USB_BUSY;
        else if (p->pending_read) {
            result = poll_state(p, 6, &value);
            if (!result) result = usb_mmio_read(&p->registers, RDATA, &value);
            if (!result) result = usb_mmio_write(&p->registers, VLD_CLR, 1);
            if (!result) result = poll_state(p, 0, &value);
        } else result = poll_state(p, 0, &value);
    }
    if (!result) { p->pending_owned = false; p->faulted = false; }
    leave(p); return result;
}
