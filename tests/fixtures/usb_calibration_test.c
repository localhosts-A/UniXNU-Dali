#include "DaliUsbCalibration.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static void put(uint8_t *p, uint32_t n)
{ for (unsigned i=0;i<4;++i) p[i]=(uint8_t)(n>>(8*i)); }
static void rejected(const uint8_t *chip, size_t chip_size, const uint8_t *dev, size_t dev_size, int expected)
{
    struct dali_xsphy_calibration out, before;
    memset(&out,0xa5,sizeof out); memcpy(&before,&out,sizeof out);
    assert(dali_usb_calibration_decode(chip,chip_size,dev,dev_size,&out) == expected);
    assert(!memcmp(&out,&before,sizeof out));
}
int main(void)
{
    uint8_t chip_store[21]={0}, info_store[1605]={0};
    uint8_t *chip=chip_store+1, *dev=info_store+1; /* Unaligned properties are safe. */
    put(chip,24); put(chip+4,0x1357); put(chip+8,0x8a00); put(chip+12,0xca01);
    for (unsigned version=0;version<2;++version) for (unsigned words=119;words<=400;++words) {
        put(chip+16,version); put(dev,words);
        for (unsigned n=0;n<words;++n) put(dev+4+4*n,0x87960000u+n*337);
        struct dali_xsphy_calibration out={0};
        assert(dali_usb_calibration_decode(chip,20,dev,4+4*words,&out) == USB_OK && out.qualified && out.sw_ver == version);
        assert(out.efuse_1d4 == 0x87960000u+117*337 && out.efuse_1d8 == 0x87960000u+118*337);
    }
    put(dev,320); put(chip+16,1);
    for (size_t n=0;n<28;++n) if (n != 20) rejected(chip,n,dev,1284,USB_RANGE); /* Legacy size24 is not the property length. */
    for (size_t n=0;n<=1604;++n) if (n != 1284) rejected(chip,20,dev,n,USB_RANGE);
    const uint32_t bad[]={0,118,401,0xffffffff};
    for (unsigned n=0;n<4;++n) { put(dev,bad[n]); rejected(chip,20,dev,1284,USB_RANGE); }
    put(dev,320);
    put(chip,20); rejected(chip,20,dev,1284,USB_UNSUPPORTED); put(chip,24);
    put(chip+4,0x6991); rejected(chip,20,dev,1284,USB_UNSUPPORTED); put(chip+4,0x1357);
    put(chip+16,2); rejected(chip,20,dev,1284,USB_UNSUPPORTED); put(chip+16,1);
    put(chip,0x18000000); rejected(chip,20,dev,1284,USB_UNSUPPORTED); put(chip,24);
    rejected(NULL,20,dev,1284,USB_INVALID); rejected(chip,20,NULL,1284,USB_INVALID);
    assert(dali_usb_calibration_decode(chip,20,dev,1284,NULL) == USB_INVALID);
    memset(dev+4,0,1280);
    struct dali_xsphy_calibration zero={0};
    assert(dali_usb_calibration_decode(chip,20,dev,1284,&zero) == USB_OK && zero.qualified && !zero.efuse_1d4 && !zero.efuse_1d8);
    puts("PASS: production Dali ATAG calibration decoder; bounded LE properties, no hardware qualification");
    return 0;
}
