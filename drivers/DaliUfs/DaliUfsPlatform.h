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
bool resourceValid(const void *, size_t);
bool tokenValid(const char *);

struct Ops {
  void *context = nullptr;
  uint32_t (*read)(void *, uint64_t) = nullptr;
  bool (*write)(void *, uint64_t, uint32_t) = nullptr;
  void (*fence)(void *) = nullptr;
  void (*delay)(void *, uint32_t) = nullptr;
  uint64_t (*sip)(void *, uint32_t service, uint32_t selector, uint32_t a, uint32_t b) = nullptr;
};
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
