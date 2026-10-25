/* SPDX-License-Identifier: GPL-2.0-only */
/* MediaTek (C)2023 pinctrl-mt6991.c and pinctrl-mtk-common-v2.c field/alias
 * contract, fixed device_modules e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee. */
#include "DaliUsbGpio.h"
#define GPIO72_BIT 0x100u
#define GPIO72_DIR 0x20u
#define GPIO72_DO 0x120u
#define GPIO72_MODE 0x390u
static int fault(struct dali_usb_gpio72 *g, int result)
{ g->faulted = true; g->level_known = false; return result; }
static int gpio_mode(const struct usb_mmio *m)
{
    uint32_t mode;
    int result = usb_mmio_read(m, GPIO72_MODE, &mode);
    return result ? result : (mode & 15) ? USB_UNSUPPORTED : USB_OK;
}
int dali_usb_gpio72_init(struct dali_usb_gpio72 *g, const struct usb_mmio *m,
    const struct dali_repeater_guard *guard, bool qualified)
{
    if (!g || !m || !guard || !guard->quiesce || !m->read32 || !m->write32 || !m->barrier) return USB_INVALID;
    if (g->initialized) return USB_BUSY;
    if (!qualified) return USB_UNSUPPORTED;
    if (m->size < GPIO72_MODE + 4) return USB_RANGE;
    int result = gpio_mode(m);
    uint32_t direction, output;
    if (!result) result = usb_mmio_read(m, GPIO72_DIR, &direction);
    if (!result) result = usb_mmio_read(m, GPIO72_DO, &output);
    if (result) return result;
    *g = (struct dali_usb_gpio72){.registers=*m, .guard=*guard, .initialized=true,
        .level_known=(direction & GPIO72_BIT) != 0, .enabled=(output & GPIO72_BIT) != 0};
    return USB_OK;
}
int dali_usb_gpio72_set(struct dali_usb_gpio72 *g, bool enabled)
{
    if (!g || !g->initialized) return USB_INVALID;
    if (g->faulted && enabled) return USB_IO;
    int result = g->guard.quiesce(g->guard.context);
    if (result) return result;
    result = gpio_mode(&g->registers);
    if (result) return fault(g, result);
    /* DO-before-DIR also avoids a transient opposite output when input was
     * inherited. SET/CLR writes cannot lose another pin owner's update. */
    g->level_known = false;
    result = usb_mmio_write(&g->registers, GPIO72_DO + (enabled ? 4u : 8u), GPIO72_BIT);
    if (!result) result = usb_mmio_write(&g->registers, GPIO72_DIR + 4, GPIO72_BIT);
    uint32_t output, direction;
    if (!result) result = usb_mmio_read(&g->registers, GPIO72_DO, &output);
    if (!result) result = usb_mmio_read(&g->registers, GPIO72_DIR, &direction);
    if (!result) result = gpio_mode(&g->registers);
    if (!result && (!(direction & GPIO72_BIT) || ((output & GPIO72_BIT) != 0) != enabled)) result = USB_IO;
    if (result) return fault(g, result);
    g->enabled = enabled; g->level_known = true;
    return USB_OK;
}
int dali_usb_gpio72_destroy(struct dali_usb_gpio72 *g)
{
    int result = dali_usb_gpio72_set(g, false);
    if (!result) *g = (struct dali_usb_gpio72){0};
    return result;
}
