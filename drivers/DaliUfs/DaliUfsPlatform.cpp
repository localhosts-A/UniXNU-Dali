// SPDX-License-Identifier: BSD-2-Clause-Patent
// Adapted from UEFI-Dali b502bdf MT6991 UfsController.c. Fixed MiCode
// device_modules e45c319 ufs-mediatek.c:242-347,391-455,1686-1692,1931-2058;
// Linux f089d58 ufshci.h/unipro.h/ufshcd.c. Firmware addresses are never DMA.
#include "DaliUfsPlatform.h"
#include <string.h>

namespace dali_ufs {
static constexpr uint32_t Uccs = 0x400, UicError = 4;
bool tokenValid(const char *s) {
  if (!s) return false;
  for (unsigned i = 0; i < 32; ++i) if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
  return s[32] == 0;
}
static bool nonzero(const uint8_t *bytes, size_t count) {
  uint8_t value = 0; for (size_t i = 0; i < count; ++i) value |= bytes[i]; return value != 0;
}
static uint32_t little32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
static uint64_t little64(const uint8_t *p) {
  return uint64_t(little32(p)) | uint64_t(little32(p + 4)) << 32;
}
static void store32(uint8_t *p, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) p[i] = static_cast<uint8_t>(value >> (8 * i));
}
static void store64(uint8_t *p, uint64_t value) {
  for (unsigned i = 0; i < 8; ++i) p[i] = static_cast<uint8_t>(value >> (8 * i));
}
bool decodeScratchGrant(const void *data, size_t bytes, ScratchGrant &g) {
  static const uint8_t magic[8] = {'D','A','L','I','U','F','S','W'};
  const auto p = static_cast<const uint8_t *>(data);
  if (!p || bytes != ScratchGrantWireBytes || memcmp(p, magic, sizeof magic) ||
      little32(p + 8) != 2 || little32(p + 12) != 1) return false;
  ScratchGrant candidate{};
  candidate.enabled = true;
  memcpy(candidate.token, p + 16, 32); candidate.token[32] = 0;
  memcpy(candidate.deviceBinding, p + 48, 32);
  memcpy(candidate.candidateSha256, p + 80, 32);
  memcpy(candidate.sourceSha256, p + 112, 32);
  memcpy(candidate.partitionGuid, p + 144, 16);
  uint32_t lun = little32(p + 160);
  if (lun >= 8) return false;
  candidate.lun = static_cast<uint8_t>(lun);
  candidate.blockBytes = little32(p + 164);
  candidate.capacityBlocks = little64(p + 168);
  candidate.bootStartLba = little64(p + 176);
  candidate.bootBlocks = little64(p + 184);
  candidate.payloadEndBytes = little64(p + 192);
  candidate.scratchStartLba = little64(p + 200);
  candidate.scratchBlocks = little64(p + 208);
  candidate.tailGuardBlocks = little64(p + 216);
  candidate.sequence = little64(p + 224);
  if (!scratchGrantValid(candidate)) return false;
  g = candidate;
  return true;
}
bool scratchAbsoluteLba(const ScratchGrant &g, uint64_t relativeLba, uint32_t blocks, uint64_t &absoluteLba) {
  absoluteLba = 0;
  if (!scratchGrantValid(g) || !blocks || relativeLba >= g.scratchBlocks ||
      blocks > g.scratchBlocks - relativeLba || relativeLba > UINT64_MAX - g.scratchStartLba) return false;
  absoluteLba = g.scratchStartLba + relativeLba;
  return true;
}
bool scratchSequenceLbas(const ScratchGrant &g, uint64_t &markerRelativeLba, uint64_t &payloadRelativeLba) {
  markerRelativeLba = payloadRelativeLba = 0;
  if (!scratchGrantValid(g) || g.sequence - 1 > UINT64_MAX / 2) return false;
  const uint64_t marker = (g.sequence - 1) * 2;
  if (marker >= g.scratchBlocks || g.scratchBlocks - marker < 2) return false;
  markerRelativeLba = marker; payloadRelativeLba = marker + 1; return true;
}
bool makeScratchAttemptMarker(const char *token, uint64_t sequence, uint32_t blockBytes,
                              const uint8_t payloadDigest[32], uint8_t *bytes) {
  constexpr uint32_t HeaderBytes = 88, CommitBytes = 8;
  static const uint8_t magic[8] = {'D','A','L','I','A','T','T','1'};
  if (!tokenValid(token) || !sequence || blockBytes < HeaderBytes + CommitBytes ||
      blockBytes > 65536 || !payloadDigest || !bytes) return false;
  memset(bytes, 0, blockBytes); memcpy(bytes, magic, sizeof magic);
  store32(bytes + 8, 1); store64(bytes + 12, sequence); memcpy(bytes + 20, token, 32);
  store32(bytes + 52, blockBytes); memcpy(bytes + 56, payloadDigest, 32);
  memcpy(bytes + blockBytes - CommitBytes, "DALI-CM1", CommitBytes); return true;
}
ScratchMarkerAction scratchMarkerAction(bool allZero, bool matchesExpected) {
  if (allZero) return ScratchMarkerAction::WriteMarker;
  return matchesExpected ? ScratchMarkerAction::ReadbackOnly : ScratchMarkerAction::Refuse;
}
ScratchPayloadAction scratchPayloadAction(bool markerCreatedThisBoot, bool allZero, bool matchesExpected) {
  if (markerCreatedThisBoot) return allZero ? ScratchPayloadAction::WritePayload : ScratchPayloadAction::Refuse;
  return matchesExpected ? ScratchPayloadAction::ReadbackOnly : ScratchPayloadAction::Refuse;
}
static int hexDigit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}
bool parsePartitionGuid(const char *text, uint8_t output[16]) {
  if (!text || !output || strnlen(text, 37) != 36) return false;
  const unsigned hyphens[] = {8, 13, 18, 23};
  for (unsigned at : hyphens) if (text[at] != '-') return false;
  unsigned byte = 0;
  for (unsigned i = 0; i < 36;) {
    if (text[i] == '-') { ++i; continue; }
    int hi = hexDigit(text[i++]); int lo = hexDigit(text[i++]);
    if (hi < 0 || lo < 0 || byte >= 16) return false;
    output[byte++] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return byte == 16;
}
bool scratchGrantValid(const ScratchGrant &g) {
  constexpr uint64_t ScratchBytes = 1024 * 1024;
  if (!g.enabled || !tokenValid(g.token) || g.lun >= 8 ||
      !nonzero(g.deviceBinding, sizeof g.deviceBinding) ||
      !nonzero(g.candidateSha256, sizeof g.candidateSha256) ||
      !nonzero(g.sourceSha256, sizeof g.sourceSha256) || !g.sequence ||
      !nonzero(g.partitionGuid, sizeof g.partitionGuid)) return false;
  if (g.blockBytes < 512 || g.blockBytes > 65536 || (g.blockBytes & (g.blockBytes - 1)) ||
      ScratchBytes % g.blockBytes || !g.capacityBlocks || !g.bootBlocks || !g.payloadEndBytes) return false;
  if (g.bootBlocks > UINT64_MAX / g.blockBytes) return false;
  const uint64_t bootBytes = g.bootBlocks * g.blockBytes;
  if (g.payloadEndBytes > bootBytes || g.bootStartLba >= g.capacityBlocks ||
      g.bootBlocks > g.capacityBlocks - g.bootStartLba) return false;
  const uint64_t expectedWindowBlocks = ScratchBytes / g.blockBytes;
  if (g.scratchBlocks != expectedWindowBlocks || g.tailGuardBlocks != expectedWindowBlocks ||
      g.sequence > g.scratchBlocks / 2 ||
      g.scratchStartLba < g.bootStartLba) return false;
  const uint64_t bootEndLba = g.bootStartLba + g.bootBlocks;
  const uint64_t scratchOffsetBlocks = g.scratchStartLba - g.bootStartLba;
  if (scratchOffsetBlocks > UINT64_MAX / g.blockBytes ||
      scratchOffsetBlocks * g.blockBytes < g.payloadEndBytes ||
      g.scratchStartLba > bootEndLba || g.scratchBlocks > bootEndLba - g.scratchStartLba ||
      g.tailGuardBlocks != bootEndLba - g.scratchStartLba - g.scratchBlocks) return false;
  return true;
}
bool scratchWriteAllowed(const ScratchGrant &g, const ScratchObservation &o, uint64_t lba, uint32_t blocks) {
  if (!scratchGrantValid(g) || !o.identityVerified || !o.gptVerified || !o.scratchMediaReady || o.writeProtected ||
      !tokenValid(o.token) || strcmp(g.token, o.token) ||
      memcmp(o.deviceBinding, g.deviceBinding, sizeof g.deviceBinding) ||
      memcmp(o.candidateSha256, g.candidateSha256, sizeof g.candidateSha256) ||
      memcmp(o.sourceSha256, g.sourceSha256, sizeof g.sourceSha256) ||
      o.lun != g.lun || o.blockBytes != g.blockBytes || o.capacityBlocks != g.capacityBlocks ||
      o.bootStartLba != g.bootStartLba || o.bootBlocks != g.bootBlocks ||
      memcmp(o.partitionGuid, g.partitionGuid, sizeof g.partitionGuid) || !blocks ||
      lba < g.scratchStartLba) return false;
  const uint64_t offset = lba - g.scratchStartLba;
  return offset < g.scratchBlocks && blocks <= g.scratchBlocks - offset;
}
bool resourceValid(const void *p, size_t bytes) {
  if (!p || bytes != ResourceBytes || memcmp(p, ResourceWords, sizeof(ResourceWords))) return false;
  auto digest = static_cast<const uint8_t *>(p) + sizeof(ResourceWords);
  uint8_t nonzero = 0; for (unsigned i = 0; i < 32; ++i) nonzero |= digest[i];
  return nonzero != 0;
}
void Controller::capture(Snapshot &s) {
  io_.fence(io_.context);
  s.interrupt = io_.read(io_.context, Hci + 0x20); s.hcs = io_.read(io_.context, Hci + 0x30); s.hce = io_.read(io_.context, Hci + 0x34);
  for (unsigned i = 0; i < 3; ++i) s.arguments[i] = io_.read(io_.context, Hci + 0x94 + 4 * i);
  for (unsigned i = 0; i < 5; ++i) s.layers[i] = io_.read(io_.context, Hci + 0x38 + 4 * i);
  io_.fence(io_.context); s.captured = true;
}
bool Controller::fail(Result r, bool snapshot) {
  if (!d_.failed) {
    d_.result = r; d_.faultStage = d_.stage; d_.faultAddress = d_.address; d_.faultValue = d_.value;
    if (snapshot && d_.touched && !d_.failure.captured &&
        (d_.stage == Stage::PreLink || d_.stage == Stage::LinkStartup || d_.stage == Stage::PostLink)) capture(d_.failure);
  }
  d_.failed = true; d_.ready = false; return false;
}
uint32_t Controller::read(uint64_t address) { d_.address = address; return d_.value = io_.read(io_.context, address); }
bool Controller::write(uint64_t address, uint32_t value) {
  d_.address = address; d_.value = value; d_.touched = true;
  if (!io_.write(io_.context, address, value)) return fail(Result::Hardware);
  io_.fence(io_.context); return true;
}
bool Controller::wait(uint32_t off, uint32_t mask, uint32_t value, uint32_t timeout) {
  for (uint32_t elapsed = 0;; elapsed += 10) {
    uint32_t actual = read(Hci + off);
    if (actual == UINT32_MAX) return fail(Result::Hardware);
    if ((actual & mask) == value) return true;
    if (elapsed >= timeout) return fail(Result::Timeout);
    io_.delay(io_.context, 10);
  }
}
bool Controller::sip(uint32_t selector, uint32_t a, uint32_t b) {
  d_.touched = true; d_.sipSelector = selector; d_.sipA = a; d_.sipB = b;
  d_.sipStatus = io_.sip(io_.context, 0xc2000276, selector, a, b);
  return d_.sipStatus == 0 || fail(Result::Sip);
}
bool Controller::rmw(uint32_t off, uint32_t mask, uint32_t bits) {
  uint32_t old = read(Hci + off); return old == UINT32_MAX ? fail(Result::Hardware) : write(Hci + off, (old & ~mask) | bits);
}
bool Controller::reset(uint32_t off, uint32_t bits) {
  uint32_t old = read(Ao + off); return old == UINT32_MAX ? fail(Result::Hardware) : write(Ao + off, old | bits);
}
bool Controller::stoppedQueues(bool clearBases) {
  // Unlike LK, a quiesced firmware may leave idle old base addresses and HCS.F.
  // Require HCE=0, both doorbells/run bits zero, IE=0 and MEM_CFG=0
  // BEFORE clearing those addresses. No inherited pointer is ever dereferenced.
  // Fixed Linux ufshci.h:206-212: HCS low nibble is present/list/UIC ready,
  // not outstanding work. ufs-mcq.c:27,91-98: MCQ_CFG[16:8] is a configured
  // maximum, not an active count; ufshcd.c:8751 selects MCQ via MEM_CFG bit0.
  // MediaTek 0x14c contains vendor state (ufs-mediatek.c:505-513,545-552),
  // not a count that must be zero. Qualified idle values are 0x410 / 0x1f00.
  const uint32_t off[] = {0x34, 0x30, 0x58, 0x60, 0x78, 0x80, 0x300, 0x24};
  for (unsigned i = 0; i < 8; ++i) {
    uint32_t v = read(Hci + off[i]);
    if (v == UINT32_MAX) return fail(Result::Hardware);
    if ((i == 1 ? (v & ~15U) : v) != 0) return fail(Result::Busy);
    if (!d_.touched) d_.inherited[i] = v;
  }
  const uint32_t bases[] = {0x50, 0x54, 0x70, 0x74};
  uint32_t values[4];
  for (unsigned i = 0; i < 4; ++i) {
    values[i] = read(Hci + bases[i]);
    if (values[i] == UINT32_MAX) return fail(Result::Hardware);
    if (!d_.touched) d_.inherited[i + 8] = values[i];
  }
  for (unsigned i = 0; i < 4; ++i) {
    if (clearBases && !write(Hci + bases[i], 0)) return false;
    if (clearBases && read(Hci + bases[i]) != 0) return fail(Result::Hardware);
  }
  return true;
}
bool Controller::deviceReset() {
  bool low = sip(2, 0, 0);
  bool stopped = low && write(Hci + 0x34, 0) && wait(0x34, 1, 0, 100000);
  io_.delay(io_.context, 10);
  if (!stopped) {
    // One cleanup release, not a replay. Preserve the first failure/SipStatus.
    d_.resetCleanup = io_.sip(io_.context, 0xc2000276, 2, 1, 0);
    if (!d_.resetCleanup) io_.delay(io_.context, 15000);
    return false;
  }
  if (!sip(2, 1, 0)) return false;
  io_.delay(io_.context, 15000); return true;
}
bool Controller::ack(uint32_t mask) {
  if (!write(Hci + 0x20, mask)) return false;
  d_.interrupt = read(Hci + 0x20);
  return d_.interrupt == 0 || fail(Result::Hardware);
}
bool Controller::uic(uint32_t opcode, uint32_t attribute, uint32_t &value) {
  if (d_.failed || !wait(0x30, 8, 8, 500000)) return false;
  if (d_.value & 0x30) return fail(Result::Hardware);
  d_.interrupt = read(Hci + 0x20);
  if (d_.interrupt & ~Uccs) return fail(Result::Hardware);
  d_.opcode = opcode; d_.uicResult = UINT32_MAX;
  if (!ack(Uccs) || !write(Hci + 0x94, attribute << 16) || !write(Hci + 0x98, 0) ||
      !write(Hci + 0x9c, value) || !write(Hci + 0x90, opcode)) return false;
  bool link = d_.stage == Stage::LinkStartup && opcode == 0x16;
  for (uint32_t elapsed = 0;; elapsed += 10) {
    d_.interrupt = read(Hci + 0x20);
    if (link) {
      if (d_.interrupt & ~(UicError | Uccs)) return fail(Result::Hardware);
      d_.linePending |= (d_.interrupt & UicError) != 0;
    } else if (d_.interrupt & ~Uccs) return fail(Result::Hardware);
    if (d_.interrupt & Uccs) break;
    if (elapsed >= 500000) return fail(Result::Timeout);
    io_.delay(io_.context, 10);
  }
  if (link && d_.linePending) {
    if (d_.lineReset.captured || d_.interrupt != (UicError | Uccs)) return fail(Result::Hardware);
    capture(d_.lineReset); const auto &s = d_.lineReset;
    bool isolated = s.interrupt == (UicError | Uccs) && s.hce == 1 && s.hcs != UINT32_MAX &&
                    (s.hcs & 0x38) == 8 && s.layers[0] == 0x80000010;
    for (unsigned i = 1; i < 5; ++i) isolated &= s.layers[i] == 0;
    if (!isolated) { d_.failure = s; return fail(Result::Hardware); }
    d_.lineAccepted = true;
  }
  uint32_t result = read(Hci + 0x98); d_.uicResult = result & 255;
  if (result == UINT32_MAX) return fail(Result::Hardware);
  if (d_.uicResult) return fail(Result::Uic);
  value = read(Hci + 0x9c); if (value == UINT32_MAX) return fail(Result::Hardware);
  uint32_t mask = Uccs | (link && d_.linePending ? UicError : 0);
  if (!ack(mask)) return false;
  d_.lineAcked |= (mask & UicError) != 0; return true;
}
bool Controller::dmeRmw(uint32_t attribute, uint32_t mask, uint32_t bits) {
  uint32_t value = 0; return uic(1, attribute, value) && set(attribute, (value & ~mask) | bits);
}
Result Controller::stop() {
  d_.ready = false;
  if (!d_.touched) return d_.stop = Result::Stopped;
  // Do not sample read-clear UIC layers after stop writes have started.
  Controller q; q.io_ = io_;
  bool ok = q.write(Hci + 0x24, 0) && q.write(Hci + 0x60, 0) && q.write(Hci + 0x80, 0) &&
            q.write(Hci + 0x34, 0) && q.wait(0x34, 1, 0, 100000) && q.stoppedQueues(false);
  if (!ok) {
    if (!d_.failed) { d_.address = q.d_.faultAddress; d_.value = q.d_.faultValue; fail(q.d_.result, false); }
    d_.stopped = false; return d_.stop = q.d_.result;
  }
  d_.stopped = true; return d_.stop = Result::Stopped;
}
Result Controller::start(bool enabled, const void *resources, size_t bytes, const char *token, const Ops &io) {
  if (!enabled) return Result::Off;
  if (d_.stopped) return d_.failed ? d_.result : Result::Rejected;
  if (d_.attempted) return d_.ready ? Result::Ready : d_.failed ? d_.result : Result::Rejected;
  d_.attempted = true;
  if (!io.read || !io.write || !io.fence || !io.delay || !io.sip || !resourceValid(resources, bytes) || !tokenValid(token)) {
    fail(Result::Rejected, false); return d_.result;
  }
  io_ = io;
  uint32_t g0 = read(Ao + 0x104);
  if (g0 == UINT32_MAX || (g0 & 3)) { fail(g0 == UINT32_MAX ? Result::Hardware : Result::Rejected); return d_.result; }
  uint32_t g1 = read(Ao + 4);
  if (g1 == UINT32_MAX || (g1 & 0x11f)) { fail(g1 == UINT32_MAX ? Result::Hardware : Result::Rejected); return d_.result; }
  if (read(Hci) != 0x55c7051f || read(Hci + 8) != 0x400 || read(Hci + 0x2240) != 0x10470000) {
    fail(Result::Rejected); return d_.result;
  }
  if (!stoppedQueues(true)) goto failed;
  d_.stage = Stage::Power;
  if (!sip(512, 0, 0) || !sip(512, 1, 0) || !stoppedQueues(true)) goto failed;
  d_.stage = Stage::ReferenceClock;
  { uint32_t ref = read(Hci + 0x144);
    if (ref == UINT32_MAX) { fail(Result::Hardware); goto failed; }
    if ((ref & 3) != 3) {
      if (!sip(8, 1, 0)) goto failed;
      if (!write(Hci + 0x144, 1) || !wait(0x144, 3, 3, 3000)) {
        d_.clockCleanup = io_.sip(io_.context, 0xc2000276, 8, 0, 1); goto failed;
      }
      io_.delay(io_.context, 32); if (!sip(8, 1, 1)) goto failed;
    }
  }
  d_.stage = Stage::DeviceReset; if (!deviceReset()) goto failed;
  d_.stage = Stage::HostReset;
  if (!reset(0x148, 4) || !reset(0x148, 2) || !reset(0x148, 1)) goto failed;
  io_.delay(io_.context, 100);
  if (!reset(0x14c, 1) || !reset(0x14c, 2) || !reset(0x14c, 4) || !write(Hci + 0x18, 0) ||
      !write(Hci + 0x24, 0) || !rmw(0x140, 0, 0x80) || !rmw(0x160, (0x7fffU << 8) | (1U << 28), 0x453000 | (1U << 25))) goto failed;
  d_.stage = Stage::Hce;
  if (!write(Hci + 0x34, 1)) goto failed;
  io_.delay(io_.context, 600);
  if (!wait(0x34, 1, 1, 50000)) goto failed;
  { const uint32_t offsets[] = {0x50, 0x54, 0x58, 0x60, 0x70, 0x74, 0x78, 0x80, 0x300};
    for (uint32_t off : offsets) if (read(Hci + off) != 0) { fail(Result::Busy); goto failed; }
  }
  d_.stage = Stage::PreLink;
  if (!uic(1, 0x15a9, d_.localVersion) || !set(0xd0a8, 0) || !set(0x155e, 0) || !dmeRmw(0xd0a6, 64, 0)) goto failed;
  d_.stage = Stage::LinkStartup;
  { uint32_t value = 0; if (!uic(0x16, 0, value) || !wait(0x30, 1, 1, 50000)) goto failed; }
  d_.stage = Stage::PostLink;
  if (!dmeRmw(0xd09e, 0, 0x100) || !dmeRmw(0xd0a6, 0, 13) || !dmeRmw(0xd0a1, 32, 0)) goto failed;
  d_.ready = true; return d_.result = Result::Ready;
failed:
  stop(); return d_.result;
}
} // namespace dali_ufs
