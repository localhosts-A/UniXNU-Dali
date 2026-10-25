/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_CLOCK_RATES_H
#define DALI_USB_CLOCK_RATES_H
#include "UsbCore.h"
#ifdef __cplusplus
extern "C" {
#endif
enum dali_usb_clock_index {
    DALI_CLOCK_SYS, DALI_CLOCK_HOST, DALI_CLOCK_FRAME,
    DALI_CLOCK_I2C6, DALI_CLOCK_I2C_DMA, DALI_CLOCK_COUNT
};
struct dali_usb_clock_maps {
    struct usb_mmio vlp, top, apmixed, impw, perao;
};
struct dali_usb_clock_rates {
    uint32_t hz[DALI_CLOCK_COUNT];
    uint8_t parent[DALI_CLOCK_COUNT], enabled;
};
/* Read-only MT6991 A0/B0 selected-source snapshot, no PLL/mux/gate/voter writes.
 * Exact validated enabled providers: VLP1c016000, TOP10000000, APMIXED10000800,
 * IMPW131b0000 and PERAO16640000. qualified means these powered mappings and
 * the supplied 26MHz reference are proven, and a source-stability lease excludes
 * software/firmware changes throughout this call AND subsequent consumer use.
 * ulposc_hz is an independently proven, held source rate, required only when
 * AXI selects ULPOSC/8; zero means unavailable, never a guessed DT/OPP default.
 * Two masked register passes reject observed transitions; they cannot establish
 * that external lease or catch an ABA change. PLL FENC means prepared in the
 * fixed driver, not PLL lock. Rates reproduce CCF PCW/postdiv + factor rounding;
 * they are configured rates, not frequency measurements or suitability proof.
 * enabled bits describe all gates in that path, not ownership. Gated outputs
 * retain their selected source rate. Unprepared selected parents, CKSTA busy,
 * inconsistent active fences or changing snapshots refuse publication.
 * Output must be distinct from input storage and changes only on success. */
int dali_usb_clock_rates_read(const struct dali_usb_clock_maps *, uint32_t sw_ver,
    uint32_t reference_hz, uint32_t ulposc_hz, bool qualified,
    struct dali_usb_clock_rates *);
#ifdef __cplusplus
}
#endif
#endif
