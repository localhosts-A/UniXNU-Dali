// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once
#include <stdint.h>
#include <stddef.h>
namespace dali_ufs {
struct CacheSpan { uintptr_t begin = 0, end = 0; };
// Round only inside a dedicated page allocation. Polling its 32-byte TRD may
// invalidate a whole cache line, but never an unrelated allocation's bytes.
inline bool cacheSpan(uintptr_t base, size_t capacity, uintptr_t address, size_t bytes, size_t line, CacheSpan &out) {
  out = {};
  if (!base || (base & 4095) || !capacity || (capacity & 4095) || capacity > UINTPTR_MAX - base ||
      line < 16 || line > 4096 || (line & (line - 1)) || !bytes || address < base ||
      address - base >= capacity || bytes > capacity - (address - base)) return false;
  uintptr_t begin = address & ~(uintptr_t(line) - 1);
  uintptr_t end = address + bytes;
  if (end & (line - 1)) {
    if (line - (end & (line - 1)) > UINTPTR_MAX - end) return false;
    end += line - (end & (line - 1));
  }
  if (begin < base || end > base + capacity) return false;
  out = {begin, end}; return true;
}
inline bool dmaSpan(uintptr_t cpu, uint64_t physical, size_t capacity, uint64_t contiguous) {
  return cpu && !(cpu & 4095) && physical && !(physical & 4095) && capacity && !(capacity & 4095) &&
         capacity <= UINTPTR_MAX - cpu && contiguous >= capacity && physical <= UINT32_MAX &&
         capacity - 1 <= UINT32_MAX - physical;
}
} // namespace dali_ufs
