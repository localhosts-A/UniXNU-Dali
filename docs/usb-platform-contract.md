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
register backends are implemented separately below; installed resource-provider
callbacks remain pending. Repeater tests execute
production C against PMIC-byte/I2C/GPIO models, with per-operation faults in
power-on, mode, pull-up, idle, wake and shutdown. They do not qualify real PMIF,
I2C, GPIO or USB operation.

The separate `DaliUsbGpio.c` implements actual MT6991 GPIO72 MMIO access using
the fixed pinctrl field tables. MODE is `0x390[3:0]`; DIR/DO/DI are
`0x20/0x120/0x220[8]` relative to GPIO base `0x1002d000`
(`pinctrl-mt6991.c:40-110`). `FLAG_RACE_FREE_ACCESS` selects SET/CLR at+4/+8;
mode uses MWR+0xc (`pinctrl-mtk-common-v2.c:22-35,100-129,245-292`). This backend
only accepts inherited GPIO mode0 and never writes MODE. After USB quiesce it
writes DO SET/CLR before DIR SET, then reads DO/DIR/MODE back. Other pin fields
are untouched. Failed accesses retain context and unknown output; reset-low
may be retried but enable stays refused after a fault. Destroy confirms reset
low before clearing context. The actual GPIO/NXP combined fixture still models
MMIO and I2C: it proves code order and neighboring-bit protection, not pad voltage.

## I2C6 FIFO backend and reference locator

The original sparse reference did not expand I2C/pinctrl; this was a checkout
omission rather than missing source. Both directories now belong to the pinned
source inventory and required-file hashes. Existing isolated checkouts can be
expanded using the manifest's complete `paths` with `git sparse-checkout set
--no-cone --stdin`, after confirming the exact HEAD and clean state, then rerun
the fetcher to regenerate and verify its source manifest. No original tree is used.

`drivers/i2c/busses/i2c-mt65xx.c:725-756` explicitly matches MT6991, v2 registers,
16-byte FIFO, 32-bit register accesses, byte-wide DATA_PORT, APDMA sync, speed_v2
and separate slave-address/aux-length registers. WRRD optimization for equal
slave write/read messages clears auto_restart (`:2353-2362`); the combined
transaction uses CONTROL DIR_CHANGE/RS, two lengths and transaction-count2.
The intended NXP requests are1+1-byte WRRD reads and2-byte writes; neither needs
DMA. Polling masks interrupts only on its explicit FIFO path (`:1979-2007`).

The implementation derives from complete transfer/IRQ `:1838-2492`,
register/accessor definitions `:155-292,767-866`, init/clock `:868-1104`,
speed_v2 calculation/selection `:1352-1607` and DT/probe `:2508-2559,2681-2793`.
The saved FDT gives400kHz, clock-div1, main/dma clocks, SPI343, controller
`0x13170000/0x20000` and APDMA `0x16410000/0x10000`; it does not prove actual
clock rate, inherited timing, stopped DMA or ownership. Generic Linux MT65xx
similarity alone cannot qualify this newer vendor register version.

`DaliUsbI2c.c` now implements the fixed4f slave's byte-register FIFO transport,
using native byte DATA_PORT operations, v2 slave-address register, combined
WRRD/repeated-start reads and two-byte writes. The vendor speed_v2 factor/duty
search runs with compensation disabled to keep the bus at or below400kHz;
it requires a proven stable parent rate rather than assuming130MHz. Completion
wait is bounded5ms plus at most100us for START to clear. The controller remains
polling-only with interrupts masked. APDMA-v2 local handshake reset and its
reference0x1000 sentinel are reproduced, but no data DMA is enabled or mapped.

Init checks active DMA, shadow/channel mode, START, pending interrupts and FIFO
before changing registers. Transfers repeat these idle checks, refuse foreign
state without clearing it, and mark ownership before configuring/starting their
own work. Post-configuration errors latch a fault and retain dirty/pending state;
failed reads never publish bytes and uncertain writes are never replayed. Only
explicit USB-quiesced owned recovery may reset the channel; active foreign DMA
still refuses recovery. A recovered fault stays latched until destroy/reinit.

`dali_nxp_io_bind` joins the actual I2C6 and GPIO72 implementations to the NXP
bus API. Its combined production-C fixture covers CID/mode/reset, normal and
fault teardown, per-operation initialization/transfer failures, timeout, NACK,
arbitration/config errors, short reads, late errors, applied-but-error START and
runtime foreign-state rejection. These are register models. The installed
UEFI/XNU resource provider, validated FDT/clocks/pinmux and exclusive-controller
qualification are still required before any physical access.

`drivers/phy/mediatek/phy-mtk-xsphy.c` initializes/powers/modes the repeater list
in order (`:2512-2520,2570-2583,2604-2618,2643-2654`). USB2 mode sets/clears
DTM1 FORCE_IDDIG/RG_IDDIG, while SoC and repeater calibration apply mode-specific
offsets. SoC register bases include ACR0 `0x300`, ACR6 `0x318`, DTM0/1
`0x368/0x36c`, reserve intr/term `0x330/0x334`, reserve0/1 `0x340/0x344`.
Init, power-on/off and mode sequences are in `:1784-1950`; eFuse and calibration
paths are in `:1492-1512,1693-1749,2047-2111,2338-2456`.

`DaliUsbPhy.c` composes the actual NXP and MT6379 implementations with SoC USB2
PHY0. Read-only init captures intr/term field bits from330/334 and refuses an
inherited active meter. The qualified provider must supply real chipid.sw_ver
and nvmem words1d4/1d8; masks f00000/7c00/f8000 produce HSRX term and TX N/P
calibration. Zero acquired efuse fields preserve hardware values. Missing
acquisition or unknown clocks never imply zero/default calibration.
The native read-only acquisition path is now implemented below; installed
controller use and the reference-clock qualification are still separate gates.

Dali sets reference-clock select, software PLL LPM parameters25/30/30,
VREF7/FS7 and the revision0 CHP-disable quirk. It has no SoC eye, host or
intr/term offset overrides. Cold startup follows reference init, repeater power,
SoC power and calibration, then role mode. Role changes detach MT6379 pull-up
before changing NXP, tune both repeaters and apply the SoC IDDIG controls.

Slew calibration uses26MHz*17*1024/FM, rounds by1000, and orders meter/free-clock
off before slew write before oscillator-off. Unlike the source's ignored timeout
and default3 fallback, invalid/zero/out-of-range measurements refuse startup.
Failed cleanup retains FM ownership for explicit shutdown. Power-off uses the
reference valid-USB-role suspend sequence with BC1.1 disabled; charger/L2 analog
policy remains owned by the connector/role layer. Dependencies/clocks stay held
on failure, and a fault requires full teardown before restart.

The XS-PHY fixture executes production SoC and both repeater code against SoC
register/PMIC-byte/NXP-I2C seams. It checks revisions, efuse/zero preservation,
calibration order, cold restart, detach order, per-operation on/mode/pullup/off
failures and FM cleanup retry. This does not qualify physical calibration or
the production PMIF/I2C transport (tested separately). DTM1 session indicators
are logical controls, not external-voltage evidence; actual TCPC/charger checks,
held clocks/domains, DMA and installed OS/controller providers remain required.

## Clock, power, bus protection and IPPC

| Resource | Fixed reference location |
| --- | --- |
| VLP sys/host clocks, IDs34/35 | `drivers/clk/mediatek/clk-mt6991.c:3378-3385,3885-3898`; VLP config8 `0x90`, mux groups8/16 |
| Frame clock, CKSYS ID36 | same file `:1216-1219,2103-2109`; config10 `0xb0`, group24 |
| USB2/USB3 domains IDs2/3 | `drivers/soc/mediatek/mtk-scpsys-mt6991-spm.c:297-315` HWV-compatible table; SSUSB_P0 `0xe1c`, DP_PHY_P0 `0xe18` |
| Domain dependency | same file `:612-614`: SSUSB_P0 parent of SSUSB_DP_PHY_P0 |
| Bus protection | syscon `0x1c00d000`, set/clear/enable/status `0xdc/0xe0/0xd8/0x208`; P0 bit7, DP PHY bit6 |
| AO clock gate / MBIST | `0x16790000` mask `0x13` / `0x16781000+0x34` idle bit0 |

USB domains use ordinary SCPSYS control rather than HWV ops despite the parent
HWV regmap; PWR_ACK/ACK2 are bits30/31, SRAM bit8/ACK12 for SSUSB_P0. Generic
SCPSYS sequencing and bounded polls are implemented by `DaliUsbDomains.c`.
The live FDT's `mediatek,mt6991-scpsys-hwv` compatible selects this table, whose
two USB caps lack HWV/PBUS/INFRA flags (`mtk-scpsys.c:1924-1943`). The distinct
PBUS table is unsupported. Initialization only reads coherent ON/OFF snapshots;
power-on is parent then child, power-off child then parent, with NON_CPU_RTFF
restore/save and only bit6/7 SET/CLR writes. Bus-protection clear intentionally
ignores the stale status ACK, as required by BUS_PROT_IGN, but verifies EN clear.
Unlike the reference, failed SET retries cannot pass using a stale status ACK.

The persistent per-domain cursor retains uncertain steps and RTFF restore
selection. A failure latches out new startup. Explicit shutdown may complete its
own partial startup without issuing a new unprotect, then save/stop. Destroy
clears context only after both domains are off. The caller must own both entire
domains, stop all controllers/PHY and DMA, and hold required supplies/clocks
through failures. An external reset or mutation invalidates retained cursors;
they cannot be resumed after such an event. The production-C fixture covers
each operation failing before/after writes, both domains' ACK timeouts, SRAM,
stale protection ACK, foreign idle states and shutdown retention. This is a
register model, not an installed provider or hardware power qualification.

USB clock muxes use
gate/fence/update-check macros, not simple unguarded selector writes. They depend
on26MHz/PLL parent state; the resource graph does not establish those states.

`DaliUsbResources.c` now implements only the three actual USB gate leases,
preserving the selected parents. USB sys/host use VLP CFG8 `0x90`, CLR `0x98`,
SET `0x94`, gate bits15/23 and fence `0x3a0` bits30/29. Frame uses TOP CFG10
`0xb0`, CLR `0xb8`, SET `0xb4`, gate31 and fence `0x274` bit20. The fixed mux
enable operation writes CLR then waits fence; UPDATE/check belongs to parent
switching, which this API never performs (`clk-mux.c:65-109,132-138,847-854`).
Fence waits use the source's1us/5s deadline via five bounded1s core polls.
Readback verifies gate/parent fields. Shutdown restores acquired gates in
reverse order, preserving inherited enabled gates and all adjacent fields.
Parent PLL state/rate, PHY26MHz and I2C clocks remain external qualifications.

`drivers/usb/mtu3/mtu3_plat.c` probe attaches domains, resets AO gates, enables
vusb33/clocks/PHY, resets IPPC and requests power before host/gadget init
(`:927-941,1001-1075,1279-1336,1380-1425`). MT6991 HWRSC version2 uses IPPC
`SPM_CTRL_V2=0xe0`, ACK `0xe8`, request bits0,1,2,3,4,6; AO REQ_GATE0..7
`0x70..0x8c` receive mask0xf (`:167-248`). Production code must treat failed
resource ACK as a refusal, not merely log it as the Linux reference does.
`ssusb_set_power_state()` selects SPM_V4 at compile time when
`IS_ENABLED(CONFIG_MTK_SPM_V4)` (`:279-314`); this branch returns even if its
callbacks are absent, so it never falls back to HWRSC v2. The final external
vendor-module config and callback registration remain unverified. Saved
`kernel-config.gz` is the GKI config and contains neither SPM_V4 nor MTU3/SCPSYS
symbols; it does not establish the vendor-module branch. No native provider
may assume the v2 route from that absence.

The new native HWRSC API requires the explicit
`DALI_USB_RESOURCE_NATIVE_HWRSC_V2` provider choice and validated matching FDT,
accessible powered IPPC, held clocks/supplies and exclusive request/gate fields.
It implements only native POWER_ON/OFF, with native sleep/offload disabled;
it does not claim to reproduce Android's compile-time branch. Linux SPM_V4
registers `spm_resource_req` and `slp_set_infra_on` in `spm_v4/mtk_spm_init.c:22-40`.
Those callbacks update software votes/flags (`mtk_spm_resource_req.c:54-88`,
`mtk_sleep.c:417-433`), not a proven MT6991 power-register/SMC ABI. The fixed
tree's configuration sources do not explicitly enable SPM_V4; final generated
vendor config remains unknown. No SPM_V4 or SMC stub is supplied.

Native enable opens AO request-gate low nibbles before setting `0x5f` request
bits and clearing force `0x7f00`; it requires matching ACK within20ms at100us
intervals and2ms settling. Disable clears requests/force, waits ACK/settling,
then restores only acquired AO nibbles. A shared-provider RMW lock must cover
the whole call. Resources must be released while IPPC remains accessible,
before dropping domains and clocks. Both clock/HWRSC APIs latch failures out
of new enable, retain uncertain ownership before writes and permit explicit
cleanup; failed destroy retains context. Their fixture covers inherited
states, parent/gate combinations, delayed fences/ACKs, before/applied-error
fault matrices, ignored writes, delayed read errors and cleanup retention.
Actual parent qualification, native provider selection/locking and installed
OS/controller binding remain pending; register-model results prove none of
the physical resource or USB behavior.

IPPC stable polling requires STS1 bits10/8/0 plus role-specific reset bits within
20ms, then STS2 bit0 within10ms (`:828-852`). Role selection disables/powers down
the port before changing HOST_SEL/OTG_SEL (`mtu3_dr.c:35-61`, `mtu3_core.c:231-275`).
MBIST idle may wait3s in the Linux reference; timeout cannot authorize role or
power changes while DMA ownership remains uncertain. No physical state from
these registers has been qualified by this offline implementation.

`DaliUsbIppc.c` implements this IPPC lifecycle. Init reads only, refusing
inherited asserted AO/IP resets and unsupported capabilities; one/two U2 ports
have known Dali UTMI16 fields, up to four U3 register slots fit before U2.
Cold order is clocks/domains, AO reset (`0x0`, mask`0x13`,1ms), HWRSC request,
PHY/repeater power/role, IP reset (`0x0` bit0,1us), then role enable and actual
MAC/xHCI startup. AO reset must precede PHY power-on. All windows, supply/clock
leases and exclusive ownership must remain valid through failed cleanup.

The IPPC guard must prove both controllers' submissions/IRQs/queues/DMA stopped.
For an owned asserted AO reset, it must use the persistent previous drain proof
without reading gated MAC windows. Ownership is set before reset writes, and
explicit stop deasserts uncertain owned resets with minimum pulse delays before
continuing. No new startup is accepted after a fault. The shared-provider lock
must cover complete API calls; external reset/mutation invalidates the context.

IP reset establishes a known stopped state by disabling every capability-reported
U3/U2 port before clearing DUAL_MODE/OTG, then powering down device and host and
requiring IP sleep. Enable validates that stopped state, sets UTMI16 while IPs
and ports are off, powers only the selected IP and enables only USB2 port0.
HOST_SEL is bit9|bit2; device clears it while enabling port0, then sets OTG_SEL
in a separate write as in the device reference. All U3 and secondary U2 ports
stay disabled. Stable waits require STS1 `0x501` (+`0x800` for host), then STS2
bit0; no U3 MAC clock is requested. Logical role/session fields do not prove
physical VBUS; the actual TCPC/charger interlock remains mandatory.

MBIST `0x34` bit0 is a hard3s/100ms gate. Sleep is a hard100ms/100us gate with
100us settling. Unlike the source's logged bus-busy fallback, failure cannot
authorize further reset, role or clock release. Repeated stop rereads both port
state and sleep, with no register writes if already stopped. `stopped=true`
means confirmed port/IP power-down; the role field alone after a fault does not.
Destroy only clears a successfully stopped object. PHY, HWRSC, domains and
clocks may be released afterward by the higher-level worker.

The production-C fixture models reset pulses, all supported port counts,
device/host transitions, secondary-port shutdown, neighbors, delayed idle/stable
status, before/applied-error faults at every operation and repeated reset-cleanup
errors. MBIST/STS/sleep timeout and cached-stop wake checks retain resources.
This does not implement or prove the actual guard/IRQ/DMA/provider, platform
calibration, TCPC role worker or installed UEFI/XNU controller integration.

## Native calibration acquisition from boot FDT

The actual Dali nvmem provider is `mediatek,devinfo`, not a memory-mapped efuse
driver. Fixed `drivers/nvmem/mtk-devinfo.c:23-25,31-85,99-123` copies
`/chosen/atag,devinfo` (or `/chosen@0`) into RAM and serves word `reg/4`.
The unit address in `/efuse@13260000` is not a usable MMIO resource: that node
has no `reg` and this provider never maps it. USB cells1d4/1d8 select data words
117/118; the provider's count word precedes the array. No efuse MMIO/SMC access
is needed or introduced by this acquisition path.

Fixed `phy-mtk-xsphy.c:475-481,1475-1489` consumes five native little-endian
chipid words. Saved Dali has a20-byte property whose legacy size word is24,
hardware code1357, revision B0=1; missing data must not become A0=0. The native
decoder accepts explicit A0/B0 payloads, requires that known Dali format,
checks119..400 devinfo words and exact `4+count*4` length, and byte-decodes only
the two required words. Unlike source warnings/unchecked casts, malformed,
missing, oversized or unknown data is an error. Acquired zero words remain
valid and preserve inherited analog fields. The structure layout is unchanged;
its declaration moved from DaliUsbPhy.h to DaliUsbCalibration.h.

UEFI `DaliUsbReadCalibration()` in `UsbPlatformLib` accepts a complete immutable
aligned snapshot plus accessible size. It verifies enabled MT6991 root/SOC,
USB0/PHY0 identity and reg/cell shapes, relevant unique phandles and matching
aliases, exact masks/name order, both cells belonging to one enabled root
devinfo provider, and chosen properties. Only a fully successful decode changes
the output. `qualified` covers these calibration inputs, not physical behavior,
bootloader authenticity, clock/DMA/power or installed OS/controller resources.

Header validation alone is insufficient: libfdt's depthless `next_node` can
treat a missing final token as normal end. This library requires the pinned
BaseFdtLib upstream `fdt_check_full` entry (UINTN=size_t ABI), validates the whole
bounded structure/reservation map before traversal, and has regression cases
for missing FDT_END and root close despite a valid header. The EDK2 build now
also builds/checks actual BaseFdtLib fdt_check/FdtLib objects and full-check
symbol. No Mu source patch or placeholder checker is used.

Host tests execute both actual decoder/binding C and the pinned real libfdt
parser (only UEFI ABI/API names are mapped). They cover alignment, capacities,
property lengths/endian/counts, enabled ancestors, masks/names/cells/provider
mixing, ambiguous references, structural corruption and unchanged failure
output. A separate local invocation also succeeds on the saved live Dali DTB;
raw ATAG/unused devinfo data is not printed or committed. This qualifies the
offline acquisition logic, not hardware calibration or a running USB stack.

## Read-only selected clock source rates

`DaliUsbClockRates.c` reads the actual MT6991 USB0 VLP sys/host muxes, TOP frame
and west-I2C/AXI muxes, APMIXED main/univ PLL configuration/prepared fences and
IMPW/PERAO physical gate status. USB0 VLP parents are26MHz or mainpll/9
(`clk-mt6991.c:3378-3385,3885-3898`), distinct from TOP USB1 univpll/20.
Frame is26MHz or univpll/52 (`:1216-1219,2103-2109`). West-I2C selector
`TOP+0xc0[18:16]` has26MHz/main32/univ20/main16/univ10 parents
(`:1237-1243,2125-2131`); APDMA's AXI selector `TOP+0x10[26:24]` has
26MHz/main56/main40/ULPOSC8/main28/main20/main16/main14 (`:889-898,1872-1876`).

Main/univ CON1 are `APMIXED+0x254/0x268`, PCW22 bits with8 integer bits,
postdiv `[26:24]`; prepared fences are `+0x3c` bits7/6. The fixed PLL ops
round VCO upward then round postdiv upward (`clk-pll.c:137-162,287-300`),
followed by fixed-factor truncation. Only source VCO1.5..3.8GHz is accepted.
FENC is the source's prepared status, not a PLL lock or measured frequency.
I2C6 main is IMPW `+0xe00[2]`; DMA shares PERAO `+0x14[26]`, both enabled
when clear (`clk-mt6991-peri.c:173-206,224-228,383-388`). West mux itself
also has gate23/fence17. The caller must keep shared sources held; this API
does not claim exclusive ownership of unrelated I2C/APDMA clients.

Two complete masked snapshots reject observed selector/PCW/gate/status changes,
CKSTA busy and inconsistent active fences before publishing any
output. Unknown west indexes, unknown revisions, unprepared selected PLLs and
invalid PCW fail. ULPOSC/8 requires an independently held proven rate; no520MHz
DT or OPP-table fallback is used. Rates of gated outputs remain configured
source rates, with enabled bits reported separately. Both revisions share these
fields. No PLL/mux/gate writes, source switch or global voter changes occur.

The required validated mappings, reference source and stability lease are still
platform inputs. Two reads cannot prove a lease or exclude ABA changes. The
installed UEFI provider must validate graph identities, own/hold resources and
apply actual consumer rate requirements before use; XNU needs its own adapter.
Production tests cover all320 parent combinations,32 gate combinations, PLL
postdiv/rounding, unknown/off sources, transitions, per-read errors and unchanged
failed output. These are register models, not physical clock qualification.
CKSTA registers are VLP254/TOP1cc/1c8; `clk-mux.c:511-528` waits their relevant
bits clear and reports "cksta busy". Separate UPDATE registers are write-command
locations and are not read as a stability indication by this snapshot API.

## Native UEFI clock resource discovery

`DaliUsbReadClockResources()` now consumes the immutable bounded boot snapshot,
validates calibration/full structure through the actual production calibration
entry, then checks the specific clock provider and consumer graph. Common
status/cell/unique-handle checks are shared in the private `UsbFdt.h`; the
calibration API retains its behavior. The returned object contains only copied
physical ranges,26MHz declaration and decoded calibration; it retains no pointer.

Clock providers live directly under the root, not under `/soc`: VLP
`/syscon@1c016000` size1000, TOP `/syscon@10000000` size800, APMIXED
`/syscon@10000800` size1000, IMPW `/syscon@131b0000` size1000 and PERAO
`/syscon@16640000` size1000. Each requires its exact enabled MT6991 compatible,
syscon compatible, reg/cell shape and root parent. USB0 IDs34/35 reference VLP,
ID36 references TOP; I2C6 main ID5 and DMA ID42 reference IMPW/PERAO. Reference
providers must resolve uniquely; matching names or frequencies alone cannot
substitute an unreferenced node.

PHY0 and child `xhci0@16700000` both require the same enabled zero-cell
`/clocks/clk26m` fixed provider with26MHz. `/clocks` must be enabled. USB0
MAC/IPPC and host resources/names, high-speed/OTG flags, ordered clock names,
identity child ranges/cells and I2C6 windows/400kHz/clock-div1 are validated.
This is a description of clock resources only: it does not qualify channel
ownership, active pins, supplies, power domains, connector/ADC, DMA, source
stability, actual frequency or MMIO accessibility. No mapping/vote/MMIO or
controller startup occurs, and the installed native caller remains pending.

Host tests execute both actual resource/calibration units and the pinned real
libfdt, covering disabled ancestors/providers, windows, cell counts, malformed
references/names, IDs, same-frequency wrong provider, ambiguous aliases,
translation, missing tags, structural corruption and unchanged error output.
A separate local run succeeds on the saved Dali FDT without publishing its raw
ATAG payload. Real EDK2 compilation verifies the actual new object/definition.

## Actual source UEFI host-stack QEMU and FV gate

Firmware `tools/dali/build_usb_qemu.py` builds actual pinned Mu xHCI, USB bus,
keyboard/mouse, BOT, DiskIo, partition and FAT drivers plus a separate test EFI
application. `run_usb_qemu.py` creates fresh GPT/FAT32 image files and exercises
the source stack through actual QEMU xHCI USB2 ports and its full-speed hub.
Platform services still come from an explicitly hashed external AArch64 UEFI
image; this is not a boot of Dali's hardware-specific firmware in QEMU.

The app detaches the platform USB drivers, explicitly rebinds source drivers at
each layer, and verifies actual BY_DRIVER agents. It requires one xHCI, one
direct hub, one keyboard/mouse/BOT disk under that hub, and the tagged GPT volume
under that same disk. Reserved test files must be absent before application
writes. QMP supplies key press/release and mouse movement/button press/release;
the app checks SimpleTextIn/SimplePointer and a complete zero keyboard report.
FAT cases create/modify/flush/rename, remove the filesystem via BOT disconnect,
reconnect/remount and read the exact file, then create/flush/delete a nonempty
file. After shutdown, mtools verifies the complete modified data's SHA256 and
persistent directory entries. The report requires ordered milestones, records
source-tree changes/untracked files/submodules and firmware/initial-vars hashes,
and keeps `hardware_qualified=false`. Only new ordinary image files are written.

`DALI_USB_HOST_STACK=true` opts the Dali FV into xHCI/bus/HID/BOT/partition and
console aggregation; the flag defaults false in both DSC and DeviceBuild. Some
modules remain inherited DSC compile inputs when the FV flag is false. Full
opt-in firmware build/package succeeds, and `verify_usb_fv.py` extracts all ten
expected PE modules from the final compressed FD and matches them byte-for-byte
to the corresponding compiled images. The QEMU test app never enters Dali FV.

This verifies source class-stack behavior and opt-in FV assembly. Native
MT6991 MMIO registration, resource/connector/ADC/DMA qualification, CDC/role
worker, firmware menu/pointer behavior and EBS handoff remain separate required
implementation gates, along with the source XNU/IOHID/msdosfs/macOS gates.
