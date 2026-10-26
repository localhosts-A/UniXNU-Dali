// SPDX-License-Identifier: BSD-2-Clause-Patent
// Separate kext: the real IOAcceleratorES superclass is supplied by IOGPUFamily.
// It must be linked/loaded after that dependency, never force-loaded into XNU.
#include "../NativeMetal/OriginalAcceleratorABI.h"
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSBoolean.h>
#include <mach/kmod.h>

class DaliMaliAccelerator : public IOAcceleratorES {
    OSDeclareDefaultStructors(DaliMaliAccelerator)
public:
    bool start(IOService *provider) override;
};
OSDefineMetaClassAndStructors(DaliMaliAccelerator,IOAcceleratorES)
bool DaliMaliAccelerator::start(IOService *provider) {
    if(!provider||!provider->metaCast("DaliMali"))return false;
    auto *abi=OSDynamicCast(OSNumber,provider->getProperty("NativeMaliABI"));
    if(!abi||abi->unsigned32BitValue()!=1||provider->getProperty("device-lost")==kOSBooleanTrue)return false;
    if(!IOAcceleratorES::start(provider))return false;
    setName("DaliMaliAccelerator");
    setProperty("MetalPluginName","NativeMaliMetal");
    setProperty("MetalPluginClassName","NativeMaliMetalDevice");
    setProperty("NativeMaliABI",uint64_t(1),32);
    setProperty("hardware-qualified",false);
    registerService();return true;
}
// OSKext::start calls OSRuntimeInitializeCPP(this) before the module start;
// OSKext::stop finalizes C++ after this stop succeeds (XNU 10063.141.1).
extern "C" kern_return_t dali_mali_accelerator_start(kmod_info_t *,void *) {return KERN_SUCCESS;}
extern "C" kern_return_t dali_mali_accelerator_stop(kmod_info_t *,void *) {return KERN_SUCCESS;}
extern "C" {
KMOD_EXPLICIT_DECL(org.unixnu.DaliMaliAccelerator,"1.0.0",dali_mali_accelerator_start,dali_mali_accelerator_stop)
}
