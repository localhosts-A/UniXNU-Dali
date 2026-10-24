// SPDX-License-Identifier: BSD-2-Clause-Patent
#ifndef DALI_PSCI_H
#define DALI_PSCI_H
#include <stdint.h>

// Separate non-inlined boundaries let CI inspect the actual selected SMC ABIs.
#define DALI_SMC \
  __asm__ volatile ("smc #0" : "+r" (x0), "+r" (x1), "+r" (x2), "+r" (x3), \
                    "+r" (x4), "+r" (x5), "+r" (x6), "+r" (x7) : \
                    : "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17", "cc", "memory")
#define DALI_ZERO_REST \
  register uint64_t x2 __asm__ ("x2") = 0; \
  register uint64_t x3 __asm__ ("x3") = 0; \
  register uint64_t x4 __asm__ ("x4") = 0; \
  register uint64_t x5 __asm__ ("x5") = 0; \
  register uint64_t x6 __asm__ ("x6") = 0; \
  register uint64_t x7 __asm__ ("x7") = 0

static __attribute__((noinline)) uint32_t recoveryPsciFeatures(void) {
  register uint64_t x0 __asm__ ("x0") = 0x8400000A;
  register uint64_t x1 __asm__ ("x1") = 0xC4000012;
  DALI_ZERO_REST;
  DALI_SMC;
  return static_cast<uint32_t>(x0);
}

static __attribute__((noinline)) uint32_t recoveryPsciColdReset(void) {
  register uint64_t x0 __asm__ ("x0") = 0x84000009;
  register uint64_t x1 __asm__ ("x1") = 0;
  DALI_ZERO_REST;
  uint64_t daif;
  __asm__ volatile ("mrs %0, daif\n\tmsr daifset, #3\n\tdsb sy" : "=r" (daif) :: "memory");
  DALI_SMC;
  __asm__ volatile ("msr daif, %0" :: "r" (daif) : "memory");
  return static_cast<uint32_t>(x0);
}

static __attribute__((noinline)) uint32_t recoveryPsciReset2(uint32_t type) {
  register uint64_t x0 __asm__ ("x0") = 0xC4000012;
  // Match the stock Dali module's sign extension of the vendor type.
  register uint64_t x1 __asm__ ("x1") = static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(type)));
  DALI_ZERO_REST;
  uint64_t daif;
  __asm__ volatile ("mrs %0, daif\n\tmsr daifset, #3\n\tdsb sy" : "=r" (daif) :: "memory");
  DALI_SMC;
  __asm__ volatile ("msr daif, %0" :: "r" (daif) : "memory");
  return static_cast<uint32_t>(x0);
}
#undef DALI_ZERO_REST
#undef DALI_SMC
#endif
