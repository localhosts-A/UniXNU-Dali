// SPDX-License-Identifier: BSD-2-Clause-Patent
// Append bounded XNU platform/GIC/timer checkpoints to the inherited ramoops console.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <libkern/c++/OSSharedPtr.h>
#include <pexpert/pexpert.h>

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
  auto *ring = reinterpret_cast<RamoopsConsole *> (map->getVirtualAddress ());
  if (ring->signature != kConsoleSignature) { return false; }
  const uint32_t capacity = kConsoleBytes - sizeof (RamoopsConsole);
  auto *data = reinterpret_cast<volatile uint8_t *> (ring + 1);
  if (ring->start >= capacity || ring->size > capacity) { return false; }
  for (size_t index = 0; index < length; index++) {
    const uint32_t at = ring->start;
    data[at] = static_cast<uint8_t> (text[index]);
    ring->start = at + 1 == capacity ? 0 : at + 1;
    if (ring->size < capacity) { ring->size++; }
  }
  __sync_synchronize ();
  return true;
}

class DaliBootLog : public IOService {
  OSDeclareDefaultStructors (DaliBootLog)
  OSSharedPtr<IOMemoryMap> mConsole;
public:
  bool start (IOService *provider) override
  {
    // Dali exposes one boot CPU only. Closing the locally IRQ/FIQ-masked early
    // writer here hands ownership over before this service accesses the ring.
    dali_bootstrap_log("DALI:DL IOKit persistent logger entered; early sink ending\n");
    dali_bootstrap_log_end();
    if (!IOService::start (provider)) { return false; }
    OSSharedPtr<IOMemoryDescriptor> descriptor (
        IOMemoryDescriptor::withPhysicalAddress (kConsolePhysical, kConsoleBytes, kIODirectionInOut), OSNoRetain);
    if (descriptor) {
      mConsole = OSSharedPtr<IOMemoryMap> (
          descriptor->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
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
    appendRamoops (mConsole.get (), line, length);
    IOLog ("%s", line);
    registerService ();
    return true;
  }
};

OSDefineMetaClassAndStructors (DaliBootLog, IOService)
