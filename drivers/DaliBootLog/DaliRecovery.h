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
  void (*write)(void *, uint32_t, uint32_t);
  void (*fence)(void *);
  bool (*record)(void *, const char *, uint32_t, uint32_t);
};

// The UEFI one-shot watchdog must still be armed. Do not reload it, change
// its length, or write any boot-mode/scratch register here. Follow the same
// MODE/SWRST sequence as the firmware's Mt6991WarmReset.
static inline bool daliIssueSoftwareReset(const DaliRecoveryOps &ops)
{
  const uint32_t marker = ops.read(ops.context, 0x24);
  const uint32_t mode = ops.read(ops.context, 0);
  if (marker == UINT32_MAX || (marker & 0xF) != 3 || mode == UINT32_MAX || !(mode & 1)) {
    ops.record(ops.context, "state-refused", marker, mode);
    return false;
  }
  ops.write(ops.context, 0, (mode & ~8U) | 0x22000000);
  ops.fence(ops.context);
  const uint32_t readback = ops.read(ops.context, 0);
  if (readback == UINT32_MAX || (readback & 0xFFFFFF) != ((mode & ~8U) & 0xFFFFFF) ||
      ops.read(ops.context, 0x24) != marker) {
    ops.record(ops.context, "mode-refused", marker, readback);
    return false;
  }
  if (!ops.record(ops.context, "issue", marker, readback)) { return false; }
  ops.fence(ops.context); // Commit the persistent record before resetting.
  ops.write(ops.context, 0x14, 0x1209);
  ops.fence(ops.context);
  ops.record(ops.context, "returned", marker, readback);
  return true; // A real successful reset does not return to this caller.
}
#endif
