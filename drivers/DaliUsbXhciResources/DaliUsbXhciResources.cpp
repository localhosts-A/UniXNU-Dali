// SPDX-License-Identifier: BSD-2-Clause-Patent
#include <IOKit/IOLib.h>
#include <libkern/c++/OSData.h>
#include "DaliUsbXhciResources.hpp"

OSDefineMetaClassAndStructors(DaliUsbXhciResources, IOService)

static bool
hasXhciCompatible(IOService *provider)
{
    static constexpr char expected[] = "mediatek,mtk-xhci";
    auto *compatible = OSDynamicCast(OSData, provider->getProperty("compatible"));
    if (compatible == nullptr || compatible->getLength() != sizeof(expected)) {
        return false;
    }
    const auto *bytes = static_cast<const char *>(compatible->getBytesNoCopy());
    for (size_t index = 0; index < sizeof(expected); ++index) {
        if (bytes[index] != expected[index]) {
            return false;
        }
    }
    return true;
}

bool
DaliUsbXhciResources::start(IOService *provider)
{
    mValid = false;
    mResources = {};
    if (provider == nullptr || !IOService::start(provider)) {
        return false;
    }

    if (!hasXhciCompatible(provider)) {
        IOLog("DaliUsbXhciResources: rejected incompatible xhci0 provider\n");
        return false;
    }

    auto *wire = OSDynamicCast(OSData, provider->getProperty("resources-v1"));
    struct dali_usb_xhci_resource_v1 decoded = {};
    if (wire == nullptr ||
        !dali_usb_xhci_decode_resource_v1(wire->getBytesNoCopy(), wire->getLength(),
                                          &decoded)) {
        IOLog("DaliUsbXhciResources: rejected missing or invalid resources-v1\n");
        return false;
    }

    mResources = decoded;
    mValid = true;
    IOLog("DaliUsbXhciResources: validated xhci0 resource descriptor v1\n");
    registerService();
    return true;
}

void
DaliUsbXhciResources::stop(IOService *provider)
{
    mValid = false;
    mResources = {};
    IOService::stop(provider);
}

bool
DaliUsbXhciResources::copyResourceDescription(
    struct dali_usb_xhci_resource_v1 *output) const
{
    if (!mValid || output == nullptr) {
        return false;
    }
    *output = mResources;
    return true;
}
