# tests/shield_path — reproduce the MPS3 LAN8720 failure signature, board-free

Wires this repo's own Clause-22 virtual PHY (`fpga/ethernet/mdio_phy_model/`) behind a model of
the MPS3 shield/PMOD path, and walks **healthy → inbound dead → restored → both dead**.

Pure-VCS SystemVerilog (no cocotb), standalone like `tests/rmii_conformance/` and
`tests/rmii_speed/`. Not wired into repo-root `make check`.

```
source set_env.sh
make -C tests/shield_path
make -C tests/shield_path result   # regenerate RESULT.txt
```

## The fault being modelled

From `nanoSoC-refactor/ethernet-subsystem-ahb/docs/mps3_ethernet_debug_plan.md`,
"Diagnostic findings (2026-04-22 session)":

> *"a single physical fault: the SH0 level shifter channel(s) between PMOD J28 and the FPGA are
> passing FPGA→PHY bits cleanly but are dead on the PHY→FPGA return"*

That asymmetry breaks every FPGA **input** — `RXD[1:0]`, `CRS_DV`, `REF_CLK`, and the MDIO read
turn-around — while every FPGA **output** survives. `shield_path.sv` models exactly that, with the
two directions as *runtime* inputs so one simulation can toggle them.

`FLOAT_LEVEL = 1'b1`: a dead inbound channel presents a pull-up to the FPGA pin. That is *why* a
dead MDIO read reads `0xFFFF`.

## Results (VCS 2022.06-SP2)

| Phase | Shield state | Check | Result |
|---|---|---|---|
| 1 | both directions pass | PHYID1/PHYID2/ANAR | `0x0007` / `0xC0F1` / `0x01E1` |
| 2 | **inbound dead** | BMSR at **all 32** PHY addresses | `0xFFFF` — board finding #3 |
| 2 | inbound dead | write `ANAR = 0x0181` (what the real firmware writes) | issued; readback `0xFFFF` |
| 3 | inbound restored | read ANAR | **`0x0181`** — the write *had* landed. Board finding #2 |
| 4 | **both directions dead** | write `ANAR = 0x0000`, restore, read | `0x0181` unchanged — negative control |

`errors=0  VERDICT=PASS`.

Phase 4 matters. A bench that only ever confirms its own hypothesis is worthless: if a write got
through a channel the model claims is dead, nothing else here could be believed.

## What this is for

**It does not prove the board's fault.** It proves the hypothesis is *sufficient* to explain every
observation, and it hands the bring-up session a reference signature and a discriminating procedure.

The useful conclusion is in the last two lines the bench prints:

> A dead inbound channel is indistinguishable from a dead PHY, a wrong PHY address, or a dead MAC —
> **by reads alone.** Every one of those returns `0xFFFF`. Only *write-then-verify* separates them.

On the bench you cannot "restore" a failed shifter to do phase 3. But you do not need to: the board's
own finding #2 is the verify step — a `BMCR.RESET` write makes the PHY's LEDs blink. **The PHY's LEDs
are a one-bit read channel that does not traverse the broken direction.** Any register whose effect is
externally observable (LED mode, loopback, power-down) can be used the same way.

## Reusable pieces

`shield_path.sv` also carries the RMII inbound signals (`crs_dv`, `rxd[1:0]`, `ref_clk`), so the same
model can host an RMII-level test once someone wants to show that a dead inbound `REF_CLK` leaves the
MAC's RX clock domain stopped and the descriptors empty (board finding #4). Add a bandwidth limit to
the inbound path and it becomes the frequency-sweep bench that answers the real open question:

> Is the shield translator simply too slow for 50 MHz inbound, in which case no re-pinning fixes it
> and the shield is closed as an RMII carrier — or is one shifter IC on this board dead, in which case
> it is a repair?

`docs/LAN8720_BRINGUP_WORK_ITEM.md` §3–§4. The discriminating experiment nobody has run is to move
RMII to **SH1 / bank 94**; every failing configuration so far has stayed on SH0.

## What it does not cover

- The `mode_speed` defect in the multicore wrapper — that is `tests/rmii_speed/`.
- Anything about the PYNQ-Z2, which works.
