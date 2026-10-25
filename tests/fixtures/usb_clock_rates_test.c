#include "DaliUsbClockRates.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
enum { N=14 };
static const unsigned bank_id[N]={0,1,1,1,2,2,2,3,4,0,1,1,0,1};
static const uint32_t offset[N]={0x90,0xb0,0xc0,0x10,0x3c,0x254,0x268,0xe00,0x14,0x254,0x1cc,0x1c8,0x3a0,0x274};
static const uint32_t masks[N]={0x818100,0x81000000,0x870000,0x7000000,0xc0,0x73fffff,0x73fffff,4,0x4000000,0xc00,0x90000,0x10000000,0x60000000,0x120000};
struct model;
struct bank { struct model *model; unsigned id; };
struct model { uint32_t reg[N]; unsigned reads, fail, mutate_at, mutate_index; uint32_t mutation; struct bank bank[5]; };
static int rd(void *context,uint32_t reg,uint32_t *value)
{
    struct bank *b=context; struct model *m=b->model;
    ++m->reads;
    if (m->reads==m->fail) return USB_IO;
    if (m->reads==m->mutate_at) m->reg[m->mutate_index]^=m->mutation;
    for (unsigned n=0;n<N;++n) if (b->id==bank_id[n] && reg==offset[n]) { *value=m->reg[n]; return USB_OK; }
    assert(0 && "Unexpected register read"); return USB_RANGE;
}
static int wr(void *context,uint32_t reg,uint32_t value)
{ (void)context; (void)reg; (void)value; assert(0 && "Clock snapshot must never write"); return USB_IO; }
static struct dali_usb_clock_maps setup(struct model *m)
{
    *m=(struct model){0};
    m->reg[0]=0x10100; m->reg[1]=0x1000000; m->reg[2]=2u<<16; m->reg[3]=7u<<24;
    m->reg[4]=0xc0; m->reg[5]=84u<<14; m->reg[6]=96u<<14;
    m->reg[12]=0x60000000; m->reg[13]=0x120000;
    struct usb_mmio maps[5];
    for (unsigned n=0;n<5;++n) {
        m->bank[n]=(struct bank){m,n};
        maps[n]=(struct usb_mmio){.context=&m->bank[n],.size=0x1000,.read32=rd,.write32=wr};
    }
    return (struct dali_usb_clock_maps){maps[0],maps[1],maps[2],maps[3],maps[4]};
}
static int run(struct model *m,struct dali_usb_clock_maps *maps,struct dali_usb_clock_rates *out)
{ m->reads=0; return dali_usb_clock_rates_read(maps,1,26000000,520000000,true,out); }
static void reject(struct model *m,struct dali_usb_clock_maps *maps,int status)
{
    struct dali_usb_clock_rates out,before; memset(&out,0xa5,sizeof out); memcpy(&before,&out,sizeof out);
    assert(run(m,maps,&out)==status); assert(!memcmp(&out,&before,sizeof out));
}
static void reject_args(struct dali_usb_clock_maps *maps,uint32_t version,uint32_t ref,uint32_t osc,bool qualified,int status)
{
    struct dali_usb_clock_rates out,before; memset(&out,0xa5,sizeof out); memcpy(&before,&out,sizeof out);
    assert(dali_usb_clock_rates_read(maps,version,ref,osc,qualified,&out)==status);
    assert(!memcmp(&out,&before,sizeof out));
}
int main(void)
{
    struct model m; struct dali_usb_clock_maps maps=setup(&m); struct dali_usb_clock_rates out;
    assert(run(&m,&maps,&out)==USB_OK && m.reads==2*N && out.enabled==31);
    assert(out.hz[0]==242666666 && out.hz[1]==242666666 && out.hz[2]==48000000 && out.hz[3]==124800000 && out.hz[4]==156000000);
    const uint32_t west[]={26000000,68250000,124800000,136500000,249600000};
    const uint32_t axi[]={26000000,39000000,54600000,65000000,78000000,109200000,136500000,156000000};
    for (unsigned sys=0;sys<2;++sys) for (unsigned host=0;host<2;++host) for (unsigned frame=0;frame<2;++frame)
    for (unsigned w=0;w<5;++w) for (unsigned a=0;a<8;++a) {
        maps=setup(&m); m.reg[0]=(sys<<8)|(host<<16); m.reg[1]=frame<<24; m.reg[2]=w<<16; m.reg[3]=a<<24;
        assert(run(&m,&maps,&out)==USB_OK && out.enabled==31);
        assert(out.hz[0]==(sys ? 242666666u : 26000000u) && out.parent[0]==sys);
        assert(out.hz[1]==(host ? 242666666u : 26000000u) && out.parent[1]==host);
        assert(out.hz[2]==(frame ? 48000000u : 26000000u) && out.parent[2]==frame);
        assert(out.hz[3]==west[w] && out.parent[3]==w && out.hz[4]==axi[a] && out.parent[4]==a);
    }
    for (unsigned gates=0;gates<32;++gates) {
        maps=setup(&m);
        if (gates&1) m.reg[0]|=0x8000;
        if (gates&2) m.reg[0]|=0x800000;
        if (gates&4) m.reg[1]|=0x80000000;
        if (gates&8) m.reg[7]|=4;
        if (gates&16) m.reg[8]|=0x4000000;
        assert(run(&m,&maps,&out)==USB_OK && out.enabled==(31u^gates));
    }
    maps=setup(&m); m.reg[2]|=0x800000; m.reg[13]&=~0x20000u;
    assert(run(&m,&maps,&out)==USB_OK && out.enabled==23); /* Root gate dominates I2C main. */
    maps=setup(&m); m.reg[5]=(1u<<24)|(84u<<14)|1u; m.reg[2]=1u<<16;
    assert(run(&m,&maps,&out)==USB_OK && out.hz[0]==121333421 && out.hz[3]==34125024 && out.hz[4]==78000056);
    for (unsigned post=0;post<8;++post) {
        maps=setup(&m); m.reg[5]|=post<<24; m.reg[6]|=post<<24;
        assert(run(&m,&maps,&out)==USB_OK);
        assert(out.hz[0]==(2184000000u>>post)/9 && out.hz[2]==(2496000000u>>post)/52);
    }
    for (unsigned n=0;n<2*N;++n) { maps=setup(&m); m.fail=n+1; reject(&m,&maps,USB_IO); }
    for (unsigned n=0;n<N;++n) {
        maps=setup(&m); m.mutate_index=n; m.mutate_at=N+n+1; m.mutation=masks[n]&(~masks[n]+1u); reject(&m,&maps,USB_BUSY);
        maps=setup(&m); m.mutate_index=n; m.mutate_at=N+n+1; m.mutation=~masks[n]&(~(~masks[n])+1u);
        assert(run(&m,&maps,&out)==USB_OK); /* Unrelated fields never enter equality checks. */
    }
    for (unsigned n=9;n<12;++n) { maps=setup(&m); m.reg[n]=masks[n]; reject(&m,&maps,USB_BUSY); }
    for (unsigned n=0;n<4;++n) {
        maps=setup(&m); const unsigned index[]={12,12,13,13}; const uint32_t bits[]={0x40000000,0x20000000,0x100000,0x20000};
        m.reg[index[n]]&=~bits[n]; reject(&m,&maps,USB_BUSY);
    }
    for (unsigned w=5;w<8;++w) { maps=setup(&m); m.reg[2]=w<<16; reject(&m,&maps,USB_UNSUPPORTED); }
    maps=setup(&m); m.reg[4]=0x40; reject(&m,&maps,USB_BUSY); /* Main missing, univ prepared. */
    maps=setup(&m); m.reg[4]=0x80; reject(&m,&maps,USB_BUSY); /* Univ missing, main prepared. */
    maps=setup(&m); m.reg[0]=m.reg[1]=m.reg[2]=m.reg[3]=m.reg[4]=m.reg[5]=m.reg[6]=0;
    assert(run(&m,&maps,&out)==USB_OK); /* Unselected PLLs need no preparation. */
    for (unsigned pll=5;pll<7;++pll) for (unsigned bad=0;bad<2;++bad) {
        maps=setup(&m); m.reg[pll]=bad ? 0x3fffff : 0; reject(&m,&maps,USB_RANGE);
    }
    /* Adjacent representable PCWs bracket the VCO limits; neither limit is an
     * exact 26MHz/2^14 multiple. Values here are independent precomputed Hz. */
    const uint32_t pcw[]={945230,945231,2394584,2394585};
    const uint32_t vco[]={1499998780,1500000367,3799999024,3800000611};
    for (unsigned pll=5;pll<7;++pll) for (unsigned n=0;n<4;++n) {
        maps=setup(&m); m.reg[pll]=pcw[n];
        if (n==0 || n==3) reject(&m,&maps,USB_RANGE);
        else {
            assert(run(&m,&maps,&out)==USB_OK);
            assert(out.hz[pll==5 ? DALI_CLOCK_SYS : DALI_CLOCK_FRAME]==vco[n]/(pll==5 ? 9u : 52u));
        }
    }
    maps=setup(&m); m.reg[3]=3u<<24;
    reject_args(&maps,1,26000000,0,true,USB_UNSUPPORTED);
    reject_args(&maps,1,26000000,7,true,USB_UNSUPPORTED);
    assert(dali_usb_clock_rates_read(&maps,1,26000000,8,true,&out)==USB_OK && out.hz[4]==1);
    assert(dali_usb_clock_rates_read(&maps,1,26000000,UINT32_MAX,true,&out)==USB_OK && out.hz[4]==536870911);
    maps=setup(&m);
    assert(dali_usb_clock_rates_read(&maps,0,26000000,0,true,&out)==USB_OK); /* A0 same fields. */
    m.reads=0;
    reject_args(&maps,2,26000000,0,true,USB_UNSUPPORTED);
    reject_args(&maps,1,0,0,true,USB_UNSUPPORTED);
    reject_args(&maps,1,26000000,0,false,USB_UNSUPPORTED); assert(!m.reads);
    struct usb_mmio *banks[]={&maps.vlp,&maps.top,&maps.apmixed,&maps.impw,&maps.perao};
    const uint32_t sizes[]={0x3a4,0x278,0x26c,0xe04,0x18};
    for (unsigned n=0;n<5;++n) {
        banks[n]->size=sizes[n]-1; reject(&m,&maps,USB_RANGE); assert(!m.reads); banks[n]->size=0x1000;
        banks[n]->read32=NULL; reject(&m,&maps,USB_INVALID); banks[n]->read32=rd;
        banks[n]->write32=NULL; assert(run(&m,&maps,&out)==USB_OK);
    }
    assert(dali_usb_clock_rates_read(NULL,1,26000000,0,true,&out)==USB_INVALID);
    assert(dali_usb_clock_rates_read(&maps,1,26000000,0,true,NULL)==USB_INVALID);
    puts("PASS: production MT6991 selected clock rates, PLL rounding, gates and changing/read-error snapshots; register model only");
    return 0;
}
