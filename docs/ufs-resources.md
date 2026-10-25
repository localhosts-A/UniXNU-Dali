# MT6991 UFS resource baseline

This table records observed resources and the pinned source paths needed to
initialize them. It is not a runtime configuration or a write authorization.
The device remains on its verified original Android B image after the probe.

## Evidence inputs

- MiCode Linux: `f089d58cab670dc0d75db1203fa63850e57a940d`.
- MiCode device modules: `e45c3199537fbba9aae5fb06c31fc8dbd3c1dcee`.
- MiCode vendor modules: `fe0fa7ff68a2de0dec7ce1a04f5db6f07fd4a3b9`.
- Private read-only Android baseline: `dali-ufs-baseline-20261025-v3`, with
  `runtime.dtb` SHA-256 `5bc6727f0fe588003323b66b46c3b7c348eae220ecf917b9b534f3e40275a0f6`.
- Raw pre-Linux console: `run-20261025T053825Z-aea37137`, SHA-256
  `80db35adf490657967327b08773ed92333da1e82690da60bf5e220e258833c89`.
  The exact token, source revisions, candidate and raw capture remain in the
  private run directory. Android B, root and all 18 original hashes passed
  after original `boot_b` restoration.
- Extended version-2 console: `run-20261025T060856Z-1b8960e3`, SHA-256
  `93e500d6f03620c5cec2618bc874d1d93a3f4778ddf7ef4e50480823c44a196d`.
  Firmware `dc401de`, generic `5899e85`, device `eca4fec`; all 25 expected
  register reads, original-image recovery and the cleared pending ledger were
  independently verified. No UFS media commands were submitted.

The earlier attempt `run-20261025T053337Z-36b9917f` had no surviving pstore
file. It is not register evidence. Its original-image restoration passed.

## Resources and measured limits

| Resource | Live FDT / Android observation | Fixed source and remaining measurement |
| --- | --- | --- |
| HCI | `/soc/ufshci@16810000`, `0x16810000 + 0x2a00`, `mediatek,mt8183-ufshci` | Device modules `arch/arm64/boot/dts/mediatek/mt6991.dts`, UFS node; actual probe read HCI 4.0, CAP `0x55c7051f`. The compatibility string does not select an MT8183 hardware recipe. |
| PHY | `/soc/ufsphy@16800000`, `0x16800000 + 0x10000`, `#phy-cells=0`, `mphy-ver=1` | Device modules `drivers/phy/mediatek/phy-mtk-ufs.c:154,257`: version 1 returns from `power_on` without the old PLL/CDR register sequence. Do not execute that version-0 sequence. Actual PHY/link state remains unmeasured. |
| Parent clocks | `/syscon@10000000`, size `0x800`; six IDs `42,97,115,41,118,96` for `ufs_sel`, min/max source, `ufs_fde`, min/max source | FDT references and all six IDs are checked by the production probe. Parent frequencies, mux selection and voltage at firmware handoff remain unmeasured. |
| AO gates | `/syscon@168a0000`, size `0x1000`; consumer aliases IDs `1,3,5,7,9,11,13,15` | Device modules `drivers/clk/mediatek/clk-mt6991-peri.c:616-693`: status `+0x104`, HCI bit 0/AES bit 1; status `+0x4`, TX bit 0/RX0 bit 1/RX1 bit 2/UniPro SYS bit 3/SAP bit 4/PHY SAP bit 8. Both read zero on hardware. Gates use active-low set/clear semantics; parent clock and PHY qualification does not follow from zero gates. |
| Host resets | AO child `reset-controller`, `ti,syscon-reset`; UFS uses IDs 1/2/3, named `unipro_rst`, `crypto_rst`, `hci_rst` | `ti,reset-bits` has seven cells per entry. Those three entries assert at AO `+0x148` bits 0/1/2 and deassert at `+0x14c` bits 0/1/2, flags `0x1c`. Linux `drivers/reset/reset-ti-syscon.c:66-115,177-196` and `include/dt-bindings/reset/ti-syscon.h:15-22` decode the flags as write-set assertion, write-set deassertion, no status operation. Entry 0 is a different reset and must not be confused with the UFS references. No reset write has been tested in UEFI. |
| Interrupts | SPI 320 plus SPI 329 through 336; each specifier has the FDT's four cells | Device modules `ufs-mediatek.c:1454` obtains one host IRQ and eight MCQ IRQs. Initial driver work uses bounded polling; interrupt routing is not qualified. |
| Device VCC | `vcc-supply` resolves to `mt6363_vemc`, marked `regulator-always-on` | Live FDT supply identity is known; this property is not a measurement of actual rail voltage or handoff state. No PMIC write path is authorized by this table. |
| Core supply | `dvfsrc-vcore-supply` resolves to the DVFSRC core regulator; minimum requested during scale-up is 650000 microvolts | Device modules `ufs-mediatek.c` clock-scaling path consumes the FDT value. Preserve inherited state until the relevant power contract is verified. |
| Power domain / reference clock | `mediatek,ufs-rtff-mtcmos` is present | `ufs-mediatek.c:1686-1692` issues MTCMOS restore then power-on through SiP; `:391-455` brackets REFCLK requests with SiP notifications. The secure firmware handler and current domain state remain unverified. |
| Reference-clock register | HCI `+0x144` reads `0x00000003`: request bit 0 and acknowledge bit 1 are set | `ufs-mediatek.h:48,113-118`; `ufs-mediatek.c:391-455` uses a 3000 microsecond acknowledgment deadline. The version-2 snapshot measures these bits, without establishing active PHY/link state. |
| Hardware IP version | HCI `+0x2240` reads `0x10470000` | `ufs-mediatek.c:1088-1113` normalizes old `01MMmmmm` numbering to `1MMMmmmm` before selecting quirks. The observed value already uses the new encoding. Version-dependent writes remain untested. |
| Vendor controls | HCI XOUFS `+0x140 = 0x102`, OPT `+0x160 = 0x10553070`, OTSD `+0x14c = 0x410` | Captured by version 2 using the fixed `ufs-mediatek.h` offsets. These raw values are observations, not an initialization recipe or completion status. |
| DMA / IOMMU | `/soc/dma-ranges = <0 0 0 0 0x10 0>` declares zero-offset 64 GiB; HCI has no `iommus`, and no Android `iommu_group` link was observed | The fixed Linux path below uses the declared range and establishes no OF IOMMU binding for this node. UEFI/XNU bus firewall, actual DMA access and cache coherency remain unqualified. No global IOMMU bypass is permitted. |
| Media | Android boot partition is LUN 2; logical/physical block size 4096; LU capacity 64577536 blocks | Read-only baseline cross-checks sysfs, unit descriptor, raw reciprocal GPT CRCs and original backup. Re-query after controller ownership changes. Never substitute LUN 0 or a fixed 4 KiB assumption. |
| `boot_b` | Absolute LBA 418560, 16384 blocks; partition GUID `00000046-39c2-4488-9bb0-00cb43c9ccd4` | GPT disk GUID is zero in both original headers and cannot identify the device. Candidate-only scratch proposal is absolute LBA 434432 for 256 blocks, plus 256-block tail guard; runtime write authorization remains false. |

## Initialization dependencies

### DMA source contract

The saved live FDT has no `dma-coherent` or `dma-noncoherent` property on the
HCI, `/soc` or root. Absence is not a coherency measurement. The following
references are all from the fixed Linux revision above, including the generic
files fetched without changing that revision:

- `drivers/base/platform.c:1447-1466` calls `of_dma_configure` for the device's
  own OF node and then uses its default IOMMU domain when applicable.
- `drivers/of/device.c:104-110,170-199` obtains the parent's DMA ranges, records
  its bus limit/range map, computes coherency and calls `of_iommu_configure`
  before `arch_setup_dma_ops`. `drivers/iommu/of_iommu.c:133-179` reads the
  device's own `iommus`; it does not inherit a parent's IOMMU binding.
- `arch/arm64/mm/dma-mapping.c:50-67` records the computed coherency and installs
  IOMMU DMA ops only when an IOMMU was found. Platform notifiers or vendor hooks
  can affect runtime setup; source inspection does not observe the final ops.
- `kernel/dma/mapping.c:116-163` chooses direct mapping when no DMA ops exist.
  `include/linux/dma-direct.h:54-90` translates physical addresses through the
  range map, refusing addresses outside it. The live zero offset would preserve
  an address within the declared range on this path.
- `kernel/dma/direct.h:84-112` checks DMA capability and bounce requirements,
  then synchronizes noncoherent mappings. The arm64 implementation cleans to
  PoC before device access and invalidates for CPU access
  (`arch/arm64/mm/dma-mapping.c:17-40`).

CAP `0x55c7051f` includes UFSHCI's 64-bit-address capability bit `0x01000000`
(`include/ufs/ufshci.h:75`). That capability does not enlarge the FDT bus
window or establish a firmware mapping. An initial firmware mapping must own
its allocations, validate every full span against its supported address window
and HCI width, and explicitly synchronize descriptors/data. It must never adopt
an LK buffer address. A bounded read into such a private buffer, with completion
and content checked against the Android baseline, remains necessary to qualify
the actual access path. DEVAPC's `UFS0_M` violation-decoding table is not an
IOMMU SID or permission-programming recipe.

### Controller sequence

The successful snapshot has HCE/HCS, interrupt status/enable, AHIT and all
legacy queue bases/doorbells/run bits equal to zero. MCQCAP is `0x080b0207`,
MEMCFG is zero and MCQCFG is `0x00001f00`. These values do not prove DMA access
or an active link. They require an initialization path that handles disabled HCI.

The pinned MTK implementation supplies the following order and conditional work:

1. Bind the versioned PHY, resolve the three host reset controls, and establish
   clock/power dependencies. Its `mphy-ver=1` path skips version-0 analog writes.
2. For the live `ufs-rtff-mtcmos` property, issue the documented MTCMOS operations;
   obtain a reference clock acknowledgment using the bounded source sequence.
   `drivers/ufs/ufs-mediatek-sip.h:14-24,56-92` defines the UFS SiP service and
   selector bits. Return status and resulting hardware state need explicit
   firmware checks; the Linux caller alone is not secure-firmware verification.
3. On HCE enable outside UniPro low power, assert HCI/crypto/UniPro resets, wait
   100 microseconds, deassert in reverse order, and allow 600 microseconds for
   HBA enable. No MPHY reset is requested by the live host binding.
   (`ufs-mediatek.c:242-304`.)
4. Set the documented XOUFS clock-gating bit and apply OPT_CTRL settings only
   after hardware IP identification (`ufs-mediatek.c:320-347`). Keep crypto and
   performance features outside the initial unencrypted, serial read path.
5. The MTK pre-link path exits UniPro low power, disables host TX LCC and deep
   stall; the post-link path applies its IP-version-dependent PA_INIT adjustment
   and UniPro clock gating (`ufs-mediatek.c:1931-2030`). These use UIC commands,
   so command-ready, completion, result and timeout checks must precede DMA.
6. Allocate private descriptors and buffers only after exclusive ownership and
   the address-translation/cache contract are established. No firmware queue
   pointer may become an XNU buffer. A timed-out write is never replayed.

The device-reset path separately asserts RST_n via SiP, stops the HBA, waits at
least 10 microseconds, releases RST_n and waits 10–15 milliseconds
(`ufs-mediatek.c:2033-2058`). It is not evidence that a full device reset is
necessary for every warm handoff, nor a substitute for validating the retained
power/PHY state first.
