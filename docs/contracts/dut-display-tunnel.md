# Contract: the DUT display tunnel (RP → shell, over the spare `dut_gpio` bits)

**Version:** v1.0 (2026-07-14, W1 of `CLCD_KVM_WAVE_PLAN.md` (internal note, not in the public tree)). **FROZEN.**

This is the wire-level encoding by which a **DUT-side** display accelerator (inside
the nanosoc RM) reaches the on-board QVGA panel. It is the seam between
`fpga/rp/nanosoc_exp/` (RM side) and `fpga/shell/ip/clcd_kvm/` (shell side), and
those two are written by **different agents who do not talk to each other**. Every
bit is normative.

Companion documents:
`fpga/shell/ip/clcd_kvm/README.md` (the KVM), `fpga/rp/nanosoc_exp/README.md` (the
socket a student fills in), `docs/contracts/partition-pins.md` (the RP boundary this
does **not** change), `docs/CLCD_PANEL_FACTS.md` (the panel that is actually lit).

---

## 1. What the tunnel is, and why it costs nothing

The panel pads are **static** and shell-owned. A DUT-side 8080 bus master needs ~13
signals to reach the shell. Adding real partition pins for them would cost: an edit
to `partition-pins.md`; ports on **all 8 RM wrappers** (`fpga/dfx/pin_check.py` has
no concept of an optional port — `MISSING` and `EXTRA` are both hard errors,
`pin_check.py:164-166`); an **OOC re-synthesis** of all 8 RMs; and new
`dfx_decoupler` interfaces whose `DECOUPLED_VALUE` would have to be `0x1` on the
active-low strobes — the *inverse* of all 15 existing entries, which are uniformly
`0x0` (`shell_bd.tcl:611-630`). That last one is the easiest thing in the whole
change to get wrong, and getting it wrong holds the panel selected, in reset, with
both strobes asserted, for the entire duration of a partial reconfiguration.

Instead: **16 DUT→shell wires are already crossing the boundary, already decoupled,
and are physically discarded today.**

- `dut_gpio_o[15:0]` and `dut_gpio_oe[15:0]` both cross the RP boundary
  (`fpga/shell/rp_dut_stub.sv:55-56`) and are both already decoupler members
  (`shell_bd.tcl:626`, **ID 13** and **ID 14**, `WIDTH 16`, `DECOUPLED_VALUE 0x0`).
- In the shell, **only the low 8 bits of each are used**: `shell_top.sv:352-354`
  drives `USER_nLED` from `board_gpio_pad_o[7:0] & board_gpio_pad_oe[7:0]`.
  `shell_top.sv:349-350` says outright that the upper half *"have no pad to drive
  and are intentionally unconnected."*

So the upper 8 bits of **both** vectors are free. The tunnel takes them.

**Cost: zero.** No `partition-pins.md` edit. No RM-wrapper port change. No OOC
re-synthesis. No new decoupler interface. **`pin_check` stays green, untouched** —
and that is the *proof* the tunnel cost nothing (Gate 2).

---

## 2. The bit map — FROZEN

Everything below is **post-decoupler** (the shell taps the same clamped nets that
already feed `board_gpio_0`), so the existing clamp protects the KVM too.

### `dut_gpio_o[15:8]` — the 8080 data byte

| Tunnel bit | Signal | Notes |
|---|---|---|
| `dut_gpio_o[15]` | `PD[7]` | MSB → pad `CLCD_PD[17]` (`AR16`) |
| `dut_gpio_o[14]` | `PD[6]` | |
| `dut_gpio_o[13]` | `PD[5]` | |
| `dut_gpio_o[12]` | `PD[4]` | |
| `dut_gpio_o[11]` | `PD[3]` | |
| `dut_gpio_o[10]` | `PD[2]` | |
| `dut_gpio_o[9]`  | `PD[1]` | |
| `dut_gpio_o[8]`  | `PD[0]` | LSB → pad `CLCD_PD[10]` (`AN17`) |

i.e. **`dut_gpio_o[15:8] = PD[7:0]`**, MSB-aligned, no permutation. The
block-bit → pad mapping on the shell side is board-verified
(`docs/CLCD_PANEL_FACTS.md` §2).

### `dut_gpio_oe[15:8]` — the control/status byte

| Tunnel bit | Signal | Sense | v1 |
|---|---|---|---|
| `dut_gpio_oe[8]`  | **`cs`**    | **ACTIVE-HIGH** — `1` = chip select **asserted** | live |
| `dut_gpio_oe[9]`  | **`wr`**    | **ACTIVE-HIGH** — `1` = write strobe **asserted** | live |
| `dut_gpio_oe[10]` | **`rs`**    | `0` = **command**, `1` = **data** (not a strobe; no inversion) | live |
| `dut_gpio_oe[11]` | **`rd`**    | **ACTIVE-HIGH** — `1` = read strobe asserted | **RESERVED — drive `0`** |
| `dut_gpio_oe[12]` | **`pd_oe`** | **ACTIVE-HIGH** — `1` = the DUT is driving `PD` | **RESERVED — drive `0`** |
| `dut_gpio_oe[13]` | **`busy`**  | **ACTIVE-HIGH** — `1` = the DUT's display engine is **not quiescent** | live |
| `dut_gpio_oe[14]` | **`req`**   | **ACTIVE-HIGH** — `1` = the DUT **requests** the panel | live |
| `dut_gpio_oe[15]` | **`spare`** | — | **RESERVED — drive `0`** |

**Every RESERVED bit must be driven `0`.** One rule, and it is the *same* value the
decoupler clamps to — so an RM that ignores the display entirely, an RM that is
mid-swap, and an RM that predates this contract all present **identical, safe** bits.

`dut_gpio_o[7:0]` and `dut_gpio_oe[7:0]` are **UNTOUCHED**. They are still
`board_gpio`'s LED path (`shell_top.sv:352-354`). **The LEDs keep working while the
DUT drives the display.**

### Signal semantics (restated normatively — the socket README repeats these locally)

- **`busy`** ≡ *"the accelerator's display engine has any byte in flight **or**
  queued"* — i.e. `(fsm != IDLE) || !fifo_empty`. It is the exact analogue of the
  shell block's `!(fifo_empty && !busy)`. The KVM's safe-switch gate reads
  `dut_quiet = !cs && !busy`.
- **`req`** is sampled as an **edge**, not a level: **rising** → *"I want the
  panel"*, **falling** → *"you can have it back"*. It is only a request source at
  all when the KVM's `CTRL.dut_req_en` is set (reset **0** — firmware opts the DUT
  in). The `USER_nPB1` button overrides it either way.
- **`rs`** clamps to `0` = *"command"*, which is harmless with `cs` idle.

---

## 3. 🚨 STROBES ARE CARRIED **ACTIVE-HIGH**. THIS IS A SAFETY PROPERTY. 🚨

The pads `CLCD_CS`, `CLCD_WR_SCL` and `CLCD_RD` are **active-LOW** at the panel
(`docs/CLCD_PANEL_FACTS.md`; `clcd.sv:99-101`). The tunnel carries them
**active-HIGH**, and **the inversion happens inside the KVM
(`fpga/shell/ip/clcd_kvm/clcd_kvm.sv`) and nowhere else.**

**Why — and do not "tidy this up":**

The decoupler clamps `dut_gpio_o` and `dut_gpio_oe` to **`0x0`**
(`shell_bd.tcl:626`, IDs 13/14, `DECOUPLED_VALUE 0x0`) whenever the RP is
decoupled — i.e. for the whole duration of every partial reconfiguration, and
during every RM swap. With **active-high** encoding, `0x0` decodes to:

> `cs = 0` (deselected) · `wr = 0` (idle) · `rd = 0` (idle) · `pd_oe = 0` ·
> `busy = 0` (quiescent) · `req = 0` (no request) · `PD = 0x00`

— i.e. **"all strobes idle, nothing requested, nothing in flight", *by
construction*, with no special clamp value, no KVM logic, and no way for a future
maintainer to get it wrong.**

Under the "natural" active-low encoding, the same `0x0` clamp would decode to
**`CS` asserted, `WR` asserted and `RD` asserted, continuously, for the entire
reconfiguration** — spraying the panel while the RP's outputs are garbage. Avoiding
that would need `DECOUPLED_VALUE 0x1` on individual bits of a 16-bit vector the
decoupler clamps as one word — which it *cannot express*. There is no fix at the
decoupler; the fix is the encoding.

A second property falls out for free: **`busy` is active-high, so a clamped or
absent RP reads as *quiescent***. The KVM's safe-switch gate therefore can never
hang waiting for a dead RP to declare itself idle.

> **If you are reading this because the active-high strobes looked like a bug and
> you were about to "fix" them: they are not a bug. Inverting them re-introduces a
> panel-corrupting, silicon-only failure that no simulation of the RM alone will
> catch. The KVM's forced-revert FSM (`clcd_kvm/README.md` §9) is *belt-and-braces
> on top of this*, not a substitute for it.**

---

## 4. An RM that has no display simply does nothing

There is no "display present" capability bit and none is needed.

An RM that does not implement a display leaves `dut_gpio_o/oe[15:8]` at whatever
its GPIO drives — and every existing RM (`rm_greybox`, `rm_led`, `rm_uart_echo`,
`rm_nanosoc` today, …) drives `dut_gpio_oe[15:8]` from a GPIO output-enable that is
`0` at reset. The KVM sees `cs = 0` (idle) and `busy = 0` (quiescent) and **nothing
happens**: no cycle is ever launched at the panel, and the DUT can never be granted
ownership because `req` never rises (and `CTRL.dut_req_en` is `0` at reset anyway).

**No existing RM needs any change.** That is the second half of "cost: zero".

---

## 5. Timing floor — NORMATIVE (the tunnel is a CDC)

The tunnel is **asynchronous**: the RM drives it in the **`dut_clk`** domain
(**50 MHz** shipped) and the KVM samples it in **`s_axi_aclk`** (**100 MHz**). The
KVM synchronises all 16+16 bits (2-FF) and then applies a **2-cycle stability
filter** — the pad-facing DUT registers update only when the synchronised vector
has been identical on two consecutive shell cycles — so **per-bit synchroniser skew
can never tear the 8080 vector apart at the pads**
(`fpga/shell/ip/clcd_kvm/README.md` §12).

That guarantee is conditional on the DUT respecting a floor:

> **Every 8080 phase the accelerator drives lasts ≥ 8 `dut_clk` cycles.
> `PD` and `RS` are stable ≥ 4 `dut_clk` cycles before `wr` asserts and ≥ 4
> `dut_clk` cycles after `wr` deasserts.
> `dut_clk` must not exceed `s_axi_aclk` (100 MHz) while the DUT owns the panel.**

At the shipped 50 MHz `dut_clk` that is **≥160 ns per phase** and **≥80 ns of PD/RS
guard band** — 8× and 4× the KVM's ≤20 ns filter latency. It is also comfortably
above what the panel needs: the *harness* drives 20 ns setup / 40 ns `WR` low /
40 ns `WR` high and the panel renders (`docs/CLCD_PANEL_FACTS.md` §6).

Reference accelerator defaults: `CS_SETUP = WR_LO = WR_HI = 8` `dut_clk` cycles →
~480 ns/byte → ~2 MB/s → a full 320×240×2 = 150 KB repaint in **~75 ms**. Ample.

`dut_clk` is programmable via the MMCM DRP (`0x44AB_0000`). The 100 MHz ceiling is a
**hard rule**, not a guideline: above it the stability filter can no longer
guarantee a settled vector.

---

## 6. Shell→DUT: there is **no** grant-back wire in v1 (and the DUT does not need one)

`dut_gpio_i[15:0]` (shell → RP) is **fully allocated**: `shell_top.sv:354` drives
`board_gpio_pad_i = { USER_SW, led_drive }`, so `[15:8]` are the **DIP switches**
and `[7:0]` are a loopback of the DUT's own LED drive. **Nothing is free.**

**NORMATIVE for v1: the DUT is never told that it owns the panel.** The protocol is:

- The DUT drives `req` to **ask**.
- The DUT's display driver **re-initialises the panel and repaints, unconditionally
  and periodically** (the same shape as the harness's 250 ms refresh loop).
- Bytes the DUT emits while it does **not** own the panel are silently **discarded
  by the KVM**. They cost the DUT some bus cycles and nothing else.
- Because **every** handover hard-resets the panel, an unconditional periodic
  re-init + repaint **converges within one period** whether or not the DUT was ever
  granted the panel, and whether or not it noticed. There is no state to
  reconcile — this is *why* the "reset the panel on every handover" rule (which
  exists for a different reason: the panel is the framebuffer and its state is not
  shared) makes the grant-back wire unnecessary.

The **host** can always see the truth: `CLCDKVM.STATUS.owner` and `CLCDKVM.TUNNEL`.

> **The v2 path, if a real grant-back is ever wanted:** reclaim `dut_gpio_i[7:0]` —
> today it is only a **loopback of the DUT's own LED drive**, i.e. it carries no
> information the DUT does not already have — and put `{grant, panel_ready, …}`
> there. That is a **shell-only** change (`board_gpio.sv` + `shell_top.sv` + the
> KVM); it is still **zero** partition-pin change. It is **explicitly out of scope
> for Waves 2–5** and must not be attempted by any Wave-2 agent: `board_gpio.sv`
> and `shell_top.sv` have other owners.

---

## 7. The tunnel is now **FULL**

16 bits allocated, 16 bits used (one of them `spare`). There is no room for a touch
channel, a second data lane, or anything else. **Any further DUT→shell display
signal requires real partition pins** — Option A of `docs/CLCD_KVM_PLAN.md` §2, with
all of its costs (contract edit, 8 RM wrappers, 8 OOC re-syntheses, new decoupler
INTFs with **inverted** `DECOUPLED_VALUE`s).

Touch is not a candidate: `docs/CLCD_PANEL_FACTS.md` §7.6 — the four touch pads are
deliberately unconstrained, there are no ports, and **no touch capability exists**.
Do not offer it to students.

---

## 8. Provenance, and the honest downside

This is a **deliberate overload of a generic bus with a hidden meaning**. It is
sanctioned by `docs/contracts/partition-pins.md:97-101`:

> *"A shell CSR (regmap, new GPIO block) can also mux/override bits for host-driven
> board I/O when no DUT needs them."*

— i.e. the contract already establishes that the **shell decides what these generic
bits mean**, and that the RP boundary is deliberately pin-agnostic. The tunnel is
the same principle applied to the upper half of the vector, which has no pad at all.

The honest cost is a **documentation burden, not a correctness one**: a reader of
`rp_nanosoc_wrapper.sv` who does not know this file will see a GPIO
output-enable register carrying an 8080 chip select and be baffled. Mitigations,
all normative:

1. This file. It is a **contract**, in `docs/contracts/`, next to `partition-pins.md`.
2. `fpga/rp/nanosoc_exp/README.md` restates the pin semantics **locally** so a
   student never has to find this file to write an accelerator.
3. `rp_nanosoc_wrapper.sv`'s mux (W3-A) and `clcd_kvm.sv` (W2-A) must each carry a
   comment citing **this file by path**, and must not restate the encoding without
   that citation.

**How to promote it to real partition pins**, if the RP boundary is ever re-cut for
some other reason (do it **then**, not before — never re-cut the boundary *for* this):

1. Add a `dut_clcd_*` group to `docs/contracts/partition-pins.md` (the **only**
   declaration point `fpga/dfx/pin_check.py` reads, `:73-75`).
2. Add the ports to **all 8** RM wrappers (`pin_check` treats `MISSING` **and**
   `EXTRA` as hard errors, `:164-166`) — including RMs that will never drive a
   display. OOC-re-synthesise all 8.
3. Add `dfx_decoupler` INTFs. **Keep the active-high encoding anyway** (§3) so
   `DECOUPLED_VALUE 0x0` stays correct — do **not** "restore" active-low strobes at
   the boundary just because you now have dedicated wires. The clamp argument is
   unchanged.
4. `pin_check.py`'s `width_of()` (`:135-144`) parses only a literal `N:0` and the
   one special case `NGPIO-1:0` — a parameterised display bus will surface as a
   spurious width mismatch. Fix it or use literal widths.
5. Free the 16 `dut_gpio` bits and delete this file.
