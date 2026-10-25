#include "DaliUsbI2cClocks.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct gates { uint32_t status[2]; unsigned ops,fail_at,writes,time; bool applied,quiesced,drop; };
struct map { struct gates *g; unsigned n; };
static const uint32_t sta[]={0xe00,0x14},set[]={0xe08,0x2c},clr[]={0xe04,0x30},mask[]={4,1u<<26};
static int read32(void *ctx,uint32_t offset,uint32_t *out) {
  struct map *m=ctx; assert(offset==sta[m->n]);
  if(++m->g->ops==m->g->fail_at) return USB_IO;
  *out=m->g->status[m->n]; return USB_OK;
}
static int write32(void *ctx,uint32_t offset,uint32_t value) {
  struct map *m=ctx; struct gates *g=m->g;
  assert((offset==set[m->n] || offset==clr[m->n]) && value==mask[m->n]); g->writes++;
  bool failing=++g->ops==g->fail_at;
  if((!failing || g->applied) && !g->drop) {
    if(offset==set[m->n]) g->status[m->n]|=value; else g->status[m->n]&=~value;
  }
  return failing?USB_IO:USB_OK;
}
static void barrier(void *ctx) { (void)ctx; }
static void delay(void *ctx,uint32_t us) { ((struct map *)ctx)->g->time+=us; }
static int quiesce(void *ctx) { return ((struct gates *)ctx)->quiesced?USB_OK:USB_BUSY; }
static void setup(struct gates *g,unsigned initial) {
  *g=(struct gates){.quiesced=true};
  for(unsigned n=0;n<2;n++) g->status[n]=(0xa5a5a5a5u&~mask[n])|((initial&(1u<<n))?mask[n]:0);
}
static int init(struct gates *g,struct dali_i2c_clocks *c,struct map m[2]) {
  m[0]=(struct map){g,0};m[1]=(struct map){g,1};
  struct usb_mmio a={m,0x1000,read32,write32,barrier,delay},b={m+1,0x1000,read32,write32,barrier,delay};
  struct dali_repeater_guard guard={g,quiesce};
  return dali_i2c_clocks_init(c,&a,&b,&guard,true);
}
static unsigned enabled(struct gates *g) {
  return ((g->status[0]&4)?0u:1u)|((g->status[1]&mask[1])?0u:2u);
}
int main(void) {
  struct gates g; struct map maps[2]; struct dali_i2c_clocks c;
  for(unsigned initial=0;initial<4;initial++) {
    setup(&g,initial); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK && !g.writes);
    assert(dali_i2c_clocks_enable(&c)==USB_OK && c.on && c.owned==initial && enabled(&g)==3);
    unsigned writes=g.writes; assert(dali_i2c_clocks_enable(&c)==USB_OK && g.writes==writes);
    assert(dali_i2c_clocks_disable(&c)==USB_OK && !c.owned && enabled(&g)==(initial^3));
    assert(dali_i2c_clocks_enable(&c)==USB_OK && c.on && enabled(&g)==3);
    assert(dali_i2c_clocks_destroy(&c)==USB_OK && !c.initialized && enabled(&g)==(initial^3));
    for(unsigned n=0;n<2;n++) assert((g.status[n]&~mask[n])==(0xa5a5a5a5u&~mask[n]));
  }
  setup(&g,3); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK && dali_i2c_clocks_enable(&c)==USB_OK);
  unsigned startup=g.ops;
  for(unsigned at=1;at<=startup;at++) for(unsigned applied=0;applied<2;applied++) {
    setup(&g,3); memset(&c,0,sizeof c); g.fail_at=at;g.applied=applied!=0;
    int result=init(&g,&c,maps); if(!result) result=dali_i2c_clocks_enable(&c);
    assert(result==USB_IO && !c.on && g.ops==at);
    if(c.initialized) {
      unsigned operations=g.ops; assert(dali_i2c_clocks_enable(&c)==USB_IO && g.ops==operations);
      g.fail_at=0;assert(dali_i2c_clocks_destroy(&c)==USB_OK);
    }
    assert(enabled(&g)==0);
  }
  setup(&g,3); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK && dali_i2c_clocks_enable(&c)==USB_OK);
  unsigned base=g.ops; assert(dali_i2c_clocks_destroy(&c)==USB_OK); unsigned cleanup=g.ops-base;
  for(unsigned at=1;at<=cleanup;at++) for(unsigned applied=0;applied<2;applied++) {
    setup(&g,3); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK && dali_i2c_clocks_enable(&c)==USB_OK);
    g.fail_at=g.ops+at; g.applied=applied!=0; assert(dali_i2c_clocks_destroy(&c)==USB_IO && c.initialized && c.faulted);
    g.fail_at=0; assert(dali_i2c_clocks_destroy(&c)==USB_OK && enabled(&g)==0);
  }
  setup(&g,0); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK && dali_i2c_clocks_enable(&c)==USB_OK);
  g.status[1]|=mask[1]; unsigned writes=g.writes;
  assert(dali_i2c_clocks_destroy(&c)==USB_BUSY && c.initialized && g.writes==writes);
  g.status[1]&=~mask[1]; assert(dali_i2c_clocks_destroy(&c)==USB_OK);
  setup(&g,3); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK); g.drop=true;
  assert(dali_i2c_clocks_enable(&c)==USB_TIMEOUT && c.owned==1 && g.time==100);
  g.drop=false; assert(dali_i2c_clocks_destroy(&c)==USB_OK && enabled(&g)==0);
  setup(&g,3); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK && dali_i2c_clocks_enable(&c)==USB_OK); g.drop=true;
  assert(dali_i2c_clocks_destroy(&c)==USB_TIMEOUT && c.initialized && c.owned==3);
  g.drop=false; assert(dali_i2c_clocks_destroy(&c)==USB_OK && enabled(&g)==0);
  setup(&g,3); memset(&c,0,sizeof c); assert(init(&g,&c,maps)==USB_OK); g.quiesced=false; unsigned ops=g.ops;
  assert(dali_i2c_clocks_enable(&c)==USB_BUSY && c.faulted && g.ops==ops);
  assert(dali_i2c_clocks_destroy(&c)==USB_BUSY && c.initialized && g.ops==ops);
  g.quiesced=true; assert(dali_i2c_clocks_destroy(&c)==USB_OK);
  printf("PASS: actual I2C6/APDMA shared setclr gates,4 inherited combinations,%u startup/%u cleanup MMIO fault positions before/applied effects; hardware_qualified=false\n",startup,cleanup);
  return 0;
}
