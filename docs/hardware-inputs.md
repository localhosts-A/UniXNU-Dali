# Dali hardware inputs

## Source

The observations below come from a read-only live FDT capture taken on
2026-10-26. Its SHA-256 is
`b1e31720e948e8769a2fcb7fd9a097c30b934200b8033692f9ed1214b1985182`. The raw
FDT contains device-specific identifiers and Android boot arguments, so only
sanitized facts are recorded here; the capture remains in private local
evidence.

## Observed facts

- Root model `MT6991`; compatible `mediatek,MT6991`.
- Eight CPU affinity IDs (`0x0` through `0x700`), grouped as four, three and
  one. The live CPU nodes contain no `compatible` strings or MIDR values. The
  shared MT6991 DTSI now records a derived mapping: CPUs 0–3 A720, CPUs 4–6 X4,
  and CPU 7 X925, inferred from the public MediaTek core mix plus the reference
  DTS cluster/PMU association pinned by `UniXNU-MT6991-Common/soc.toml`. This
  is not a live CPU identity capture.
- PSCI uses the SMC conduit.
- GICv3 distributor `0x0c400000`/`0x40000` and redistributor region
  `0x0c440000`/`0x200000`.
- The architected timer reports 13 MHz and the interrupt tuples recorded in
  the MT6991 common DTSI.
- The root memory node reports base `0x80000000` and size `0x300000000`.

## Format 2 blockers

The per-core CPU compatible mapping is evidence-backed but remains inferred;
runtime MIDRs are still required to make it authoritative. The root memory
range is 12 GiB, but the FDT has 54 fixed reserved ranges and four dynamically
allocated reservations. The latter do not have final physical addresses. The
current UniXNU FDT-to-ADT converter also drops `/reserved-memory` and its
allocation constraints, so the reservation map cannot yet be handed to XNU.
Review the timer/GIC ADT contract against the exact target XNU source as well.

Until then `board.toml` stays `example = true` and intentionally has no active
`[device_tree]`, `[firmware]` or `[payload]` section. The current profile and
board metadata are not a buildable device configuration.
