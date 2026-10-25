/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_RESOURCES_H
#define DALI_USB_RESOURCES_H
#include "DaliUsbDomains.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_usb_clocks {
    struct usb_mmio vlp, top;
    struct dali_domain_guard guard;
    uint32_t parent[3];
    uint8_t initially_gated, owned;
    bool initialized, on, faulted;
};
/* Zeroed persistent objects, serialized worker context. qualified means exact
 * enabled Dali FDT clock providers/IDs/windows, exclusive gate ownership, and
 * stable, held, suitable selected parent sources/rates proven by the platform.
 * This API never changes a parent, PLL, UPDATE or global voter. It only gates
 * USB sys/host/frame. PHY 26MHz and I2C clocks are separate provider obligations.
 * vlp=1c016000, top=10000000. Guard proves all affected consumers/PHY/DMA stopped
 * before mutation; no external gate/parent changes or reset during the lease.
 * Enable waits the source fence deadline (1us/5s); shutdown restores only gates
 * acquired here, preserving initially enabled clocks and every parent field. */
int dali_usb_clocks_init(struct dali_usb_clocks *, const struct usb_mmio *vlp,
    const struct usb_mmio *top, const struct dali_domain_guard *, bool qualified);
int dali_usb_clocks_enable(struct dali_usb_clocks *);
int dali_usb_clocks_disable(struct dali_usb_clocks *);
int dali_usb_clocks_destroy(struct dali_usb_clocks *);

enum dali_usb_resource_provider {
    DALI_USB_RESOURCE_UNQUALIFIED,
    DALI_USB_RESOURCE_NATIVE_HWRSC_V2
};
struct dali_usb_hwrsc {
    struct usb_mmio ippc, ao;
    struct dali_domain_guard guard;
    uint8_t gate_initial[8], gates_owned;
    uint32_t initial_request;
    bool initialized, request_owned, on, faulted;
};
/* Explicit native POWER_ON/OFF provider, not an inference from Android's GKI
 * config. qualified additionally proves validated MT6991 USB0 FDT hwrscs-vers=2,
 * no smc-req, ippc=16703e00, AO=16790000, powered accessible domains, held clocks,
 * suitable supplies and exclusive ownership of the IPPC request AND all eight
 * AO request-gate low nibbles. It is not Linux SPM_V4's software sleep voting.
 * Callers hold the shared-provider RMW lock across each entire API call (not
 * just individual callbacks), excluding concurrent writers to these words.
 * Native sleep/low-power/offload is disabled while held; no SMC is issued.
 * Guard must keep all consumers/PHY/DMA quiesced through changes and cleanup.
 * Coherent initial zero/all requests are adopted; partial requests are refused.
 * Shutdown clears request/force bits even if inherited (exclusive ownership),
 * waits matching ACK (100us/20ms) and 2ms settling, then restores only owned AO
 * nibbles. Resource ACK timeout is a failure, never permission to continue. */
int dali_usb_hwrsc_init(struct dali_usb_hwrsc *, const struct usb_mmio *ippc,
    const struct usb_mmio *ao, const struct dali_domain_guard *,
    enum dali_usb_resource_provider, bool qualified);
int dali_usb_hwrsc_enable(struct dali_usb_hwrsc *);
int dali_usb_hwrsc_disable(struct dali_usb_hwrsc *);
int dali_usb_hwrsc_destroy(struct dali_usb_hwrsc *);
/* Both APIs retain ownership on failure; enable is then latched out. Only
 * explicit disable/destroy retries owned cleanup. Dependencies remain held.
 * External reset/mutation invalidates retained state and needs platform
 * requalification; never resume such a context. on is usable only !faulted. */
#ifdef __cplusplus
}
#endif
#endif
