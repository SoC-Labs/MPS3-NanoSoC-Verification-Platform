# fpga/rp/common — shared pieces for RMs that carry ILAs

An RM reaches Vivado's hardware manager over XVC through the static
`debug_bridge_0` (mode 2, `C_NUM_BS_MASTER=1`), whose BSCAN master crosses the
partition as the 12 `dbg_bscan_*` pins. An RM that wants ILAs holds its own
debug hub on those pins. Design and evidence:
`docs/planning/HANDOVER_RM_ILA_OVER_XVC.md` §1, §4.6;
`docs/planning/ILA_MINT_PLAN_2026-09-23.md` §3 DEBUG-RM.

| File | What it is |
|---|---|
| `dbg_ip.tcl` | `dbg_ip_build ip_dir part bridge_name ila_specs ?hub_clk_hz?` — creates, in a throwaway in-memory project, a `debug_bridge` (mode 1, `C_DESIGN_TYPE 1`, 50 MHz hub clock, `C_USE_BUFR 0`) and one `ila` per spec `{name probe_widths depth ?strg_qual?}`, runs `generate_target all` + `synth_ip` on each, and returns the `.xci` list to `read_ip`. `dbg_rm_netlist_checks ila_names` prints the `DBG_CHECK` lines (0 BSCANE2, 0 clock cells, exactly 1 xsdbm, ≥1 ILA, LUT/FF/BRAM) and returns the failure count. |
| `rp_dbg_hub.sv` | The hub: wraps the mode-1 bridge under the fixed module name `rp_dbg_bridge`. Ports `clk` (= `phy_rmii_ref_clk`, the only always-on shell clock in the RP) and the 12 `dbg_bscan_*`. |

## Adding ILAs to an RM

1. Make the RM `synth_mode "prebuilt"` (an inline RM reads one `.sv` and cannot
   carry IP) and set `RM_LIB(<rm>,debug) 1` in `fpga/dfx/rm_list.tcl`.
2. In its `ooc_synth.tcl`, BEFORE its own `create_project`:
   `source fpga/rp/common/dbg_ip.tcl`, then
   `set xcis [dbg_ip_build $out_dir/ip $part rp_dbg_bridge {{ila_x {8 1} 4096}}]`.
   Then `read_ip $xcis`, read `rp_dbg_hub.sv` + the wrapper, `synth_design
   -mode out_of_context`, and gate the completion marker on
   `dbg_rm_netlist_checks {ila_x}` returning 0.
3. In the wrapper: `rp_dbg_hub u_dbg_hub (.clk(phy_rmii_ref_clk), .dbg_bscan_*…)`
   plus the ILAs, clocked by what they sample. The ILA joins the hub at
   `opt_design` — no `connect_debug_cores`.
4. OOC XDC: `create_clock -period 80.000` on `dbg_bscan_tck` and
   `dbg_bscan_drck`, asynchronous groups against `phy_rmii_ref_clk` and
   `dut_clk`. Synthesis only.

## Rules that bite

- **No clock buffer and no BSCANE2 in the RP** (handover F7, traps 1–2). The
  checks enforce both.
- **ILA ⇒ hub.** An ILA with no hub in the RM makes Vivado look for a BSCANE2
  (HDPR-16).
- **One hub per RM**, on `phy_rmii_ref_clk`, never `dut_clk` (the DRP can stop it).
- **Probes see only what the RM's own RTL exposes.** Synthesis cannot reach
  into a read-only SoC hierarchy, and netlist insertion (`mark_debug` +
  `create_debug_core` on the linked RM) was never tested.

Examples: `fpga/rp/dbg_demo/` (a counter), `fpga/rp/nanosoc_ila/` (nanosoc's
boundary nets).
