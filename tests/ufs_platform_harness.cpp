// SPDX-License-Identifier: BSD-2-Clause-Patent
// Production controller and range logic; only MMIO/secure firmware are modeled.
#include "DaliUfsPlatform.h"
#include "DaliUfsMemory.h"
#include <map>
#include <cstring>
using namespace dali_ufs;
struct Sim {
  std::map<uint64_t,uint32_t> r;
  uint64_t us = 0, linkAt = 0; unsigned mode, writes = 0, uics = 0, smcs = 0, resets = 0, ack4 = 0, bad = 0, layers = 0;
  bool linkPending = false, earlyAck = false;
  explicit Sim(unsigned m): mode(m) {
    r[Hci]=0x55c7051f; r[Hci+8]=0x400; r[Hci+0x2240]=0x10470000; r[Hci+0x144]=3;
    r[Hci+0x14c]=0x410; r[Hci+0x380]=0x1f00; // Idle config, not active requests.
    if(m==2){r[Hci+0x50]=0x12345000;r[Hci+0x54]=7;r[Hci+0x70]=0x23456000;r[Hci+0x74]=8;r[Hci+0x30]=15;}
    if(m==3)r[Hci+0x34]=1;
    if(m==4)r[Hci+0x58]=1;
    if(m==5)r[Hci+0x60]=1;
    if(m==6)r[Hci+0x300]=1;
    if(m==9)r[Hci+0x2240]=0;
    if(m==10)r[Ao+4]=8;
    if(m==12)r[Hci+0x144]=0;
    if(m==23)r[Hci+0x74]=UINT32_MAX;
    if(m==25)r[Hci+0x20]=0x400;
    if(m==26)r[Hci+0x78]=1;
    if(m==27)r[Hci+0x80]=1;
    if(m==28)r[Hci+0x24]=1;
    if(m==29)r[Hci+0x30]=0x10;
    if(m==30)r[Ao+0x104]=UINT32_MAX;
    if(m==35||m==36)r[Hci+0x144]=0;
  }
  static uint32_t read(void *p,uint64_t a) {
    auto &s=*static_cast<Sim*>(p);
    if(s.linkPending && s.mode!=17 && s.us-s.linkAt>=5000){s.r[Hci+0x20]|=0x400;s.r[Hci+0x30]=15;s.linkPending=false;}
    if(a>=Hci+0x38&&a<=Hci+0x48){++s.layers;uint32_t v=s.r[a];s.r[a]=0;return v;}
    return s.r[a];
  }
  static bool write(void *p,uint64_t a,uint32_t v) {
    auto &s=*static_cast<Sim*>(p);++s.writes;
    if((a<Hci||a>=Hci+0x2a00)&&(a<Ao||a>=Ao+0x1000))++s.bad;
    if(a==Hci+0x20){
      if(v&4){++s.ack4;if(s.us-s.linkAt<5000)s.earlyAck=true;}
      s.r[a]&=~v;
      if(s.mode==18&&s.uics==6&&v==0x400)s.r[a]|=8;
      return true;
    }
    s.r[a]=v;
    if(s.mode==31&&a==Ao+0x148)return false; // Uncertain non-UIC store.
    if(a==Hci+0x34){s.r[Hci+0x30]=v?8:0;if(s.mode==19&&!v&&s.uics==12)s.r[a]=1;}
    if(a==Hci+0x34&&!v&&s.uics==12){if(s.mode==33)s.r[Hci+0x58]=1;if(s.mode==34)s.r[Hci+0x300]=1;}
    if(s.mode==32&&a==Hci+0x34&&v==1)return false;
    if(a==Hci+0x144&&s.mode!=12)s.r[a]=3;
    if(a==Hci+0x90){
      ++s.uics;
      if(s.mode==14)return true;
      s.r[Hci+0x98]=s.mode==13?1:0;s.r[Hci+0x9c]=0;
      if(v==0x16){
        s.r[Hci+0x30]=15;
        if(s.mode==15||s.mode==16||s.mode==17){s.r[Hci+0x20]=4;s.r[Hci+0x38]=0x80000010;s.linkPending=true;s.linkAt=s.us;
          if(s.mode==16)s.r[Hci+0x3c]=1;
          return true;
        }
      }
      s.r[Hci+0x20]=0x400;
      if(s.mode==20)return false; // Command and completion reached hardware, no replay.
    }
    return true;
  }
  static void fence(void *) {}
  static void delay(void *p,uint32_t us){static_cast<Sim*>(p)->us+=us;}
  static uint64_t sip(void *p,uint32_t service,uint32_t selector,uint32_t a,uint32_t b){
    auto &s=*static_cast<Sim*>(p);++s.smcs;
    if(service!=0xc2000276||(selector!=512&&selector!=8&&selector!=2)||a>1||b>1)++s.bad;
    if(selector==2)++s.resets;
    return (s.mode==11&&selector==512)||(s.mode==21&&selector==2&&a==1)||(s.mode==22&&selector==2&&a==0)||
           (s.mode==35&&selector==8&&b==0)||(s.mode==36&&selector==8&&b==1)?1:0;
  }
};
extern "C" void platform_case(unsigned mode,uint64_t *out){
  Sim s(mode);Controller c;uint8_t resources[ResourceBytes];memcpy(resources,ResourceWords,sizeof ResourceWords);memset(resources+sizeof ResourceWords,1,32);
  if(mode==7)resources[16]^=1;
  Ops io{&s,Sim::read,Sim::write,Sim::fence,Sim::delay,Sim::sip};
  auto result=c.start(mode!=1,resources,sizeof resources,mode==8?"bad":"0123456789abcdef0123456789abcdef",io);
  const auto &d=c.diagnostic();out[0]=uint32_t(result);out[1]=uint32_t(d.stage);out[2]=s.writes;out[3]=s.uics;out[4]=s.smcs;
  out[5]=s.us;out[6]=s.r[Hci+0x34];out[7]=d.failed;out[8]=s.ack4;out[9]=s.earlyAck;out[10]=d.lineAccepted;out[11]=d.lineAcked;
  out[12]=s.bad;out[13]=s.layers;out[14]=d.failure.layers[0];out[15]=d.resetCleanup;out[16]=s.resets;
  out[17]=uint32_t(d.faultStage);out[18]=d.faultAddress;out[19]=d.faultValue;out[20]=d.inherited[8];out[21]=d.inherited[11];
  out[22]=s.r[Hci+0x50];out[23]=s.r[Hci+0x74];
  auto before=s.uics;out[24]=uint32_t(c.start(true,resources,sizeof resources,"0123456789abcdef0123456789abcdef",io));out[25]=s.uics-before;
  out[26]=uint32_t(c.stop());out[27]=s.r[Hci+0x34];out[28]=uint32_t(c.diagnostic().result);
}
extern "C" bool memory_case(uint64_t base,uint64_t cap,uint64_t at,uint64_t bytes,uint64_t line,uint64_t *out){
  CacheSpan r;bool ok=cacheSpan(base,cap,at,bytes,line,r);out[0]=r.begin;out[1]=r.end;return ok;
}
extern "C" bool dma_case(uint64_t cpu,uint64_t pa,uint64_t bytes,uint64_t contiguous){return dmaSpan(cpu,pa,bytes,contiguous);}
extern "C" void scratch_policy_case(unsigned mode,uint64_t *out){
  ScratchGrant g{};g.enabled=true;strcpy(g.token,"0123456789abcdef0123456789abcdef");
  memset(g.deviceBinding,1,sizeof g.deviceBinding);memset(g.candidateSha256,2,sizeof g.candidateSha256);
  memset(g.sourceSha256,3,sizeof g.sourceSha256);memset(g.partitionGuid,4,sizeof g.partitionGuid);
  g.lun=2;g.blockBytes=4096;g.capacityBlocks=64577536;g.bootStartLba=418560;g.bootBlocks=16384;
  g.payloadEndBytes=5062656;g.scratchStartLba=434432;g.scratchBlocks=256;g.tailGuardBlocks=256;
  g.sequence=1;
  ScratchObservation o{};o.identityVerified=true;o.gptVerified=true;o.writeProtected=false;o.scratchMediaReady=true;o.lun=2;
  o.candidateVerified=o.sourceVerified=true;
  strcpy(o.token,g.token);memcpy(o.deviceBinding,g.deviceBinding,sizeof g.deviceBinding);
  memcpy(o.candidateSha256,g.candidateSha256,sizeof g.candidateSha256);
  memcpy(o.sourceSha256,g.sourceSha256,sizeof g.sourceSha256);
  memset(o.partitionGuid,4,sizeof o.partitionGuid);o.blockBytes=4096;o.capacityBlocks=64577536;
  o.bootStartLba=418560;o.bootBlocks=16384;
  uint64_t lba=g.scratchStartLba;uint32_t blocks=1;
  switch(mode){
    case 1:g.enabled=false;break;
    case 2:g.token[0]='z';break;
    case 3:memset(g.deviceBinding,0,sizeof g.deviceBinding);break;
    case 4:memset(g.candidateSha256,0,sizeof g.candidateSha256);break;
    case 5:memset(g.sourceSha256,0,sizeof g.sourceSha256);break;
    case 6:memset(g.partitionGuid,0,sizeof g.partitionGuid);break;
    case 7:g.blockBytes=0;break;
    case 8:g.capacityBlocks=1;break;
    case 9:g.scratchStartLba=g.bootStartLba+g.payloadEndBytes/g.blockBytes-1;break;
    case 10:--g.tailGuardBlocks;break;
    case 11:o.writeProtected=true;break;
    case 12:o.lun=1;break;
    case 13:--o.capacityBlocks;break;
    case 14:o.blockBytes=512;break;
    case 15:o.partitionGuid[0]^=1;break;
    case 16:o.gptVerified=false;break;
    case 17:o.identityVerified=false;break;
    case 18:lba=g.scratchStartLba-1;break;
    case 19:lba=g.scratchStartLba+g.scratchBlocks-1;blocks=2;break;
    case 20:lba=g.scratchStartLba+g.scratchBlocks;break;
    case 21:blocks=0;break;
    case 22:o.token[0]='f';break;
    case 23:o.deviceBinding[0]^=1;break;
    case 24:o.candidateSha256[0]^=1;break;
    case 25:o.sourceSha256[0]^=1;break;
    case 26:lba=g.scratchStartLba+g.scratchBlocks-1;break;
    case 27:o.scratchMediaReady=false;break;
    case 28:o.candidateVerified=false;break;
    case 29:o.sourceVerified=false;break;
    default:break;
  }
  out[0]=scratchGrantValid(g);out[1]=scratchWriteAllowed(g,o,lba,blocks);
}
extern "C" void scratch_wire_case(unsigned mode,uint64_t *out){
  uint8_t wire[ScratchGrantWireBytes]{};
  const uint8_t magic[8]={'D','A','L','I','U','F','S','W'};memcpy(wire,magic,8);
  auto put32=[](uint8_t *p,uint32_t v){for(unsigned i=0;i<4;++i)p[i]=uint8_t(v>>(8*i));};
  auto put64=[](uint8_t *p,uint64_t v){for(unsigned i=0;i<8;++i)p[i]=uint8_t(v>>(8*i));};
  put32(wire+8,2);put32(wire+12,1);memcpy(wire+16,"0123456789abcdef0123456789abcdef",32);
  memset(wire+48,1,32);memset(wire+80,2,32);memset(wire+112,3,32);memset(wire+144,4,16);
  put32(wire+160,2);put32(wire+164,4096);put64(wire+168,64577536);put64(wire+176,418560);
  put64(wire+184,16384);put64(wire+192,5062656);put64(wire+200,434432);put64(wire+208,256);put64(wire+216,256);
  put64(wire+224,1);
  size_t length=sizeof wire;
  switch(mode){case 1:wire[0]^=1;break;case 2:put32(wire+8,3);break;case 3:put32(wire+12,0);break;
    case 4:wire[16]='z';break;case 5:put32(wire+160,8);break;case 6:memset(wire+48,0,32);break;
    case 7:put64(wire+200,UINT64_MAX);break;case 8:length--;break;default:break;}
  ScratchGrant g{};out[0]=decodeScratchGrant(wire,length,g);out[1]=g.enabled;out[2]=g.lun;out[3]=g.scratchStartLba;out[4]=g.scratchBlocks;
  uint64_t absolute=0;bool valid=scratchAbsoluteLba(g,255,1,absolute);
  out[5]=valid;out[6]=absolute;out[7]=scratchAbsoluteLba(g,255,2,absolute);
}
extern "C" bool scratch_wire_decode_case(const uint8_t *wire,size_t length,uint64_t *out){
  ScratchGrant g{};bool ok=decodeScratchGrant(wire,length,g);out[0]=ok;out[1]=g.enabled;out[2]=g.lun;
  out[3]=g.scratchStartLba;out[4]=g.scratchBlocks;out[5]=g.capacityBlocks;out[6]=g.blockBytes;
  uint64_t absolute=0;out[7]=ok&&scratchAbsoluteLba(g,255,1,absolute);out[8]=absolute;out[9]=g.sequence;return ok;
}
extern "C" void scratch_sequence_case(unsigned mode,uint64_t *out){
  ScratchGrant g{};g.enabled=true;strcpy(g.token,"0123456789abcdef0123456789abcdef");
  memset(g.deviceBinding,1,sizeof g.deviceBinding);memset(g.candidateSha256,2,sizeof g.candidateSha256);
  memset(g.sourceSha256,3,sizeof g.sourceSha256);memset(g.partitionGuid,4,sizeof g.partitionGuid);
  g.lun=2;g.blockBytes=4096;g.capacityBlocks=64577536;g.bootStartLba=418560;g.bootBlocks=16384;
  g.payloadEndBytes=5062656;g.scratchStartLba=434432;g.scratchBlocks=256;g.tailGuardBlocks=256;g.sequence=1;
  if(mode==1)g.sequence=128; if(mode==2)g.sequence=129;
  uint64_t marker=UINT64_MAX,payload=UINT64_MAX;bool valid=scratchSequenceLbas(g,marker,payload);
  out[0]=valid;out[1]=marker;out[2]=payload;out[3]=static_cast<uint64_t>(scratchMarkerAction(true,false));
  out[4]=static_cast<uint64_t>(scratchMarkerAction(false,true));out[5]=static_cast<uint64_t>(scratchMarkerAction(false,false));
  out[6]=static_cast<uint64_t>(scratchPayloadAction(true,true,false));
  out[7]=static_cast<uint64_t>(scratchPayloadAction(true,false,true));
  out[8]=static_cast<uint64_t>(scratchPayloadAction(false,false,true));
  out[9]=static_cast<uint64_t>(scratchPayloadAction(false,true,false));
  uint8_t digest[32];memset(digest,0xa5,sizeof digest);uint8_t bytes[512];
  bool made=makeScratchAttemptMarker(g.token,g.sequence,512,digest,bytes);
  out[10]=made;out[11]=made&&!memcmp(bytes,"DALIATT1",8);out[12]=made&&bytes[512-8]=='D';
  out[13]=made&&bytes[56]==0xa5;out[14]=made&&bytes[88]==0;
}
extern "C" bool guid_case(const char *text,uint8_t *out){return parsePartitionGuid(text,out);}
