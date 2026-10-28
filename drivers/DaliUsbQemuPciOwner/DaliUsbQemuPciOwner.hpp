/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DALI_USB_QEMU_PCI_OWNER_HPP
#define DALI_USB_QEMU_PCI_OWNER_HPP

#include <IOKit/IOService.h>
#include <IOKit/IOLocks.h>
#include "DaliUsbQemuPci.h"
#include "UsbCore.h"

class IOMemoryMap;

/* QEMU virt PCI-host only. This service matches the published /arm-io nub,
 * reads the nested /arm-io/qemu-pcie-host contract, validates bus-0 ECAM and
 * the firmware-assigned xHCI BAR0, then owns its bounded CPU mapping. It does
 * not write PCI configuration space or start the controller. */
class DaliUsbQemuPciOwner : public IOService {
    OSDeclareDefaultStructors(DaliUsbQemuPciOwner)

    struct dali_qemu_pci_host mHost = {};
    struct dali_qemu_pci_xhci mXhci = {};
    IOMemoryMap *mEcamMap = nullptr;
    IOMemoryMap *mBarMap = nullptr;
    IOLock *mRegisterLock = nullptr;
    int (*mRegisterGuard)(void *) = nullptr;
    void *mRegisterGuardContext = nullptr;
    bool mMapped = false;
    bool mRegisterOpsBound = false;
    bool mRegisterRefHeld = false;

    static bool readConfigFunction(void *, uint8_t, uint8_t, uint8_t,
                                   uint8_t[DALI_QEMU_PCI_CONFIG_BYTES]);
    static int registerRead32(void *, uint32_t, uint32_t *);
    static int registerWrite32(void *, uint32_t, uint32_t);
    static bool validRegisterAccess(const DaliUsbQemuPciOwner *, uint32_t);
    static void registerBarrier(void *);
    static void registerDelay(void *, uint32_t);
    void releaseRegisterLock();

public:
    bool start(IOService *provider) override;
    void stop(IOService *provider) override;
    void free() override;
    bool hasMappedBar() const { return mMapped && mBarMap != nullptr; }
    bool copyXhciDescription(struct dali_qemu_pci_xhci *output) const;

    /* Bind xHCI MMIO only under a caller-owned QEMU controller lease. The
     * caller must retain the guard context until releaseGuardedMmio succeeds. */
    bool bindGuardedMmio(int (*guard)(void *), void *guard_context,
                         struct usb_mmio *out);
    bool releaseGuardedMmio(bool controller_stopped);
};

#endif
