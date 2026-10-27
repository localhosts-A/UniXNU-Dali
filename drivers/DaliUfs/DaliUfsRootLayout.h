// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once
// Dali's SHNGROOT container is on UFS LUN 2, not the generic LUN 0/userdata
// location. Keep this parser local to the board driver so its build does not
// depend on a mutable NativeUfs submodule revision.
struct RootWindow {
  char name[32];
  uint64_t offset;
  uint64_t size;
  uint8_t sha256[32];
};

struct RootLayout {
  uint8_t lun;
  uint64_t userdataFirst, userdataLast;
  uint64_t imageBytes;
  uint32_t windows;
  RootWindow window[4];
  uint32_t manifestStatus;
  uint32_t magic0, magic1;
};

static uint8_t gDaliRootScratch[kDataMax];

static uint32_t le32(const uint8_t *at) {
  return uint32_t(at[0]) | (uint32_t(at[1]) << 8) |
         (uint32_t(at[2]) << 16) | (uint32_t(at[3]) << 24);
}

static uint64_t le64(const uint8_t *at) {
  return uint64_t(le32(at)) | (uint64_t(le32(at + 4)) << 32);
}

static uint32_t rootCrc32(const uint8_t *data, size_t bytes, bool header = false) {
  uint32_t crc = 0xffffffffU;
  for (size_t i = 0; i < bytes; ++i) {
    crc ^= header && i >= 16 && i < 20 ? 0 : data[i];
    for (unsigned bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1)));
  }
  return ~crc;
}

static bool rootEntryUsed(const uint8_t *entry) {
  uint8_t used = 0;
  for (unsigned i = 0; i < 16; ++i) used |= entry[i];
  return used != 0;
}

static bool findRootLayout(RootLayout *layout, const char **why, uint8_t lun = 0,
                           const char *partitionName = "userdata") {
  gRootDiagnostic.stage = "GPT header";
  if (!layout || !why || !partitionName) {
    if (why) *why = "root layout arguments";
    return false;
  }
  bzero(layout, sizeof(*layout));
  size_t wantedLength = 0;
  while (wantedLength <= 36 && partitionName[wantedLength]) ++wantedLength;
  if (!wantedLength || wantedLength > 36) { *why = "partition name length"; return false; }
  for (size_t i = 0; i < wantedLength; ++i) {
    char c = partitionName[i];
    if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
          (c >= '0' && c <= '9') || c == '_' || c == '-')) {
      *why = "partition name encoding"; return false;
    }
  }
  layout->lun = lun;
  if (hostRead4k(lun, 1, 1, gDaliRootScratch) != kIOReturnSuccess) {
    *why = "GPT header read"; return false;
  }
  if (le32(gDaliRootScratch) != 0x20494645U ||
      le32(gDaliRootScratch + 4) != 0x54524150U ||
      le32(gDaliRootScratch + 84) != 128) {
    *why = "GPT header"; return false;
  }
  uint32_t headerBytes = le32(gDaliRootScratch + 12);
  if (le32(gDaliRootScratch + 8) != 0x00010000 || headerBytes < 92 ||
      headerBytes > 4096 || le32(gDaliRootScratch + 20) ||
      le64(gDaliRootScratch + 24) != 1 ||
      rootCrc32(gDaliRootScratch, headerBytes, true) != le32(gDaliRootScratch + 16)) {
    *why = "GPT header CRC/revision"; return false;
  }
  uint64_t backup = le64(gDaliRootScratch + 32);
  uint64_t first = le64(gDaliRootScratch + 40);
  uint64_t last = le64(gDaliRootScratch + 48);
  uint64_t entriesLba = le64(gDaliRootScratch + 72);
  uint32_t count = le32(gDaliRootScratch + 80);
  uint32_t entriesCrc = le32(gDaliRootScratch + 88);
  if (entriesLba < 2 || entriesLba > 64 || !count || count > 128) {
    *why = "GPT geometry"; return false;
  }
  uint32_t blocks = (count * 128 + 4095) / 4096;
  if (first < entriesLba + blocks || last < first || backup <= last ||
      backup > UINT32_MAX || backup - last <= blocks) {
    *why = "GPT usable range"; return false;
  }
  gRootDiagnostic.stage = "GPT entries";
  if (hostRead4k(lun, entriesLba, blocks, gDaliRootScratch) != kIOReturnSuccess) {
    *why = "GPT entries read"; return false;
  }
  if (rootCrc32(gDaliRootScratch, count * 128) != entriesCrc) {
    *why = "GPT entries CRC"; return false;
  }
  bool found = false;
  for (uint32_t index = 0; index < count; ++index) {
    const uint8_t *entry = gDaliRootScratch + index * 128;
    if (!rootEntryUsed(entry)) continue;
    uint64_t entryFirst = le64(entry + 32), entryLast = le64(entry + 40);
    if (entryFirst < first || entryLast < entryFirst || entryLast > last) {
      *why = "GPT partition bounds"; return false;
    }
    for (uint32_t prior = 0; prior < index; ++prior) {
      const uint8_t *other = gDaliRootScratch + prior * 128;
      if (rootEntryUsed(other) && entryFirst <= le64(other + 40) &&
          le64(other + 32) <= entryLast) {
        *why = "GPT partition overlap"; return false;
      }
    }
    bool match = true;
    for (unsigned c = 0; c < 36; ++c) {
      uint8_t low = entry[56 + 2 * c], high = entry[57 + 2 * c];
      if (c < wantedLength ?
          (low != static_cast<uint8_t>(partitionName[c]) || high != 0) :
          (low != 0 || high != 0)) {
        match = false; break;
      }
    }
    if (match) {
      if (found) { *why = "duplicate root container partition"; return false; }
      layout->userdataFirst = le64(entry + 32);
      layout->userdataLast = le64(entry + 40);
      found = true;
    }
  }
  if (!found || layout->userdataLast < layout->userdataFirst) {
    *why = "no root container partition"; return false;
  }
  gRootDiagnostic.first = layout->userdataFirst;
  gRootDiagnostic.last = layout->userdataLast;
  gRootDiagnostic.stage = "root manifest";
  if (hostRead4k(lun, layout->userdataFirst, 1, gDaliRootScratch) != kIOReturnSuccess) {
    *why = "container first block read"; return false;
  }
  layout->magic0 = le32(gDaliRootScratch);
  layout->magic1 = le32(gDaliRootScratch + 4);
  layout->manifestStatus = 1;
  if (layout->magic0 != 0x474e4853U || layout->magic1 != 0x544f4f52U)
    return true;
  uint32_t version = le32(gDaliRootScratch + 8);
  uint32_t windows = le32(gDaliRootScratch + 12);
  uint64_t imageBytes = le64(gDaliRootScratch + 16);
  layout->manifestStatus = 2;
  if (version != 1 || !windows || windows > 4) return true;
  uint8_t digest[32];
  SHA256_CTX hash;
  SHA256_Init(&hash); SHA256_Update(&hash, gDaliRootScratch, 4064); SHA256_Final(digest, &hash);
  layout->manifestStatus = 3;
  if (memcmp(digest, gDaliRootScratch + 4064, sizeof digest)) return true;
  uint64_t partitionBytes = (layout->userdataLast - layout->userdataFirst + 1) * 4096ULL;
  layout->manifestStatus = 4;
  bool bounds = imageBytes >= 4096 && !(imageBytes & 4095) && imageBytes <= partitionBytes;
  for (uint32_t index = 0; index < windows && bounds; ++index) {
    const uint8_t *entry = gDaliRootScratch + 24 + 128 * index;
    uint64_t offset = le64(entry + 32), size = le64(entry + 40);
    if ((offset & 4095) || offset < 4096 || (size & 511) || !size ||
        offset >= imageBytes || size > imageBytes - offset ||
        ((size + 4095) & ~4095ULL) > imageBytes - offset) {
      bounds = false; break;
    }
    if (le32(entry + 48) != 512 || le32(entry + 52) != 1) {
      layout->manifestStatus = 7; bounds = false; break;
    }
    unsigned length = 0;
    while (length < 32 && entry[length] >= 0x20 && entry[length] <= 0x7e) ++length;
    if (!length || length == 32 || entry[length] != 0) {
      layout->manifestStatus = 5; bounds = false; break;
    }
    for (uint32_t prior = 0; prior < index; ++prior) {
      const RootWindow &other = layout->window[prior];
      if (!strcmp(reinterpret_cast<const char *>(entry), other.name)) {
        layout->manifestStatus = 5; bounds = false; break;
      }
      if (offset < other.offset + ((other.size + 4095) & ~4095ULL) &&
          other.offset < offset + ((size + 4095) & ~4095ULL)) {
        layout->manifestStatus = 6; bounds = false; break;
      }
    }
    if (!bounds) break;
    memcpy(layout->window[index].name, entry, 32);
    layout->window[index].name[31] = 0;
    layout->window[index].offset = offset;
    layout->window[index].size = size;
    memcpy(layout->window[index].sha256, entry + 56, 32);
  }
  if (bounds) {
    layout->windows = windows;
    layout->imageBytes = imageBytes;
    layout->manifestStatus = 0;
  }
  gRootDiagnostic.magic0 = layout->magic0;
  gRootDiagnostic.magic1 = layout->magic1;
  gRootDiagnostic.manifest = layout->manifestStatus;
  gRootDiagnostic.windows = layout->windows;
  return true;
}
