/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_PHY_H
#define DALI_USB_PHY_H
#include "DaliUsbRepeater.h"
#include "DaliUsbCalibration.h"
#ifdef __cplusplus
extern "C" {
#endif
struct dali_usb_phy {
    struct usb_mmio registers;
    struct dali_repeater_guard guard;
    struct dali_xsphy_calibration calibration;
    struct dali_nxp_repeater *nxp;
    struct dali_mt6379_repeater *mt;
    enum dali_repeater_power power;
    enum usb_role role;
    uint8_t intr, term;
    bool initialized, faulted, fm_owned;
};
/* Zeroed persistent storage, serialized worker. qualified requires actual
 * USB2 PHY0@16730000/400, held clocks/power, stable26MHz calibration reference,
 * validated saved-Dali FDT properties and exclusive controller/PHY ownership.
 * No UART/JTAG owner may remain. Both actual repeaters must already be probed
 * and retained, NXP enabled/identified and MT not powered. Init is read-only;
 * refuses an inherited active frequency meter. Never infer calibration values
 * or chip revision from missing data. Runtime resource provider is separate. */
int dali_usb_phy_init(struct dali_usb_phy *, const struct usb_mmio *,
    struct dali_nxp_repeater *, struct dali_mt6379_repeater *,
    const struct dali_xsphy_calibration *, const struct dali_repeater_guard *, bool qualified);
/* Cold USB2 startup includes reference init, both repeaters, SoC power/eFuse,
 * checked slew calibration, role tuning and IDDIG. Leaves D+ pull-up clear.
 * Failed writes retain unknown power; faulted instances cannot restart.
 * DTM1 session indicators are logical PHY controls, not voltage evidence:
 * the role worker must check actual TCPC/charger interlocks before starting
 * a controller or enabling VBUS. This layer never enables a source supply. */
int dali_usb_phy_power_on(struct dali_usb_phy *, enum usb_role);
int dali_usb_phy_mode(struct dali_usb_phy *, enum usb_role);
int dali_usb_phy_pullup(struct dali_usb_phy *, bool);
/* Full valid-USB-role suspend with BC1.1 kept disabled. Charger/L2 analog
 * policy is not enabled by this API. Quiesce before changing either repeater
 * or SoC. Retain all dependencies/clock ownership on failure. */
int dali_usb_phy_power_off(struct dali_usb_phy *);
/* Destroy PHY before destroying repeaters/I2C/GPIO/PMIF or releasing clocks. */
int dali_usb_phy_destroy(struct dali_usb_phy *);
#ifdef __cplusplus
}
#endif
#endif
