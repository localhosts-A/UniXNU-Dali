/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed device_modules e45c319: clk-mt6991.c, clk-pll.c, clk-mt6991-peri.c.
 * USB0 sys/host are VLP mainpll/9, NOT TOP USB1 univpll/20. */
#include "DaliUsbClockRates.h"
enum { VLP_CFG, FRAME_CFG, I2C_CFG, AXI_CFG, PLL_FENCE, MAIN_PCW, UNIV_PCW,
    I2C_GATE, DMA_GATE, VLP_CKSTA, TOP_CKSTA1, TOP_CKSTA0,
    VLP_FENCE, TOP_FENCE, SNAP_COUNT };
struct field { uint8_t bank; uint32_t reg, mask; };
static const struct field fields[SNAP_COUNT]={
    {0,0x90,0x818100}, {1,0xb0,0x81000000}, {1,0xc0,0x870000},
    {1,0x10,0x7000000}, {2,0x3c,0xc0}, {2,0x254,0x73fffff},
    {2,0x268,0x73fffff}, {3,0xe00,4}, {4,0x14,0x4000000},
    {0,0x254,0xc00}, {1,0x1cc,0x90000}, {1,0x1c8,0x10000000},
    {0,0x3a0,0x60000000}, {1,0x274,0x120000}
};
static int capture(const struct usb_mmio *const banks[5], uint32_t out[SNAP_COUNT])
{
    for (unsigned n=0;n<SNAP_COUNT;++n) {
        int r=usb_mmio_read(banks[fields[n].bank],fields[n].reg,&out[n]);
        if (r) return r;
        out[n] &= fields[n].mask;
    }
    return USB_OK;
}
static int pll_rate(uint32_t word, bool prepared, uint32_t *hz)
{
    if (!prepared) return USB_BUSY;
    /* 22 PCW bits, 8 integer bits. Match vendor's ceil VCO then ceil postdiv. */
    uint64_t vco=((uint64_t)26000000*(word & 0x3fffff)+16383)/16384;
    if (vco<1500000000 || vco>3800000000ULL) return USB_RANGE;
    uint32_t divisor=1u<<((word>>24)&7);
    *hz=(uint32_t)((vco+divisor-1)/divisor);
    return USB_OK;
}
static int source(const uint32_t s[SNAP_COUNT], unsigned pll, uint32_t divisor, uint32_t *hz)
{
    uint32_t rate;
    int r=pll_rate(s[pll ? UNIV_PCW : MAIN_PCW],(s[PLL_FENCE] & (pll ? 0x40u : 0x80u)) != 0,&rate);
    if (!r) *hz=rate/divisor; /* CCF fixed-factor truncation, after PLL rounding. */
    return r;
}
int dali_usb_clock_rates_read(const struct dali_usb_clock_maps *maps, uint32_t sw_ver,
    uint32_t reference_hz, uint32_t ulposc_hz, bool qualified,
    struct dali_usb_clock_rates *out)
{
    if (!maps || !out) return USB_INVALID;
    if (!qualified || sw_ver>1 || reference_hz!=26000000) return USB_UNSUPPORTED;
    const struct usb_mmio *banks[]={&maps->vlp,&maps->top,&maps->apmixed,&maps->impw,&maps->perao};
    const uint32_t sizes[]={0x3a4,0x278,0x26c,0xe04,0x18};
    for (unsigned n=0;n<5;++n) {
        if (!banks[n]->read32) return USB_INVALID;
        if (banks[n]->size<sizes[n]) return USB_RANGE;
    }
    uint32_t s[SNAP_COUNT], check[SNAP_COUNT];
    int r=capture(banks,s); if (r) return r;
    r=capture(banks,check); if (r) return r;
    for (unsigned n=0;n<SNAP_COUNT;++n) if (s[n]!=check[n]) return USB_BUSY;
    /* Vendor set_parent waits these CKSTA bits clear after issuing UPDATE.
     * They are busy status, not the separate write-command UPDATE registers. */
    if (s[VLP_CKSTA] || s[TOP_CKSTA1] || s[TOP_CKSTA0]) return USB_BUSY;
    struct dali_usb_clock_rates value={0};
    value.parent[DALI_CLOCK_SYS]=(uint8_t)((s[VLP_CFG]>>8)&1);
    value.parent[DALI_CLOCK_HOST]=(uint8_t)((s[VLP_CFG]>>16)&1);
    value.parent[DALI_CLOCK_FRAME]=(uint8_t)((s[FRAME_CFG]>>24)&1);
    value.parent[DALI_CLOCK_I2C6]=(uint8_t)((s[I2C_CFG]>>16)&7);
    value.parent[DALI_CLOCK_I2C_DMA]=(uint8_t)((s[AXI_CFG]>>24)&7);
    if (value.parent[DALI_CLOCK_I2C6]>4) return USB_UNSUPPORTED;
    for (unsigned n=0;n<3;++n) {
        value.hz[n]=reference_hz;
        if (value.parent[n]) {
            r=source(s,n==DALI_CLOCK_FRAME ? 1u : 0u,n==DALI_CLOCK_FRAME ? 52u : 9u,&value.hz[n]);
            if (r) return r;
        }
    }
    unsigned west=value.parent[DALI_CLOCK_I2C6], axi=value.parent[DALI_CLOCK_I2C_DMA];
    const uint32_t west_div[]={1,32,20,16,10}, axi_div[]={1,56,40,8,28,20,16,14};
    value.hz[DALI_CLOCK_I2C6]=reference_hz;
    if (west) { r=source(s,(west==2 || west==4) ? 1u : 0u,west_div[west],&value.hz[DALI_CLOCK_I2C6]); if (r) return r; }
    value.hz[DALI_CLOCK_I2C_DMA]=reference_hz;
    if (axi==3) {
        if (ulposc_hz<8) return USB_UNSUPPORTED;
        value.hz[DALI_CLOCK_I2C_DMA]=ulposc_hz/8;
    } else if (axi) { r=source(s,0,axi_div[axi],&value.hz[DALI_CLOCK_I2C_DMA]); if (r) return r; }
    const bool enabled[]={!(s[VLP_CFG]&0x8000),!(s[VLP_CFG]&0x800000),
        !(s[FRAME_CFG]&0x80000000),!(s[I2C_CFG]&0x800000) && !s[I2C_GATE],!s[DMA_GATE]};
    if ((enabled[0] && !(s[VLP_FENCE]&0x40000000)) ||
        (enabled[1] && !(s[VLP_FENCE]&0x20000000)) ||
        (enabled[2] && !(s[TOP_FENCE]&0x100000)) ||
        (!(s[I2C_CFG]&0x800000) && !(s[TOP_FENCE]&0x20000))) return USB_BUSY;
    for (unsigned n=0;n<DALI_CLOCK_COUNT;++n) if (enabled[n]) value.enabled |= (uint8_t)(1u<<n);
    *out=value;
    return USB_OK;
}
