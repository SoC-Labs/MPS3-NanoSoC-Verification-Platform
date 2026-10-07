// -----------------------------------------------------------------------------
// dfx_ctl.sv — DFXCTL regmap block (shell-regmap.md v0.1 @ 0x44A1_0000)
//
// REAL, synthesizable AXI4-Lite slave (graduated from the A1 Phase-0 stub).
// Port list is UNCHANGED from the stub (already correct/confirmed by
// tests/dfx_ctl/dut_notes.md) — only the internal logic is new.
//
// Responsibility (spec §7 "Decoupling is mandatory", §16, shell-regmap.md):
//   - AXI-Lite front-end for the DFX Decoupler + DFX AXI Shutdown Manager
//     vendor IP (both are Vivado BD cells, `dfx_decoupler_0` /
//     `dfx_axi_shutdown_manager_0` in shell_bd.tcl — this module is the
//     coordinator-facing register layer, not the isolation IP itself).
//   - Owns the "RP reset gate" (shell-regmap.md DFXCTL purpose column):
//     during a reconfig sequence this module must be able to hold
//     rp_resetn low regardless of dut_clkrst.sv's RESET_CTRL.rp_resetn bit —
//     see rp_resetn_gate_o, consumed by dut_clkrst.sv's rp_resetn_gate_i.
//   - RM-load verify readback (I8, v0.1): RM_ID@0x10 = the RP's `rm_id`
//     partition pin, RM_STATUS@0x14 = {dut_eth_irq, dut_lockup, rm_id_valid}.
//     the ground truth `firmware/coordinator/swap_fsm.c`'s step_verify()
//     compares against, and what HARDWARE_HUB_INTEGRATION.md §3 reports as
//     the two-level FpgaStatus `rp.rm_id`.
//
// Sequencing this register file exists to support (spec §6.2, MicroBlaze
// coordinator firmware drives it in this order):
//   1. host requests reconfig -> MB gates XVC/SWD/UART/VPHY link (firmware,
//      not this module)
//   2. MB writes DECOUPLE.decouple_en=1 -> decouple_en_o asserts AND (this
//      module's own composition, see rp_resetn_gate_o below) rp_resetn_gate_o
//      drops in the same cycle-pair -- there is no separate register for the
//      gate itself; it is a hardware-derived consequence of DECOUPLE, not an
//      independent firmware-poked bit (shell-regmap.md's DFXCTL table has no
//      offset for it -- confirmed intentional, see ambiguity list below).
//   3. MB streams partial via HWICAP (separate block)
//   4. MB verifies RM load: DFXCTL.RM_ID == target && RM_STATUS.rm_id_valid
//      (spec §13; firmware/coordinator/swap_fsm.c step_verify(), I25)
//   5. MB writes DECOUPLE.decouple_en=0 -> rp_resetn_gate_o releases once
//      decoupled_i also confirms clean re-coupling (see FSM below)
//   6. MB confirms RM id to host
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module dfx_ctl #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; base
                                           // address (0x44A1_0000, 64 KB page)
                                           // is set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave — MicroBlaze data bus, DFXCTL regmap (shell-regmap.md
  // v0.1: 0x00 DECOUPLE / 0x04 SHUTDOWN / 0x08 STATUS(ro) / 0x10 RM_ID(ro)
  // / 0x14 RM_STATUS(ro))
  // ---------------------------------------------------------------------
  input  logic                          s_axi_aclk,
  input  logic                          s_axi_aresetn,

  // ---------------------------------------------------------------------
  // THE ISOLATION BIT'S OWN RESETS (added with the shell watchdog —
  // docs/planning/SERVICES_PARTITION.md §5.4, "HAZARD — latent today, live
  // the moment a watchdog is wired").
  //
  // `s_axi_aresetn` is `proc_sys_reset_shell/peripheral_aresetn`, which is
  // exactly what `aux_reset_in` — the watchdog's reset — pulses. While
  // `decouple_en_q` was reset by it, a watchdog fire during an ICAP write
  // CLEARED decouple_en and UN-CLAMPED the partition boundary at the precise
  // moment the RP is transient garbage: a stuck `phy_rmii_tx_en` injecting a
  // runaway frame, a stuck `uart_tx_tvalid` flooding the console FIFO, a
  // driven `dut_gpio_oe` fighting a board pad, a spurious `irq_out` — every
  // hazard the decoupler exists to prevent, live, on a board that has just
  // lost its supervisor.
  //
  //   ext_por_n_i  the board POR (`sys_rst_n`), active low. The ONLY reset
  //                that may RELEASE the clamp, because it is the only one the
  //                watchdog cannot assert — a POR reconfigures the whole
  //                device anyway, so there is no RP state left to protect.
  //   wdt_reset_i  `axi_timebase_wdt_0/wdt_reset`, active high, same clock
  //                domain (the WDT is an s_axi_aclk AXI-Lite slave), so no
  //                synchroniser. SET-DOMINANT: a watchdog fire ASSERTS the
  //                clamp on its way out rather than merely preserving it. The
  //                study offered "POR-only reset" OR "set-dominant wdt input"
  //                and preferred the second; both are here, because the second
  //                alone still leaves the POR value to be decided and the
  //                first alone is only correct when the RP was already clamped.
  //
  // Tie `wdt_reset_i` low and `ext_por_n_i` to the board reset in any shell
  // that has no watchdog; the behaviour is then identical to the old RTL
  // except that a peripheral reset no longer clears the clamp.
  // ---------------------------------------------------------------------
  input  logic                          ext_por_n_i,
  input  logic                          wdt_reset_i,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
  input  logic [2:0]                    s_axi_awprot,
  input  logic                          s_axi_awvalid,
  output logic                          s_axi_awready,

  input  logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_wdata,
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,
  input  logic                          s_axi_wvalid,
  output logic                          s_axi_wready,

  output logic [1:0]                    s_axi_bresp,
  output logic                          s_axi_bvalid,
  input  logic                          s_axi_bready,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,
  input  logic [2:0]                    s_axi_arprot,
  input  logic                          s_axi_arvalid,
  output logic                          s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0] s_axi_rdata,
  output logic [1:0]                    s_axi_rresp,
  output logic                          s_axi_rvalid,
  input  logic                          s_axi_rready,

  // ---------------------------------------------------------------------
  // DFX Decoupler control/status (Xilinx dfx_decoupler BD cell)
  // ---------------------------------------------------------------------
  output logic                          decouple_en_o,     // -> DFX Decoupler DECOUPLE
  input  logic                          decoupled_i,        // <- DFX Decoupler STATUS  -> STATUS[0]

  // ---------------------------------------------------------------------
  // DFX AXI Shutdown Manager control/status (Xilinx dfx_axi_shutdown_manager
  // BD cell) — quiesces any AXI master reaching toward the RP before/through
  // decouple (spec §16 "Static shell must survive RP teardown").
  // axi_shutdown_ack_i is accepted/electrically connected but NOT exposed in
  // any register today -- shell-regmap.md's STATUS (0x08) only defines
  // {rp_in_reset, decoupled}, no shutdown-ack bit. Flagged as an ambiguity
  // for A6 below (is a STATUS.shutdown_idle bit wanted?).
  // ---------------------------------------------------------------------
  output logic                          axi_shutdown_req_o, // -> shutdown_req
  // verilator lint_off UNUSED
  input  logic                          axi_shutdown_ack_i,  // <- shutdown_ack / idle (reserved, see comment above)
  // verilator lint_on UNUSED

  // ---------------------------------------------------------------------
  // RP reset gate — consumed by fpga/shell/ip/clkrst/dut_clkrst.sv
  // (rp_resetn_gate_i). Active-low: 0 = force RP held in reset regardless
  // of CLKRST.RESET_CTRL.rp_resetn. Real state machine below (not a bare
  // mirror of decouple_en_q): drops the SAME cycle-pair DECOUPLE.decouple_en
  // is written 1 (matches swap_fsm.c's step_decouple_assert(), which treats
  // the two as one atomic action), and only re-releases once decouple_en_q
  // has cleared AND the (synchronized) decoupled_i status confirms the
  // vendor Decoupler has actually finished re-coupling — guards against
  // firmware clearing DECOUPLE a cycle before the Decoupler IP has caught up.
  // ---------------------------------------------------------------------
  output logic                          rp_resetn_gate_o,
  input  logic                          rp_in_reset_i,       // observed RP reset state -> STATUS[1]

  // ---------------------------------------------------------------------
  // RM-load verify taps (spec §7, §13) — now regmap-addressable per
  // shell-regmap.md v0.1 (OPEN_ISSUES I8 resolved): RM_ID@0x10 mirrors
  // rm_id_i directly (synchronized + settle-checked, see below);
  // RM_STATUS@0x14 = {dut_eth_irq[2], dut_lockup[1] (both synchronized), rm_id_valid[0]}.
  // ---------------------------------------------------------------------
  input  logic [31:0]                   rm_id_i,             // partition pin: rm_id (RP -> shell)
  input  logic                          dut_lockup_i,        // partition pin: dut_lockup (RP -> shell)
  // DUT ethernet IRQ observability (added 2026-07-24). The RP's irq_out
  // (MAC int_o | cksum_int, e.g. eth_ss raising RX-frame-received) is a
  // dut_clk-domain partition signal that shell_bd.tcl previously tied off at
  // the decoupler with the explicit note "irq_out has no such synced consumer".
  // This gives it one: same 2-FF ASYNC_REG sync as dut_lockup, surfaced at
  // RM_STATUS[2] so the DUT's ethernet activity is readable/observable from the
  // shell (JTAG mrd or firmware poll) WITHOUT a new unsynchronised CDC into the
  // INTC. Purely additive: no decode, register-map, or swap-FSM change.
  input  logic                          dut_eth_irq_i        // partition pin: DUT eth irq (RP -> shell)
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM
  // (aw_en handshake so AW/W can arrive in either order or together; single
  // in-flight transaction, exactly what a MicroBlaze AXI-Lite master issues).
  // ===========================================================================

  localparam int ADDR_LSB = 2;

  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr_q;
  logic                          axi_awready_q, axi_wready_q, aw_en_q;
  logic                          axi_bvalid_q;
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr_q;
  logic                          axi_arready_q, axi_rvalid_q;
  logic [C_S_AXI_DATA_WIDTH-1:0] axi_rdata_q;

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00;   // OKAY always -- contract defines no error cases
  assign s_axi_bvalid  = axi_bvalid_q;
  assign s_axi_arready = axi_arready_q;
  assign s_axi_rresp   = 2'b00;   // OKAY
  assign s_axi_rvalid  = axi_rvalid_q;
  assign s_axi_rdata   = axi_rdata_q;

  // -- Write address ready / aw_en --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awready_q <= 1'b0;
      aw_en_q       <= 1'b1;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awready_q <= 1'b1;
      aw_en_q       <= 1'b0;
    end else if (s_axi_bready && axi_bvalid_q) begin
      aw_en_q       <= 1'b1;
      axi_awready_q <= 1'b0;
    end else begin
      axi_awready_q <= 1'b0;
    end
  end

  // -- Write address latch --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awaddr_q <= '0;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awaddr_q <= s_axi_awaddr;
    end
  end

  // -- Write data ready --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_wready_q <= 1'b0;
    end else if (~axi_wready_q && s_axi_wvalid && s_axi_awvalid && aw_en_q) begin
      axi_wready_q <= 1'b1;
    end else begin
      axi_wready_q <= 1'b0;
    end
  end

  wire slv_reg_wren = axi_wready_q && s_axi_wvalid && axi_awready_q && s_axi_awvalid;

  // -- Write response --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_bvalid_q <= 1'b0;
    end else if (slv_reg_wren && ~axi_bvalid_q) begin
      axi_bvalid_q <= 1'b1;
    end else if (s_axi_bready && axi_bvalid_q) begin
      axi_bvalid_q <= 1'b0;
    end
  end

  // -- Read address ready --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_arready_q <= 1'b0;
      axi_araddr_q  <= '0;
    end else if (~axi_arready_q && s_axi_arvalid) begin
      axi_arready_q <= 1'b1;
      axi_araddr_q  <= s_axi_araddr;
    end else begin
      axi_arready_q <= 1'b0;
    end
  end

  // -- Read data valid --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rvalid_q <= 1'b0;
    end else if (axi_arready_q && s_axi_arvalid && ~axi_rvalid_q) begin
      axi_rvalid_q <= 1'b1;
    end else if (axi_rvalid_q && s_axi_rready) begin
      axi_rvalid_q <= 1'b0;
    end
  end

  wire slv_reg_rden = axi_arready_q && s_axi_arvalid && ~axi_rvalid_q;

  // ===========================================================================
  // Register file (offsets per shell-regmap.md v0.1 DFXCTL table)
  // ===========================================================================
  logic decouple_en_q;      // 0x00 DECOUPLE[0]     -- reset 0 (not decoupled)
  logic axi_shutdown_req_q; // 0x04 SHUTDOWN[0]     -- reset 0 (not requested)

  // Full local word-address decode (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]).
  // ONLY the five mapped offsets respond; every other offset in the page is
  // unmapped -- reads return 0, writes are ignored (BRESP=OKAY). This matches
  // the SystemRDL-generated decode, which compares the whole address. (Was:
  // only addr[4:2] was decoded, so 0x20 aliased onto DECOUPLE and a stray
  // write silently rewrote decouple_en -- see RESOLVED ambiguity #4 below.)
  // LOCAL page decode -- NOT C_S_AXI_ADDR_WIDTH.
  //
  // shell_bd.tcl instantiates every CSR block with C_S_AXI_ADDR_WIDTH = 32 (the
  // RTL default of 12 is never what ships), so the interconnect hands this slave
  // the FULL system address -- 0x44A9_0000, not 0x0. Decoding
  // addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] therefore compares 0x1128_4000 against
  // 'h0 and never matches: on silicon every write was ignored and every read
  // returned 0, in all six blocks at once. (Xilinx's HWICAP, on the same bus,
  // worked -- which is what isolated it.)
  //
  // Decode the block's own 64 KiB page instead. 64 KiB because that is the
  // spacing assign_bd_address gives these slaves; decoding fewer bits would let
  // BASE+0x1000 alias offset 0 and re-create the destructive alias that widening
  // this decode was meant to kill (a read of 0x20 used to POP a console byte).
  //
  // Guarded against a narrower instantiation so the block still elaborates at
  // its 12-bit default. tests/csr_decode_width/ elaborates at 32 and drives
  // base+offset, and fails on the un-fixed decode.
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_DECOUPLE  = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_SHUTDOWN  = 'h1;  // 0x04
  localparam logic [IDX_W-1:0] IDX_STATUS    = 'h2;  // 0x08 (ro)
  localparam logic [IDX_W-1:0] IDX_RM_ID     = 'h4;  // 0x10 (ro)
  localparam logic [IDX_W-1:0] IDX_RM_STATUS = 'h5;  // 0x14 (ro)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  // ---------------------------------------------------------------------
  // THE ISOLATION BIT. Split out of the shared register-write block on
  // purpose: it is the ONE bit in this module that must survive
  // s_axi_aresetn. See the ext_por_n_i/wdt_reset_i port comment.
  //
  // ext_por_n_i is a board pad, asynchronous to s_axi_aclk, so it is
  // synchronised before being used as a synchronous reset here. A POR press
  // is milliseconds; two shell clocks of latency is not a consideration. The
  // synchroniser itself takes NO RESET, and that is load-bearing rather than an
  // omission. Both available reset values are wrong:
  //   reset to 2'b11 ("not in POR") -- a POR press drops sys_rst_n AND
  //       s_axi_aresetn together, so the reset branch would overwrite the 0s
  //       the pad is presenting and the clamp would never release;
  //   reset to 2'b00 ("in POR")     -- a WATCHDOG reset would then read as a
  //       POR for two clocks and clear the clamp, i.e. the original bug.
  // In hardware the pair comes up at its configuration INIT (0) = "in POR",
  // which gives today's power-on behaviour. In simulation it is X until the
  // bench drives the pad, which is why tests/dfx_ctl's _bring_up() holds
  // ext_por_n_i low through reset. A VCS `initial` here is not an option:
  // Error-[ICPD] forbids any second procedural driver on an always_ff variable.
  // ---------------------------------------------------------------------
  (* ASYNC_REG = "TRUE" *) logic [1:0] por_n_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    por_n_sync_q <= {por_n_sync_q[0], ext_por_n_i};
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!por_n_sync_q[1]) begin
      decouple_en_q <= 1'b0;              // POR, and ONLY POR, releases
    end else if (wdt_reset_i) begin
      decouple_en_q <= 1'b1;              // watchdog: CLAMP, set-dominant
    end else if (slv_reg_wren && (waddr_idx == IDX_DECOUPLE)
                              && s_axi_wstrb[0]) begin
      decouple_en_q <= s_axi_wdata[0];
    end
  end

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_shutdown_req_q <= 1'b0;
    end else if (slv_reg_wren) begin
      unique case (waddr_idx)
        IDX_SHUTDOWN: if (s_axi_wstrb[0]) axi_shutdown_req_q <= s_axi_wdata[0];
        default: ; // DECOUPLE is handled above; STATUS/RM_ID/RM_STATUS are
                    // read-only -- write accepted (BRESP=OKAY) but has no
                    // effect; unmapped offsets same.
      endcase
    end
  end

  assign decouple_en_o      = decouple_en_q;
  assign axi_shutdown_req_o = axi_shutdown_req_q;

  // ===========================================================================
  // Status-input synchronizers (s_axi_aclk domain).
  //   - rp_in_reset_i and dut_lockup_i are REAL dut_clk-domain crossings
  //     (rp_in_reset_i = inverted rp_resetn_o, a dut_clk reset-sync output;
  //     dut_lockup_i is an RP partition pin) -> 2-FF sync is load-bearing.
  //   - decoupled_i is a DFX-decoupler status tap; R1 confirms it is a
  //     COMBINATIONAL feedback of decouple_en_o (an s_axi_aclk signal), so it
  //     is effectively same-domain -- the 2-FF here is defensive (harmless).
  // R4 CDC: (* ASYNC_REG = "TRUE" *) on all three so the placer packs each
  // 2-FF pair and TIMING-10 clears. Single-bit control/status -> plain 2-FF
  // is the correct scheme (no multi-bit coherency concern).
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [1:0] decoupled_sync_q;
  (* ASYNC_REG = "TRUE" *) logic [1:0] rp_in_reset_sync_q;
  (* ASYNC_REG = "TRUE" *) logic [1:0] dut_lockup_sync_q;
  (* ASYNC_REG = "TRUE" *) logic [1:0] dut_eth_irq_sync_q;   // DUT eth irq (dut_clk -> s_axi_aclk)

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      decoupled_sync_q    <= 2'b00;
      rp_in_reset_sync_q  <= 2'b00;
      dut_lockup_sync_q   <= 2'b00;
      dut_eth_irq_sync_q  <= 2'b00;
    end else begin
      decoupled_sync_q    <= {decoupled_sync_q[0],    decoupled_i};
      rp_in_reset_sync_q  <= {rp_in_reset_sync_q[0],  rp_in_reset_i};
      dut_lockup_sync_q   <= {dut_lockup_sync_q[0],   dut_lockup_i};
      dut_eth_irq_sync_q  <= {dut_eth_irq_sync_q[0],  dut_eth_irq_i};
    end
  end

  // ===========================================================================
  // RM_ID / rm_id_valid — rm_id_i is a 32-bit quasi-static bus from the RP
  // (in DFX practice, often a hard ID constant tied to a dedicated output
  // net in the RM, readable the instant ICAP finishes writing the partial —
  // it need not wait for rp_resetn/decouple to clear, which is exactly why
  // swap_fsm.c's step_verify() reads it BEFORE releasing DECOUPLE, see the
  // header sequence above). A per-bit 2-FF synchronizer is safe for a
  // multi-bit bus ONLY if consumers wait for it to settle -- so this module
  // also runs a stability detector: rm_id_valid asserts once the
  // synchronized value has been unchanged for RM_ID_SETTLE_CYCLES
  // consecutive s_axi_aclk cycles (catches both ordinary CDC settle time and
  // the brief window right after a partial load where the ID net itself may
  // still be settling). This is the "RM-load-verify ground truth" the task
  // brief calls out -- firmware polls RM_STATUS.rm_id_valid before trusting
  // RM_ID, exactly as swap_fsm.c's step_verify() already does.
  // ===========================================================================
  localparam int RM_ID_SETTLE_CYCLES = 8;

  // R4 CDC: 32-bit rm_id_i is a MULTI-BIT bus read as ONE value, so it must
  // NOT be trusted bit-wise -- a per-bit 2-FF sync can present a TORN word
  // during a transition (different bits from different cycles). The scheme
  // here is the "sample only when known stable + synchronize a qualifier"
  // pattern the R4 brief calls for: the raw value still goes through a per-bit
  // 2-FF (ASYNC_REG below) for metastability, but consumers must gate on the
  // single-bit rm_id_valid qualifier, which asserts ONLY after the
  // synchronized word has been byte-for-byte unchanged for RM_ID_SETTLE_CYCLES
  // consecutive s_axi_aclk cycles. rm_id is static after a swap completes, so
  // a settle qualifier is the natural (cheap, no back-channel) answer -- a
  // full req/ack handshake would need an RM-side responder the partition
  // contract does not define.
  (* ASYNC_REG = "TRUE" *) logic [31:0] rm_id_sync0_q, rm_id_sync1_q;
  logic [31:0] rm_id_prev_q;
  logic [3:0]  rm_id_stable_cnt_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      rm_id_sync0_q <= '0;
      rm_id_sync1_q <= '0;
    end else begin
      rm_id_sync0_q <= rm_id_i;       // stage 1 -- may catch metastability
      rm_id_sync1_q <= rm_id_sync0_q; // stage 2 -- safe (per-bit) to sample
    end
  end

  // R1/R4 INTERACTION -- do NOT latch a clamped id as valid.
  // The DFX decoupler (R1) clamps rm_id to 0 while DECOUPLE is asserted, and
  // the RP's id net is also meaningless while the RP is held in reset. Without
  // a guard, the settle detector would see the clamped 0 hold stable for 8
  // cycles and assert rm_id_valid=1 with VALUE 0 -- firmware would then trust
  // 0 as a real RM id. rm_id_gate forces the detector cleared (and rm_id_valid
  // low) whenever the id is not meaningful: DECOUPLE requested, the decoupler
  // still reports decoupled (clamp not yet lifted), or the RP is in reset.
  // CONSEQUENCE FOR FIRMWARE (flagged to swap_fsm / net-protocol owners):
  // with the R1 clamp, step_verify() MUST read RM_ID *after* releasing
  // DECOUPLE and the RP coming out of reset -- NOT before, as the original
  // header sequence (step 4 before step 5) assumed. rm_id_valid enforces this:
  // it cannot assert until the id is genuinely the running RM's.
  wire rm_id_gate = ~decouple_en_q
                  & ~decoupled_sync_q[1]
                  & ~rp_in_reset_sync_q[1];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      rm_id_prev_q       <= '0;
      rm_id_stable_cnt_q <= '0;
    end else if (!rm_id_gate) begin
      // Clamped / decoupled / RP-in-reset: track the (clamped) value so the
      // first post-release change re-arms cleanly, but hold the count at 0 so
      // rm_id_valid can NEVER assert on a clamped id.
      rm_id_prev_q       <= rm_id_sync1_q;
      rm_id_stable_cnt_q <= '0;
    end else if (rm_id_sync1_q != rm_id_prev_q) begin
      rm_id_prev_q       <= rm_id_sync1_q;
      rm_id_stable_cnt_q <= '0;
    end else if (rm_id_stable_cnt_q != '1) begin
      rm_id_stable_cnt_q <= rm_id_stable_cnt_q + 1'b1;
    end
  end

  // rm_id_gate is ANDed in combinationally too, so a gate-drop deasserts
  // rm_id_valid the same cycle (not only after the counter next clears).
  wire rm_id_valid = rm_id_gate
                   & (rm_id_stable_cnt_q >= RM_ID_SETTLE_CYCLES[3:0]);

  // ===========================================================================
  // RP reset gate FSM (see port comment above for the exact composition
  // rule). Reset value = 1 (released/not gated) -- DECOUPLE defaults to 0
  // at power-up, so the gate must not spuriously hold the RP in reset before
  // firmware ever runs; firmware always asserts DECOUPLE=1 explicitly as
  // step 1 of every swap (net-protocol.md), so there is no window where an
  // un-decoupled RP is holding stale isolation state.
  // ===========================================================================
  logic rp_gate_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      // NOT a bare 1'b1 any more. decouple_en_q now survives this reset
      // (watchdog hazard, above), so a bare 1 would RELEASE the RP-reset hold
      // for the length of the reset pulse while the boundary is still clamped
      // -- a ~160 ns window of "clamped but running" on every watchdog fire.
      // Deriving the reset value from the clamp closes it, and is identical to
      // the old behaviour whenever decouple_en_q is 0 (which is every case the
      // old RTL could reach, since it cleared the bit in this same branch).
      rp_gate_q <= ~decouple_en_q;
    end else if (decouple_en_q) begin
      rp_gate_q <= 1'b0;
    end else if (~decoupled_sync_q[1]) begin
      rp_gate_q <= 1'b1;
    end
    // else: decouple_en_q cleared but the decoupler hasn't confirmed
    // re-coupling yet (decoupled_sync_q[1] still 1) -- hold current (0).
  end

  assign rp_resetn_gate_o = rp_gate_q;

  // ===========================================================================
  // Read data mux
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_DECOUPLE:  axi_rdata_q <= {31'd0, decouple_en_q};
        IDX_SHUTDOWN:  axi_rdata_q <= {31'd0, axi_shutdown_req_q};
        IDX_STATUS:    axi_rdata_q <= {30'd0, rp_in_reset_sync_q[1], decoupled_sync_q[1]};
        IDX_RM_ID:     axi_rdata_q <= rm_id_sync1_q;
        IDX_RM_STATUS: axi_rdata_q <= {29'd0, dut_eth_irq_sync_q[1], dut_lockup_sync_q[1], rm_id_valid};
        default:       axi_rdata_q <= '0;
      endcase
    end
  end

endmodule

// -----------------------------------------------------------------------------
// AMBIGUITIES FOR A6 (see also the top-level reply this file ships with):
//
// 1. rp_resetn_gate_o has NO dedicated regmap offset in shell-regmap.md --
//    it is entirely a hardware-derived consequence of DECOUPLE (see the FSM
//    above). Confirmed intentional reading of the header comment's original
//    sequencing note ("MB writes DECOUPLE.decouple_en=1 AND asserts
//    rp_resetn_gate_o=0" as one atomic action, not two separate register
//    pokes) -- flag if a future rev wants this independently controllable.
// 2. axi_shutdown_ack_i is accepted but not exposed in STATUS (shell-regmap.md
//    STATUS@0x08 only defines {rp_in_reset, decoupled}). Left un-registered;
//    add a STATUS[2]=shutdown_idle bit if firmware ever needs to poll it.
// 3. RM_ID's CDC treatment (2-stage sync + N-cycle stability detector) is
//    this module's own defensible-but-unconfirmed interpretation of "the
//    RP's rm_id partition pin" crossing an unspecified clock relationship --
//    confirm whether rm_id_i is actually dut_clk-domain-registered (in which
//    case a plain 2-FF sync suffices) or a raw/asynchronous ID-constant net
//    (which is why the settle-detector is here at all).
// 4. RESOLVED (2026-07-09, SystemRDL equivalence gate). Previously only
//    address bits [4:2] were decoded, so offsets >= 0x20 aliased onto the
//    mapped registers (0x20 -> DECOUPLE, ...) instead of reading 0 -- and a
//    WRITE to 0x20 aliased onto DECOUPLE, silently asserting/clearing the RP
//    decouple gate. The equivalence TB (poc/systemrdl, hand vs generated
//    decode) flagged exactly this. Fix: waddr_idx/raddr_idx now span the full
//    local word address (addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB]), so ONLY the five
//    mapped offsets match; every other offset falls to `default` (read 0 /
//    write ignored, BRESP=OKAY). Mapped-offset behaviour is bit-for-bit
//    unchanged.
// -----------------------------------------------------------------------------
