// SPDX-License-Identifier: BSD-2-Clause-Patent
#ifndef DALI_MANUAL_RECOVERY_H
#define DALI_MANUAL_RECOVERY_H
#include <stdint.h>

// Samples are 10 ms apart. Require a fresh release before a deliberate hold;
// a key held during boot must never start a recovery timer.
struct DaliManualKey {
  unsigned released = 0, held = 0;
  bool ready = false;
};
static inline bool daliManualKeySample(DaliManualKey &key, bool pressed)
{
  if (!key.ready) {
    key.released = pressed ? 0 : key.released + 1;
    key.ready = key.released >= 20;
    return false;
  }
  key.held = pressed ? key.held + 1 : 0;
  return key.held == 70;
}

struct DaliManualWdtOps {
  void *context;
  uint32_t (*read)(void *, uint32_t);
  void (*write)(void *, uint32_t, uint32_t);
  void (*fence)(void *);
};
static inline bool daliArmManualWatchdog(const DaliManualWdtOps &ops)
{
  const uint32_t before = ops.read(ops.context, 0);
  if (before == UINT32_MAX || (before & 1)) { return false; }
  // Only a deliberate key hold reaches this function. Match the already
  // qualified firmware watchdog encoding; do not write a boot-mode marker.
  ops.write(ops.context, 4, 0x1008); // 2 seconds, count key
  ops.write(ops.context, 8, 0x1971);
  ops.fence(ops.context);
  if ((ops.read(ops.context, 4) & 0xffe0) != 0x1000) { return false; }
  const uint32_t requested = (before & ~(8U | 64U)) | 1U;
  ops.write(ops.context, 0, requested | 0x22000000);
  ops.fence(ops.context);
  return ops.read(ops.context, 0) == requested;
}
#endif
