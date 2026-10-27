// SPDX-License-Identifier: BSD-2-Clause-Patent
// Append bounded XNU platform/GIC/timer checkpoints to the inherited ramoops console.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <libkern/c++/OSSharedPtr.h>
#include <libkern/c++/OSData.h>
#include <pexpert/pexpert.h>
#include "DaliRecovery.h"
#include "DaliManualRecovery.h"
#include "DaliPsci.h"
#include "../DaliUfs/DaliUfsRecovery.h"

#if !defined(DALI_XNU_DARWIN) || !defined(DALI_XNU_BUILD)
#error "DaliBootLog must be built with the pinned XNU version and build number"
#endif

extern "C" {
extern volatile uint32_t armdt_platform_configured;
extern volatile uint32_t armcpu_registered;
extern volatile uint32_t armcpu_gic_ready;
extern volatile uint64_t armcpu_timer_interrupts;
void dali_bootstrap_log(const char *message);
void dali_bootstrap_log_end(void);
}

static unsigned ufsPlatformReady = 0;
static char ufsPlatformToken[33] = {};
extern "C" bool dali_ufs_platform_ready(const char *token)
{
  return __atomic_load_n (&ufsPlatformReady, __ATOMIC_ACQUIRE) == 1 && token != nullptr &&
      memcmp (token, ufsPlatformToken, sizeof (ufsPlatformToken)) == 0 &&
      armdt_platform_configured == 1 && armcpu_registered == 1 &&
      armcpu_gic_ready == 1 && armcpu_timer_interrupts != 0;
}

static constexpr uintptr_t kConsolePhysical = 0xAEC0F000;
static constexpr uint32_t kConsoleSignature = 0x43474244;
static constexpr uint32_t kConsoleBytes = 0x00200000;

struct RamoopsConsole {
  volatile uint32_t signature;
  volatile uint32_t start;
  volatile uint32_t size;
};

static char *appendUnsigned(char *out, char *end, uint64_t value, unsigned radix)
{
  char digits[24];
  unsigned count = 0;
  do {
    const unsigned digit = static_cast<unsigned> (value % radix);
    digits[count++] = static_cast<char> (digit < 10 ? '0' + digit : 'a' + digit - 10);
    value /= radix;
  } while (value != 0 && count < sizeof (digits));
  while (count != 0 && out < end) { *out++ = digits[--count]; }
  return out;
}

static char *appendText(char *out, char *end, const char *text)
{
  while (*text != '\0' && out < end) { *out++ = *text++; }
  return out;
}

static bool appendRamoops(IOMemoryMap *map, const char *text, size_t length)
{
  if (map == nullptr || text == nullptr) { return false; }
  uint64_t daif;
  __asm__ volatile ("mrs %0, daif\n\tmsr daifset, #3" : "=r" (daif) :: "memory");
  auto *ring = reinterpret_cast<RamoopsConsole *> (map->getVirtualAddress ());
  const uint32_t capacity = kConsoleBytes - sizeof (RamoopsConsole);
  auto *data = reinterpret_cast<volatile uint8_t *> (ring + 1);
  if (ring->signature != kConsoleSignature || ring->start >= capacity || ring->size > capacity) {
    __asm__ volatile ("msr daif, %0" :: "r" (daif) : "memory");
    return false;
  }
  for (size_t index = 0; index < length; index++) {
    const uint32_t at = ring->start;
    data[at] = static_cast<uint8_t> (text[index]);
    ring->start = at + 1 == capacity ? 0 : at + 1;
    if (ring->size < capacity) { ring->size++; }
  }
  __sync_synchronize ();
  __asm__ volatile ("msr daif, %0" :: "r" (daif) : "memory");
  return true;
}

struct RecoveryContext {
  uintptr_t rgu;
  IOMemoryMap *console;
  const char *token;
  uint32_t psciFeatures;
  uint32_t psciResult;
  uint32_t resetType;
  DaliResetMethod method = DaliResetMethod::Aee;
  bool qualification = false;
  uint32_t frequency = 0;
  uint64_t elapsedTicks = 0;
};

static bool recoveryRecord(void *, const char *, uint32_t, uint32_t);

static uint64_t recoveryTicks(void) {
  uint64_t ticks;
  __asm__ volatile ("isb\n\tmrs %0, cntpct_el0" : "=r" (ticks) :: "memory");
  return ticks;
}

static __attribute__((noinline)) uint32_t recoveryRead(void *opaque, uint32_t offset)
{
  auto *context = static_cast<RecoveryContext *> (opaque);
  const uint32_t value = *reinterpret_cast<volatile uint32_t *> (context->rgu + offset);
  __asm__ volatile ("dsb sy" ::: "memory");
  return value;
}

static __attribute__((noinline)) void recoverySystemReset(void *opaque)
{
  auto *context = static_cast<RecoveryContext *> (opaque);
  if (context->method == DaliResetMethod::Off) { return; }
  if (context->method != DaliResetMethod::Cold) {
    context->psciFeatures = recoveryPsciFeatures ();
    if (!daliPsciFeatureSupported (context->psciFeatures)) {
      if (context->rgu != 0) {
        recoveryRecord (context, "features-refused", recoveryRead (context, 0x24), recoveryRead (context, 0));
      }
      return; // Never fall back to another reset method.
    }
  }
  if (context->rgu != 0 &&
      !recoveryRecord (context, "smc", recoveryRead (context, 0x24), recoveryRead (context, 0))) { return; }
  context->psciResult = context->method == DaliResetMethod::Cold ?
      recoveryPsciColdReset () : recoveryPsciReset2 (context->resetType);
}

static __attribute__((noinline)) void recoveryFence(void *)
{
  __asm__ volatile ("dsb sy" ::: "memory");
}

static void manualWatchdogWrite(void *opaque, uint32_t offset, uint32_t value)
{
  auto *context = static_cast<RecoveryContext *> (opaque);
  *reinterpret_cast<volatile uint32_t *> (context->rgu + offset) = value;
}

static bool manualRecord(IOMemoryMap *console, const char *token, const char *stage)
{
  char line[128] = "DALI-MANUAL: run=";
  char *at = line + sizeof ("DALI-MANUAL: run=") - 1;
  char *end = line + sizeof (line) - 2;
  at = appendText (at, end, token);
  at = appendText (at, end, " stage=");
  at = appendText (at, end, stage);
  *at++ = '\n';
  return appendRamoops (console, line, static_cast<size_t> (at - line));
}

static __attribute__((noinline)) void waitForManualRecovery(IOMemoryMap *console, const char *token,
    bool (*quiesce)(void *) = nullptr, void *owner = nullptr)
{
  IORegistryEntry *entry = IORegistryEntry::fromPath ("/arm-io/dali-boot-log", gIODTPlane);
  if (entry == nullptr) { return; }
  auto *resource = OSDynamicCast (OSData, entry->getProperty ("manual-recovery-gpio"));
  uint64_t gpio[2] = {};
  if (resource != nullptr && resource->getLength () == sizeof (gpio)) {
    memcpy (gpio, resource->getBytesNoCopy (), sizeof (gpio));
  }
  entry->release ();
  if (gpio[0] != 0x1002D000 || gpio[1] != 0x1000) { return; }
  OSSharedPtr<IOMemoryDescriptor> gd (
      IOMemoryDescriptor::withPhysicalAddress (gpio[0], gpio[1], kIODirectionInOut), OSNoRetain);
  OSSharedPtr<IOMemoryDescriptor> wd (
      IOMemoryDescriptor::withPhysicalAddress (0x1C010000, 0x1000, kIODirectionInOut), OSNoRetain);
  if (!gd || !wd) { return; }
  OSSharedPtr<IOMemoryMap> gm (gd->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
  OSSharedPtr<IOMemoryMap> wm (wd->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
  if (!gm || !wm) { return; }
  RecoveryContext pins = {static_cast<uintptr_t> (gm->getVirtualAddress ()), console, token, 0, 0, 0};
  RecoveryContext watchdog = {static_cast<uintptr_t> (wm->getVirtualAddress ()), console, token, 0, 0, 0};
  const uint32_t mode = (recoveryRead (&pins, 0x370) >> 16) & 15;
  if (mode > 1 || (recoveryRead (&pins, 0x10) & (1U << 28)) ||
      recoveryRead (&watchdog, 0) == UINT32_MAX || (recoveryRead (&watchdog, 0) & 1)) {
    manualRecord (console, token, "state-refused");
    return;
  }
  // No timer or reset is armed during this wait. GPIO60 is sampled read-only;
  // it is the physically calibrated Volume Up input inherited from LK.
  if (!manualRecord (console, token, "waiting-for-volume-up")) { return; }
  DaliManualKey key;
  for (;;) {
    IODelay (10000);
    if (!daliManualKeySample (key, (recoveryRead (&pins, 0x210) & (1U << 28)) == 0)) { continue; }
    if (quiesce && !quiesce(owner)) { manualRecord(console, token, "storage-stop-refused"); return; }
    if (!manualRecord (console, token, "volume-up-requested-watchdog-2s")) { return; }
    recoveryFence (nullptr);
    DaliManualWdtOps ops = {&watchdog, recoveryRead, manualWatchdogWrite, recoveryFence};
    const bool armed = daliArmManualWatchdog (ops);
    manualRecord (console, token, armed ? "watchdog-armed-2s" : "watchdog-refused");
    recoveryFence (nullptr);
    // Do not reload or retry. Keep Volume Down held to select LK fastboot.
    for (;;) { __asm__ volatile ("wfe"); }
  }
}

extern "C" void dali_ufs_wait_for_manual_recovery(const char *token, bool (*quiesce)(void *), void *owner)
{
  unsigned mode = 0, automatic = 0, pid1 = 0;
  char actual[33] = {}, reset[16] = {};
  PE_parse_boot_argn("dali-ufs-readonly", &mode, sizeof mode);
  PE_parse_boot_argn("dali-auto-fastboot", &automatic, sizeof automatic);
  PE_parse_boot_argn("dali-pid1", &pid1, sizeof pid1);
  if (mode != 1 || automatic || pid1 || !token || !quiesce ||
      PE_parse_boot_argn("dali-reset-method", reset, sizeof reset) ||
      !PE_parse_boot_argn("dali-run", actual, sizeof actual) || actual[32] || memcmp(actual, token, 33)) { return; }
  OSSharedPtr<IOMemoryDescriptor> memory(
      IOMemoryDescriptor::withPhysicalAddress(kConsolePhysical, kConsoleBytes, kIODirectionInOut), OSNoRetain);
  OSSharedPtr<IOMemoryMap> console(memory ? memory->map(kIOMapAnywhere | kIOMapInhibitCache) : nullptr, OSNoRetain);
  if (console) waitForManualRecovery(console.get(), token, quiesce, owner);
}

static bool recoveryRecord(void *opaque, const char *stage, uint32_t marker, uint32_t mode)
{
  auto *context = static_cast<RecoveryContext *> (opaque);
  char line[384] = "DALI-RESET: run=";
  char *at = line + sizeof ("DALI-RESET: run=") - 1;
  char *end = line + sizeof (line) - 2;
  at = appendText (at, end, context->token);
  at = appendText (at, end, " method=");
  at = appendText (at, end, daliResetLogMethod (context->method));
  at = appendText (at, end, " stage=");
  at = appendText (at, end, stage);
  at = appendText (at, end, " marker=0x");
  at = appendUnsigned (at, end, marker, 16);
  at = appendText (at, end, " mode=0x");
  at = appendUnsigned (at, end, mode, 16);
  if (context->qualification) {
    at = appendText (at, end, " qualification=1 delay_ms=5000 elapsed_ticks=");
    at = appendUnsigned (at, end, context->elapsedTicks, 10);
    at = appendText (at, end, " freq=");
    at = appendUnsigned (at, end, context->frequency, 10);
    at = appendText (at, end, " type=0x");
    at = appendUnsigned (at, end, context->resetType, 16);
  }
  if (stage[0] == 'r') {
    at = appendText (at, end, " features=0x");
    at = appendUnsigned (at, end, context->psciFeatures, 16);
    at = appendText (at, end, " result=0x");
    at = appendUnsigned (at, end, context->psciResult, 16);
  }
  *at++ = '\n';
  return appendRamoops (context->console, line, static_cast<size_t> (at - line));
}

static DaliRecoveryContract decodeRecoveryContract(const void *resource, const void *reboot)
{
  // Copy whole objects. XNU's fortified memcpy rejects a multi-field copy
  // through &contract.base or &contract.offset as a subobject overflow.
  uint64_t resourceValues[2];
  uint32_t rebootValues[3];
  memcpy (resourceValues, resource, sizeof (resourceValues));
  memcpy (rebootValues, reboot, sizeof (rebootValues));
  return {resourceValues[0], resourceValues[1], rebootValues[0], rebootValues[1], rebootValues[2]};
}

static __attribute__((noinline)) void recoverToFastboot(IOMemoryMap *console, const char *token,
    DaliResetMethod method = DaliResetMethod::Aee, bool qualification = false)
{
  IORegistryEntry *entry = IORegistryEntry::fromPath ("/arm-io/dali-boot-log", gIODTPlane);
  if (entry == nullptr || console == nullptr) {
    if (entry != nullptr) { entry->release (); }
    return;
  }
  auto *resource = OSDynamicCast (OSData, entry->getProperty ("recovery-rgu"));
  auto *reboot = OSDynamicCast (OSData, entry->getProperty ("recovery-reboot-mode"));
  auto *aee = OSDynamicCast (OSData, entry->getProperty ("recovery-aee-reset-type"));
  uint32_t resetType = 0;
  if (aee != nullptr && aee->getLength () == sizeof (resetType)) {
    memcpy (&resetType, aee->getBytesNoCopy (), sizeof (resetType));
  }
  DaliRecoveryContract contract = {};
  if (resource != nullptr && resource->getLength () == 2 * sizeof (uint64_t) &&
      reboot != nullptr && reboot->getLength () == 3 * sizeof (uint32_t)) {
    contract = decodeRecoveryContract (resource->getBytesNoCopy (), reboot->getBytesNoCopy ());
  }
  entry->release ();
  if (!daliRecoveryContractValid (contract) || method == DaliResetMethod::Off ||
      (method == DaliResetMethod::Aee && resetType != 0x80000201U)) { return; }
  if (method != DaliResetMethod::Aee) { resetType = 0; }
  OSSharedPtr<IOMemoryDescriptor> descriptor (
      IOMemoryDescriptor::withPhysicalAddress (contract.base, contract.size, kIODirectionInOut), OSNoRetain);
  if (!descriptor) { return; }
  OSSharedPtr<IOMemoryMap> mapping (descriptor->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
  if (!mapping) { return; }
  RecoveryContext context = {static_cast<uintptr_t> (mapping->getVirtualAddress ()), console, token,
                             UINT32_MAX, UINT32_MAX, resetType};
  context.method = method;
  context.qualification = qualification;
  DaliRecoveryOps ops = {&context, recoveryRead, recoveryFence, recoveryRecord, recoverySystemReset};
  if (qualification) {
    uint64_t frequency;
    __asm__ volatile ("mrs %0, cntfrq_el0" : "=r" (frequency));
    context.frequency = static_cast<uint32_t> (frequency);
    if (frequency != 13000000 ||
        !recoveryRecord (&context, "wait", recoveryRead (&context, 0x24), recoveryRead (&context, 0))) { return; }
    const uint64_t start = recoveryTicks ();
    for (unsigned attempts = 0; attempts < 6000; ++attempts) {
      IODelay (1000);
      context.elapsedTicks = recoveryTicks () - start;
      if (daliQualifiedDelayComplete (context.frequency, context.elapsedTicks)) { break; }
    }
    if (!daliQualifiedDelayComplete (context.frequency, context.elapsedTicks)) {
      recoveryRecord (&context, "delay-refused", recoveryRead (&context, 0x24), recoveryRead (&context, 0));
      return;
    }
  } else { IODelay (1000000); }
  if (daliIssueSystemReset (ops, qualification)) {
    // Keep a bounded observation interval and persist an unexpected return.
    IODelay (100000);
    recoveryRecord (&context, "returned", recoveryRead (&context, 0x24), recoveryRead (&context, 0));
    recoveryFence (&context);
  }
}

#include "DaliPid1Service.h"

class DaliBootLog : public IOService {
  OSDeclareDefaultStructors (DaliBootLog)
  OSSharedPtr<IOMemoryMap> mConsole;
public:
  bool start (IOService *provider) override
  {
    // Dali exposes one boot CPU only. Closing the locally IRQ/FIQ-masked early
    // writer here hands ownership over before this service accesses the ring.
    dali_bootstrap_log("DALI:DL IOKit persistent logger entered; mapping console\n");
    if (!IOService::start (provider)) { return false; }
    unsigned pid1Mode = 0, offline = 0, autoFastboot = 0, ufsMode = 0;
    char resetArgument[16] = {};
    DaliResetMethod resetMethod = DaliResetMethod::Off;
    unsigned resetDelay = 0;
    const bool qualification = PE_parse_boot_argn ("dali-reset-method", resetArgument, sizeof (resetArgument));
    PE_parse_boot_argn ("dali-reset-delay", &resetDelay, sizeof (resetDelay));
    PE_parse_boot_argn ("dali-pid1", &pid1Mode, sizeof (pid1Mode));
    PE_parse_boot_argn ("dali-pid1-qemu", &offline, sizeof (offline));
    PE_parse_boot_argn ("dali-auto-fastboot", &autoFastboot, sizeof (autoFastboot));
    PE_parse_boot_argn ("dali-ufs-readonly", &ufsMode, sizeof (ufsMode));
    if (ufsMode > 1 || (ufsMode == 1 && (pid1Mode || offline || autoFastboot || qualification))) { return false; }
    if (qualification && (resetArgument[15] != 0 || !daliParseResetMethod (resetArgument, resetMethod) ||
                         autoFastboot != 0 || pid1Mode != 0 || offline != 0 || resetDelay != 5)) { return false; }
    if (pid1Mode > 1 || offline > 1 || (pid1Mode == 1 && autoFastboot != 0)) { return false; }
    if (offline == 1) {
      // This backend has no hardware MMIO or persistence claim. Both explicit
      // opt-in and the synthetic fixture's exact DT model are required.
      if (pid1Mode != 1) { return false; }
      IORegistryEntry *root = IORegistryEntry::fromPath ("/", gIODTPlane);
      auto *model = root == nullptr ? nullptr : OSDynamicCast (OSData, root->getProperty ("model"));
      const bool synthetic = model != nullptr && model->getLength () == sizeof ("qemu-entry-fixture") &&
          memcmp (model->getBytesNoCopy (), "qemu-entry-fixture", sizeof ("qemu-entry-fixture")) == 0;
      if (root != nullptr) { root->release (); }
      char token[33] = {};
      if (!synthetic || !PE_parse_boot_argn ("dali-run", token, sizeof (token)) || token[32] != '\0') { return false; }
      for (unsigned i = 0; i < 32; i++) {
        if (!((token[i] >= '0' && token[i] <= '9') || (token[i] >= 'a' && token[i] <= 'f'))) { return false; }
      }
      if (!pid1Initialize (nullptr, token, true)) { return false; }
      registerService ();
      return true;
    }
    OSSharedPtr<IOMemoryDescriptor> descriptor (
        IOMemoryDescriptor::withPhysicalAddress (kConsolePhysical, kConsoleBytes, kIODirectionInOut), OSNoRetain);
    if (descriptor) {
      mConsole = OSSharedPtr<IOMemoryMap> (
          descriptor->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
    }
    if (!mConsole) {
      dali_bootstrap_log("DALI:DL console mapping failed; early sink retained\n");
      return false;
    }
    if (pid1Mode == 1) {
      dali_bootstrap_log("DALI:DL console mapped; retaining single-CPU early sink for PID1\n");
    } else {
      dali_bootstrap_log("DALI:DL console mapped; early sink ending\n");
      dali_bootstrap_log_end();
    }
    for (unsigned attempt = 0; attempt < 200 &&
         (!armdt_platform_configured || !armcpu_registered || !armcpu_gic_ready || !armcpu_timer_interrupts); attempt++) {
      IODelay (10000);
    }
    uint64_t mpidr;
    unsigned bootArg = 0;
    char runToken[33] = {};
    __asm__ volatile ("mrs %0, mpidr_el1" : "=r" (mpidr));
    PE_parse_boot_argn ("dali-earlyboot", &bootArg, sizeof (bootArg));
    const bool tokenPresent = PE_parse_boot_argn ("dali-run", runToken, sizeof (runToken));
    bool tokenValid = tokenPresent && runToken[32] == '\0';
    for (unsigned index = 0; tokenValid && index < 32; index++) {
      const char digit = runToken[index];
      tokenValid = (digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f');
    }
    const bool passed = tokenValid && bootArg == 1 && armdt_platform_configured == 1 && armcpu_registered == 1 &&
                        armcpu_gic_ready == 1 && armcpu_timer_interrupts != 0 &&
                        (mpidr & 0xff00ffffffULL) == 0;
    char line[224] = "DALI-XNU: darwin=" DALI_XNU_DARWIN " build=" DALI_XNU_BUILD " run=";
    char *at = line + sizeof ("DALI-XNU: darwin=" DALI_XNU_DARWIN " build=" DALI_XNU_BUILD " run=") - 1;
    char *end = line + sizeof (line) - 2;
    at = appendText (at, end, tokenValid ? runToken : "invalid");
    at = appendText (at, end, " bootarg=");
    at = appendUnsigned (at, end, bootArg, 10);
    at = appendText (at, end, " registered=");
    at = appendUnsigned (at, end, armcpu_registered, 10);
    at = appendText (at, end, " platform=");
    at = appendUnsigned (at, end, armdt_platform_configured, 10);
    at = appendText (at, end, " gic=");
    at = appendUnsigned (at, end, armcpu_gic_ready, 10);
    at = appendText (at, end, " timer=");
    at = appendUnsigned (at, end, armcpu_timer_interrupts, 10);
    at = appendText (at, end, " mpidr=0x");
    at = appendUnsigned (at, end, mpidr, 16);
    at = appendText (at, end, passed ? " checkpoint=PASS" : " checkpoint=FAIL");
    if (at < end) { *at++ = '\n'; }
    const size_t length = static_cast<size_t> (at - line);
    const bool checkpointSaved = appendRamoops (mConsole.get (), line, length);
    IOLog ("%s", line);
    registerService ();
    // UFS has its own worker. It retains the read-only devices until a physical
    // key request and stops/synchronizes DMA before arming manual recovery.
    if (ufsMode == 1) {
      if (!passed || !checkpointSaved) { return false; }
      memcpy (ufsPlatformToken, runToken, sizeof (ufsPlatformToken));
      __atomic_store_n (&ufsPlatformReady, 1, __ATOMIC_RELEASE);
      return true;
    }
    if (pid1Mode == 1) {
      if (!passed || !checkpointSaved || !pid1Initialize (mConsole.get (), runToken, false)) { return false; }
      dali_bootstrap_log("DALI:DL continuing to RAM root and PID1; recovery watchdog disabled\n");
      return true;
    }
    if (qualification) {
      if (passed && checkpointSaved && resetMethod != DaliResetMethod::Off) {
        recoverToFastboot (mConsole.get (), runToken, resetMethod, true);
      }
      // No alternate reset, manual-watchdog helper or later userspace on this path.
      for (;;) { __asm__ volatile ("wfe"); }
    }
    if (passed && checkpointSaved && autoFastboot == 1) { recoverToFastboot (mConsole.get (), runToken); }
    if (passed && checkpointSaved && autoFastboot == 0) { waitForManualRecovery (mConsole.get (), runToken); }
    return true;
  }
};

OSDefineMetaClassAndStructors (DaliBootLog, IOService)
