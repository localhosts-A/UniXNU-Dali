/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_TYPEC_H
#define DALI_USB_TYPEC_H
#include "DaliUsbPmif.h"
#ifdef __cplusplus
extern "C" {
#endif

struct dali_typec_ops {
    void *context;
    /* Real USB worker quiesce: stop submissions, sync filesystems, cancel and
     * stop all DMA before termination/orientation or VBUS can change. This is
     * the controller/storage stop primitive, not a whole-role callback that
     * recursively changes CC or VBUS. */
    int (*quiesce)(void *);
    /* Board connector checks (including enabled ADC/DPDM checks). Required for
     * source power. A missing/unqualified check must return UNSUPPORTED. */
    int (*connector_ok)(void *);
};
enum dali_cc_mode { DALI_CC_OPEN, DALI_CC_DEVICE, DALI_CC_HOST, DALI_CC_DRP };
struct dali_typec_sample {
    enum usb_role role;
    uint16_t millivolts;
    uint8_t cc1, cc2, polarity;
    bool vbus_present, safe0v, toggling, unsupported, rearm_required;
};
struct dali_usb_typec {
    struct dali_usb_pmif *pmif;
    struct dali_typec_ops ops;
    struct dali_typec_sample candidate, published;
    uint64_t last_us, candidate_since;
    uint16_t device_id;
    enum dali_cc_mode mode;
    bool initialized, started, faulted, have_candidate, have_poll;
};
/* Objects must be zero-initialized before probe/init; never reinitialize a live
 * object. Serialized platform worker; TCPC/charger ownership remains exclusive,
 * and all PMIF users must use the same transport lock.
 * Probe is read-only and checks real TCPCI VID/PID/DID. No PD transmission,
 * high-voltage negotiation, VCONN, source boost or global PMIC IRQ change. */
int dali_usb_typec_probe(struct dali_usb_typec *, struct dali_usb_pmif *, const struct dali_typec_ops *);
/* Polling TCPC setup with standard/vendor alerts masked locally; requires boost
 * already off and real USB quiesce. Initializes analog/filter/protection state,
 * disables PD RX and selects fixed Rd, fixed default Rp, open or DRP. */
int dali_usb_typec_start(struct dali_usb_typec *, enum dali_cc_mode);
int dali_usb_typec_mode(struct dali_usb_typec *, enum dali_cc_mode);
int dali_usb_typec_orientation(struct dali_usb_typec *, uint8_t polarity);
/* Freeze a freshly rechecked published attachment into fixed Rd or default Rp
 * (inactive source side Rd), and apply its orientation while USB/boost are off.
 * This preserves the published role, unlike starting a new unattached mode. */
int dali_usb_typec_latch_attachment(struct dali_usb_typec *);
int dali_usb_typec_read(struct dali_usb_typec *, struct dali_typec_sample *);
/* Poll at <=5ms intervals; a missed interval or rejected snapshot restarts the
 * candidate timer. Stable attachment requires120ms; departure/change
 * from an active role publishes STOPPED after15ms before a new120ms attachment.
 * No power or USB role is changed here: the caller feeds the published role to
 * usb_role_switch and applies orientation while quiesced. A lost DRP toggle is
 * rearmed only while unpublished, quiesced and boost-off; otherwise it faults. */
int dali_usb_typec_poll(struct dali_usb_typec *, uint64_t now_us, struct dali_typec_sample *);
int dali_usb_typec_stop(struct dali_usb_typec *);
int dali_usb_typec_destroy(struct dali_usb_typec *);

enum dali_vbus_state { DALI_VBUS_OFF, DALI_VBUS_ON, DALI_VBUS_UNKNOWN };
struct dali_usb_vbus {
    struct dali_usb_typec *typec;
    enum dali_vbus_state state;
    bool initialized, faulted;
};
/* Reads TOP2 and refuses to adopt inherited source power. Charger/TM ownership
 * is an additional platform prerequisite, not implied by PMIF bus ownership. */
int dali_usb_vbus_init(struct dali_usb_vbus *, struct dali_usb_typec *, bool charger_owned);
/* Exactly5.00V and500mA, default-current Rp only. Requires published HOST plus a
 * fresh matching CC sample latched into fixed source, safe0V/no incoming VBUS, connector check and DMA
 * quiesce. Uses BE16 voltage selector and protected PP7/TM sequence. Repeated
 * enable rechecks actual output/boost without replaying power writes; any lost
 * output latches UNKNOWN until explicit disable and object reinitialization. */
int dali_usb_vbus_enable(struct dali_usb_vbus *);
/* May be called after a fault; explicitly recovers the owned PMIF command then
 * closes/restores TM/PP7 and confirms OUR boost disabled. It never forces the
 * connector to0V: incoming VBUS from a new source may legitimately remain. Any
 * failure latches UNKNOWN and the caller must retain ownership/stop role work. */
int dali_usb_vbus_disable(struct dali_usb_vbus *);
/* Destroy VBUS before its TCPC instance; failed shutdown retains the object. */
int dali_usb_vbus_destroy(struct dali_usb_vbus *);

#ifdef __cplusplus
}
#endif
#endif
