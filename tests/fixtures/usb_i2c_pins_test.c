#include "DaliUsbI2cPins.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct model { uint32_t regs[9], flip; unsigned reads, barriers, fail_at, change_at, changed; };
struct bank { struct model *m; bool gpio; };
static const uint32_t off[9]={0x3d0,0x20,0x50,0xc0,0x90,0x80,0,0x10,0xb0};
static const uint32_t mask[9]={0xff,(7u<<6)|(7u<<15),(1u<<11)|(1u<<14),(1u<<8)|(1u<<11),
 (1u<<11)|(1u<<14),(1u<<11)|(1u<<14),7u<<24,7u<<3,(7u<<6)|(7u<<15)};
static int read32(void *ctx,uint32_t offset,uint32_t *out) {
    struct bank *b=ctx; struct model *m=b->m; unsigned index=9;
    for(unsigned n=0;n<9;n++) if((n==0)==b->gpio && off[n]==offset) { index=n; break; }
    assert(index<9);
    if(++m->reads==m->fail_at) return USB_IO;
    *out=m->regs[index];
    if(m->reads==m->change_at) m->regs[m->changed]^=m->flip;
    return USB_OK;
}
static int write32(void *ctx,uint32_t offset,uint32_t value) { (void)ctx;(void)offset;(void)value; assert(0); return USB_IO; }
static void barrier(void *ctx) { ((struct bank *)ctx)->m->barriers++; }
static void setup(struct model *m) {
    *m=(struct model){0};
    const uint32_t values[9]={0x11,(3u<<6)|(5u<<15),1u<<11,1u<<11,1u<<11,1u<<14,6u<<24,2u<<3,(4u<<6)|(7u<<15)};
    for(unsigned n=0;n<9;n++) m->regs[n]=(0xa5a5a5a5u&~mask[n])|values[n];
}
static int sample(struct model *m,struct dali_i2c6_pin_snapshot *out) {
    struct bank g={m,true},i={m,false};
    struct usb_mmio gpio={&g,0x1000,read32,write32,barrier,NULL},iocfg={&i,0x1000,read32,write32,barrier,NULL};
    return dali_usb_i2c6_pins_read(&gpio,&iocfg,out);
}
static void preserved(struct model *m,int expected) {
    struct dali_i2c6_pin_snapshot out,old; memset(&out,0xa5,sizeof out); old=out;
    assert(sample(m,&out)==expected && !memcmp(&out,&old,sizeof out));
}
int main(void) {
    struct model m; setup(&m); struct dali_i2c6_pin_snapshot out;
    assert(sample(&m,&out)==USB_OK && m.reads==18 && m.barriers==8);
    const struct dali_i2c6_pin_state scl={1,3,1,0,1,0,6,4},sda={1,5,0,1,0,1,2,7};
    assert(!memcmp(&out.scl,&scl,sizeof scl) && !memcmp(&out.sda,&sda,sizeof sda));
    // All EH drive combinations: only the selector LSB is mandatory.
    for(unsigned a=0;a<8;a++) for(unsigned b=0;b<8;b++) {
        setup(&m); m.regs[1]=(m.regs[1]&~mask[1])|(a<<6)|(b<<15);
        if((a&b&1)==0) preserved(&m,USB_UNSUPPORTED);
        else { assert(sample(&m,&out)==USB_OK && out.scl.enhanced_drive==a && out.sda.enhanced_drive==b); }
    }
    for(unsigned mode=0;mode<256;mode++) {
        setup(&m); m.regs[0]=(m.regs[0]&~255u)|mode;
        if(mode==0x11) assert(sample(&m,&out)==USB_OK); else preserved(&m,USB_UNSUPPORTED);
    }
    // Do not impose an unsupported electrical policy. Report both pins' fields.
    for(unsigned n=2;n<=5;n++) for(unsigned value=0;value<4;value++) {
        setup(&m); unsigned low=n==3?8:11,high=n==3?11:14;
        m.regs[n]=(m.regs[n]&~mask[n])|((value&1)<<low)|(((value>>1)&1)<<high);
        assert(sample(&m,&out)==USB_OK);
        if(n==2) assert(out.scl.input_enable==(value&1) && out.sda.input_enable==(value>>1));
        if(n==3) assert(out.scl.schmitt==(value&1) && out.sda.schmitt==(value>>1));
        if(n==4) assert(out.scl.pull_up==(value&1) && out.sda.pull_up==(value>>1));
        if(n==5) assert(out.scl.pull_down==(value&1) && out.sda.pull_down==(value>>1));
    }
    for(unsigned value=0;value<8;value++) {
        setup(&m); m.regs[6]=(m.regs[6]&~mask[6])|(value<<24); m.regs[7]=(m.regs[7]&~mask[7])|((7-value)<<3);
        m.regs[8]=(m.regs[8]&~mask[8])|(value<<6)|((7-value)<<15);
        assert(sample(&m,&out)==USB_OK && out.scl.drive==value && out.sda.drive==7-value &&
            out.scl.resistance==value && out.sda.resistance==7-value);
    }
    for(unsigned n=1;n<=18;n++) { setup(&m); m.fail_at=n; preserved(&m,USB_IO); assert(m.reads==n); }
    unsigned changed_bits=0;
    for(unsigned n=0;n<9;n++) for(unsigned bit=0;bit<32;bit++) if(mask[n]&(1u<<bit)) {
        setup(&m); m.change_at=9; m.changed=n; m.flip=1u<<bit; preserved(&m,USB_BUSY); changed_bits++;
    }
    for(unsigned n=0;n<9;n++) { setup(&m); m.change_at=9; m.changed=n; m.flip=~mask[n]; assert(sample(&m,&out)==USB_OK); }
    setup(&m); struct bank g={&m,true},i={&m,false};
    struct usb_mmio gpio={&g,0x3d3,read32,write32,barrier,NULL},iocfg={&i,0x1000,read32,write32,barrier,NULL};
    assert(dali_usb_i2c6_pins_read(&gpio,&iocfg,&out)==USB_RANGE && !m.reads);
    gpio.size=0x1000; iocfg.size=0xc3;
    assert(dali_usb_i2c6_pins_read(&gpio,&iocfg,&out)==USB_RANGE && !m.reads);
    iocfg.size=0x1000; gpio.barrier=NULL;
    assert(dali_usb_i2c6_pins_read(&gpio,&iocfg,&out)==USB_INVALID && !m.reads);
    assert(dali_usb_i2c6_pins_read(NULL,&iocfg,&out)==USB_INVALID && dali_usb_i2c6_pins_read(&gpio,&iocfg,NULL)==USB_INVALID);
    printf("PASS: actual I2C6 pin fields,256 mux/64 EH combinations,%u independent masked changes,18 read errors; no electrical/hardware qualification\n",changed_bits);
    return 0;
}
