// SPDX-License-Identifier: BSD-2-Clause-Patent
#define OPENSSL_SUPPRESS_DEPRECATED
#include <openssl/sha.h>
#include <cassert>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <RootDiagnostics.h>
using IOReturn = uint32_t;
constexpr IOReturn kIOReturnSuccess = 0;
constexpr IOReturn kIOReturnNotReady = 1;
constexpr size_t kDataMax = 131072;
static UfsRootDiagnostics gRootDiagnostic;
static uint8_t header[4096], entries[4096], manifest[4096];
static uint64_t containerFirst = 8;
static uint8_t lastLun = 0xff;
static bool failRead = false;
static IOReturn hostRead4k(uint8_t lun, uint64_t lba, uint32_t blocks, void *out);
#include "../drivers/DaliUfs/DaliUfsRootLayout.h"

static IOReturn hostRead4k(uint8_t lun, uint64_t lba, uint32_t blocks, void *out) {
  lastLun = lun;
  if (failRead || lun != 2) return kIOReturnNotReady;
  if (lba == 1 && blocks == 1) memcpy(out, header, 4096);
  else if (lba == 2 && blocks == 1) memcpy(out, entries, 4096);
  else if (lba == containerFirst && blocks == 1) memcpy(out, manifest, 4096);
  else assert(false);
  return kIOReturnSuccess;
}
static void put32(uint8_t *p, uint32_t v) {
  for (unsigned i = 0; i < 4; ++i) p[i] = uint8_t(v >> (i * 8));
}
static void put64(uint8_t *p, uint64_t v) {
  for (unsigned i = 0; i < 8; ++i) p[i] = uint8_t(v >> (i * 8));
}
static void checksum() {
  SHA256(manifest, 4064, manifest + 4064);
  put32(header + 88, rootCrc32(entries, 32 * 128));
  put32(header + 16, rootCrc32(header, 92, true));
}
static void partitionName(const char *name) {
  memset(entries + 56, 0, 72);
  for (unsigned i = 0; name[i] && i < 36; ++i)
    entries[56 + 2 * i] = static_cast<uint8_t>(name[i]);
}
static void fixture() {
  memset(header, 0, sizeof(header)); memset(entries, 0, sizeof(entries));
  memset(manifest, 0, sizeof(manifest)); failRead = false; containerFirst = 8;
  memcpy(header, "EFI PART", 8); put32(header + 8, 0x10000); put32(header + 12, 92);
  put64(header + 24, 1); put64(header + 32, 511); put64(header + 40, 6); put64(header + 48, 506);
  put64(header + 72, 2); put32(header + 80, 32); put32(header + 84, 128);
  entries[0] = 1; entries[16] = 2; put64(entries + 32, containerFirst); put64(entries + 40, 506);
  partitionName("unixnu_data");
  memcpy(manifest, "SHNGROOT", 8); put32(manifest + 8, 1); put32(manifest + 12, 2);
  put64(manifest + 16, 65536);
  for (unsigned i = 0; i < 2; ++i) {
    uint8_t *window = manifest + 24 + i * 128;
    strcpy(reinterpret_cast<char *>(window), i ? "SystemOS" : "OS");
    put64(window + 32, i ? 32768 : 4096); put64(window + 40, 8192);
    put32(window + 48, 512); put32(window + 52, 1);
  }
  checksum();
}
int main() {
  fixture();
  RootLayout layout{}; const char *why = nullptr;
  assert(findRootLayout(&layout, &why, 2, "unixnu_data"));
  assert(lastLun == 2 && layout.lun == 2 && layout.manifestStatus == 0);
  assert(layout.userdataFirst == 8 && layout.userdataLast == 506);
  assert(layout.windows == 2 && !strcmp(layout.window[0].name, "OS") &&
         !strcmp(layout.window[1].name, "SystemOS"));

  fixture();
  assert(!findRootLayout(&layout, &why, 1, "unixnu_data"));
  fixture();
  assert(!findRootLayout(&layout, &why, 2, "userdata"));
  fixture();
  put64(manifest + 24 + 32, 32768); checksum();
  assert(findRootLayout(&layout, &why, 2, "unixnu_data") && layout.manifestStatus == 6);
  fixture();
  put32(manifest + 24 + 52, 0); checksum();
  assert(findRootLayout(&layout, &why, 2, "unixnu_data") && layout.manifestStatus == 7);
  fixture();
  manifest[4064] ^= 1;
  assert(findRootLayout(&layout, &why, 2, "unixnu_data") && layout.manifestStatus == 3);
  fixture(); failRead = true;
  assert(!findRootLayout(&layout, &why, 2, "unixnu_data"));
  puts("PASS: Dali LUN 2 unixnu_data GPT and SHNGROOT parser, bounds, attributes, overlap and hash refusal");
}
