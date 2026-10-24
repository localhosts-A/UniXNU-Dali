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

struct DaliRecoveryOps {
  void *context;
  uint32_t (*read)(void *, uint32_t);
  void (*fence)(void *);
  bool (*record)(void *, const char *, uint32_t, uint32_t);
  void (*reset)(void *);
};

// Keep the UEFI one-shot watchdog and bootloader marker unchanged. Linux's
// PSCI restart handler has priority 129, ahead of mtk_wdt's 128. The direct
// SWRST trial returned on this device, so request the standard secure reset.
static __attribute__((noinline)) bool daliIssueSystemReset(const DaliRecoveryOps &ops)
{
  const uint32_t marker = ops.read(ops.context, 0x24);
  const uint32_t mode = ops.read(ops.context, 0);
  if (marker == UINT32_MAX || (marker & 0xF) != 3 || mode == UINT32_MAX || (mode & 9) != 1) {
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
