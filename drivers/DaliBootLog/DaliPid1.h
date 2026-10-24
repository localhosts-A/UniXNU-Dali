// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once
#include <stdint.h>

// Fixed little-endian ABI shared with the owned ARM64 EL0 test program.
struct DaliPid1Message {
  uint32_t magic, version, sequence, pid;
  uint32_t pageSize, syscallErrors, readonlyError, reserved;
};
static_assert(sizeof (DaliPid1Message) == 32, "PID1 message ABI");

struct DaliPid1State {
  uint64_t firstNs = 0, lastNs = 0;
  uint32_t nextSequence = 0;
  bool complete = false;
};
enum class DaliPid1Result { Reject, Heartbeat, Complete };

static inline DaliPid1Result daliPid1Advance(DaliPid1State &state,
    const DaliPid1Message &message, int actualPid, bool readonlyRoot, uint64_t nowNs)
{
  if (state.complete || actualPid != 1 || !readonlyRoot ||
      message.magic != 0x444c5031U || message.version != 1 || message.pid != 1 ||
      message.pageSize != 4096 || message.syscallErrors != 0 ||
      message.readonlyError != 30 || message.reserved != 0 ||
      message.sequence != state.nextSequence || message.sequence > 30) {
    return DaliPid1Result::Reject;
  }
  if (message.sequence != 0 &&
      (nowNs < state.lastNs || nowNs - state.lastNs < 900000000ULL ||
       nowNs < state.firstNs ||
       (message.sequence == 30 && nowNs - state.firstNs < 30000000000ULL))) {
    return DaliPid1Result::Reject;
  }
  if (message.sequence == 0) { state.firstNs = nowNs; }
  state.lastNs = nowNs;
  state.nextSequence++;
  state.complete = message.sequence == 30;
  return state.complete ? DaliPid1Result::Complete : DaliPid1Result::Heartbeat;
}
