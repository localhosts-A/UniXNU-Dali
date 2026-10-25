/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DALI_USB_PMIF_H
#define DALI_USB_PMIF_H
#include "UsbCore.h"
#ifdef __cplusplus
extern "C" {
#endif

struct dali_pmif_lock {
    void *context;
    /* Shared, IRQ-safe arbitration for every user of PMIF-M SWINF2, including
     * keypad sampling. Return BUSY without waiting when it cannot be acquired.
     * This is not the Android/device inspection lock. The platform must reserve
     * the channel for this persistent client when release reports pending_owned;
     * other clients stay blocked until this owner recovers and releases false.
     * Merely checking hardware idle cannot preserve response ownership. */
    int (*try_acquire)(void *);
    /* A failed release must be reported: no subsequent operation may assume
     * global lease/TPL restoration succeeded. */
    int (*release)(void *, bool pending_owned);
};
struct dali_usb_pmif {
    struct usb_mmio registers;
    struct dali_pmif_lock lock;
    uint16_t last_write_address;
    uint8_t last_write_size, locked, revision, sid;
    bool initialized, identified, faulted, pending_owned, pending_read, write_uncertain;
};

/* Fixed MT6991 PMIF-M channel2, MT6379 SID14 (M-route bit14 clear). The platform
 * validates the FDT, inherits valid clocks/routes and proves ownership before
 * setting qualified. Init only reads INIT_DONE/STA under the shared lock.
 * Storage must be zeroed; a live object cannot be reinitialized. */
int dali_usb_pmif_init(struct dali_usb_pmif *, const struct usb_mmio *,
                       const struct dali_pmif_lock *, uint16_t spmi_device_mask, bool qualified);
/* Separate MT6373 SID5 VUSB-only client on the SAME PMIF-M/SWINF2 lease.
 * qualified additionally validates the exact enabled FDT MT6373/regulator and
 * USB vusb33 reference. No clock/route/IRQ writes or arbitrary SID access. */
int dali_usb_pmif_init_vusb(struct dali_usb_pmif *, const struct usb_mmio *,
                           const struct dali_pmif_lock *, uint16_t spmi_device_mask, bool qualified);
/* Verify CID high0x9=0x73 from the fixed MT6373 debug cid-addr declaration,
 * cache CID low0x8 and also require nonzero SWCID0xb like mtk-spmi-pmic.c.
 * The platform validates the same cid-addr tuple before permitting this client. */
int dali_mt6373_vusb_identify(struct dali_usb_pmif *);
/* Read whitelist: CID low/high0x8/0x9, SWCID0xb, VUSB control1b95,
 * trim1e0c, selector1e0d.
 * Sole writable operation atomically sets enable bit0; never disable, change
 * voltage/trim/LP, or touch any other MT6373 register. Identification required. */
int dali_mt6373_vusb_read8(struct dali_usb_pmif *, uint16_t, uint8_t *);
int dali_mt6373_vusb_enable(struct dali_usb_pmif *);
/* Read DEV_INFO and verify the real MT6379 vendor nibble (0x70). All PMIC
 * writes, including update8, remain disabled until this check succeeds. */
int dali_mt6379_identify(struct dali_usb_pmif *);
/* MT6379 regmap contract: 16-bit addresses <=0xdff, 1 or 2 byte transfers.
 * Writes wait for completion and error status. They are never replayed.
 * Read buffers remain unchanged on any failure. No PMIF clock/route, global
 * reset, IRQ or RCS-retrigger configuration is changed by this transport. */
int dali_mt6379_read(struct dali_usb_pmif *, uint16_t address, void *, size_t);
int dali_mt6379_write(struct dali_usb_pmif *, uint16_t address, const void *, size_t);
/* Atomic read/modify/write while holding the SAME shared lock. */
int dali_mt6379_update8(struct dali_usb_pmif *, uint16_t address, uint8_t mask, uint8_t value);
/* Recover only a transaction issued by this object: consume its late read
 * response, or wait for its write to become idle. Foreign valid state is never
 * acknowledged. A successful recovery does not clear write_uncertain or replay
 * an operation; the caller must account for the possibly applied PMIC write. */
int dali_usb_pmif_recover(struct dali_usb_pmif *);

#ifdef __cplusplus
}
#endif
#endif
