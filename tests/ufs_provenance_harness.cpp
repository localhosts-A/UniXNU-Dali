// Executes the real production preimage/binding/range gates; no hardware I/O.
#include "DaliUfsPlatform.h"
#include <string.h>
using namespace dali_ufs;
extern "C" size_t preimage(const SourceIdentity *id, const uint8_t *kernel, uint8_t *out, size_t capacity) {
  return sourceBindingPreimage(*id, kernel, out, capacity);
}
extern "C" void verify(const uint8_t *wire, size_t wireBytes, const void *measured, size_t measuredBytes,
                        const SourceIdentity *id, Hash hash, uint64_t *out, uint8_t *digests) {
  ScratchGrant g{};decodeScratchGrant(wire,wireBytes,g);
  ScratchObservation o{};o.identityVerified=o.gptVerified=o.scratchMediaReady=true;o.writeProtected=false;
  o.candidateVerified=o.sourceVerified=true;memset(o.candidateSha256,0xaa,32);memset(o.sourceSha256,0xbb,32);
  memcpy(o.token,g.token,33);memcpy(o.deviceBinding,g.deviceBinding,32);memcpy(o.partitionGuid,g.partitionGuid,16);
  o.lun=g.lun;o.blockBytes=g.blockBytes;o.capacityBlocks=g.capacityBlocks;o.bootStartLba=g.bootStartLba;o.bootBlocks=g.bootBlocks;
  out[0]=verifyScratchProvenance(g,measured,measuredBytes,*id,hash,nullptr,o);
  out[1]=o.candidateVerified;out[2]=o.sourceVerified;out[3]=scratchWriteAllowed(g,o,g.scratchStartLba,1);
  memcpy(digests,o.candidateSha256,32);memcpy(digests+32,o.sourceSha256,32);
}
