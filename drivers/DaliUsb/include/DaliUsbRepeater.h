/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_REPEATER_H
#define DALI_USB_REPEATER_H
#include "DaliUsbPmif.h"
#ifdef __cplusplus
extern "C" {
#endif

struct dali_repeater_guard {
    void *context;
    /* Stop real USB submissions/requests/DMA before reset, mode or power changes.
     * Must not recursively change repeater/power state. Serialized worker only. */
    int (*quiesce)(void *);
};
enum dali_repeater_power { DALI_REPEATER_UNKNOWN, DALI_REPEATER_OFF, DALI_REPEATER_ON, DALI_REPEATER_IDLE };
struct dali_repeater_tuning {
    uint8_t vrt, squelch, disconnect, emphasis, equalization;
    int16_t intr_offset, term_offset;
};
struct dali_mt6379_repeater_config {
    struct dali_repeater_tuning device, host;
    uint8_t fs_slew, fs_hysteresis;
};
extern const struct dali_mt6379_repeater_config dali_mt6379_dali_tuning;
struct dali_mt6379_repeater {
    struct dali_usb_pmif *pmif;
    struct dali_repeater_guard guard;
    struct dali_mt6379_repeater_config config;
    enum dali_repeater_power power;
    enum usb_role role;
    uint8_t intr, term, revision;
    bool initialized, faulted, low_power;
};
/* Zeroed persistent objects, exclusive PHY/PMIC ownership and validated Dali
 * FDT/resources/tuning are mandatory before qualified=true. Probe only reads
 * real DEV_INFO/calibration/LP state; unknown LP patterns refuse startup.
 * Calibration caches contain field bits only, preserving unrelated upper bits. */
int dali_mt6379_repeater_probe(struct dali_mt6379_repeater *, struct dali_usb_pmif *,
    const struct dali_repeater_guard *, const struct dali_mt6379_repeater_config *, bool qualified);
int dali_mt6379_repeater_power_on(struct dali_mt6379_repeater *);
/* Both power_on and mode clear D+ pull-up; DEVICE must explicitly enable it
 * after its controller/endpoints are ready. HOST cannot retain a device pull-up. */
int dali_mt6379_repeater_mode(struct dali_mt6379_repeater *, enum usb_role);
int dali_mt6379_repeater_pullup(struct dali_mt6379_repeater *, bool);
/* E3+ disconnected low-power path; caller must prove no attached peripheral.
 * E1/E2 returns UNSUPPORTED. power_on performs the reference LP wake then full
 * normal initialization. Connected USB suspend keeps this PHY powered. */
int dali_mt6379_repeater_idle(struct dali_mt6379_repeater *);
/* Fault shutdown may recover only this PMIF owner's pending command. Any error
 * keeps ownership/object/state UNKNOWN; success does not authorize fault restart.
 * Destroy only after the dependent SoC PHY/controller has also stopped. */
int dali_mt6379_repeater_power_off(struct dali_mt6379_repeater *);
int dali_mt6379_repeater_destroy(struct dali_mt6379_repeater *);

struct dali_nxp_repeater_bus {
    void *context;
    /* Validated exclusive I2C6@13170000 / address0x4f. SMBus byte-data framing:
     * register byte followed by data, combined repeated-start for reads. Each
     * callback must complete/recover within10ms; never replay uncertain writes. */
    int (*read8)(void *, uint8_t reg, uint8_t *);
    int (*write8)(void *, uint8_t reg, uint8_t);
    int (*recover)(void *);
    /* Validated GPIO72 pinctrl state, FALSE=low/reset, TRUE=high/enable. */
    int (*enable)(void *, bool);
    void (*delay_us)(void *, uint32_t);
};
struct dali_nxp_repeater {
    struct dali_nxp_repeater_bus bus;
    struct dali_repeater_guard guard;
    enum usb_role role;
    bool initialized, enabled, identified, faulted;
};
/* Probe resets low10us/high4ms then checks15h=32h,14h=22h. A post-reset error
 * retains context until explicit destroy. Dali has no nonzero NXP tuning
 * properties: power-on does not write its calibration registers. */
int dali_nxp_repeater_probe(struct dali_nxp_repeater *, const struct dali_nxp_repeater_bus *,
    const struct dali_repeater_guard *, bool qualified);
int dali_nxp_repeater_mode(struct dali_nxp_repeater *, enum usb_role);
/* Full exit asserts reset; this is stronger than Linux's no-op power_off.
 * Failed quiesce/recovery/reset retains ownership. Reprobe after destruction. */
int dali_nxp_repeater_destroy(struct dali_nxp_repeater *);
#ifdef __cplusplus
}
#endif
#endif
