# `mdio_phy_model` — RMII virtual-PHY MDIO register model

**Real, synthesizable RTL** (graduated from the Phase-0 stub) — spec §12
calls this out as one of only two blocks needing meaningful new RTL (the
other is `gen_checker/`). Without this block answering the DUT's MDIO polls,
the DUT's PHY bring-up / auto-negotiation poll **hangs** (spec §8.1, §16).

## What it is

A synthesizable MDIO (MDC/MDIO, Clause-22) **slave** that presents a
plausible PHY identity and reports link-up/100/full via the standard
BMCR/BMSR/PHYID1/PHYID2/ANAR/ANLPAR register set, plus an AXI-Lite control
surface (`VPHY` @ `0x44A3_0000`, `docs/contracts/shell-regmap.md`) so the
host/MicroBlaze can inject link events (force link down, pulse a transition)
to test the DUT's link-handling code — spec §8.1: "MicroBlaze-writable so the
host can inject link events... to test the DUT's link handling."

## Module list

| File | Module | Role |
|---|---|---|
| `mdio_slave.sv` | `mdio_slave` | Clause-22 frame engine: samples `mdc`/`mdio_o`/`mdio_oe` from the DUT, decodes preamble/ST/OP/PHYAD/REGAD/TA/DATA, drives `mdio_i` back on a read. Knows nothing about register *meaning* — reaches the register file only through `reg_addr`/`reg_wr_en`/`reg_wr_data`/`reg_rd_data`. Externally clocked by `mdc` (no clock of its own). |
| `phy_reg_model.sv` | `phy_reg_model` | The Clause-22 register *content*: BMCR/BMSR/PHYID1/PHYID2/ANAR/ANLPAR, plus the AXI-Lite -> `mdc`-domain CDC (2-flop synchronizers on the level config, toggle + edge-detect on the one-shot pulse). Also runs on `mdc`. |
| `mdio_phy_model.sv` | `mdio_phy_model` (top) | AXI4-Lite front-end for the `VPHY` regmap (`PHY_STATE`/`PHY_ID`/`LINK_EVENT`), the `s_axi_aresetn` -> `mdc` reset synchronizer, and the instantiation of the two modules above. This is the module that binds to the partition pins and to the shell's AXI-Lite fabric. |

Build note: `tests/mdio_phy_model/Makefile` (outside this block's directory)
lists only `mdio_phy_model.sv` as `VERILOG_SOURCES` — a holdover from when
the whole thing was one file ("no separate testbench wrapper: mdio_phy_model
is a leaf module"). Rather than fold the frame engine and the register file
back into one module, `mdio_phy_model.sv` uses two `` `include `` directives
to pull in `mdio_slave.sv` and `phy_reg_model.sv` (resolved relative to its
own directory, no `-I`/`+incdir` needed — standard for every mainstream
Verilog toolchain, including VCS). The three modules stay genuinely separate
and independently lintable/instantiable; only the *build* stays single-file
for the existing cocotb harness.

## Two register spaces — don't conflate them

1. **AXI-Lite `VPHY` regmap** (shell-regmap.md, MicroBlaze-facing,
   `s_axi_*` on `mdio_phy_model`): `PHY_STATE` (0x00), `PHY_ID` (0x04),
   `LINK_EVENT` (0x08). This is how the *host* configures what the model
   presents and injects events.
2. **MDIO-visible Clause-22 register file** (`mdio_i`/`mdio_o`/`mdio_oe`/`mdc`
   partition pins, DUT-facing): the standard 32-entry (5-bit REGAD) PHY
   management register space — this model implements the handful of
   registers real PHY bring-up code actually reads (registers 6-31 return
   `0x0000`):

   | Reg # | Name | R/W | What drives it |
   |---|---|---|---|
   | 0 | `BMCR` (Basic Mode Control) | R/W | DUT-writable; echoed back on read. `[15]` reset and `[9]` restart-auto-neg are self-clearing (read back 1 for exactly one `mdc` cycle, then auto-clear — a `[15]` write also reloads `ANAR` to its default). Reset default `0x3100` (auto-neg-enable \| speed100 \| full-duplex). |
   | 1 | `BMSR` (Basic Mode Status) | RO | Unconditionally AXI-Lite-configured (see "Read-back policy" below). `[14:11]` tie a fixed 100BASE-TX/10BASE-T FD+HD capability advertisement; `[8]`/`[6]`/`[4]`/`[1]` tie 0; `[7]`/`[3]`/`[0]` tie 1; `[5]` (auto-neg complete) and `[2]` (link status) both track `effective_link_up` (see below). |
   | 2 | `PHYID1` | RO | `PHY_ID[31:16]` |
   | 3 | `PHYID2` | RO | `PHY_ID[15:0]` |
   | 4 | `ANAR` (Auto-Neg Advertisement) | R/W | DUT-writable; latched and echoed back on read (and on a `BMCR[15]` reset). Reset default `0x01E1` (IEEE 802.3 selector \| 10BASE-T \| 10BASE-T-FD \| 100BASE-TX \| 100BASE-TX-FD). Not authoritative for anything else the model reports. |
   | 5 | `ANLPAR` (Auto-Neg Link Partner Ability) | RO | `0x0000` when `effective_link_up` is false ("nothing negotiated"); otherwise advertises 100BASE-TX (`[7]`) or 10BASE-T (`[5]`) per `PHY_STATE.speed100`, the matching full-duplex bit (`[8]`/`[6]`) per `PHY_STATE.full_duplex`, selector `00001`, and ack (`[14]`) tied to `effective_link_up`. |

   **Read-back policy** (resolves the Phase-0 stub's open question — "does
   the model reflect what the DUT wrote, or always reassert the AXI-Lite-
   configured values?"): `BMSR`/`PHYID1`/`PHYID2`/`ANLPAR` are read-only and
   driven *unconditionally* from the AXI-Lite-configured state. The DUT can
   never write its way into a link state the host didn't configure — that's
   what makes a host-injected `LINK_EVENT` authoritative over whatever the
   DUT's own driver believes it negotiated. `BMCR`/`ANAR` are real
   read/write registers (a PHY driver has to be able to write them) but
   never feed back into `BMSR`/`ANLPAR`.

## Link-up defaults

Out of reset (before the host writes anything to `PHY_STATE`), the model
already reports link-up/100/full over MDIO, so the DUT's PHY bring-up never
stalls waiting on a host write:
- `PHY_STATE` config defaults: `link_up=1`, `speed100=1`, `full_duplex=1`,
  `force_down=0` — so `BMSR` bits `[5,3,2,0]` (auto-neg complete, auto-neg
  capable, link status, extended capability) are all set, and `ANLPAR`
  advertises 100BASE-TX full-duplex (`ANLPAR[8]`), at reset.
- `PHY_ID` default: **`0x0007_C0F1`** — an SMSC LAN8720A-style OUI/model ID
  (`PHYID1=0x0007`, `PHYID2=0xC0F1`). Chosen because that family is the
  closest real-world analog to the MPS3 board's own SMSC LAN9220 (spec §12);
  it is a documented placeholder, not a claim of exact silicon match, and is
  fully host-overridable via the `PHY_ID` AXI-Lite register.
- `BMCR` default `0x3100`, `ANAR` default `0x01E1` (see table above).

## Host-injected link events -- how they map onto the MDIO-visible registers

`LINK_EVENT` (`VPHY` offset `0x08`) has two independent mechanisms:

- **`force_down` (bit 0, persistent level).** While set, `effective_link_up`
  is forced to 0 regardless of `PHY_STATE.link_up`: `BMSR.link_status` and
  `BMSR.auto_neg_complete` both clear, and `ANLPAR` collapses to `0x0000`.
  Clearing it (write `LINK_EVENT=0`) restores whatever `PHY_STATE` says. This
  is the re-attach path the coordinator firmware uses (`swap_fsm.c`'s
  `step_release()` writes `VPHY_LINK_EVENT=0` to bring the virtual PHY back
  up after a DFX swap — spec §6.3 "virtual PHY re-asserts link-up").
- **`pulse` (bit 1, one-shot strobe).** Write 1 to inject a transient
  link-down blip without a separate follow-up write: the AXI-Lite side
  self-clears the bit the very next `s_axi_aclk` cycle (so a readback almost
  always sees 0), while a toggle crosses into the `mdc` domain and, on the
  detected edge, forces the link down for `C_PULSE_HOLD_CYCLES` (default 32)
  `mdc` cycles before automatically releasing back to whatever the level
  config (`link_up`/`force_down`) says. Useful for exercising a DUT's
  link-change interrupt/poll handling without needing two synchronized
  writes from the host.

Both mechanisms combine with `PHY_STATE.link_up` into one `effective_link_up`
signal inside `phy_reg_model.sv`, which is the single thing `BMSR`/`ANLPAR`
actually key off — `PHY_STATE.speed100`/`full_duplex` separately shape *what*
`ANLPAR` advertises once linked.

## Address decode — full local word address (RESOLVED 2026-07-09)

The AXI4-Lite `VPHY` decode now compares the **full** local word address
(`s_axi_awaddr[11:2]` / latched `s_axi_araddr[11:2]`), so only the three mapped
offsets (`PHY_STATE` 0x00 / `PHY_ID` 0x04 / `LINK_EVENT` 0x08) respond; every
other offset in the block's page (including reserved 0x0C) is unmapped — reads
return 0, writes are accepted (`BRESP=OKAY`) with no effect. Previously only
`addr[3:2]` were decoded, so unmapped offsets aliased onto real registers (first
alias `0x10 → PHY_STATE`): worse, a stray **write** aliasing onto `LINK_EVENT`
could fire the self-clearing `PULSE` strobe and its `mdc`-domain link-down.
Flagged by the SystemRDL decode-equivalence gate
(`poc/systemrdl/EQUIV_ALL_RESULT.txt`, "vphy 0x10 → PHY_STATE"). Mapped-offset
behaviour — including the `LINK_EVENT.PULSE` one-shot and the AXI→`mdc` CDC — is
bit-for-bit unchanged.

## Clock domains / CDC

- `mdio_slave.sv` and `phy_reg_model.sv` both run on `mdc` — the DUT's own
  MDIO clock (Clause-22 caps it at 2.5 MHz; the model doesn't assume a rate
  and tolerates `mdc` stopping between transactions, since it is an
  **externally clocked slave** with no clock of its own).
- The only real clock-domain crossing is AXI-Lite (`s_axi_aclk`) ->
  `mdc`, both in `mdio_phy_model.sv` (a 2-flop reset synchronizer) and inside
  `phy_reg_model.sv` (2-flop synchronizers per config bit / per `phy_id` bit,
  plus a toggle + edge-detect for the one-shot `pulse`). The level config is
  host/test-paced — at most one update every many `mdc` cycles in practice —
  so a bit sampled in the one `mdc` cycle immediately following an AXI-Lite
  write can read stale-vs-fresh for that single cycle; every cycle after
  that it is consistent. **A cocotb bench should allow at least a few `mdc`
  cycles of settling after an AXI-Lite `VPHY` write before relying on the
  next MDIO read to see it** — in practice this is automatic, since even the
  shortest possible MDIO read (32-bit preamble + 14-bit header before any
  data is sampled) takes far longer than the 2-cycle synchronizer delay.

## Port groups (see `mdio_phy_model.sv`)

1. **AXI4-Lite slave** — `VPHY` regmap, standard `s_axi_*` naming (matches
   `dut_clkrst.sv`/`dfx_ctl.sv` style for consistency across shell IP).
2. **MDIO partition pins** — names/directions per
   `docs/contracts/partition-pins.md` lines 49-52, shell's view: `mdc` (I),
   `mdio_o` (I), `mdio_oe` (I) sampled from the DUT; `mdio_i` (O) driven back
   to the DUT.

## Verification

`verilator --lint-only -Wall` passes clean on all three files (standalone
for `mdio_slave.sv`/`phy_reg_model.sv`; `mdio_phy_model.sv` pulls the other
two in via `include` for the same single-file-friendly reason the build
does). The two intentionally-unused AXI signal groups (`awprot`/`arprot` --
no privilege/secure distinction in this block; the upper, undecoded address
bits -- only `[3:2]` select among this block's 3 registers, the rest is
already qualified by the BD's per-block page decode) are wrapped in
`verilator lint_off/on UNUSED` with an inline justification rather than
silently ignored.

`tests/mdio_phy_model/test_mdio_phy_model.py` (cocotb, VCS) is the intended
functional bench -- it drives the model as a bit-banging Clause-22 MDIO
master (`tests/common/mdio_master.py`) crossed with AXI-Lite `VPHY` writes
(`tests/common/regmap.py`), and was written bench-first against this exact
register table. `tests/common/dut_presence.rtl_ready()` gates it on the
literal "Phase 0 stub" marker string being gone from `mdio_phy_model.sv`,
which it now is -- confirmed with `python3 tests/common/list_benches.py`
reporting `READY mdio_phy_model ...`.
