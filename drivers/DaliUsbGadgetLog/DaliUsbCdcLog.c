/* SPDX-License-Identifier: GPL-2.0-only */
#include "DaliUsbCdcLog.h"

#include <limits.h>

struct dali_usb_cdc_log_registry {
    struct usb_mtu3_device *device;
    struct usb_mtu3_device *retiring;
    uint64_t dropped;
    uint32_t writers;
    uint32_t management_lock;
};

static struct dali_usb_cdc_log_registry g_registry;

static bool management_lock(void)
{
    uint32_t expected = 0;
    return __atomic_compare_exchange_n(&g_registry.management_lock, &expected, 1, false,
                                      __ATOMIC_ACQUIRE, __ATOMIC_RELAXED);
}

static void management_unlock(void)
{
    __atomic_store_n(&g_registry.management_lock, 0, __ATOMIC_RELEASE);
}

static void record_dropped(size_t length)
{
    __atomic_fetch_add(&g_registry.dropped, (uint64_t)length, __ATOMIC_RELAXED);
}

int dali_usb_cdc_log_bind(struct usb_mtu3_device *device)
{
    if (!device) return USB_INVALID;
    if (!management_lock()) return USB_BUSY;

    int result = USB_OK;
    struct usb_mtu3_device *current = g_registry.device;
    if (!device->initialized || !device->cdc || device->faulted || device->tearing_down) {
        result = USB_INVALID;
    } else if (current == device) {
        result = USB_OK;
    } else if (current || g_registry.retiring ||
               __atomic_load_n(&g_registry.writers, __ATOMIC_ACQUIRE) != 0) {
        result = USB_BUSY;
    } else {
        g_registry.device = device;
    }

    management_unlock();
    return result;
}

int dali_usb_cdc_log_unbind(struct usb_mtu3_device *device)
{
    if (!device) return USB_INVALID;
    if (!management_lock()) return USB_BUSY;

    struct usb_mtu3_device *current = g_registry.device;
    if (current == device && !g_registry.retiring) {
        g_registry.device = NULL;
        g_registry.retiring = device;
    } else if (current || g_registry.retiring != device) {
        management_unlock();
        return USB_INVALID;
    }

    if (__atomic_load_n(&g_registry.writers, __ATOMIC_ACQUIRE) == 0) {
        g_registry.retiring = NULL;
        management_unlock();
        return USB_OK;
    }

    management_unlock();
    return USB_BUSY;
}

size_t dali_usb_cdc_log_write(const void *bytes, size_t length)
{
    if (!length) return 0;
    if (!bytes) {
        record_dropped(length);
        return 0;
    }

    /* Producers take only a single try-lock to pair the published object with
     * its transient reference. Contention drops this write rather than waiting
     * in a diagnostic or interrupt context.
     */
    if (!management_lock()) {
        record_dropped(length);
        return 0;
    }
    struct usb_mtu3_device *device = g_registry.device;
    if (!device || __atomic_load_n(&g_registry.writers, __ATOMIC_RELAXED) == UINT32_MAX) {
        management_unlock();
        record_dropped(length);
        return 0;
    }
    __atomic_fetch_add(&g_registry.writers, 1, __ATOMIC_ACQ_REL);
    management_unlock();

    size_t accepted = usb_mtu3_device_log(device, bytes, length);
    __atomic_fetch_sub(&g_registry.writers, 1, __ATOMIC_RELEASE);
    return accepted;
}

uint64_t dali_usb_cdc_log_dropped(void)
{
    return __atomic_load_n(&g_registry.dropped, __ATOMIC_RELAXED);
}
