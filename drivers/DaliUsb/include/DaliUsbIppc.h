/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_IPPC_H
#define DALI_USB_IPPC_H
#include "DaliUsbDomains.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_usb_ippc {
    struct usb_mmio registers, ao, mbist;
    struct dali_domain_guard guard;
    enum usb_role role;
    uint8_t u2_ports, u3_ports;
    bool initialized, faulted, stopped, reset_done, ao_reset_owned, ip_reset_owned;
};
/* Zeroed persistent object, serialized worker with shared-provider RMW lock
 * held across each entire API call. qualified proves enabled Dali USB0 OTG
 * resources: IPPC16703e00, AO16790000/reset-mask13, MBIST16781000, UTMI16,
 * keep-host-on=false and exclusive ownership of the complete IP/ports/reset.
 * Domains/clocks/windows must already be accessible and remain held on error.
 * Init is read-only, rejects inherited asserted resets/gates, and captures real
 * host port capabilities: one/two USB2 ports (known UTMI16 fields), up to four
 * USB3 register slots. Only USB2 port0 will be enabled; all others stay off.
 * Guard proves BOTH controllers' submissions/IRQs/queues/DMA have stopped;
 * it must not touch windows while our AO reset is asserted. A persistent
 * successful drain proof is required for retrying such an owned reset. */
int dali_usb_ippc_init(struct dali_usb_ippc *, const struct usb_mmio *ippc,
    const struct usb_mmio *ao, const struct usb_mmio *mbist,
    const struct dali_domain_guard *, bool qualified);
/* Cold preparation order: held clocks/domains -> AO reset -> HWRSC request ->
 * PHY/repeaters on+role -> IP reset -> enable(role) -> actual MAC/xHCI start.
 * AO reset is only legal before PHY power-on, with no active native role.
 * Source pulse widths are1ms AO and1us IP. Faults retain reset ownership;
 * stop can finish only our own uncertain reset, then shut all ports down. */
int dali_usb_ippc_ao_reset(struct dali_usb_ippc *);
int dali_usb_ippc_reset(struct dali_usb_ippc *);
int dali_usb_ippc_enable(struct dali_usb_ippc *, enum usb_role);
/* Controller drain and real TCPC/VBUS interlocks belong to the role worker.
 * IPPC HOST_SEL/OTG/session bits are logical controls, never voltage evidence.
 * No external power, forced gadget VBUS or DMA queue is enabled here.
 * Stop must succeed before PHY/HWRSC/domains/clocks may be released. MBIST
 * (100ms/3s), sleep(100us/100ms), stable clocks(100us/20ms+10ms) are hard gates.
 * No bus-busy reset fallback. Destroy retains context if any gate fails.
 * External mutation/reset invalidates context; platform requalification is
 * required. New startup is refused after a fault until teardown/reinit. */
/* role is usable only when !faulted; stopped=true is confirmed physical port/IP
 * power-down, not merely the worker's STOPPED intent after an error. */
int dali_usb_ippc_stop(struct dali_usb_ippc *);
int dali_usb_ippc_destroy(struct dali_usb_ippc *);
#ifdef __cplusplus
}
#endif
#endif
