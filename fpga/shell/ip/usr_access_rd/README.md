# `usr_access_rd` — USRACC, the fabric's own build identity

`docs/VERSIONING_PLAN.md` §3.4 ("Deferred: fabric readback of `USR_ACCESS` —
ride the next re-key"), built. It is a small read-only AXI4-Lite CSR block
wrapping the **one** `USR_ACCESSE2` BEL on this device, so the running fabric
can tell the MicroBlaze which bitstream it is.

## Why it exists

`ba2f4be` built the whole firmware/bitstream cross-check **except the sensor**,
and said so in its own commit message: the `version` verb carries `usr_access`
(what the FABRIC says it is) beside `ver32` (what the IMAGE was built as) plus a
three-state `skew` verdict; the codec, the pyverify client, the operator command
and the FakeShell conformance are all in place and correct — and it reads
`null` / `null` on every board, because `coordinator_handle_version()` has
nothing to read.

One generator (`scripts/gen_version.py`) produces `HARNESS_VER32`.
`fpga/dfx/build_dfx.tcl` stamps it into `BITSTREAM.CONFIG.USR_ACCESS` (the
device AXSS configuration register) and the same number is compiled into the
image. They agree **by construction** — unless the `.bit` and the image baked
into it came from different builds. That is the *"flashable base whose
`updatemem` was never re-run"* hazard this platform has already been bitten by,
and nothing running on the board could see it. This block is what makes it a
readable number.

## Register map (USRACC, 64 KiB page)

Generated into `firmware/common/platform_regs.h`, `tests/common/regmap.py`,
`host/pyverify/pyverify/regmap.py` and `docs/contracts/shell-regmap.md` by
`tools/gen_regmap.py` — **do not restate the base anywhere else**.

| offset | name | access | meaning |
|---|---|---|---|
| `0x00` | `MAGIC` | ro | `0x55535241` (`"USRA"`) — the block-present witness |
| `0x04` | `VALUE` | ro | the AXSS word: `HARNESS_VER32` as the `.bit` carries it |
| `0x08` | `STATUS` | ro | `[0] VALID` — a stable AXSS sample has been captured since reset (sticky; does **not** follow the primitive's `DATAVALID`, see below) |

**`MAGIC` is not decoration.** Every unmapped page in this shell's AXI-Lite
window reads back `0`, so on a shell without this block a bare `VALUE` read
returns `0` — and `0` is a perfectly plausible build identity. Without the
witness, *"the fabric says `0x00000000` and it disagrees with your image"* and
*"there is no sensor here"* are the same read. Firmware
(`firmware/platform/mps3_usr_access.c`) reads `MAGIC` first, then `STATUS.VALID`,
and answers `""` — which the codec renders as `null` — for either miss. A zero,
or a `false` skew with no value, would read as "checked, fine": the one answer
this must never give for a check that did not happen.

## Why a block of its own, and not `DFXCTL.SHELL_USR_ACCESS @ 0x44A1_001C`

§3.4 proposed the register inside DFXCTL, where there is free decode space. The
**mechanism** was taken exactly as specified; the **address** was not:

* `dfx_ctl` is the swap-critical block — the one `swap_fsm.c` pokes first and
  last in every reconfiguration, and the one whose reset behaviour is being
  changed in this same wave (the watchdog un-clamp hazard,
  `docs/planning/SERVICES_PARTITION.md` §5.4). Adding a `CONFIG_SITE` BEL
  dependency to it would put a device-global configuration primitive inside the
  block that most needs to stay trivially reviewable, and would drag that
  primitive into `tests/dfx_ctl`'s elaboration.
* The two answer different questions. DFXCTL is about the **RP**: what is
  loaded, is it clamped, is it held in reset. This is about the **static**:
  which build am I.
* It costs one 64 KiB page out of a region that had to be extended anyway, and
  one interconnect master port. Both are re-key-class costs, and this change is
  a re-key regardless.

## Simulation, and the one rule this bench keeps

There is **no `ifdef` in the RTL**. `USR_ACCESSE2` is instantiated
unconditionally, exactly as it synthesises; `tests/usr_access_rd/` supplies its
own module of that name (`usr_accesse2_model.sv`, plusarg-driven) because
unisims is not on the bench's library path. The substitution lives in the
bench's *file list*, where it is visible, not inside the shipping RTL. This
repo's most expensive bug (bug #1, the CSR address-decode escape) was exactly
"the thing under test was not the thing that ships".

```
source set_env.sh
make -C tests/usr_access_rd                 # 3/3 PASS
make -C tests/usr_access_rd control-value   # MUST FAIL (3/3 FAIL)
make -C tests/usr_access_rd control-decode  # MUST FAIL (3/3 FAIL)
make -C tests/usr_access_rd control-silicon # MUST FAIL (3/3 FAIL) -- the pre-fix RTL
make -C tests/usr_access_rd USRACC_DV_MODE=level  # old primitive model, 3/3 PASS
```

Recorded because it was **tried and came out green**, so nobody repeats it:
driving the registers at offset-*without*-base is **not** a control. `LOCAL_ADDR_W`
caps the decode at this block's own 64 KiB page, so `araddr[31:16]` is ignored
by design — selecting the page is the interconnect's job. Only a mutation of the
decode itself reproduces bug #1, which is what `control-decode` does.

---

# STATIC-SIDE CHANGES — for the MINT-PREP lane

**Every overlay is re-keyed.** This is a static RTL + BD change: it moves the
static netlist and therefore `static_id`, so **all** fielded partials are
invalidated and every RM in `fpga/dfx/rm_list.tcl` must be re-implemented against
the new locked static and re-stamped. There is no subset. The overlays affected
are the whole set — as of this writing: `rm_greybox`, `rm_regdemo_a`,
`rm_regdemo_b`, `rm_led`, `rm_uart_echo`, `rm_nanosoc`, `rm_eth_ss`,
`rm_nanosoc_multicore`, `rm_nanosoc_upy`, `rm_socscope`, `rm_clcd_demo`,
`rm_nanosoc_iice` — plus the QSPI overlay store and the baked greybox blob, and
a fresh `updatemem` of the flashable base.

**The RP boundary is UNCHANGED.** No partition pin is added, moved or removed —
both new blocks are static-side only, and `usr_access_rd` has no RP-facing port
at all. So `fpga/shell/boundary.yaml`, `docs/contracts/partition-pins.md` and
every generated boundary view are untouched, and `pin_check` (`make check` stage
2) needs nothing.

## 1. BD additions — `fpga/shell/bd/shell_bd.tcl`

| what | where |
|---|---|
| `create_bd_cell ... soclabs.org:user:usr_access_rd:1.0 usr_access_rd_0` | SECTION 3, after `dut_egress_0` |
| `set_property CONFIG.C_S_AXI_ADDR_WIDTH {32} $usr_access_rd_0` | same place, **one `set_property` per line** (the bug-#1 gate matches `CONFIG.*` and the cell on the same line) |
| `usr_access_rd_0` appended to the common CSR clock/reset `foreach` | SECTION 3 |
| `create_bd_cell ... xilinx.com:ip:axi_timebase_wdt:3.0 axi_timebase_wdt_0` | SECTION 3, immediately after |
| `set_property CONFIG.C_WDT_INTERVAL {27}` | ≈1.34 s to `WDS`, ≈2.7 s to the reset, at 100 MHz |
| `set_property CONFIG.WDT_ENABLE_ONCE {Enable_repeatedly}` | **must be set explicitly** — see "measured, not guessed" below |
| `xlconstant gnd_wdt_freeze` → `axi_timebase_wdt_0/freeze` | an unconnected input fails `validate_bd_design` |
| `axi_timebase_wdt_0/wdt_reset` → `proc_sys_reset_shell/aux_reset_in` | **`aux_reset_in` was unconnected in every shell up to `0xA8C1C535`** |
| `axi_timebase_wdt_0/wdt_reset` → `dfx_ctl_0/wdt_reset_i` | the set-dominant clamp |
| `sys_rst_n` **port** → `dfx_ctl_0/ext_por_n_i` | the raw POR port, **not** any `proc_sys_reset` output — every one of those is pulsed by `aux_reset_in`, i.e. by the watchdog |
| `CONFIG.NUM_MI` **19 → 21** | `M19` = `usr_access_rd_0/s_axi`, `M20` = `axi_timebase_wdt_0/S_AXI`; existing masters are **not** renumbered |

`wdt_interrupt` / `timebase_interrupt` are deliberately left unconnected: the
INTC concat is full at 4 ports, the first-stage expiry is already visible as
`TWCSR0.WDS`, and a superloop that has stopped cannot service an interrupt.

## 2. Address pages

| block | master | page | range |
|---|---|---|---|
| `USRACC` (`usr_access_rd_0`) | `M19` | **`0x44B3_0000`** | 64K |
| `WDOG` (`axi_timebase_wdt_0`) | `M20` | **`0x44B4_0000`** | 64K |

`tools/gen_regmap.py`: `WINDOW_HI` and `REGION_HI` both `0x44B3_0000` →
**`0x44B5_0000`**, each with the comment the generator's own full-region message
demands ("a new block must extend `REGION_HI`, **and say so**"). All four
generated views regenerated; `check_generated_fresh.py` green.

`WDOG` is a **vendor** block: its offsets are quoted from PG128 into
`VENDOR_REGS`, not derived from RTL, and are labelled `[vendor doc, not derived]`
in every emitted view.

## 3. The gated touch add-on — `fpga/shell/bd/touch_iic_add.tcl`

**`CONFIG.NUM_MI` 20 → 22, and `touch_iic_0` moves `M19` → `M21`.** Its address
stays `0x44AE_0000`, which is TOUCH's by contract.

This file is sourced **only** under `SHELL_TOUCH=1`, so a default regen never
runs it and a stale number here fails **nothing** until somebody builds with
touch — at which point two blocks would be wired to `M19` and one of them would
simply not be there. That is why the default build's lane owns the edit.

## 4. Packaging — `fpga/shell/ip_packaged/package_csr_ip.tcl`

`usr_access_rd` added to the `blocks` dict (single `.sv`, default keep-list
`{s_axi s_axi_aclk s_axi_aresetn}`). It contains a **Xilinx primitive**, which is
new for this repo's packaged IP and needs no special handling: unisim primitives
are resolved by the synthesis tool, not by packaging.

## 5. `static_canon` inputs — no line needed

`fpga/dfx/tools/static_inputs.txt` already covers both changes, and says so in
its own §"IDENTITY-REG" note: `fpga/shell/ip/*.sv` hashes
`fpga/shell/ip/usr_access_rd/usr_access_rd.sv`, and
`include fpga/shell/bd/shell_bd.tcl` hashes the BD instantiation, the address
assignment **and** the watchdog IP configuration (which has no local RTL). A new
line would only be needed if this block ever ships a `.xdc` or a `.tcl` — it
ships neither.

## 6. Also changed, static-side, in the same wave

* `fpga/shell/ip/dfx_ctl/dfx_ctl.sv` — **two new input ports**
  (`ext_por_n_i`, `wdt_reset_i`) and the reset fix they serve. This re-keys on
  its own; see §5.4 of `docs/planning/SERVICES_PARTITION.md` and the RTL header.
* `scripts/harness_gates/check_bench_param_parity.py` — `usr_access_rd`
  registered in `CSR_BLOCKS`. Not static, but it must land in the same commit:
  an unregistered cell reads as **silence**, not as a complaint.

## 7. Measured, not guessed — `axi_timebase_wdt:3.0` on `xcku115`, Vivado 2024.1

`docs/planning/SERVICES_PARTITION.md` §5.5 recorded that
`C_WDT_ENABLE_ONCE`/`C_WDT_INTERVAL` "were rejected by `set_property` ... the
exact parameter names need confirming against the live Customize IP dialog".
Probed directly. The findings, in order of how much they would have cost:

1. **A misspelled `CONFIG.*` on a BD cell is NOT an error.**
   `set_property CONFIG.C_WDT_ENABLE_ONCE 0 $wdt` emits only
   `CRITICAL WARNING: [BD 41-1276] ... Parameter does not exist` and **returns
   success**, then reads back empty. That is why the study read it as
   "rejected". Anything set that way is silently a no-op.
2. The real names are **`C_WDT_INTERVAL`** (integer, default `30`) and
   **`WDT_ENABLE_ONCE`** (an **enum**, not 0/1). Valid values:
   `Enable_repeatedly`, `Enable_only_once`.
3. **The default is `Enable_only_once`** — exactly the latch-and-never-adjust
   behaviour §5.5 says must not ship. *Leaving the parameter alone is the wrong
   answer*, so it is set explicitly.
4. `proc_sys_reset:5.0`'s **`C_AUX_RESET_HIGH` defaults to `1`**, and the WDT's
   `wdt_reset` is active high, so they connect directly with no inverter.
   `C_AUX_RST_WIDTH` is `4`, and `peripheral_aresetn` is held low for a **tail**
   after `aux_reset_in` deasserts — that tail is why a naive "clamp while
   `wdt_reset` is high" fix does not work, and it is modelled in the bench.
5. WDT pins on this version: `s_axi_*`, `freeze`, `wdt_reset`, `wdt_interrupt`,
   `timebase_interrupt`. Bus interface `S_AXI`, address-block segment `Reg`.

## The 2026-09-22 silicon failure and its fix

On `0x3F1A560F` the block read `MAGIC=0x55535241`, `VALUE=0`, `VALID=0`, with
the AXSS word proven present in the fielded `.bit`
(`docs/evidence/2026-09-w2/usracc_20260922.txt`). The first RTL captured `DATA`
only after a synchronised `DATAVALID`. UG570 (v1.9.1 p.121, Figure 7-9) draws
`DATAVALID` as a **one-`CFGCLK`-cycle pulse per AXSS write**, and the
bitstream's AXSS write happens during configuration — while GSR holds the fabric
and long before `s_axi_aresetn` releases — so the pulse is gone before the
block can see it. The bench passed because its model raised `DATAVALID` as a
level *after* reset; the model was wrong before the RTL was.

Fix: the model now fires the pulse during reset and stops `CFGCLK` (default
`USRACC_DV_MODE=pulse`), which reproduces the silicon read against the old RTL
(`control-silicon`, snapshot in `tests/usr_access_rd/control/`). The RTL now
samples `DATA` on `s_axi_aclk` through a 2-FF synchroniser, publishes a word only
when two successive synchronised samples agree, and ignores `DATAVALID`.
Unproven on silicon until the next mint is fielded.
