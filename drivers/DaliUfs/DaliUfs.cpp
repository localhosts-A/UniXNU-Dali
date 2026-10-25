// SPDX-License-Identifier: BSD-2-Clause-Patent
// Default-off Dali physical UFS diagnostic with an explicitly granted scratch child.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/storage/IOBlockStorageDevice.h>
#include <IOKit/storage/IOBlockStorageDriver.h>
#include <IOKit/storage/IOMedia.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSString.h>
#include <libkern/c++/OSIterator.h>
#include <libkern/crypto/sha2.h>
#include <kern/thread.h>
#include <kern/task.h>
#include <pexpert/pexpert.h>
#include "UfsTransport.h"
#include "DaliUfsPlatform.h"
#include "DaliUfsMemory.h"
#include "DaliUfsRecovery.h"

using unixnu_ufs::Status;
class DaliUfsBlock;
class DaliUfs : public IOService {
  OSDeclareDefaultStructors(DaliUfs)
  friend class DaliUfsBlock;
  IOLock *gate_ = nullptr;
  IOMemoryMap *hci_ = nullptr, *ao_ = nullptr, *console_ = nullptr;
  IOBufferMemoryDescriptor *dma_ = nullptr;
  uint8_t *staging_ = nullptr;
  DaliUfsBlock *children_[8]{};
  DaliUfsBlock *scratchChild_ = nullptr;
  uint8_t resources_[dali_ufs::ResourceBytes]{};
  char token_[33]{};
  uint32_t dmaBytes_ = 0, line_ = 0;
  uint64_t dmaPhysical_ = 0, reads_ = 0, syncs_ = 0;
  uint32_t frequency_ = 0;
  bool prepared_ = false, exposed_ = false, online_ = false, closed_ = false, scratchWriteMode_ = false;
  bool scratchMediaReady_ = false;
  Status stopSync_ = Status::Success;
  dali_ufs::ScratchGrant scratchGrant_{};
  dali_ufs::ScratchObservation scratchObservation_{};
  dali_ufs::Controller controller_{};
  unixnu_ufs::Transport transport_{};
  static void worker(void *, wait_result_t);
  static IOMemoryMap *map(uint64_t base, uint64_t bytes);
  static uint32_t read(void *, uint64_t);
  static bool write(void *, uint64_t, uint32_t);
  static void fence(void *);
  static void delay(void *, uint32_t);
  static uint64_t sip(void *, uint32_t, uint32_t, uint32_t, uint32_t);
  static bool portRead(void *, uint32_t, uint32_t *);
  static bool portWrite(void *, uint32_t, uint32_t);
  static bool allowWrite(void *, uint8_t, const unixnu_ufs::Geometry &, uint64_t, uint32_t);
  static bool cache(void *, void *, uint32_t, unixnu_ufs::Cache);
  static uint64_t micros(void *);
  static bool exclusive(void *);
  static bool stopBeforeReset(void *p) { return static_cast<DaliUfs *>(p)->quiesce(true); }
  void record(const char *, uint64_t a = 0, uint64_t b = 0, uint64_t c = 0);
  void textRecord(uint32_t kind, const char *text);
  bool sample(uint8_t, IOMedia *, uint64_t lba, const char *kind);
  bool initialize();
  bool diagnose();
  void partitions(uint8_t, IORegistryEntry *, unsigned depth, unsigned &count, bool &bootSample);
  IOMedia *findBootB(IORegistryEntry *, unsigned depth, unsigned &count);
  bool publishScratch(uint8_t, IOMedia *whole);
  bool quiesce(bool requireSync = false);
  IOReturn readBlocks(uint8_t, IOMemoryDescriptor *, uint64_t, uint64_t, uint64_t &actual);
  IOReturn writeBlocks(uint8_t, IOMemoryDescriptor *, uint64_t, uint64_t, uint64_t &actual);
  IOReturn sync(uint8_t);
  static IOReturn error(Status);
public:
  bool start(IOService *) override;
  void stop(IOService *) override;
  void free() override;
};

class DaliUfsBlock : public IOBlockStorageDevice {
  OSDeclareDefaultStructors(DaliUfsBlock)
  DaliUfs *owner_ = nullptr; uint8_t lun_ = 0;
  unixnu_ufs::Geometry geometry_{};
  uint64_t baseBlock_ = 0, exposedBlocks_ = 0;
  bool writable_ = false, scratch_ = false;
public:
  bool initFor(DaliUfs *owner, uint8_t lun, const unixnu_ufs::Geometry &g,
               uint64_t baseBlock, uint64_t blocks, bool writable, bool scratch) {
    if (!owner || !g.valid || !blocks || baseBlock >= g.blocks || blocks > g.blocks - baseBlock ||
        (writable && !scratch) || (scratch && (!writable || blocks != owner->scratchGrant_.scratchBlocks ||
        baseBlock != owner->scratchGrant_.scratchStartLba)) || !IOBlockStorageDevice::init(nullptr)) return false;
    owner_ = owner; lun_ = lun; geometry_ = g; baseBlock_ = baseBlock;
    exposedBlocks_ = blocks; writable_ = writable; scratch_ = scratch; return true;
  }
  bool start(IOService *p) override { if (!IOBlockStorageDevice::start(p)) return false; registerService(); return true; }
  IOReturn doEjectMedia() override { return kIOReturnUnsupported; }
  IOReturn doFormatMedia(UInt64) override { return kIOReturnLockedWrite; }
  UInt32 doGetFormatCapacities(UInt64 *, UInt32) const override { return 0; }
  char *getVendorString() override { return const_cast<char *>("MediaTek"); }
  char *getProductString() override { return const_cast<char *>(scratch_ ? "Dali UFS scratch test" : "Dali UFS read-only"); }
  char *getRevisionString() override { return const_cast<char *>("1"); }
  char *getAdditionalDeviceInfoString() override { return const_cast<char *>("MT6991 private DMA"); }
  IOReturn reportBlockSize(UInt64 *p) override { if (!p) return kIOReturnBadArgument; *p = geometry_.blockBytes; return kIOReturnSuccess; }
  IOReturn reportMaxValidBlock(UInt64 *p) override { if (!p || !geometry_.valid || !exposedBlocks_) return kIOReturnBadArgument; *p = exposedBlocks_ - 1; return kIOReturnSuccess; }
  IOReturn reportEjectability(bool *p) override { if (!p) return kIOReturnBadArgument; *p = false; return kIOReturnSuccess; }
  IOReturn reportRemovability(bool *p) override { return reportEjectability(p); }
  IOReturn reportWriteProtection(bool *p) override { if (!p) return kIOReturnBadArgument; *p = !writable_; return kIOReturnSuccess; }
  IOReturn reportMediaState(bool *present, bool *changed = nullptr) override {
    if (!present) return kIOReturnBadArgument;
    IOLockLock(owner_->gate_); *present = owner_->online_ && !owner_->closed_ && !owner_->transport_.poisoned();
    IOLockUnlock(owner_->gate_); if (changed) *changed = false; return kIOReturnSuccess;
  }
  IOReturn doAsyncReadWrite(IOMemoryDescriptor *buffer, UInt64 block, UInt64 count,
                           IOStorageAttributes *, IOStorageCompletion *completion) override {
    if (!completion || !completion->action) return kIOReturnBadArgument;
    uint64_t actual = 0, absolute = 0; IOReturn status = kIOReturnBadArgument;
    if (buffer && count && count <= UINT32_MAX && block < exposedBlocks_ && count <= exposedBlocks_ - block &&
        count <= UINT64_MAX / geometry_.blockBytes) {
      if (scratch_) {
        if (!dali_ufs::scratchAbsoluteLba(owner_->scratchGrant_, block, static_cast<uint32_t>(count), absolute))
          status = kIOReturnBadArgument;
        else status = kIOReturnSuccess;
      } else if (block <= UINT64_MAX - baseBlock_) {
        absolute = baseBlock_ + block; status = kIOReturnSuccess;
      }
      if (status == kIOReturnSuccess && buffer->getLength() != count * geometry_.blockBytes)
        status = kIOReturnBadArgument;
      else if (status == kIOReturnSuccess && buffer->getDirection() == kIODirectionOut)
        status = writable_ ? owner_->writeBlocks(lun_, buffer, block, count, actual) : kIOReturnLockedWrite;
      else if (status == kIOReturnSuccess && buffer->getDirection() == kIODirectionIn)
        status = owner_->readBlocks(lun_, buffer, absolute, count, actual);
      else if (status == kIOReturnSuccess) status = kIOReturnBadArgument;
    }
    IOStorage::complete(completion, status, actual);
    return kIOReturnSuccess; // Exactly one completion; generic driver must not repeat it.
  }
  IOReturn doSynchronize(UInt64 block, UInt64 count, IOStorageSynchronizeOptions options = 0) override {
    if (options || block >= exposedBlocks_ || (count && count > exposedBlocks_ - block)) return kIOReturnBadArgument;
    return owner_->sync(lun_); // Actual ordered device SYNC(10), even on a RO LU.
  }
  IOMedia *copyMedia() {
    auto drivers = getChildIterator(gIOServicePlane); IOMedia *found = nullptr;
    if (!drivers) return nullptr;
    while (auto e = drivers->getNextObject()) {
      auto d = OSDynamicCast(IOBlockStorageDriver, e); if (!d || d->getProvider() != this) continue;
      auto entries = d->getChildIterator(gIOServicePlane); if (!entries) continue;
      while (auto child = entries->getNextObject()) {
        auto media = OSDynamicCast(IOMedia, child);
        if (media && media->getProvider() == d && media->isWhole()) { media->retain(); found = media; break; }
      }
      entries->release(); if (found) break;
    }
    drivers->release(); return found;
  }
};

IOMemoryMap *DaliUfs::map(uint64_t base, uint64_t bytes) {
  auto memory = IOMemoryDescriptor::withPhysicalAddress(base, bytes, kIODirectionInOut);
  if (!memory) return nullptr;
  auto result = memory->map(kIOMapAnywhere | kIOMapInhibitCache); memory->release(); return result;
}
void DaliUfs::fence(void *) { __asm__ volatile("dsb sy" ::: "memory"); }
void DaliUfs::delay(void *, uint32_t us) { IODelay(us); }
uint32_t DaliUfs::read(void *p, uint64_t address) {
  auto s = static_cast<DaliUfs *>(p); uintptr_t va = 0;
  if ((address & 3) == 0 && address >= dali_ufs::Hci && address - dali_ufs::Hci <= 0x2a00 - 4 && s->hci_)
    va = s->hci_->getVirtualAddress() + address - dali_ufs::Hci;
  else if ((address & 3) == 0 && address >= dali_ufs::Ao && address - dali_ufs::Ao <= 0x1000 - 4 && s->ao_)
    va = s->ao_->getVirtualAddress() + address - dali_ufs::Ao;
  if (!va) return UINT32_MAX;
  fence(nullptr); uint32_t value = *reinterpret_cast<volatile uint32_t *>(va); fence(nullptr); return value;
}
bool DaliUfs::write(void *p, uint64_t address, uint32_t value) {
  auto s = static_cast<DaliUfs *>(p); uintptr_t va = 0;
  if ((address & 3) == 0 && address >= dali_ufs::Hci && address - dali_ufs::Hci <= 0x2a00 - 4 && s->hci_)
    va = s->hci_->getVirtualAddress() + address - dali_ufs::Hci;
  else if ((address & 3) == 0 && address >= dali_ufs::Ao && address - dali_ufs::Ao <= 0x1000 - 4 && s->ao_)
    va = s->ao_->getVirtualAddress() + address - dali_ufs::Ao;
  if (!va) return false;
  fence(nullptr); *reinterpret_cast<volatile uint32_t *>(va) = value; fence(nullptr); return true;
}
__attribute__((noinline)) uint64_t DaliUfs::sip(void *, uint32_t service, uint32_t selector, uint32_t a, uint32_t b) {
  register uint64_t x0 __asm__("x0") = service, x1 __asm__("x1") = selector;
  register uint64_t x2 __asm__("x2") = a, x3 __asm__("x3") = b;
  register uint64_t x4 __asm__("x4") = 0, x5 __asm__("x5") = 0, x6 __asm__("x6") = 0, x7 __asm__("x7") = 0;
  __asm__ volatile("dsb sy\n\tsmc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3), "+r"(x4), "+r"(x5), "+r"(x6), "+r"(x7) :
                   : "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17", "memory", "cc");
  return x0;
}
bool DaliUfs::portRead(void *p, uint32_t off, uint32_t *v) { if (off > 0x2a00 - 4 || (off & 3) || !v) return false; *v = read(p, dali_ufs::Hci + off); return *v != UINT32_MAX; }
bool DaliUfs::portWrite(void *p, uint32_t off, uint32_t v) { return off <= 0x2a00 - 4 && !(off & 3) && write(p, dali_ufs::Hci + off, v); }
bool DaliUfs::exclusive(void *p) { auto s = static_cast<DaliUfs *>(p); return s->controller_.ready() && read(p, dali_ufs::Hci + 0x300) == 0; }
bool DaliUfs::allowWrite(void *p, uint8_t lun, const unixnu_ufs::Geometry &geometry, uint64_t lba, uint32_t blocks) {
  auto s = static_cast<DaliUfs *>(p);
  // Transport calls are serialized by gate_; no lock recursion here. The
  // scratch child stays unarmed until its published media is independently checked.
  if (!s->online_ || s->closed_ || !s->scratchMediaReady_ || !geometry.valid) return false;
  auto observed = s->scratchObservation_;
  observed.lun = lun; observed.blockBytes = geometry.blockBytes;
  observed.capacityBlocks = geometry.blocks; observed.writeProtected = geometry.writeProtected;
  return dali_ufs::scratchWriteAllowed(s->scratchGrant_, observed, lba, blocks);
}
uint64_t DaliUfs::micros(void *p) {
  auto s = static_cast<DaliUfs *>(p); uint64_t ticks; __asm__ volatile("isb\n\tmrs %0, cntpct_el0" : "=r"(ticks) :: "memory");
  return (ticks / s->frequency_) * 1000000 + (ticks % s->frequency_) * 1000000 / s->frequency_;
}
__attribute__((noinline)) bool DaliUfs::cache(void *p, void *cpu, uint32_t bytes, unixnu_ufs::Cache operation) {
  auto s = static_cast<DaliUfs *>(p); dali_ufs::CacheSpan span;
  if (!s->dma_ || !s->prepared_ || !dali_ufs::cacheSpan(reinterpret_cast<uintptr_t>(s->dma_->getBytesNoCopy()),
       s->dmaBytes_, reinterpret_cast<uintptr_t>(cpu), bytes, s->line_, span)) return false;
  fence(nullptr);
  for (uintptr_t at = span.begin; at < span.end; at += s->line_) {
    if (operation == unixnu_ufs::Cache::ToDevice) __asm__ volatile("dc civac, %0" :: "r"(at) : "memory");
    else __asm__ volatile("dc ivac, %0" :: "r"(at) : "memory");
  }
  fence(nullptr); return true;
}
void DaliUfs::record(const char *stage, uint64_t a, uint64_t b, uint64_t c) {
  char line[256]; int n = snprintf(line, sizeof line, "DALI-XNU-UFS: run=%s stage=%s a=%llx b=%llx c=%llx\n", token_, stage, a, b, c);
  IOLog("%s", line);
  if (!console_ || n <= 0 || static_cast<size_t>(n) >= sizeof line) return;
  auto ring = reinterpret_cast<volatile uint32_t *>(console_->getVirtualAddress());
  constexpr uint32_t capacity = 0x200000 - 12;
  uint64_t daif; __asm__ volatile("mrs %0, daif\n\tmsr daifset, #3" : "=r"(daif) :: "memory");
  if (ring[0] == 0x43474244 && ring[1] < capacity && ring[2] <= capacity) {
    auto text = reinterpret_cast<volatile uint8_t *>(ring + 3);
    for (int i = 0; i < n; ++i) { text[ring[1]] = static_cast<uint8_t>(line[i]); ring[1] = ring[1] + 1 == capacity ? 0 : ring[1] + 1; if (ring[2] < capacity) ++ring[2]; }
    fence(nullptr);
  }
  __asm__ volatile("msr daif, %0" :: "r"(daif) : "memory");
}
IOReturn DaliUfs::error(Status s) {
  if (s == Status::Success) return kIOReturnSuccess;
  if (s == Status::Argument) return kIOReturnBadArgument;
  if (s == Status::NotReady) return kIOReturnNotReady;
  if (s == Status::Denied) return kIOReturnLockedWrite;
  if (s == Status::Timeout) return kIOReturnTimeout;
  return kIOReturnIOError;
}
void DaliUfs::textRecord(uint32_t kind, const char *text) {
  // GPT names/UUID strings are data. Hex framing prevents newline/log injection.
  size_t length = text ? strnlen(text, 129) : 129;
  if (length == 129) { record("metadata-invalid", kind); return; }
  for (size_t offset = 0; offset < length; offset += 32) {
    size_t count = length - offset < 32 ? length - offset : 32; char hex[65];
    for (size_t i = 0; i < count; ++i) { static const char digits[] = "0123456789abcdef"; uint8_t v = static_cast<uint8_t>(text[offset+i]); hex[i*2] = digits[v >> 4]; hex[i*2+1] = digits[v & 15]; }
    hex[count*2] = 0; record("text", kind, offset, length); record(hex);
  }
}
bool DaliUfs::initialize() {
  if (!dali_ufs_platform_ready(token_)) { record("platform-refused"); return false; }
  // Real kernel SHA wrappers, checked before the first MMIO write / SMC.
  static const uint8_t expected[32] = {0xba,0x78,0x16,0xbf,0x8f,0x01,0xcf,0xea,0x41,0x41,0x40,0xde,0x5d,0xae,0x22,0x23,0xb0,0x03,0x61,0xa3,0x96,0x17,0x7a,0x9c,0xb4,0x10,0xff,0x61,0xf2,0x00,0x15,0xad};
  SHA256_CTX hash; uint8_t digest[32]; SHA256_Init(&hash); SHA256_Update(&hash, "abc", 3); SHA256_Final(digest, &hash);
  if (memcmp(digest, expected, 32)) { record("sha-failed"); return false; }
  record("sha-pass");
  uint64_t ctr, hz, mpidr;
  __asm__ volatile("mrs %0, ctr_el0\n\tmrs %1, cntfrq_el0\n\tmrs %2, mpidr_el1" : "=r"(ctr), "=r"(hz), "=r"(mpidr));
  line_ = 4U << ((ctr >> 16) & 15); frequency_ = static_cast<uint32_t>(hz);
  if (line_ < 16 || line_ > 4096 || hz != 13000000 || (mpidr & 0xff00ffffffULL)) return false;
  auto result = controller_.start(true, resources_, sizeof resources_, token_, {this, read, write, fence, delay, sip});
  const auto &d = controller_.diagnostic();
  record("controller", static_cast<uint32_t>(result), static_cast<uint32_t>(d.stage), d.faultValue);
  if (result != dali_ufs::Result::Ready) { record("controller-fault", d.faultAddress, d.interrupt, d.sipStatus); return false; }
  IOOptionBits options = static_cast<IOOptionBits>(kIODirectionInOut) | static_cast<IOOptionBits>(kIOMemoryPhysicallyContiguous) |
                        static_cast<IOOptionBits>(kIOMemoryMapperNone) | static_cast<IOOptionBits>(kIOMapCopybackCache);
  dma_ = IOBufferMemoryDescriptor::inTaskWithPhysicalMask(kernel_task, options, unixnu_ufs::DmaBytes, UINT32_MAX);
  if (!dma_ || dma_->getCapacity() > UINT32_MAX || dma_->prepare(kIODirectionInOut) != kIOReturnSuccess) return false;
  prepared_ = true; dmaBytes_ = static_cast<uint32_t>(dma_->getCapacity());
  IOByteCount span = 0; dmaPhysical_ = dma_->getPhysicalSegment(0, &span, kIOMemoryMapperNone);
  auto cpu = dma_->getBytesNoCopy();
  if (dmaBytes_ < unixnu_ufs::DmaBytes || !dali_ufs::dmaSpan(reinterpret_cast<uintptr_t>(cpu), dmaPhysical_, dmaBytes_, span)) return false;
  staging_ = static_cast<uint8_t *>(IOMallocZero(unixnu_ufs::DataMax)); if (!staging_) return false;
  bzero(cpu, dmaBytes_);
  record("dma", dmaPhysical_, dmaBytes_, line_);
  unixnu_ufs::Port port{this, portRead, portWrite, cache, micros, delay, exclusive, allowWrite};
  exposed_ = true; // Conservative: keep allocations even if a doorbell/base write becomes uncertain.
  Status status = transport_.start(port, {cpu, dmaPhysical_, UINT32_MAX, dmaBytes_});
  if (status == Status::Success) status = transport_.initializeDevice();
  if (status != Status::Success) { record("transport-failed", static_cast<uint32_t>(status)); return false; }
  if (scratchWriteMode_) {
    uint8_t identity[256] = {}, digest[32] = {}; uint32_t identityBytes = 0;
    status = transport_.readDeviceIdentity(identity, sizeof identity, identityBytes);
    if (status != Status::Success || !identityBytes) { record("identity-read-failed", static_cast<uint32_t>(status)); return false; }
    SHA256_CTX hash; SHA256_Init(&hash); SHA256_Update(&hash, identity, identityBytes); SHA256_Final(digest, &hash);
    bzero(identity, sizeof identity);
    if (memcmp(digest, scratchGrant_.deviceBinding, sizeof digest)) {
      bzero(digest, sizeof digest); record("identity-mismatch"); return false;
    }
    memcpy(scratchObservation_.token, token_, sizeof scratchObservation_.token);
    memcpy(scratchObservation_.deviceBinding, digest, sizeof digest);
    memcpy(scratchObservation_.candidateSha256, scratchGrant_.candidateSha256, sizeof scratchObservation_.candidateSha256);
    memcpy(scratchObservation_.sourceSha256, scratchGrant_.sourceSha256, sizeof scratchObservation_.sourceSha256);
    scratchObservation_.identityVerified = true;
    bzero(digest, sizeof digest);
  }
  bool any = false, grantLunSeen = false;
  for (uint8_t lun = 0; lun < 8; ++lun) {
    status = transport_.probe(lun); const auto &c = transport_.completion();
    if (status == Status::NotReady || (status == Status::Target && c.transaction == 0x16 && c.opcode == 1 && c.target == 0xfc)) continue;
    if (status != Status::Success) { record("probe-failed", lun, static_cast<uint32_t>(status), c.target); return false; }
    const auto &g = *transport_.geometry(lun); record("geometry", lun, g.blocks, g.blockBytes);
    if (scratchWriteMode_ && lun == scratchGrant_.lun) {
      if (g.blocks != scratchGrant_.capacityBlocks || g.blockBytes != scratchGrant_.blockBytes) {
        record("scratch-geometry-mismatch", g.blocks, g.blockBytes); return false;
      }
      grantLunSeen = true;
      scratchObservation_.lun = lun; scratchObservation_.blockBytes = g.blockBytes;
      scratchObservation_.capacityBlocks = g.blocks; scratchObservation_.writeProtected = g.writeProtected;
    }
    auto child = new DaliUfsBlock;
    if (!child) return false;
    if (!child->initFor(this, lun, g, 0, g.blocks, false, false)) { child->release(); return false; }
    children_[lun] = child; any = true;
  }
  if (!any || (scratchWriteMode_ && !grantLunSeen)) return false;
  online_ = true; // initialize is called with the host gate held.
  return true;
}
IOReturn DaliUfs::readBlocks(uint8_t lun, IOMemoryDescriptor *buffer, uint64_t block, uint64_t count, uint64_t &actual) {
  actual = 0; IOLockLock(gate_);
  const auto g = transport_.geometry(lun);
  IOReturn result = kIOReturnNotReady;
  if (online_ && !closed_ && g && g->valid && !transport_.poisoned()) {
    result = kIOReturnBadArgument;
    if (count && block < g->blocks && count <= g->blocks - block && count <= UINT64_MAX / g->blockBytes && buffer->getLength() == count * g->blockBytes) {
      result = kIOReturnSuccess;
      for (uint64_t done = 0; done < count;) {
        uint32_t chunk = static_cast<uint32_t>(count - done < unixnu_ufs::DataMax / g->blockBytes ? count - done : unixnu_ufs::DataMax / g->blockBytes);
        Status status = transport_.read(lun, block + done, chunk, staging_); ++reads_;
        uint32_t bytes = status == Status::Success || status == Status::Partial ? transport_.completion().bytes : 0;
        if (bytes) {
          IOByteCount copied = buffer->writeBytes(actual, staging_, bytes); actual += copied;
          if (copied != bytes) { result = kIOReturnIOError; break; }
        }
        if (status != Status::Success) { result = error(status); break; }
        done += chunk;
      }
    }
  }
  IOLockUnlock(gate_); return result;
}
IOReturn DaliUfs::writeBlocks(uint8_t lun, IOMemoryDescriptor *buffer, uint64_t block, uint64_t count, uint64_t &actual) {
  actual = 0;
  if (!buffer || !count || count > UINT32_MAX) return kIOReturnBadArgument;
  IOLockLock(gate_);
  const auto g = transport_.geometry(lun);
  IOReturn result = kIOReturnNotReady;
  uint64_t absolute = 0;
  if (online_ && !closed_ && scratchWriteMode_ && g && g->valid && !transport_.poisoned()) {
    result = kIOReturnLockedWrite;
    if (!g->writeProtected && dali_ufs::scratchAbsoluteLba(scratchGrant_, block, static_cast<uint32_t>(count), absolute) &&
        count <= UINT64_MAX / g->blockBytes && buffer->getLength() == count * g->blockBytes) {
      result = kIOReturnSuccess;
      for (uint64_t done = 0; done < count;) {
        uint32_t chunk = static_cast<uint32_t>(count - done < unixnu_ufs::DataMax / g->blockBytes ?
                                               count - done : unixnu_ufs::DataMax / g->blockBytes);
        uint32_t bytes = chunk * g->blockBytes;
        IOByteCount copied = buffer->readBytes(actual, staging_, bytes);
        if (copied != bytes) { result = kIOReturnIOError; break; }
        Status status = transport_.write(lun, absolute + done, chunk, staging_);
        uint32_t completed = status == Status::Success ? transport_.completion().bytes :
                             status == Status::Partial ? transport_.completion().bytes : 0;
        if (completed) actual += completed;
        if (status != Status::Success || completed != bytes) {
          result = status == Status::Success ? kIOReturnIOError : error(status); break;
        }
        done += chunk;
      }
    }
  }
  IOLockUnlock(gate_); return result;
}
IOReturn DaliUfs::sync(uint8_t lun) {
  IOLockLock(gate_); Status s = online_ && !closed_ ? transport_.synchronize(lun) : Status::NotReady;
  if (s == Status::Success) ++syncs_;
  IOLockUnlock(gate_); return error(s);
}
bool DaliUfs::sample(uint8_t lun, IOMedia *media, uint64_t lba, const char *kind) {
  uint64_t size = media->getPreferredBlockSize(), actual = 0;
  if (!size || size > 65536 || lba > UINT64_MAX / size || !media->open(this, 0, kIOStorageAccessReader)) { record("sample-open-failed", lun); return false; }
  auto buffer = IOBufferMemoryDescriptor::withCapacity(size, kIODirectionIn);
  IOStorage *storage = media;
  IOReturn status = buffer ? storage->read(this, lba * size, buffer, nullptr, &actual) : kIOReturnNoMemory;
  if (status == kIOReturnSuccess && actual == size) {
    uint8_t digest[32]; SHA256_CTX hash; SHA256_Init(&hash); SHA256_Update(&hash, buffer->getBytesNoCopy(), actual); SHA256_Final(digest, &hash);
    char hex[65]; for (unsigned i = 0; i < 32; ++i) { static const char digits[] = "0123456789abcdef"; hex[i*2] = digits[digest[i] >> 4]; hex[i*2+1] = digits[digest[i] & 15]; } hex[64] = 0;
    record("sample", lun, lba, actual); record(kind, media->getBase(), media->getSize(), size); record(hex);
  } else record("sample-failed", lun, status, actual);
  bool passed = status == kIOReturnSuccess && actual == size;
  if (buffer) buffer->release(); media->close(this); return passed;
}
void DaliUfs::partitions(uint8_t lun, IORegistryEntry *root, unsigned depth, unsigned &count, bool &bootSample) {
  if (depth > 8 || count >= 512) return;
  auto entries = root->getChildIterator(gIOServicePlane); if (!entries) return;
  while (auto e = entries->getNextObject()) {
    if (++count > 512) break;
    auto entry = OSDynamicCast(IORegistryEntry, e); if (!entry) continue;
    auto media = OSDynamicCast(IOMedia, entry);
    if (media && !media->isWhole()) {
      record("partition", lun, media->getBase(), media->getSize()); textRecord(1, media->getName());
      auto uuid = OSDynamicCast(OSString, media->getProperty(kIOMediaUUIDKey)); if (uuid) textRecord(2, uuid->getCStringNoCopy());
      if (!bootSample && strcmp(media->getName(), "boot_b") == 0) bootSample = sample(lun, media, 0, "boot_b");
    }
    partitions(lun, entry, depth + 1, count, bootSample);
  }
  entries->release();
}
IOMedia *DaliUfs::findBootB(IORegistryEntry *root, unsigned depth, unsigned &count) {
  if (!root || depth > 8 || count >= 512) return nullptr;
  auto entries = root->getChildIterator(gIOServicePlane); if (!entries) return nullptr;
  IOMedia *found = nullptr;
  while (auto object = entries->getNextObject()) {
    if (++count > 512) break;
    auto entry = OSDynamicCast(IORegistryEntry, object); if (!entry) continue;
    auto media = OSDynamicCast(IOMedia, entry);
    if (media && !media->isWhole() && strcmp(media->getName(), "boot_b") == 0) {
      media->retain(); found = media; break;
    }
    found = findBootB(entry, depth + 1, count);
    if (found) break;
  }
  entries->release(); return found;
}
bool DaliUfs::publishScratch(uint8_t lun, IOMedia *whole) {
  if (!scratchWriteMode_) return true;
  if (!whole || lun != scratchGrant_.lun) return false;
  if (scratchChild_) {
    IOLockLock(gate_); bool ready = scratchMediaReady_; IOLockUnlock(gate_);
    return ready;
  }
  const auto geometry = transport_.geometry(lun);
  if (!geometry || !geometry->valid || geometry->writeProtected || !scratchObservation_.identityVerified) {
    record("scratch-observation-refused", lun); return false;
  }
  IOMedia *boot = nullptr;
  for (unsigned attempt = 0; attempt < 500 && !boot; ++attempt) {
    unsigned count = 0; boot = findBootB(whole, 0, count);
    if (!boot) IOSleep(10);
  }
  if (!boot) { record("scratch-boot-b-missing", lun); return false; }
  bool valid = false; uint8_t guid[16] = {};
  ScratchObservation verified{};
  auto uuid = OSDynamicCast(OSString, boot->getProperty(kIOMediaUUIDKey));
  const uint64_t blockBytes = geometry->blockBytes;
  const uint64_t baseBytes = boot->getBase(), sizeBytes = boot->getSize();
  if (uuid && dali_ufs::parsePartitionGuid(uuid->getCStringNoCopy(), guid) && blockBytes &&
      boot->getPreferredBlockSize() == blockBytes && baseBytes % blockBytes == 0 &&
      sizeBytes % blockBytes == 0 && sizeBytes != 0) {
    const uint64_t bootStart = baseBytes / blockBytes, bootBlocks = sizeBytes / blockBytes;
    valid = bootStart == scratchGrant_.bootStartLba && bootBlocks == scratchGrant_.bootBlocks &&
            !memcmp(guid, scratchGrant_.partitionGuid, sizeof guid);
    if (valid) {
      IOLockLock(gate_); verified = scratchObservation_; IOLockUnlock(gate_);
      memcpy(verified.partitionGuid, guid, sizeof guid);
      verified.bootStartLba = bootStart; verified.bootBlocks = bootBlocks;
      verified.gptVerified = true; verified.writeProtected = geometry->writeProtected;
      verified.scratchMediaReady = false;
    }
  }
  boot->release();
  if (!valid) { record("scratch-gpt-refused", lun); return false; }

  auto child = new DaliUfsBlock;
  if (!child) return false;
  if (!child->initFor(this, lun, *geometry, scratchGrant_.scratchStartLba, scratchGrant_.scratchBlocks, true, true)) {
    child->release(); return false;
  }
  scratchChild_ = child;
  if (!child->attach(this)) { scratchChild_ = nullptr; child->release(); return false; }
  if (!child->start(this)) { child->detach(this); scratchChild_ = nullptr; child->release(); return false; }
  IOMedia *scratchMedia = nullptr;
  for (unsigned attempt = 0; attempt < 500 && !scratchMedia; ++attempt) {
    scratchMedia = child->copyMedia(); if (!scratchMedia) IOSleep(10);
  }
  if (!scratchMedia || scratchMedia->getPreferredBlockSize() != blockBytes ||
      scratchMedia->getSize() != scratchGrant_.scratchBlocks * blockBytes || !scratchMedia->isWritable()) {
    if (scratchMedia) scratchMedia->release();
    record("scratch-media-refused", lun); return false;
  }
  uint64_t mediaBytes = scratchMedia->getSize();
  uint32_t mediaBlockBytes = scratchMedia->getPreferredBlockSize();
  scratchMedia->release();
  verified.scratchMediaReady = true;
  if (!dali_ufs::scratchWriteAllowed(scratchGrant_, verified, scratchGrant_.scratchStartLba, 1)) {
    record("scratch-policy-refused", lun); return false;
  }
  IOLockLock(gate_);
  bool arm = online_ && !closed_ && !transport_.poisoned() && !scratchMediaReady_;
  if (arm) { scratchObservation_ = verified; scratchMediaReady_ = true; }
  IOLockUnlock(gate_);
  if (!arm) { record("scratch-arm-refused", lun); return false; }
  record("scratch-media", lun, mediaBytes, mediaBlockBytes);
  record("scratch-child", lun, scratchGrant_.scratchStartLba, scratchGrant_.scratchBlocks);
  return true;
}
bool DaliUfs::diagnose() {
  bool boot = false, all = true;
  for (uint8_t lun = 0; lun < 8; ++lun) if (children_[lun]) {
    IOMedia *media = nullptr;
    for (unsigned n = 0; n < 500 && !media; ++n) { media = children_[lun]->copyMedia(); if (!media) IOSleep(10); }
    if (!media) { record("media-missing", lun); all = false; continue; }
    IOLockLock(gate_); const auto g = *transport_.geometry(lun); IOLockUnlock(gate_);
    if (media->getPreferredBlockSize() != g.blockBytes || media->getSize() != g.blocks * g.blockBytes || media->isWritable()) { all = false; media->release(); continue; }
    record("media", lun, media->getSize(), media->getPreferredBlockSize());
    all &= sample(lun, media, 0, "whole"); all &= sample(lun, media, 1, "whole"); all &= sample(lun, media, g.blocks - 1, "whole");
    // Let asynchronous partition matching finish before recording its children.
    IOSleep(1000); unsigned count = 0; partitions(lun, media, 0, count, boot);
    if (scratchWriteMode_ && lun == scratchGrant_.lun) {
      if (all) all &= publishScratch(lun, media);
      else scratchObservation_.identityVerified = false;
    }
    IOReturn result = children_[lun]->doSynchronize(0, 0);
    IOLockLock(gate_); uint64_t syncCount = syncs_; IOLockUnlock(gate_);
    record("sync", lun, result, syncCount); if (result) all = false;
    media->release();
  }
  IOLockLock(gate_); uint64_t readCount = reads_, syncCount = syncs_; IOLockUnlock(gate_);
  record("diagnostics-end", all && boot, readCount, syncCount); return all && boot;
}
bool DaliUfs::quiesce(bool requireSync) {
  IOLockLock(gate_); bool wasClosed = closed_; closed_ = true; online_ = false;
  scratchMediaReady_ = false; scratchObservation_.scratchMediaReady = false;
  // Close admission, synchronize the owned LUNs, then stop before freeing DMA.
  // A failed or uncertain scratch write is never replayed.
  if (!wasClosed && exposed_) {
    if (transport_.poisoned()) stopSync_ = Status::NotReady;
    else for (uint8_t lun = 0; lun < 8; ++lun) if (transport_.geometry(lun)->valid) {
      Status s = transport_.synchronize(lun);
      if (s == Status::Success) ++syncs_;
      else { stopSync_ = s; break; }
    }
  }
  transport_.close(); auto stopped = controller_.stop();
  bool safe = stopped == dali_ufs::Result::Stopped &&
              (!dma_ || ((!exposed_ || transport_.releaseAllowed()) && read(this, dali_ufs::Hci + 0x34) == 0));
  if (safe && dma_) { if (prepared_) { dma_->complete(kIODirectionInOut); prepared_ = false; } dma_->release(); dma_ = nullptr; }
  record("quiesce-sync", static_cast<uint32_t>(stopSync_), syncs_);
  record("quiesce", static_cast<uint32_t>(stopped), safe, read(this, dali_ufs::Hci + 0x34));
  bool accepted = safe && (!requireSync || stopSync_ == Status::Success);
  IOLockUnlock(gate_); return accepted;
}
void DaliUfs::worker(void *p, wait_result_t) {
  auto self = static_cast<DaliUfs *>(p);
  // Matching order is asynchronous. Only the token-bound, persisted platform
  // checkpoint can authorize touching UFS; absence is a bounded refusal.
  for (unsigned n = 0; n < 500 && !dali_ufs_platform_ready(self->token_); ++n) IOSleep(10);
  IOLockLock(self->gate_); bool initialized = !self->closed_ && self->initialize(); IOLockUnlock(self->gate_);
  // Publication can synchronously call back into report/read methods. Never
  // hold the transport gate across driver matching.
  if (initialized) for (auto child : self->children_) if (child) {
    if (!child->attach(self)) { initialized = false; break; }
    if (!child->start(self)) { child->detach(self); initialized = false; break; }
  }
  self->record("initialized", initialized);
  bool diagnosed = initialized && self->diagnose();
  self->record("diagnostics", diagnosed, self->scratchWriteMode_);
  if (initialized && self->scratchWriteMode_ && !diagnosed) self->quiesce();
  else {
    if (!initialized) {
      IOLockLock(self->gate_); const auto c = self->transport_.completion(); IOLockUnlock(self->gate_);
      self->record("failed-command", static_cast<uint32_t>(c.status), c.opcode, c.interrupt);
      self->quiesce();
    }
  }
  // Keep only the explicitly published storage devices usable until manual recovery.
  dali_ufs_wait_for_manual_recovery(self->token_, stopBeforeReset, self);
  self->quiesce(); self->release(); thread_terminate(current_thread());
}
bool DaliUfs::start(IOService *provider) {
  unsigned enabled = 0, early = 0, autoFastboot = 0, pid1 = 0, scratch = 0; char reset[16] = {};
  if (!PE_parse_boot_argn("dali-ufs-readonly", &enabled, sizeof enabled) || enabled != 1) return false;
  bool scratchArg = PE_parse_boot_argn("dali-ufs-scratch-write", &scratch, sizeof scratch);
  PE_parse_boot_argn("dali-earlyboot", &early, sizeof early); PE_parse_boot_argn("dali-auto-fastboot", &autoFastboot, sizeof autoFastboot); PE_parse_boot_argn("dali-pid1", &pid1, sizeof pid1);
  if ((scratchArg && scratch != 1) || early != 1 || autoFastboot || pid1 || PE_parse_boot_argn("dali-reset-method", reset, sizeof reset) ||
      !PE_parse_boot_argn("dali-run", token_, sizeof token_) || !dali_ufs::tokenValid(token_)) return false;
  auto node = IORegistryEntry::fromPath("/arm-io/dali-ufs", gIODTPlane);
  auto resource = node ? OSDynamicCast(OSData, node->getProperty("resources-v1")) : nullptr;
  if (!resource || !dali_ufs::resourceValid(resource->getBytesNoCopy(), resource->getLength())) { if (node) node->release(); return false; }
  memcpy(resources_, resource->getBytesNoCopy(), sizeof resources_);
  if (scratchArg) {
    auto grant = OSDynamicCast(OSData, node->getProperty("scratch-grant-v1"));
    if (!grant || !dali_ufs::decodeScratchGrant(grant->getBytesNoCopy(), grant->getLength(), scratchGrant_) ||
        strcmp(scratchGrant_.token, token_)) { node->release(); return false; }
    scratchWriteMode_ = true;
  }
  node->release();
  if (!IOService::start(provider)) return false;
  gate_ = IOLockAlloc(); if (!gate_) return false;
  console_ = map(0xaec0f000, 0x200000); hci_ = map(dali_ufs::Hci, 0x2a00); ao_ = map(dali_ufs::Ao, 0x1000);
  if (!console_ || !hci_ || !ao_) return false;
  record("start"); registerService(); retain(); thread_t thread;
  if (kernel_thread_start(worker, this, &thread) != KERN_SUCCESS) { release(); return false; }
  thread_deallocate(thread); return true;
}
void DaliUfs::stop(IOService *provider) { if (gate_) quiesce(); IOService::stop(provider); }
void DaliUfs::free() {
  // Failure to stop deliberately retains the object, mappings and DMA pages.
  if (gate_ && !quiesce()) { record("quarantine-retained"); return; }
  // This built-in diagnostic has no hot-unplug path. Attached children may
  // still receive offline queries; retain their owner/gate instead of dangling
  // it. Controller/DMA have already been quiesced above.
  for (auto child : children_) if (child && child->getProvider() == this) { record("published-owner-retained"); return; }
  if (scratchChild_ && scratchChild_->getProvider() == this) { record("published-scratch-owner-retained"); return; }
  if (scratchChild_) { scratchChild_->release(); scratchChild_ = nullptr; }
  for (auto &child : children_) if (child) { child->release(); child = nullptr; }
  if (staging_) { IOFree(staging_, unixnu_ufs::DataMax); staging_ = nullptr; }
  if (hci_) hci_->release(); if (ao_) ao_->release(); if (console_) console_->release();
  if (gate_) IOLockFree(gate_); IOService::free();
}
OSDefineMetaClassAndStructors(DaliUfs, IOService)
OSDefineMetaClassAndStructors(DaliUfsBlock, IOBlockStorageDevice)
