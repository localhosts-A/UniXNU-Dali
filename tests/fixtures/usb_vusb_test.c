#include "DaliUsbVusb.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct client;
struct hub {
  uint8_t pmic5[0x2000],pmic14[0x1000];
  uint32_t sta,rdata,wdata;
  uint64_t time,enable_at;
  unsigned operations,fail_at,commands,writes5,reads5,change_at;
  bool applied,quiesced,stuck,pmic_error,reenter;
  struct client *owner,*reserved,*peer;
};
struct client { struct hub *hub; struct dali_usb_pmif p; bool active; };
static bool before(struct hub *h) { h->operations++; return h->operations==h->fail_at && !h->applied; }
static int after(struct hub *h) { return h->operations==h->fail_at ? USB_IO:USB_OK; }
static int take(void *ctx) {
  struct client *c=ctx; struct hub *h=c->hub;
  if (h->owner || (h->reserved && h->reserved!=c)) return USB_BUSY;
  assert(!c->active); h->owner=c; c->active=true; return USB_OK;
}
static int drop(void *ctx,bool pending) {
  struct client *c=ctx; struct hub *h=c->hub;
  assert(c->active && h->owner==c); h->owner=NULL; c->active=false; h->reserved=pending?c:NULL; return USB_OK;
}
static int r32(void *ctx,uint32_t offset,uint32_t *out) {
  struct client *c=ctx; struct hub *h=c->hub; assert(c->active && h->owner==c);
  if (before(h)) return USB_IO;
  if (offset==0) *out=1;
  else if (offset==0x8a8) *out=h->sta;
  else if (offset==0x894) *out=h->rdata;
  else assert(0);
  return after(h);
}
static int w32(void *ctx,uint32_t offset,uint32_t value) {
  struct client *c=ctx; struct hub *h=c->hub; assert(c->active && h->owner==c);
  if (before(h)) return USB_IO;
  if (offset==0x884) h->wdata=value;
  else if (offset==0x8a4) { assert(value==1); h->sta=0; }
  else if (offset==0x880) {
    unsigned sid=(value>>24)&15, length=((value>>16)&255)+1, address=value&0xffff;
    bool writing=(value&(1u<<29))!=0;
    assert((value>>30)==3 && length<=2 && (sid==5 || sid==14));
    uint8_t *bytes=sid==5?h->pmic5:h->pmic14;
    assert(address+length<=(sid==5?sizeof h->pmic5:sizeof h->pmic14));
    h->commands++;
    if (h->reenter && h->peer) {
      uint8_t out=0xa5; assert(dali_mt6379_read(&h->peer->p,0,&out,1)==USB_BUSY && out==0xa5);
    }
    if (writing) {
      assert(sid==5 && address==0x1b95 && length==1 &&
        (uint8_t)(h->wdata&~1u)==(uint8_t)(bytes[address]&~1u) && (h->wdata&1));
      h->writes5++; h->enable_at=h->time; bytes[address]=(uint8_t)h->wdata;
      h->sta=h->stuck?2:0;
    } else {
      h->rdata=0; for(unsigned i=0;i<length;i++) h->rdata|=(uint32_t)bytes[address+i]<<(8*i);
      h->sta=h->stuck?2:12;
      if (sid==5 && ++h->reads5==h->change_at) h->pmic5[0x1e0d]^=1;
    }
    if (h->pmic_error) h->sta|=1u<<18;
  } else assert(0);
  return after(h);
}
static void barrier(void *ctx) { (void)ctx; }
static void delay(void *ctx,uint32_t us) { ((struct client *)ctx)->hub->time+=us; }
static int quiesce(void *ctx) { return ((struct hub *)ctx)->quiesced?USB_OK:USB_BUSY; }
static int init_client(struct client *c,struct hub *h,bool vusb) {
  c->hub=h;
  struct usb_mmio m={c,0x8ff,r32,w32,barrier,delay}; struct dali_pmif_lock l={c,take,drop};
  return vusb?dali_usb_pmif_init_vusb(&c->p,&m,&l,0x85c0,true):dali_usb_pmif_init(&c->p,&m,&l,0x85c0,true);
}
static void setup(struct hub *h,struct client *c,bool enabled) {
  memset(h,0,sizeof *h); memset(c,0,sizeof *c); h->quiesced=true;
  h->pmic5[8]=0x10; h->pmic5[9]=0x73; h->pmic5[0xb]=0x10;
  h->pmic5[0x1b95]=(uint8_t)(0xa2|(enabled?1:0)); h->pmic5[0x1e0c]=0xa7; h->pmic5[0x1e0d]=0xcd;
  h->pmic14[0]=0x72; c->hub=h;
}
static int startup(struct hub *h,struct client *c,struct dali_usb_vusb *v) {
  int result=init_client(c,h,true);
  if (!result) result=dali_mt6373_vusb_identify(&c->p);
  struct dali_repeater_guard g={h,quiesce};
  if (!result) result=dali_usb_vusb_init(v,&c->p,&g,true);
  if (!result) result=dali_usb_vusb_acquire(v);
  return result;
}
static void normal_and_faults(void) {
  struct hub h; struct client c; struct dali_usb_vusb v={0}; setup(&h,&c,false);
  assert(startup(&h,&c,&v)==USB_OK && v.held && v.nominal_uv==3300000 && v.trim==7 && v.selector==13);
  assert(h.writes5==1 && h.time-h.enable_at>=720 && h.pmic5[0x1b95]==0xa3 && h.pmic5[0x1e0c]==0xa7 && h.pmic5[0x1e0d]==0xcd);
  unsigned count=h.operations;
  assert(dali_usb_vusb_acquire(&v)==USB_OK && h.writes5==1);
  assert(dali_usb_vusb_release(&v)==USB_OK && !v.initialized && h.pmic5[0x1b95]==0xa3);
  for(unsigned n=1;n<=count;n++) for(unsigned applied=0;applied<2;applied++) {
    setup(&h,&c,false); v=(struct dali_usb_vusb){0}; h.fail_at=n; h.applied=applied!=0;
    assert(startup(&h,&c,&v)!=USB_OK && !v.held && h.operations==n);
    assert(h.writes5<=1 && (h.pmic5[0x1b95]&0xfe)==0xa2 && h.pmic5[0x1e0d]==0xcd && h.pmic5[0x1e0c]==0xa7);
    if (v.initialized && v.faulted) { unsigned before_count=h.operations; assert(dali_usb_vusb_acquire(&v)==USB_IO && h.operations==before_count); }
    h.fail_at=0;
    if (v.initialized) assert(dali_usb_vusb_release(&v)==USB_OK);
    else if (c.p.initialized && (c.p.faulted || c.p.pending_owned)) assert(dali_usb_pmif_recover(&c.p)==USB_OK);
    assert(!h.owner && !h.reserved && !c.active);
  }
  for(unsigned selector=0;selector<16;selector++) {
    setup(&h,&c,true); v=(struct dali_usb_vusb){0}; h.pmic5[0x1e0d]=(uint8_t)(0xc0|selector);
    assert(startup(&h,&c,&v)==USB_OK && v.held && h.writes5==0 && h.time>=720);
    assert(v.nominal_uv>=1200000 && v.nominal_uv<=3500000 && v.selector==selector);
    assert(dali_usb_vusb_release(&v)==USB_OK && h.writes5==0 && (h.pmic5[0x1b95]&1));
  }
  printf("PASS: VUSB startup faults at each of %u PMIF MMIO operations, before/after effects\n",count);
}
static void ownership_and_refusals(void) {
  struct hub h; struct client c,peer={0}; struct dali_usb_vusb v={0}; setup(&h,&c,false);
  assert(init_client(&peer,&h,false)==USB_OK && dali_mt6379_identify(&peer.p)==USB_OK);
  h.peer=&peer; h.reenter=true;
  assert(startup(&h,&c,&v)==USB_OK && !h.owner && !h.reserved);
  unsigned commands=h.commands;
  assert(dali_mt6379_identify(&c.p)==USB_UNSUPPORTED && dali_mt6379_update8(&c.p,0,1,1)==USB_UNSUPPORTED);
  uint8_t out=0xa5; assert(dali_mt6379_read(&c.p,0,&out,1)==USB_UNSUPPORTED && out==0xa5);
  unsigned operations=h.operations;
  assert(dali_mt6379_write(&c.p,0,&out,1)==USB_UNSUPPORTED && h.operations==operations);
  assert(dali_mt6373_vusb_read8(&peer.p,0x1b95,&out)==USB_UNSUPPORTED && out==0xa5 && h.operations==operations);
  assert(dali_mt6373_vusb_enable(&peer.p)==USB_UNSUPPORTED && dali_mt6373_vusb_identify(&peer.p)==USB_UNSUPPORTED && h.commands==commands);
  assert(dali_mt6373_vusb_read8(&c.p,0x400,&out)==USB_RANGE && out==0xa5 && h.commands==commands);
  h.reenter=false;
  // Applied command error leaves a SID5 response reserved across bus leases.
  h.fail_at=h.operations+4; h.applied=true;
  assert(dali_mt6373_vusb_read8(&c.p,0x1b95,&out)==USB_IO && out==0xa5 && c.p.pending_owned && h.reserved==&c);
  assert(dali_mt6379_read(&peer.p,0,&out,1)==USB_BUSY && out==0xa5);
  h.fail_at=0; assert(dali_usb_vusb_release(&v)==USB_OK && !h.reserved && (h.pmic5[0x1b95]&1));
  assert(dali_mt6379_read(&peer.p,0,&out,1)==USB_OK && out==0x72);
  for(unsigned bad=0;bad<2;bad++) {
    setup(&h,&c,false); if(bad) h.pmic5[0xb]=0; else h.pmic5[9]=0x79;
    assert(init_client(&c,&h,true)==USB_OK && dali_mt6373_vusb_identify(&c.p)==USB_UNSUPPORTED && !c.p.identified);
    unsigned operations=h.operations; assert(dali_mt6373_vusb_enable(&c.p)==USB_UNSUPPORTED && h.operations==operations && !h.writes5);
  }
  setup(&h,&c,false); struct usb_mmio m={&c,0x8ff,r32,w32,barrier,delay}; struct dali_pmif_lock l={&c,take,drop};
  assert(dali_usb_pmif_init_vusb(&c.p,&m,&l,0x85e0,true)==USB_UNSUPPORTED && h.operations==0);
  assert(dali_usb_pmif_init_vusb(&c.p,&m,&l,0x85c0,false)==USB_UNSUPPORTED && h.operations==0);
  h.sta=12; assert(init_client(&c,&h,true)==USB_BUSY && h.commands==0 && h.sta==12);
  setup(&h,&c,false); v=(struct dali_usb_vusb){0}; assert(init_client(&c,&h,true)==USB_OK && dali_mt6373_vusb_identify(&c.p)==USB_OK);
  h.reads5=0; h.change_at=3; struct dali_repeater_guard g={&h,quiesce}; struct dali_usb_vusb old=v;
  assert(dali_usb_vusb_init(&v,&c.p,&g,true)==USB_BUSY && !memcmp(&v,&old,sizeof v));
  h.change_at=0; assert(dali_usb_vusb_init(&v,&c.p,&g,false)==USB_UNSUPPORTED && !memcmp(&v,&old,sizeof v));
  assert(dali_usb_vusb_init(&v,&c.p,&g,true)==USB_OK);
  operations=h.operations; h.quiesced=false;
  assert(dali_usb_vusb_acquire(&v)==USB_BUSY && h.operations==operations && v.faulted && !h.writes5);
  assert(dali_usb_vusb_release(&v)==USB_BUSY && v.initialized);
  h.quiesced=true; assert(dali_usb_vusb_release(&v)==USB_OK && !v.initialized);
}
static void changes_and_timeouts(void) {
  struct hub h; struct client c; struct dali_usb_vusb v;
  for(unsigned reg=0;reg<4;reg++) {
    setup(&h,&c,true); memset(&v,0,sizeof v); assert(startup(&h,&c,&v)==USB_OK);
    uint16_t address=reg<2?0x1b95:reg==2?0x1e0c:0x1e0d;
    h.pmic5[address]^=(uint8_t)(reg==1?2:1); // Enable, LP, trim and selector each drift independently.
    assert(dali_usb_vusb_acquire(&v)==USB_BUSY && v.faulted && !v.held && !h.writes5);
    assert(dali_usb_vusb_release(&v)==USB_OK);
  }
  setup(&h,&c,true); memset(&v,0,sizeof v); assert(startup(&h,&c,&v)==USB_OK);
  h.stuck=true; uint8_t out=0xa5;
  assert(dali_mt6373_vusb_read8(&c.p,0x1b95,&out)==USB_TIMEOUT && out==0xa5 && c.p.pending_owned);
  h.quiesced=false; assert(dali_usb_vusb_release(&v)==USB_BUSY && v.initialized);
  h.quiesced=true; h.stuck=false; h.sta=12;
  assert(dali_usb_vusb_release(&v)==USB_OK && !h.reserved && (h.pmic5[0x1b95]&1));
  setup(&h,&c,false); memset(&v,0,sizeof v); assert(init_client(&c,&h,true)==USB_OK && dali_mt6373_vusb_identify(&c.p)==USB_OK);
  struct dali_repeater_guard g={&h,quiesce}; assert(dali_usb_vusb_init(&v,&c.p,&g,true)==USB_OK);
  h.pmic_error=true; assert(dali_usb_vusb_acquire(&v)==USB_IO && !v.held && v.faulted);
  h.pmic_error=false; assert(dali_usb_vusb_release(&v)==USB_OK);
}
int main(void) {
  normal_and_faults(); ownership_and_refusals(); changes_and_timeouts();
  puts("PASS: production SID5 MT6373 VUSB/PMIF always-on lifecycle, SID14 arbitration, identity, stable fields and retained failure models; hardware_qualified=false");
  return 0;
}
