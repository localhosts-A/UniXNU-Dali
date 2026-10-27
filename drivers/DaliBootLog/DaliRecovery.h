// SPDX-License-Identifier: BSD-2-Clause-Patent
#ifndef DALI_RECOVERY_H
#define DALI_RECOVERY_H
#include <stdint.h>

struct DaliRecoveryContract {
  uint64_t base, size;
  uint32_t offset, mask, bootloader;
};

static inline bool daliRecoveryContractValid(const DaliRecoveryContract &c)
{
  return c.base == 0x1C010000 && c.size == 0x1000 &&
         c.offset == 0x24 && c.mask == 0xF && c.bootloader == 3;
}

static inline bool daliPsciFeatureSupported(uint32_t result)
{
  // PSCI_FEATURES returns nonnegative flags, or a negative PSCI error.
  return (result & 0x80000000U) == 0;
}

struct DaliRecoveryOps {
  void *context;
  uint32_t (*read)(void *, uint32_t);
  void (*fence)(void *);
  bool (*record)(void *, const char *, uint32_t, uint32_t);
  void (*reset)(void *);
};

enum class DaliResetMethod : uint32_t { Off, Cold, Warm, Aee };

static inline const char *daliResetMethodArgument(DaliResetMethod method) {
  switch (method) {
    case DaliResetMethod::Off: return "off";
    case DaliResetMethod::Cold: return "psci-cold";
    case DaliResetMethod::Warm: return "psci-warm";
    case DaliResetMethod::Aee: return "aee";
  }
  return "invalid";
}
static inline const char *daliResetLogMethod(DaliResetMethod method) {
  switch (method) {
    case DaliResetMethod::Off: return "off";
    case DaliResetMethod::Cold: return "psci-system-reset";
    case DaliResetMethod::Warm: return "psci-system-reset2";
    case DaliResetMethod::Aee: return "psci-aee-reset2";
  }
  return "invalid";
}
static inline bool daliParseResetMethod(const char *name, DaliResetMethod &method) {
  for (uint32_t i = 0; i < 4; ++i) {
    const auto candidate = static_cast<DaliResetMethod>(i);
    const char *a = name, *b = daliResetMethodArgument(candidate);
    while (*a && *a == *b) { ++a; ++b; }
    if (*a == *b) { method = candidate; return true; }
  }
  return false;
}
static inline bool daliQualifiedDelayComplete(uint32_t frequency, uint64_t elapsed) {
  return frequency == 13000000 && elapsed >= 5ULL * frequency;
}

// Legacy images retain their enabled watchdog contract. Qualification images
// require it disabled so it cannot impersonate a successful PSCI request.
static __attribute__((noinline)) bool daliIssueSystemReset(const DaliRecoveryOps &ops, bool qualification = false)
{
  const uint32_t marker = ops.read(ops.context, 0x24);
  const uint32_t mode = ops.read(ops.context, 0);
  if (marker == UINT32_MAX || (marker & 0xF) != 3 || mode == UINT32_MAX ||
      (qualification ? (mode & 1) != 0 : (mode & 9) != 1)) {
    ops.record(ops.context, "state-refused", marker, mode);
    return false;
  }
  if (!ops.record(ops.context, "issue", marker, mode)) { return false; }
  ops.fence(ops.context); // Commit the persistent record before resetting.
  ops.reset(ops.context);
  ops.fence(ops.context);
  return true; // A returned PSCI call must fail the recovery evidence gate.
}
#endif
