# `tests/rmii_conformance` — cross-implementation RMII bit-order check

Closes the **RMII half of I22** (`docs/contracts/OPEN_ISSUES.md`) *without board
time*. See `RESULT.txt` for the evidence and the exact verdict.

## Why this exists

`tests/rmii_phy_if/` proves only **self-consistency**: both ends of that bench
are our own RTL, so a bit order that is internally consistent but wrong on the
wire would pass. This bench instead faces our shell-side virtual PHY against the
**independent implementation the real DUT uses**:

| role | module |
|---|---|
| shell PHY side | `fpga/ethernet/rmii_phy_if/rmii_phy_if.sv` (hand-rolled, no upstream) |
| DUT MAC side | `ethernet-mac-ahb/amba_wb_bridges/src/rtl/rmii_to_mii.v` (descends from WangXuan95) |

They have **different ancestry and share zero identifiers**, so byte-for-byte
agreement is evidence rather than tautology. (Beware: WangXuan95's upstream
module is *also* named `rmii_phy_if` — a name collision, not shared lineage.)

## Run

```sh
source set_env.sh
make -C tests/rmii_conformance          # real + both negative controls
make -C tests/rmii_conformance result   # regenerate RESULT.txt
```

## Negative controls

Both directions are independently falsifiable — a bench that cannot fail is
worthless:

| control | Dir A errors | Dir B errors |
|---|---|---|
| none (real RTL) | 0 | 0 |
| `neg` — TX di-bits swapped | 0 | **18** |
| `neg-rx` — RX nibble halves swapped | **18** | 0 |

Neither control touches the third-party RTL.

## Scope — what this bench does NOT cover (added 2026-07-10)

This proves **bit/nibble order**, not link speed. The `rmii_to_mii.v` it compiles
(md5 `5dd9b16f`) is the variant whose header reads *"Removed the mode_speed input —
hard-wired to 100 Mbps operation"*. So the speed divider is never exercised here.

That matters because the copy the **multicore integration actually ships**
(md5 `5e38c43c`) *has* a `mode_speed` port — and the wrapper leaves it unconnected,
so the bridge clocks for 10 Mb/s while the PHY negotiates 100. That defect is proven
separately in `tests/rmii_speed/` (falsification test + positive control). Do not read
this bench's green result as covering the speed path; it cannot, by construction.
There are at least three divergent copies of `rmii_to_mii.v` in this lab — see
`tests/rmii_speed/README.md`.
