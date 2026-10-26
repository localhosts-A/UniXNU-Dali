// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace dali_ufs {
constexpr uint64_t Hci = 0x16810000, Ao = 0x168a0000;
// Emitted only after make-dali-adt validates the private FDT. Absolute physical
// resources, not arm-io bus-relative reg cells. Followed by the source FDT SHA256.
constexpr uint64_t ResourceWords[] = {
  0x31534655494c4144, 1, Hci, 0x2a00, Ao, 0x1000,
  0x16800000, 0x10000, 0x10000000, 0x800, 0, 0x1000000000,
  1, 7, 14, 7
};
constexpr size_t ResourceBytes = sizeof(ResourceWords) + 32;
// v2 is the legacy boot_b schema.  Expdb uses a separate v3 schema with an
// explicit target word appended; never reinterpret a v2 wire by changing LBAs.
constexpr size_t ScratchGrantWireBytes = 232;
constexpr size_t ExpdbGrantWireBytes = 236;
struct ScratchGrant;
enum class ScratchTarget : uint32_t { BootB = 0, Expdb = 1 };
enum class ScratchMarkerAction : uint8_t { Refuse, WriteMarker, ReadbackOnly };
enum class ScratchPayloadAction : uint8_t { Refuse, WritePayload, ReadbackOnly };
bool resourceValid(const void *, size_t);
bool tokenValid(const char *);
bool decodeScratchGrant(const void *, size_t, ScratchGrant &);
bool scratchAbsoluteLba(const ScratchGrant &, uint64_t relativeLba, uint32_t blocks, uint64_t &absoluteLba);
bool scratchSequenceLbas(const ScratchGrant &, uint64_t &markerRelativeLba, uint64_t &payloadRelativeLba);
bool makeScratchAttemptMarker(const char *token, uint64_t sequence, uint32_t blockBytes,
                              const uint8_t payloadDigest[32], uint8_t *bytes);
ScratchMarkerAction scratchMarkerAction(bool allZero, bool matchesExpected);
ScratchPayloadAction scratchPayloadAction(bool markerCreatedThisBoot, bool allZero, bool matchesExpected);
bool parsePartitionGuid(const char *, uint8_t output[16]);

struct Ops {
  void *context = nullptr;
  uint32_t (*read)(void *, uint64_t) = nullptr;
  bool (*write)(void *, uint64_t, uint32_t) = nullptr;
  void (*fence)(void *) = nullptr;
  void (*delay)(void *, uint32_t) = nullptr;
  uint64_t (*sip)(void *, uint32_t service, uint32_t selector, uint32_t a, uint32_t b) = nullptr;
};

// An opt-in, token-bound window for one synthetic scratch BlockIo. The grant is
// copied from scratch-grant-v2 (boot_b) or scratch-grant-v3 (expdb); missing
// means deny. bootStartLba/bootBlocks name the selected partition in both modes.
struct ScratchGrant {
  bool enabled = false;
  char token[33]{};
  // candidateSha256 names the exact source-XNU kernel payload; the final boot
  // image digest is sealed separately by the host lease to avoid self-hashing.
  uint8_t deviceBinding[32]{}, candidateSha256[32]{}, sourceSha256[32]{};
  ScratchTarget target = ScratchTarget::BootB;
  uint8_t partitionGuid[16]{};
  uint8_t lun = 0;
  uint32_t blockBytes = 0;
  // payloadEndBytes is the boot image extent only for boot_b, zero for expdb.
  uint64_t capacityBlocks = 0, bootStartLba = 0, bootBlocks = 0, payloadEndBytes = 0, sequence = 0;
  uint64_t scratchStartLba = 0, scratchBlocks = 0, tailGuardBlocks = 0;
};

struct ScratchObservation {
  bool identityVerified = false, gptVerified = false, writeProtected = true, scratchMediaReady = false;
  bool candidateVerified = false, sourceVerified = false;
  char token[33]{};
  uint8_t deviceBinding[32]{}, candidateSha256[32]{}, sourceSha256[32]{};
  ScratchTarget target = ScratchTarget::BootB;
  uint8_t lun = 0;
  uint8_t partitionGuid[16]{};
  uint32_t blockBytes = 0;
  uint64_t capacityBlocks = 0, bootStartLba = 0, bootBlocks = 0;
};

bool scratchGrantValid(const ScratchGrant &);
bool scratchWriteAllowed(const ScratchGrant &, const ScratchObservation &, uint64_t lba, uint32_t blocks);

// Compiled into the running kernel, independently of the ADT grant. No kernel
// hash here: including it would create a self-hashing build dependency.
struct SourceIdentity {
  char magic[16];
  char device[41], unixnu[41], uefi[41], xnu[41];
};
static_assert(sizeof(SourceIdentity) == 180, "source identity wire layout");
constexpr char KernelMeasurementProperty[] = "darwinboot-kernel-sha256";
using Hash = bool (*)(void *, const uint8_t *, size_t, uint8_t digest[32]);
size_t sourceBindingPreimage(const SourceIdentity &, const uint8_t kernel[32], uint8_t *, size_t capacity);
bool verifyScratchProvenance(const ScratchGrant &, const void *measurement, size_t bytes,
                             const SourceIdentity &, Hash, void *, ScratchObservation &);

enum class Result : uint32_t { Off, Rejected, Busy, Timeout, Hardware, Uic, Sip, Ready, Stopped };
enum class Stage : uint32_t { Validate, Power, ReferenceClock, HostReset, Hce, PreLink, LinkStartup, PostLink, DeviceReset };
struct Snapshot {
  uint32_t interrupt = 0, hcs = 0, hce = 0, arguments[3]{}, layers[5]{};
  bool captured = false;
};
struct Diagnostic {
  Result result = Result::Off, stop = Result::Off;
  Stage stage = Stage::Validate, faultStage = Stage::Validate;
  uint64_t address = 0, faultAddress = 0, sipStatus = 0, resetCleanup = 0, clockCleanup = 0;
  uint32_t value = 0, faultValue = 0, interrupt = 0, opcode = 0, uicResult = 0, localVersion = 0;
  uint32_t sipSelector = 0, sipA = 0, sipB = 0;
  uint32_t inherited[12]{};
  Snapshot failure{}, lineReset{};
  bool attempted = false, touched = false, failed = false, ready = false, stopped = false;
  bool linePending = false, lineAccepted = false, lineAcked = false;
};

// Single owner / caller serialization, same fixed MT6991_B0 recipe as the
// qualified firmware. No DMA or SCSI commands and no IOMMU/PMIC programming.
class Controller {
public:
  Result start(bool enabled, const void *resources, size_t bytes, const char *token, const Ops &);
  Result stop();
  const Diagnostic &diagnostic() const { return d_; }
  bool ready() const { return d_.ready && !d_.failed; }
private:
  Ops io_{}; Diagnostic d_{};
  bool fail(Result, bool snapshot = true);
  uint32_t read(uint64_t);
  bool write(uint64_t, uint32_t);
  bool wait(uint32_t offset, uint32_t mask, uint32_t value, uint32_t timeout);
  bool sip(uint32_t selector, uint32_t a, uint32_t b);
  bool rmw(uint32_t offset, uint32_t mask, uint32_t bits);
  bool reset(uint32_t offset, uint32_t bits);
  void capture(Snapshot &);
  bool stoppedQueues(bool clearBases);
  bool deviceReset();
  bool ack(uint32_t);
  bool uic(uint32_t opcode, uint32_t attribute, uint32_t &value);
  bool set(uint32_t attribute, uint32_t value) { return uic(2, attribute, value); }
  bool dmeRmw(uint32_t attribute, uint32_t mask, uint32_t bits);
};
} // namespace dali_ufs
