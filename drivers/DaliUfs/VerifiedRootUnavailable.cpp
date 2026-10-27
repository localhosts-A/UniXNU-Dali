// SPDX-License-Identifier: BSD-2-Clause-Patent
// Weak, fail-closed link inputs for compile-only Dali root builds. A production
// policy archive must provide strong replacements for every symbol below.
#include <string.h>
#include "verified_root.h"
#include "VerifiedBlockPolicy.h"

extern "C" {
extern __attribute__((weak)) const struct vr_crypto vr_xnu_crypto = {nullptr, nullptr};
extern __attribute__((weak)) const uint8_t verified_root_key[VR_KEY_BYTES] = {};
extern __attribute__((weak)) const NativeVerifiedBlockPolicy verified_root_policies[1] = {{nullptr, 0}};
extern __attribute__((weak)) const uint64_t verified_root_policy_count = 0;

__attribute__((weak)) int vr_open(const uint8_t *, size_t, const uint8_t[VR_KEY_BYTES], uint64_t,
                                  const struct vr_crypto *, struct vr_view *out) {
  if (out) memset(out, 0, sizeof(*out));
  return VR_KEY;
}

__attribute__((weak)) int vr_verify_block(const struct vr_view *, uint64_t, const void *, size_t,
                                          const struct vr_crypto *) {
  return VR_BLOCK;
}
}
