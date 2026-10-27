// SPDX-License-Identifier: BSD-2-Clause-Patent
// Default-off, read-only Dali UFS windows for an authenticated Apple root.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOBSD.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <IOKit/IORegistryIterator.h>
#include <IOKit/storage/IOBlockStorageDevice.h>
#include <IOKit/storage/IOBlockStorageDriver.h>
#include <IOKit/storage/IOMedia.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSIterator.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSString.h>
#include <libkern/crypto/sha2.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <sys/conf.h>
#include "UfsTransport.h"
#include "DaliUfsPlatform.h"
#include "DaliUfsMemory.h"
#include "DaliUfsRecovery.h"
#include "verified_root.h"
#include "VerifiedBlockPolicy.h"
#include "RootDiagnostics.h"

using unixnu_ufs::Status;
static constexpr uint64_t kDataMax = unixnu_ufs::DataMax;
static UfsRootDiagnostics gRootDiagnostic;

class DaliUfsRoot;
class DaliUfsRootBlock;
static DaliUfsRoot *gRootLayoutOwner = nullptr;
static IOReturn hostRead4k(uint8_t, uint64_t, uint32_t, void *);
#include "DaliUfsRootLayout.h"
#include "RootWindowRead.h"
#include "RootPublication.h"

extern "C" {
extern const struct vr_crypto vr_xnu_crypto __attribute__((weak_import));
extern const uint8_t verified_root_key[VR_KEY_BYTES] __attribute__((weak_import));
extern const NativeVerifiedBlockPolicy verified_root_policies[] __attribute__((weak_import));
extern const uint64_t verified_root_policy_count __attribute__((weak_import));
int vr_open(const uint8_t *, size_t, const uint8_t[VR_KEY_BYTES], uint64_t,
            const struct vr_crypto *, struct vr_view *) __attribute__((weak_import));
int vr_verify_block(const struct vr_view *, uint64_t, const void *, size_t,
                    const struct vr_crypto *) __attribute__((weak_import));
}

static constexpr uint8_t kDaliRootLun = 2;
static constexpr char kDaliRootContainer[] = "unixnu_data";
static constexpr char kDaliOsRootUuid[] = "04769D26-2CA0-4825-B443-E2E5F88300F2";
static constexpr uint64_t kDaliContainerFirstLba = 7023103;
static constexpr uint64_t kDaliContainerLastLba = 15403518;

static bool parseUuid(const char *text, uint8_t out[16]) {
  if (!text || !out || strnlen(text, 37) != 36) return false;
  unsigned byte = 0;
  for (unsigned at = 0; at < 36;) {
    if (at == 8 || at == 13 || at == 18 || at == 23) {
      if (text[at++] != '-') return false;
      continue;
    }
    auto nibble = [](char c) -> int {
      if (c >= '0' && c <= '9') return c - '0';
      if (c >= 'a' && c <= 'f') return c - 'a' + 10;
      if (c >= 'A' && c <= 'F') return c - 'A' + 10;
      return -1;
    };
    int high = nibble(text[at++]), low = nibble(text[at++]);
    if (high < 0 || low < 0 || byte >= 16) return false;
    out[byte++] = static_cast<uint8_t>((high << 4) | low);
  }
  return byte == 16;
}

static void uuidString(const uint8_t uuid[16], char out[37]) {
  static const char hex[] = "0123456789ABCDEF";
  unsigned at = 0;
  for (unsigned i = 0; i < 16; ++i) {
    if (i == 4 || i == 6 || i == 8 || i == 10) out[at++] = '-';
    out[at++] = hex[uuid[i] >> 4];
    out[at++] = hex[uuid[i] & 15];
  }
  out[at] = 0;
}

class DaliUfsRoot : public IOService {
  OSDeclareDefaultStructors(DaliUfsRoot)
  friend class DaliUfsRootBlock;
  friend struct DaliRootReadOps;
  IOLock *gate_ = nullptr;
  IOMemoryMap *hci_ = nullptr, *ao_ = nullptr, *console_ = nullptr;
  IOBufferMemoryDescriptor *dma_ = nullptr;
  uint8_t *bounce_ = nullptr;
  uint8_t resources_[dali_ufs::ResourceBytes]{};
  char token_[33]{};
  uint32_t dmaBytes_ = 0, line_ = 0, frequency_ = 0;
  uint64_t dmaPhysical_ = 0, reads_ = 0, failures_ = 0;
  bool prepared_ = false, exposed_ = false, online_ = false, closed_ = false;
  bool published_ = false, rootAuthorized_ = false;
  uint8_t rootLun_ = kDaliRootLun, windowCount_ = 0, osWindow_ = 0xff;
  uint8_t publishedCount_ = 0;
  RootLayout layout_{};
  UfsRootPlan plan_{};
  uint64_t absolute_[4]{};
  struct vr_view verified_[4]{};
  DaliUfsRootBlock *windows_[4]{};
  bool attached_[4]{}, started_[4]{};
  dali_ufs::Controller controller_{};
  unixnu_ufs::Transport transport_{};

  static void worker(void *, wait_result_t);
  static IOMemoryMap *map(uint64_t, uint64_t);
  static uint32_t read(void *, uint64_t);
  static bool write(void *, uint64_t, uint32_t);
  static void fence(void *);
  static void delay(void *, uint32_t);
  static uint64_t sip(void *, uint32_t, uint32_t, uint32_t, uint32_t);
  static bool portRead(void *, uint32_t, uint32_t *);
  static bool portWrite(void *, uint32_t, uint32_t);
  static bool cache(void *, void *, uint32_t, unixnu_ufs::Cache);
  static uint64_t micros(void *);
  static bool exclusive(void *);
  void record(const char *, uint64_t = 0, uint64_t = 0, uint64_t = 0);
  bool initialize();
  bool loadPolicies();
  bool prepareWindows();
  bool publishWindows();
  void discardUnstarted();
  bool quiesce();
  IOReturn readPhysical(uint8_t, uint64_t, uint32_t, void *);
  IOReturn sync(uint8_t);
  IOReturn readWindow(DaliUfsRootBlock *, IOMemoryDescriptor *, uint64_t, uint64_t, uint64_t &);
  IOReturn error(Status) const;
  bool online() const;
  bool rootAuthorized() const;
public:
  bool start(IOService *) override;
  void stop(IOService *) override;
  void free() override;
  IOReturn readLayoutBlocks(uint8_t lun, uint64_t lba, uint32_t blocks, void *out) {
    return readPhysical(lun, lba, blocks, out);
  }
};

class DaliUfsRootBlock : public IOBlockStorageDevice {
  OSDeclareDefaultStructors(DaliUfsRootBlock)
  friend class DaliUfsRoot;
  friend struct DaliRootReadOps;
  DaliUfsRoot *owner_ = nullptr;
  uint8_t index_ = 0;
  uint64_t absolute_ = 0, bytes_ = 0;
  struct vr_view verified_{};
public:
  bool initFor(DaliUfsRoot *owner, uint8_t index, uint64_t absolute, const RootWindow &window,
               const struct vr_view &verified) {
    if (!owner || index >= 4 || !ufsWindowGeometry(absolute, window.size) ||
        verified.image_bytes != window.size || !verified.hashes ||
        !IOBlockStorageDevice::init(nullptr)) return false;
    owner_ = owner; index_ = index; absolute_ = absolute; bytes_ = window.size;
    verified_ = verified; return true;
  }
  bool start(IOService *provider) override {
    if (!owner_ || !IOBlockStorageDevice::start(provider)) return false;
    registerService(); return true;
  }
  IOReturn doEjectMedia() override { return kIOReturnUnsupported; }
  IOReturn doFormatMedia(UInt64) override { return kIOReturnLockedWrite; }
  UInt32 doGetFormatCapacities(UInt64 *, UInt32) const override { return 0; }
  char *getVendorString() override { return const_cast<char *>("MediaTek"); }
  char *getProductString() override { return const_cast<char *>("Dali verified read-only root window"); }
  char *getRevisionString() override { return const_cast<char *>("1"); }
  char *getAdditionalDeviceInfoString() override { return const_cast<char *>("MT6991 UFS LUN 2"); }
  IOReturn reportBlockSize(UInt64 *out) override {
    if (!out) return kIOReturnBadArgument; *out = 512; return kIOReturnSuccess;
  }
  IOReturn reportMaxValidBlock(UInt64 *out) override {
    if (!out || !bytes_ || (bytes_ & 511)) return kIOReturnBadArgument;
    *out = bytes_ / 512 - 1; return kIOReturnSuccess;
  }
  IOReturn reportEjectability(bool *out) override {
    if (!out) return kIOReturnBadArgument; *out = false; return kIOReturnSuccess;
  }
  IOReturn reportRemovability(bool *out) override { return reportEjectability(out); }
  IOReturn reportWriteProtection(bool *out) override {
    if (!out) return kIOReturnBadArgument; *out = true; return kIOReturnSuccess;
  }
  IOReturn reportMediaState(bool *present, bool *changed = nullptr) override {
    if (!present || !owner_) return kIOReturnBadArgument;
    *present = owner_->online(); if (changed) *changed = false; return kIOReturnSuccess;
  }
  IOReturn doAsyncReadWrite(IOMemoryDescriptor *buffer, UInt64 block, UInt64 count,
                            IOStorageAttributes *, IOStorageCompletion *completion) override {
    if (!completion || !completion->action) return kIOReturnBadArgument;
    uint64_t actual = 0; IOReturn status = kIOReturnBadArgument;
    if (buffer && count && block < bytes_ / 512 && count <= bytes_ / 512 - block &&
        count <= UINT64_MAX / 512 && buffer->getLength() == count * 512) {
      if (buffer->getDirection() == kIODirectionIn)
        status = owner_->readWindow(this, buffer, block, count, actual);
      else if (buffer->getDirection() == kIODirectionOut)
        status = kIOReturnLockedWrite;
    }
    IOStorage::complete(completion, status, actual);
    return kIOReturnSuccess;
  }
  IOReturn doSynchronize(UInt64 block, UInt64 count, IOStorageSynchronizeOptions options = 0) override {
    if (options || block >= bytes_ / 512 || (count && count > bytes_ / 512 - block))
      return kIOReturnBadArgument;
    return owner_->sync(kDaliRootLun);
  }
  bool authenticatesVolume(const char *uuid, uint8_t *manifestDigest = nullptr) const {
    if (!owner_ || !owner_->rootAuthorized() || index_ != owner_->osWindow_ || !uuid) return false;
    uint8_t parsed[16];
    if (!parseUuid(uuid, parsed) || memcmp(parsed, verified_.volume_uuid, sizeof parsed)) return false;
    if (manifestDigest) {
      SHA384_CTX hash; SHA384_Init(&hash);
      const uint64_t envelope = VR_HEADER_BYTES + VR_SIGNATURE_BYTES + verified_.block_count * 32;
      const uint8_t *manifest = verified_.hashes - (VR_HEADER_BYTES + VR_SIGNATURE_BYTES);
      SHA384_Update(&hash, manifest, envelope); SHA384_Final(manifestDigest, &hash);
    }
    return true;
  }
  bool signedVolumeUuid(char out[37]) const {
    if (!out || !verified_.hashes) return false;
    uuidString(verified_.volume_uuid, out); return true;
  }
};

static IOReturn hostRead4k(uint8_t lun, uint64_t lba, uint32_t blocks, void *out) {
  return gRootLayoutOwner ? gRootLayoutOwner->readLayoutBlocks(lun, lba, blocks, out) : kIOReturnNotReady;
}

IOMemoryMap *DaliUfsRoot::map(uint64_t base, uint64_t bytes) {
  auto memory = IOMemoryDescriptor::withPhysicalAddress(base, bytes, kIODirectionInOut);
  if (!memory) return nullptr;
  auto result = memory->map(kIOMapAnywhere | kIOMapInhibitCache); memory->release(); return result;
}
void DaliUfsRoot::fence(void *) { __asm__ volatile("dsb sy" ::: "memory"); }
void DaliUfsRoot::delay(void *, uint32_t us) { IODelay(us); }
uint32_t DaliUfsRoot::read(void *p, uint64_t address) {
  auto self = static_cast<DaliUfsRoot *>(p); uintptr_t va = 0;
  if ((address & 3) == 0 && address >= dali_ufs::Hci && address - dali_ufs::Hci <= 0x2a00 - 4 && self->hci_)
    va = self->hci_->getVirtualAddress() + address - dali_ufs::Hci;
  else if ((address & 3) == 0 && address >= dali_ufs::Ao && address - dali_ufs::Ao <= 0x1000 - 4 && self->ao_)
    va = self->ao_->getVirtualAddress() + address - dali_ufs::Ao;
  if (!va) return UINT32_MAX;
  fence(nullptr); uint32_t value = *reinterpret_cast<volatile uint32_t *>(va); fence(nullptr); return value;
}
bool DaliUfsRoot::write(void *p, uint64_t address, uint32_t value) {
  auto self = static_cast<DaliUfsRoot *>(p); uintptr_t va = 0;
  if ((address & 3) == 0 && address >= dali_ufs::Hci && address - dali_ufs::Hci <= 0x2a00 - 4 && self->hci_)
    va = self->hci_->getVirtualAddress() + address - dali_ufs::Hci;
  else if ((address & 3) == 0 && address >= dali_ufs::Ao && address - dali_ufs::Ao <= 0x1000 - 4 && self->ao_)
    va = self->ao_->getVirtualAddress() + address - dali_ufs::Ao;
  if (!va) return false;
  fence(nullptr); *reinterpret_cast<volatile uint32_t *>(va) = value; fence(nullptr); return true;
}
__attribute__((noinline)) uint64_t DaliUfsRoot::sip(void *, uint32_t service, uint32_t selector,
                                                   uint32_t a, uint32_t b) {
  register uint64_t x0 __asm__("x0") = service, x1 __asm__("x1") = selector;
  register uint64_t x2 __asm__("x2") = a, x3 __asm__("x3") = b;
  register uint64_t x4 __asm__("x4") = 0, x5 __asm__("x5") = 0, x6 __asm__("x6") = 0, x7 __asm__("x7") = 0;
  __asm__ volatile("dsb sy\n\tsmc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3), "+r"(x4), "+r"(x5), "+r"(x6), "+r"(x7) :
                   : "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17", "memory", "cc");
  return x0;
}
bool DaliUfsRoot::portRead(void *p, uint32_t off, uint32_t *v) {
  if (off > 0x2a00 - 4 || (off & 3) || !v) return false;
  *v = read(p, dali_ufs::Hci + off); return *v != UINT32_MAX;
}
bool DaliUfsRoot::portWrite(void *p, uint32_t off, uint32_t v) {
  return off <= 0x2a00 - 4 && !(off & 3) && write(p, dali_ufs::Hci + off, v);
}
bool DaliUfsRoot::exclusive(void *p) {
  return static_cast<DaliUfsRoot *>(p)->controller_.ready() && read(p, dali_ufs::Hci + 0x300) == 0;
}
uint64_t DaliUfsRoot::micros(void *p) {
  auto self = static_cast<DaliUfsRoot *>(p); if (!self->frequency_) return 0;
  uint64_t ticks; __asm__ volatile("isb\n\tmrs %0, cntpct_el0" : "=r"(ticks) :: "memory");
  return (ticks / self->frequency_) * 1000000 + (ticks % self->frequency_) * 1000000 / self->frequency_;
}
__attribute__((noinline)) bool DaliUfsRoot::cache(void *p, void *cpu, uint32_t bytes,
                                                  unixnu_ufs::Cache operation) {
  auto self = static_cast<DaliUfsRoot *>(p); dali_ufs::CacheSpan span;
  if (!self->dma_ || !self->prepared_ || !dali_ufs::cacheSpan(
      reinterpret_cast<uintptr_t>(self->dma_->getBytesNoCopy()), self->dmaBytes_,
      reinterpret_cast<uintptr_t>(cpu), bytes, self->line_, span)) return false;
  fence(nullptr);
  for (uintptr_t at = span.begin; at < span.end; at += self->line_) {
    if (operation == unixnu_ufs::Cache::ToDevice) __asm__ volatile("dc civac, %0" :: "r"(at) : "memory");
    else __asm__ volatile("dc ivac, %0" :: "r"(at) : "memory");
  }
  fence(nullptr); return true;
}
void DaliUfsRoot::record(const char *stage, uint64_t a, uint64_t b, uint64_t c) {
  char line[256]; int n = snprintf(line, sizeof line,
      "DALI-IPADOS-UFSROOT: run=%s stage=%s a=%llx b=%llx c=%llx\n", token_, stage,
      a, b, c);
  IOLog("%s", line);
  if (!console_ || n <= 0 || static_cast<size_t>(n) >= sizeof line) return;
  auto ring = reinterpret_cast<volatile uint32_t *>(console_->getVirtualAddress());
  constexpr uint32_t capacity = 0x200000 - 12;
  uint64_t daif; __asm__ volatile("mrs %0, daif\n\tmsr daifset, #3" : "=r"(daif) :: "memory");
  if (ring[0] == 0x43474244 && ring[1] < capacity && ring[2] <= capacity) {
    auto text = reinterpret_cast<volatile uint8_t *>(ring + 3);
    for (int i = 0; i < n; ++i) {
      text[ring[1]] = static_cast<uint8_t>(line[i]);
      ring[1] = ring[1] + 1 == capacity ? 0 : ring[1] + 1;
      if (ring[2] < capacity) ++ring[2];
    }
    fence(nullptr);
  }
  __asm__ volatile("msr daif, %0" :: "r"(daif) : "memory");
}
IOReturn DaliUfsRoot::error(Status status) const {
  if (status == Status::Success) return kIOReturnSuccess;
  if (status == Status::Argument) return kIOReturnBadArgument;
  if (status == Status::NotReady) return kIOReturnNotReady;
  if (status == Status::Denied) return kIOReturnLockedWrite;
  if (status == Status::Timeout) return kIOReturnTimeout;
  return kIOReturnIOError;
}
bool DaliUfsRoot::online() const { return online_ && !closed_ && !transport_.poisoned(); }
bool DaliUfsRoot::rootAuthorized() const {
  return __atomic_load_n(&rootAuthorized_, __ATOMIC_ACQUIRE) && online();
}
IOReturn DaliUfsRoot::sync(uint8_t lun) {
  IOLockLock(gate_);
  Status status = online_ && !closed_ && lun == rootLun_ ? transport_.synchronize(lun) : Status::NotReady;
  IOLockUnlock(gate_); return error(status);
}

bool DaliUfsRoot::loadPolicies() {
  gRootDiagnostic.stage = "verified root policy symbols";
  if (!reinterpret_cast<uintptr_t>(verified_root_policies) ||
      !reinterpret_cast<uintptr_t>(&verified_root_policy_count) ||
      !reinterpret_cast<uintptr_t>(verified_root_key) ||
      !reinterpret_cast<uintptr_t>(&vr_xnu_crypto) || !vr_open || !vr_verify_block) {
    record("verified-root-policy-missing"); return false;
  }
  uint64_t count = verified_root_policy_count;
  if (!count || count > 16) { record("verified-root-policy-count", count); return false; }
  uint8_t osUuid[16];
  if (!parseUuid(kDaliOsRootUuid, osUuid)) return false;
  for (uint8_t index = 0; index < windowCount_; ++index) {
    const RootWindow &window = layout_.window[index];
    const uint8_t *manifest = nullptr; size_t manifestBytes = 0;
    for (uint64_t policyIndex = 0; policyIndex < count; ++policyIndex) {
      const auto &policy = verified_root_policies[policyIndex];
      if (!policy.manifest || policy.manifestBytes < VR_HEADER_BYTES + VR_SIGNATURE_BYTES) {
        record("verified-root-policy-envelope", policyIndex); return false;
      }
      uint64_t imageBytes = le64(policy.manifest + 24);
      if (imageBytes != window.size) continue;
      if (manifest) { record("verified-root-policy-ambiguous", index, imageBytes); return false; }
      manifest = policy.manifest; manifestBytes = static_cast<size_t>(policy.manifestBytes);
    }
    if (!manifest) { record("verified-root-policy-not-found", index, window.size); return false; }
    int status = vr_open(manifest, manifestBytes, verified_root_key, 1, &vr_xnu_crypto, &verified_[index]);
    gRootDiagnostic.verify = static_cast<uint32_t>(status);
    if (status != VR_OK || verified_[index].image_bytes != window.size ||
        memcmp(verified_[index].image_sha256, window.sha256, sizeof window.sha256)) {
      record("verified-root-open-refused", index, static_cast<uint32_t>(status), window.size); return false;
    }
    if (!strcmp(window.name, "OS")) {
      osWindow_ = index;
      if (memcmp(verified_[index].volume_uuid, osUuid, sizeof osUuid)) {
        record("verified-root-os-uuid-refused", index); return false;
      }
    }
    record("verified-root-policy-pass", index, verified_[index].block_count, window.size);
  }
  return osWindow_ < windowCount_;
}

bool DaliUfsRoot::initialize() {
  if (!dali_ufs_platform_ready(token_)) { record("platform-refused"); return false; }
  uint64_t ctr, hz, mpidr;
  __asm__ volatile("mrs %0, ctr_el0\n\tmrs %1, cntfrq_el0\n\tmrs %2, mpidr_el1" : "=r"(ctr), "=r"(hz), "=r"(mpidr));
  line_ = 4U << ((ctr >> 16) & 15); frequency_ = static_cast<uint32_t>(hz);
  if (line_ < 16 || line_ > 4096 || hz != 13000000 || (mpidr & 0xff00ffffffULL)) {
    record("cpu-prerequisite-refused", line_, hz, mpidr); return false;
  }
  auto result = controller_.start(true, resources_, sizeof resources_, token_, {this, read, write, fence, delay, sip});
  const auto &diagnostic = controller_.diagnostic();
  record("controller", static_cast<uint32_t>(result), static_cast<uint32_t>(diagnostic.stage), diagnostic.faultValue);
  if (result != dali_ufs::Result::Ready) { record("controller-fault", diagnostic.faultAddress, diagnostic.interrupt, diagnostic.sipStatus); return false; }
  IOOptionBits options = static_cast<IOOptionBits>(kIODirectionInOut) |
      static_cast<IOOptionBits>(kIOMemoryPhysicallyContiguous) |
      static_cast<IOOptionBits>(kIOMemoryMapperNone) | static_cast<IOOptionBits>(kIOMapCopybackCache);
  dma_ = IOBufferMemoryDescriptor::inTaskWithPhysicalMask(kernel_task, options, unixnu_ufs::DmaBytes, UINT32_MAX);
  if (!dma_ || dma_->getCapacity() > UINT32_MAX || dma_->prepare(kIODirectionInOut) != kIOReturnSuccess) return false;
  prepared_ = true; dmaBytes_ = static_cast<uint32_t>(dma_->getCapacity());
  IOByteCount span = 0; dmaPhysical_ = dma_->getPhysicalSegment(0, &span, kIOMemoryMapperNone);
  auto cpu = dma_->getBytesNoCopy();
  if (dmaBytes_ < unixnu_ufs::DmaBytes || !dali_ufs::dmaSpan(reinterpret_cast<uintptr_t>(cpu), dmaPhysical_, dmaBytes_, span)) return false;
  bounce_ = static_cast<uint8_t *>(IOMallocZero(unixnu_ufs::DataMax)); if (!bounce_) return false;
  bzero(cpu, dmaBytes_); record("dma", dmaPhysical_, dmaBytes_, line_);
  // No allowWrite callback is installed. UfsTransport rejects every media write.
  unixnu_ufs::Port port{this, portRead, portWrite, cache, micros, delay, exclusive, nullptr};
  exposed_ = true;
  Status status = transport_.start(port, {cpu, dmaPhysical_, UINT32_MAX, dmaBytes_});
  if (status == Status::Success) status = transport_.initializeDevice();
  if (status != Status::Success) { record("transport-failed", static_cast<uint32_t>(status)); return false; }
  status = transport_.probe(rootLun_);
  if (status != Status::Success) { record("root-lun-probe-failed", rootLun_, static_cast<uint32_t>(status)); return false; }
  const auto *geometry = transport_.geometry(rootLun_);
  if (!geometry || !geometry->valid || geometry->blockBytes != 4096 || !geometry->blocks) {
    record("root-lun-geometry-refused", rootLun_, geometry ? geometry->blockBytes : 0,
           geometry ? geometry->blocks : 0); return false;
  }
  gRootDiagnostic.stage = "root layout";
  gRootLayoutOwner = this;
  const char *why = nullptr;
  bool layoutOk = findRootLayout(&layout_, &why, rootLun_, kDaliRootContainer);
  gRootLayoutOwner = nullptr;
  if (!layoutOk || layout_.manifestStatus || layout_.windows != 2 ||
      layout_.userdataFirst != kDaliContainerFirstLba || layout_.userdataLast != kDaliContainerLastLba) {
    record("root-layout-refused", layout_.manifestStatus, layout_.userdataFirst, layout_.userdataLast); return false;
  }
  const char *names[4] = {};
  for (uint8_t index = 0; index < layout_.windows; ++index) names[index] = layout_.window[index].name;
  if (!ufsBuildRootPlan(names, layout_.windows, plan_)) { record("root-window-plan-refused", layout_.windows); return false; }
  windowCount_ = static_cast<uint8_t>(layout_.windows);
  if (layout_.userdataFirst > UINT64_MAX / 4096ULL) { record("root-layout-overflow"); return false; }
  uint64_t containerBase = layout_.userdataFirst * 4096ULL;
  for (uint8_t index = 0; index < windowCount_; ++index) {
    if (layout_.window[index].offset > UINT64_MAX - containerBase) return false;
    absolute_[index] = containerBase + layout_.window[index].offset;
    if (!ufsWindowGeometry(absolute_[index], layout_.window[index].size)) {
      record("root-window-geometry-refused", index, absolute_[index], layout_.window[index].size); return false;
    }
  }
  if (!loadPolicies()) return false;
  online_ = true;
  return prepareWindows();
}

bool DaliUfsRoot::prepareWindows() {
  for (uint8_t index = 0; index < windowCount_; ++index) {
    auto child = new DaliUfsRootBlock;
    if (!child) { discardUnstarted(); return false; }
    if (!child->initFor(this, index, absolute_[index], layout_.window[index], verified_[index])) {
      child->release(); discardUnstarted(); return false;
    }
    windows_[index] = child;
    if (!child->attach(this)) { discardUnstarted(); return false; }
    attached_[index] = true;
  }
  gRootDiagnostic.prepared = windowCount_;
  return true;
}

void DaliUfsRoot::discardUnstarted() {
  for (uint8_t index = 0; index < windowCount_; ++index) {
    auto child = windows_[index];
    if (!child || started_[index]) continue;
    if (attached_[index]) child->detach(this);
    attached_[index] = false; windows_[index] = nullptr; child->release();
  }
}

static bool mediaWithUuidExists(const char *uuid, DaliUfsRootBlock *window) {
  OSDictionary *matching = IOService::serviceMatching("IOMedia");
  OSDictionary *properties = OSDictionary::withCapacity(1);
  OSString *wanted = OSString::withCString(uuid);
  bool found = false;
  if (matching && properties && wanted && properties->setObject("UUID", wanted) &&
      matching->setObject("IOPropertyMatch", properties)) {
    OSIterator *iterator = IOService::getMatchingServices(matching);
    if (iterator) {
      for (unsigned matches = 0; matches < 64 && !found; ++matches) {
        IORegistryEntry *media = OSDynamicCast(IORegistryEntry, iterator->getNextObject());
        if (!media) break;
        IORegistryIterator *parents = IORegistryIterator::iterateOver(
            media, gIOServicePlane, kIORegistryIterateRecursively | kIORegistryIterateParents);
        if (parents) { found = ufsMediaHasAncestor(*parents, window); parents->release(); }
      }
      iterator->release();
    }
  }
  OSSafeReleaseNULL(wanted); OSSafeReleaseNULL(properties); OSSafeReleaseNULL(matching);
  return found;
}

bool DaliUfsRoot::publishWindows() {
  for (unsigned position = 0; position < plan_.count; ++position) {
    unsigned index = plan_.order[position];
    auto child = windows_[index];
    if (!child || !child->start(this)) {
      record("root-window-start-failed", index, publishedCount_);
      discardUnstarted(); return false;
    }
    started_[index] = true; published_ = true; ++publishedCount_;
    char uuid[37]; if (!child->signedVolumeUuid(uuid)) { record("root-window-uuid-missing", index); return false; }
    bool ready = false;
    for (unsigned tick = 0; tick <= 300; ++tick) {
      if (mediaWithUuidExists(uuid, child)) { ready = true; break; }
      if (tick == 300) break;
      IOSleep(100);
    }
    if (!ready) { record("root-window-media-timeout", index, publishedCount_); return false; }
    record("root-window-media-ready", index, absolute_[index], layout_.window[index].size);
    gRootDiagnostic.published = publishedCount_;
  }
  if (publishedCount_ != windowCount_ || osWindow_ >= windowCount_) return false;
  __atomic_store_n(&rootAuthorized_, true, __ATOMIC_RELEASE);
  record("root-authorized", osWindow_, verified_[osWindow_].block_count);
  return true;
}

struct DaliRootReadOps {
  DaliUfsRoot &owner;
  DaliUfsRootBlock &block;
  IOMemoryDescriptor &output;
  void lock() { IOLockLock(owner.gate_); }
  void unlock() { IOLockUnlock(owner.gate_); }
  IOReturn read(uint64_t lba, uint32_t blocks, void *buffer) {
    return owner.readPhysical(kDaliRootLun, lba, blocks, buffer);
  }
  IOReturn verify(uint64_t index, const void *data, size_t bytes) {
    if (!vr_verify_block || !reinterpret_cast<uintptr_t>(&vr_xnu_crypto)) return kIOReturnNotPrivileged;
    int result = vr_verify_block(&block.verified_, index, data, bytes, &vr_xnu_crypto);
    if (result != VR_OK) {
      ++owner.failures_; __atomic_store_n(&owner.rootAuthorized_, false, __ATOMIC_RELEASE);
    }
    return result == VR_OK ? kIOReturnSuccess : kIOReturnNotPrivileged;
  }
  uint64_t copy(uint64_t offset, const void *data, uint64_t bytes) {
    return output.writeBytes(offset, data, bytes);
  }
};

IOReturn DaliUfsRoot::readWindow(DaliUfsRootBlock *block, IOMemoryDescriptor *buffer,
                                  uint64_t sector, uint64_t count, uint64_t &actual) {
  actual = 0;
  if (!block || !buffer || !online() || buffer->getDirection() != kIODirectionIn) return kIOReturnNotReady;
  DaliRootReadOps ops{*this, *block, *buffer};
  auto result = ufsReadWindow(ops, block->absolute_, block->bytes_, sector, count, bounce_);
  actual = result.done;
  return result.status;
}

void DaliUfsRoot::worker(void *p, wait_result_t) {
  auto self = static_cast<DaliUfsRoot *>(p);
  for (unsigned n = 0; n < 500 && !dali_ufs_platform_ready(self->token_); ++n) IOSleep(10);
  IOLockLock(self->gate_); bool initialized = !self->closed_ && self->initialize(); IOLockUnlock(self->gate_);
  if (!initialized) {
    self->record("root-initialize-failed", self->layout_.manifestStatus, self->layout_.windows);
    self->quiesce(); self->discardUnstarted(); self->release(); thread_terminate(current_thread());
    return;
  }
  bool published = self->publishWindows();
  if (!published) {
    self->record("root-publication-incomplete", self->publishedCount_, self->windowCount_);
    // Started children may already serve root discovery; keep their owner and DMA alive.
    if (!self->published_) { self->quiesce(); self->discardUnstarted(); self->release(); }
    thread_terminate(current_thread());
    return;
  }
  self->record("root-service-ready", self->publishedCount_, self->reads_);
  thread_terminate(current_thread());
}

IOReturn DaliUfsRoot::readPhysical(uint8_t lun, uint64_t lba, uint32_t blocks, void *out) {
  if (!gate_ || !out) return kIOReturnBadArgument;
  const auto *geometry = transport_.geometry(lun);
  if (lun != rootLun_ || !blocks || blocks > unixnu_ufs::DataMax / 4096 || !geometry || !geometry->valid ||
      lba >= geometry->blocks || blocks > geometry->blocks - lba) return kIOReturnBadArgument;
  Status status = transport_.read(lun, lba, blocks, out);
  uint32_t bytes = blocks * 4096;
  if (status != Status::Success || transport_.completion().bytes != bytes) {
    ++failures_; return status == Status::Success ? kIOReturnIOError : error(status);
  }
  ++reads_; return kIOReturnSuccess;
}

static bool queryDaliRootVolume(dev_t device) {
  OSDictionary *matching = IOService::serviceMatching("IOMedia");
  OSDictionary *properties = OSDictionary::withCapacity(2);
  OSNumber *majorNumber = OSNumber::withNumber(static_cast<unsigned>(major(static_cast<uint32_t>(device))), 32);
  OSNumber *minorNumber = OSNumber::withNumber(static_cast<unsigned>(minor(static_cast<uint32_t>(device))), 32);
  if (!matching || !properties || !majorNumber || !minorNumber) {
    OSSafeReleaseNULL(minorNumber); OSSafeReleaseNULL(majorNumber);
    OSSafeReleaseNULL(properties); OSSafeReleaseNULL(matching); return false;
  }
  bool ready = properties->setObject(kIOBSDMajorKey, majorNumber) &&
      properties->setObject(kIOBSDMinorKey, minorNumber) && matching->setObject("IOPropertyMatch", properties);
  majorNumber->release(); minorNumber->release(); properties->release();
  OSIterator *iterator = ready ? IOService::getMatchingServices(matching) : nullptr;
  matching->release();
  if (!iterator) return false;
  bool authorized = false; unsigned matches = 0;
  while (OSObject *object = iterator->getNextObject()) {
    auto media = OSDynamicCast(IOService, object);
    if (!media || ++matches != 1) { authorized = false; break; }
    auto uuid = OSDynamicCast(OSString, media->getProperty("UUID"));
    if (!uuid || uuid->getLength() != 36) break;
    for (IOService *provider = media; provider; provider = provider->getProvider()) {
      auto block = OSDynamicCast(DaliUfsRootBlock, provider);
      if (block) { authorized = block->authenticatesVolume(uuid->getCStringNoCopy()); break; }
    }
  }
  iterator->release(); return authorized && matches == 1;
}

extern "C" int native_verified_block_root_authorized(dev_t device) {
  return queryDaliRootVolume(device) ? 1 : 0;
}

bool DaliUfsRoot::quiesce() {
  if (!gate_) return true;
  IOLockLock(gate_); closed_ = true; online_ = false;
  if (published_) { IOLockUnlock(gate_); record("published-owner-retained"); return false; }
  transport_.close(); auto stopped = controller_.stop();
  bool safe = stopped == dali_ufs::Result::Stopped &&
      (!dma_ || ((!exposed_ || transport_.releaseAllowed()) && read(this, dali_ufs::Hci + 0x34) == 0));
  if (safe && dma_) {
    if (prepared_) { dma_->complete(kIODirectionInOut); prepared_ = false; }
    dma_->release(); dma_ = nullptr;
  }
  record("quiesce", static_cast<uint32_t>(stopped), safe, read(this, dali_ufs::Hci + 0x34));
  IOLockUnlock(gate_); return safe;
}

bool DaliUfsRoot::start(IOService *provider) {
  unsigned root = 0, early = 0, pid1 = 0, autoFastboot = 0, diagnostic = 0;
  char reset[16] = {};
  if (!PE_parse_boot_argn("dali-ipados-root", &root, sizeof root) || root != 1 ||
      !PE_parse_boot_argn("dali-earlyboot", &early, sizeof early) || early != 1 ||
      !PE_parse_boot_argn("dali-pid1", &pid1, sizeof pid1) || pid1 != 1 ||
      (PE_parse_boot_argn("dali-ufs-readonly", &diagnostic, sizeof diagnostic) && diagnostic) ||
      !PE_parse_boot_argn("dali-run", token_, sizeof token_) || !dali_ufs::tokenValid(token_)) return false;
  PE_parse_boot_argn("dali-auto-fastboot", &autoFastboot, sizeof autoFastboot);
  if (autoFastboot || PE_parse_boot_argn("dali-reset-method", reset, sizeof reset)) return false;
  auto node = IORegistryEntry::fromPath("/arm-io/dali-ufs", gIODTPlane);
  auto resource = node ? OSDynamicCast(OSData, node->getProperty("resources-v1")) : nullptr;
  if (!resource || !dali_ufs::resourceValid(resource->getBytesNoCopy(), resource->getLength())) {
    if (node) node->release(); return false;
  }
  memcpy(resources_, resource->getBytesNoCopy(), sizeof resources_); node->release();
  if (!IOService::start(provider)) return false;
  gate_ = IOLockAlloc(); if (!gate_) return false;
  console_ = map(0xaec0f000, 0x200000); hci_ = map(dali_ufs::Hci, 0x2a00); ao_ = map(dali_ufs::Ao, 0x1000);
  if (!hci_ || !ao_) return false;
  record("start"); registerService(); retain(); thread_t thread;
  if (kernel_thread_start(worker, this, &thread) != KERN_SUCCESS) { release(); return false; }
  thread_deallocate(thread); return true;
}
void DaliUfsRoot::stop(IOService *provider) {
  if (published_) { record("stop-refused-published"); return; }
  quiesce(); IOService::stop(provider);
}
void DaliUfsRoot::free() {
  if (published_) { record("free-refused-published"); return; }
  if (gate_ && !quiesce()) { record("quarantine-retained"); return; }
  discardUnstarted();
  if (bounce_) { bzero(bounce_, unixnu_ufs::DataMax); IOFree(bounce_, unixnu_ufs::DataMax); bounce_ = nullptr; }
  if (hci_) hci_->release(); if (ao_) ao_->release(); if (console_) console_->release();
  if (gate_) IOLockFree(gate_); IOService::free();
}
OSDefineMetaClassAndStructors(DaliUfsRoot, IOService)
OSDefineMetaClassAndStructors(DaliUfsRootBlock, IOBlockStorageDevice)
