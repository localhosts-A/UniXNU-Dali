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
  one. The live CPU nodes contain no `compatible` strings or MIDR values.
- PSCI uses the SMC conduit.
- GICv3 distributor `0x0c400000`/`0x40000` and redistributor region
  `0x0c440000`/`0x200000`.
- The architected timer reports 13 MHz and the interrupt tuples recorded in
  the MT6991 common DTSI.
- The root memory node reports base `0x80000000` and size `0x300000000`.

## Format 2 blockers

Do not infer per-core CPU compatibles from PMU cluster names. The public
reference DTS also has no CPU compatible values, so an authoritative CPU
mapping is still needed. Before advertising DRAM to XNU, reconcile the root
range with the reserved-memory, protected and firmware-owned regions. Review
the timer/GIC ADT contract against the exact target XNU source as well.

Until then `board.toml` stays `example = true` and intentionally has no active
`[device_tree]`, `[firmware]` or `[payload]` section. The current profile and
board metadata are not a buildable device configuration.
