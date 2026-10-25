# MT6991 USB platform source contract

This locator records the fixed public reference source used by the independent
USB implementation. It is not a hardware qualification report. All line numbers
below refer to MiCode `MTK_kernel_device_modules` revision
`e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee`. The reference fetcher records hashes.
The resource graph is extracted from a privately saved live FDT; it contains no
actual clock, efuse, PMIC revision or DMA ownership qualification.

## PMIF and MT6379

MT6991 uses `drivers/spmi/spmi-mtk-pmif-core.c`, multi-path `mt6xxx_pmif_arb`,
channel2 (`:870-880,2432-2434`). The M resources are PMIF `0x1c01a000` and master
`0x1c01c000`; P resources are `0x1c018000` and `0x1c01c800`. FDT route mask
`0x85c0` has SID14 clear, selecting M (`:418-435,2083-2120`). The existing keypad
uses SID4 on the same channel; it is not an MT6379 transport.

| SWINF2 register | Offset |
| --- | --- |
| ACC / WDATA | `0x880` / `0x884` |
| RDATA / VLD_CLR / STA | `0x894` / `0x8a4` / `0x8a8` |

STA state is bits3:1; idle=0, wait-valid-clear=6, error=bit18. Extended-long
SID14 read is `0xce000000 | ((length-1)<<16) | address`; write adds bit29.
The fixed source polls every10us up to100ms (`:661-829`). MT6379 regmap has
16-bit addresses, 8-bit values, max address `0xdff`, max2-byte raw transfers and
an8us write-to-overlapping-access interval (`drivers/mfd/mt6379-spmi.c:24-58,136-150`).
Parent DEV_INFO vendor nibble must equal `0x70` (`drivers/mfd/mt6379.h:15-32`).
The implemented transport does not configure RCS/retrigger or shared interrupts.

## Type-C and VBUS are separate blocks

`drivers/misc/mediatek/typec/tcpc/tcpc_mt6379.c` maps logical TCPCI offsets0–255
to PMIC `0x400+offset`, logical `0x100` bank to PMIC0, and PD2 `0x500` unchanged
(`:473-493`). VID/PID are `0x29cf/0x6379`, DID is read without a fixed value check
(`:2513-2540`). SYSCTRL3 logical `0xb0` software reset writes1 then waits1–2ms.

Standard CC registers are ROLE_CTRL `0x1a`, CC_STATUS `0x1d`, TCPC_CTRL `0x19`.
DRP programs two Rd plus DRP, waits30us, then COMMAND `LOOK_CONNECTION=0x99`;
fixed Rd/Rp and orientation handling are in `:1626-1727`. Full init/filter,
vendor protection and low-power setup are in `:1433-1533`. No PD high-voltage
policy is planned. Vendor MTST1 logical `0x9f` bit5 is VBUS-valid and bit1 is
safe0V (`:1594-1617`); standard VBUS voltage `0x70` uses25mV units (`:2214-2227`).
The scoped Linux probe also requires ADC channels and PMIC IRQ120; a polling
firmware implementation still needs a deliberate alert/mask/ownership policy.

`drivers/power/supply/mt6379-charger.c` owns the external5V source regulator,
separately from TCPC CC sensing. TOP2 `0x108` bit0 enables OTG; voltage selector
starts at `0x11c`, current selector is `0x11e[2:0]` (`mt6379-charger.h:67-95`).
Enable/disable enters TM using passcode bytes `69 96 63 79` at0x07, clears/sets
PP7 `0x135` bit5, exits TM, then sets/clears TOP2 bit0 (`charger.c:444-570`).
The board's4.85–5.50V range must constrain the driver's wider voltage table.
Source enable must be coordinated with CC role, safe0V and USB DMA stop; those
interlocks are not supplied by the raw regulator function.

## USB2 PHY and both repeaters

USB0 is high-speed OTG and references USB2 `u2port0` at `0x16730000`. Its parent
`u2phy0` references NXP I2C repeater@4f followed by MT6379/SID14 repeater@d00
(`arch/arm64/boot/dts/mediatek/xiaomi_mt6991_common.dts:6331-6352,6460-6487`).
Dali enables NXP GPIO72 low/high pinctrl and tunes MT6379 device/host separately
(`dali.dts:603-657`). Both paths must be resolved rather than treating one as a
substitute for the other. NXP init drives low10us then high4ms; its CID check is
`0x3222`. MT6379 uses parent regmap, base `0xd00`, PMIC revision and efuse fields
(`drivers/phy/mediatek/phy-mtk-*-eusb2-repeater.c`). Exact revision/efuse/CID
values are unknown until a permitted runtime qualification.

`drivers/DaliUsb/DaliUsbMt6379Repeater.c` implements the fixed reference E1/E2
and E3+ sequences, including RSV81/82 additive patches, RSV23–27 calibration,
normal power and disconnected E3+ low power. SQD is at `0xd17`, SW eFuse select
is `0xd89[6]`, and PHY select is `0xd89[7]`. These are separate fields. Cached
intr/term calibration contains only its6/4 field bits; upper unrelated bits are
preserved by RMW before offsets are applied. Invalid offsets/results use the
unadjusted field, following the reference fallback. Dali tuning is device
`vrt=2,sq=0,disc=15,pe=5,eq=4,intr+2,term+4`, host
`2,0,13,2,4,+3,-2`, with common FS slew7/hysteresis0.

Probe is read-only and only adopts a low-power state if its complete documented
register snapshot matches. A failed write retains the object and UNKNOWN power;
restart is refused until explicit shutdown/destruction. Low-power shutdown first
wakes then performs full cold-off, leaving the state recognizable by a new owner.
Power-on and each mode change clear D+ pull-up before tuning; DEVICE enables it
explicitly only after its controller is ready. HOST never inherits an attachment.

`DaliUsbNxpRepeater.c` resets low10us/high4ms, reads CID high then low, and changes
only mode bits5:4 of register2. Dali has no nonzero NXP tuning properties. Full
destroy asserts reset after USB quiesce and I2C recovery; partial failures retain
the cleanup context and never replay an uncertain mode write. Its bus requires
real combined/repeated-start byte reads and bounded I2C6/GPIO72 callbacks. Those
platform callbacks are not implemented by this library. Repeater tests execute
production C against PMIC-byte/I2C/GPIO models, with per-operation faults in
power-on, mode, pull-up, idle, wake and shutdown. They do not qualify real PMIF,
I2C, GPIO or USB operation.

`drivers/phy/mediatek/phy-mtk-xsphy.c` initializes/powers/modes the repeater list
in order (`:2512-2520,2570-2583,2604-2618,2643-2654`). USB2 mode sets/clears
DTM1 FORCE_IDDIG/RG_IDDIG, while SoC and repeater calibration apply mode-specific
offsets. SoC register bases include ACR0 `0x300`, ACR6 `0x318`, DTM0/1
`0x368/0x36c`, reserve intr/term `0x330/0x334`, reserve0/1 `0x340/0x344`.
Init, power-on/off and mode sequences are in `:1784-1950`; eFuse and calibration
paths are in `:1492-1512,1693-1749,2047-2111,2338-2456`.

## Clock, power, bus protection and IPPC

| Resource | Fixed reference location |
| --- | --- |
| VLP sys/host clocks, IDs34/35 | `drivers/clk/mediatek/clk-mt6991.c:3378-3385,3885-3898`; VLP config8 `0x90`, mux groups8/16 |
| Frame clock, CKSYS ID36 | same file `:1216-1219,2103-2109`; config10 `0xb0`, group24 |
| USB2/USB3 domains IDs2/3 | `drivers/soc/mediatek/mtk-scpsys-mt6991-spm.c:96-145`; SSUSB_P0 `0xe1c`, DP_PHY_P0 `0xe18` |
| Domain dependency | same file `:612-614`: SSUSB_P0 parent of SSUSB_DP_PHY_P0 |
| Bus protection | syscon `0x1c00d000`, set/clear/enable/status `0xdc/0xe0/0xd8/0x208`; USB0 bit7 |
| AO clock gate / MBIST | `0x16790000` mask `0x13` / `0x16781000+0x34` idle bit0 |

USB domains use ordinary SCPSYS control rather than HWV ops despite the parent
HWV regmap; PWR_ACK/ACK2 are bits30/31, SRAM bit8/ACK12 for SSUSB_P0. Generic
SCPSYS sequencing and bounded polls must be preserved. USB clock muxes use
gate/fence/update-check macros, not simple unguarded selector writes. They depend
on26MHz/PLL parent state; the resource graph does not establish those states.

`drivers/usb/mtu3/mtu3_plat.c` probe attaches domains, resets AO gates, enables
vusb33/clocks/PHY, resets IPPC and requests power before host/gadget init
(`:927-941,1001-1075,1279-1336,1380-1425`). MT6991 HWRSC version2 uses IPPC
`SPM_CTRL_V2=0xe0`, ACK `0xe8`, request bits0,1,2,3,4,6; AO REQ_GATE0..7
`0x70..0x8c` receive mask0xf (`:167-248`). Production code must treat failed
resource ACK as a refusal, not merely log it as the Linux reference does.
Whether configured SPM_V4 callbacks supersede this path remains unresolved.

IPPC stable polling requires STS1 bits10/8/0 plus role-specific reset bits within
20ms, then STS2 bit0 within10ms (`:828-852`). Role selection disables/powers down
the port before changing HOST_SEL/OTG_SEL (`mtu3_dr.c:35-61`, `mtu3_core.c:231-275`).
MBIST idle may wait3s in the Linux reference; timeout cannot authorize role or
power changes while DMA ownership remains uncertain. No physical state from
these registers has been qualified by this offline implementation.
