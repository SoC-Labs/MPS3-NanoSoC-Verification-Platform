// -----------------------------------------------------------------------------
// usr_access_rd.sv — USRACC, the fabric's own build identity, read back over
// AXI4-Lite. docs/VERSIONING_PLAN.md §3.4 ("Deferred: fabric readback of
// USR_ACCESS"), built.
//
// THE SENSOR THAT WAS MISSING
// ---------------------------
// `ba2f4be` shipped the whole firmware/fabric cross-check EXCEPT the sensor:
// the `version` verb carries `usr_access` (what the FABRIC says it is) beside
// `ver32` (what the IMAGE was built as) and a `skew` verdict, the codec is
// written, the client is written, the operator command is written -- and it
// reads `null`/`null` on every board, because `coordinator_handle_version()`
// has nothing to read. This block is that nothing, filled in.
//
// One HARNESS_VER32 is produced by `scripts/gen_version.py`; `build_dfx.tcl`
// stamps it into `BITSTREAM.CONFIG.USR_ACCESS` (the device AXSS configuration
// register) and the firmware compiles the same number in. They therefore agree
// BY CONSTRUCTION unless the `.bit` and the image baked into it came from
// different builds -- the "flashable base whose `updatemem` was never re-run"
// hazard this platform has already been bitten by, and which nothing running on
// the board has ever been able to check. `USR_ACCESSE2` is the only way the
// fabric can tell the MicroBlaze what is actually in AXSS.
//
// WHY A BLOCK OF ITS OWN, AND NOT `DFXCTL.SHELL_USR_ACCESS @ 0x44A1_001C`
// ----------------------------------------------------------------------
// §3.4 proposed the register inside DFXCTL, where there is free decode space.
// It is built here instead, deliberately:
//
//   * `dfx_ctl` is the swap-critical block -- the one `swap_fsm.c` pokes first
//     and last in every reconfiguration, and the one whose reset behaviour is
//     being changed in this same wave (the watchdog un-clamp hazard,
//     docs/planning/SERVICES_PARTITION.md §5.4). Adding a `CONFIG_SITE` BEL
//     dependency to it would put a device-global configuration primitive inside
//     the block that most needs to stay trivially reviewable, and would drag
//     that primitive into `tests/dfx_ctl`'s elaboration.
//   * The two things answer different questions. DFXCTL is about the RP: what
//     is loaded, is it clamped, is it held in reset. This is about the STATIC:
//     which build am I. A reader who finds `SHELL_USR_ACCESS` beside `RM_ID`
//     has to be told they are unrelated; a reader who finds `USRACC` does not.
//   * It costs one 64 KiB page out of a region that had to be extended anyway,
//     and one interconnect master port. Both are re-key-class costs, and this
//     change is a re-key regardless (see README.md).
//
// §3.4's ADDRESS does not survive that choice; its MECHANISM does exactly.
//
// THE THREE REGISTERS, AND WHY MORE THAN ONE
// ------------------------------------------
//   0x00 MAGIC  (ro)  0x55535241 = "USRA"
//   0x04 VALUE  (ro)  the AXSS 32-bit word, as USR_ACCESSE2 reports it
//   0x08 STATUS (ro)  [0] VALID -- a stable AXSS sample has been captured since reset
//
// MAGIC is not decoration. Every unmapped offset in this shell's AXI-Lite
// window reads back as 0 (`gen_regmap.py`'s own note: "reads return 0, writes
// are ignored"), so a bare VALUE register cannot distinguish
//
//     "this fabric was stamped 0x00000000"   from
//     "this fabric has no USRACC block at all, and you are reading empty air",
//
// and firmware reporting the first when the truth is the second is precisely
// the failure `net_proto.c`'s comment refuses to allow: "A zero ... would read
// as 'checked, fine' -- the one answer this must never give for a check that
// did not happen." Firmware reads MAGIC first; a mismatch means NO SENSOR and
// the wire stays `null`. STATUS.VALID is the second gate: until the synchroniser has
// produced a stable sample after reset there is no answer yet, and a 0 read
// in that window must not be taken for one. (VALID does NOT follow the
// primitive's DATAVALID -- see THE PRIMITIVE below for why.)
//
// SIMULATION
// ----------
// There is no `ifdef` in this file. `USR_ACCESSE2` is instantiated
// unconditionally, exactly as it synthesises, and the bench supplies its own
// module of that name (`tests/usr_access_rd/usr_accesse2_model.sv`, plusarg
// driven) because unisims is not on the bench's library path. The RTL that is
// benched is byte-identical to the RTL that ships -- this repo has been bitten
// by benching a different parameterisation than the BD instantiates (bug #1,
// the CSR address-decode escape), and a simulation-only code path is the same
// mistake with a different hat on.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module usr_access_rd #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,  // local offset decode only; the base
                                          // address is set in the BD Address
                                          // Editor. shell_bd.tcl instantiates
                                          // this at 32 like every other CSR
                                          // block -- see the LOCAL_ADDR_W note.
  parameter int C_S_AXI_DATA_WIDTH = 32
) (
  input  logic                          s_axi_aclk,
  input  logic                          s_axi_aresetn,

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
  input  logic                          s_axi_rready
);

  // verilator lint_off UNUSED
  wire _unused_prot = &{1'b0, s_axi_awprot, s_axi_arprot, s_axi_wdata,
                        s_axi_wstrb, 1'b0};
  // verilator lint_on UNUSED

  localparam int ADDR_LSB = 2;

  // "USRA". Read FIRST by firmware: a mismatch means this page has no USRACC
  // block behind it and VALUE must not be believed (see the header).
  localparam logic [31:0] USRACC_MAGIC_VALUE = 32'h5553_5241;

  // ===========================================================================
  // AXI4-Lite slave — the same Xilinx-template write/read channel FSM every
  // other custom CSR block in this shell uses (dfx_ctl.sv, clkrst, telem, ...),
  // kept identical on purpose so the shared bench BFM and the bound SVA
  // protocol checker apply unchanged. The WRITE channel is present and
  // handshakes normally even though every register is read-only: an AXI-Lite
  // slave that never asserts AWREADY/WREADY would HANG the MicroBlaze on a
  // stray write, which is a far worse failure than accepting and discarding
  // one. Writes are answered OKAY and have no effect.
  // ===========================================================================
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr_q;
  logic                          axi_awready_q, axi_wready_q, aw_en_q;
  logic                          axi_bvalid_q;
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr_q;
  logic                          axi_arready_q, axi_rvalid_q;
  logic [C_S_AXI_DATA_WIDTH-1:0] axi_rdata_q;

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00;   // OKAY always -- no error cases defined
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

  // -- Write address latch (kept so the template is unmodified; the address is
  //    never decoded because nothing here is writable) --
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_awaddr_q <= '0;
    end else if (~axi_awready_q && s_axi_awvalid && s_axi_wvalid && aw_en_q) begin
      axi_awaddr_q <= s_axi_awaddr;
    end
  end
  // verilator lint_off UNUSED
  wire _unused_awaddr = &{1'b0, axi_awaddr_q, 1'b0};
  // verilator lint_on UNUSED

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
  // Decode. LOCAL page decode, NOT C_S_AXI_ADDR_WIDTH -- see dfx_ctl.sv's long
  // note on bug #1: shell_bd.tcl instantiates every CSR block with
  // C_S_AXI_ADDR_WIDTH = 32, so the interconnect hands this slave the FULL
  // system address (0x44B3_0000, not 0x0). Comparing the whole 32-bit address
  // against 'h0 never matches, and on silicon every read returns 0 -- which for
  // THIS block would be indistinguishable from a fabric honestly stamped 0, so
  // getting it wrong here is worse than elsewhere. 64 KiB page, matching the
  // spacing assign_bd_address gives these slaves.
  // ===========================================================================
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_MAGIC  = 'h0;  // 0x00 (ro) block-present witness 0x55535241 "USRA"
  localparam logic [IDX_W-1:0] IDX_VALUE  = 'h1;  // 0x04 (ro) AXSS USR_ACCESS word, HARNESS_VER32
  localparam logic [IDX_W-1:0] IDX_STATUS = 'h2;  // 0x08 (ro) bit0 valid — a stable AXSS sample is held

  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  // ===========================================================================
  // THE PRIMITIVE.
  //
  // USR_ACCESSE2 (UG974; UG570 v1.9.1 p.119-121) has no inputs: CFGCLK is the
  // configuration clock, DATA[31:0] reflects the AXSS register and DATAVALID is
  // "Active High Data Valid". There is exactly ONE of these BELs on the whole
  // xcku115 (CONFIG_SITE_X0Y0/USR_ACCESS, clock region X5Y1 -- enumerated
  // against the real part in VERSIONING_PLAN.md §2.1), and it is OUTSIDE the
  // DFX partition (§2.2), which is why this block can only ever live in the
  // static shell and why no RM can carry its own stamp.
  //
  // DATAVALID IS NOT USED, AND THAT IS THE 2026-10 FIX
  // --------------------------------------------------
  // UG570 Figure 7-9 draws DATAVALID as a ONE-CFGCLK-CYCLE PULSE on each AXSS
  // write cycle, with DATA "persisting until the next write". The bitstream's
  // own AXSS write happens DURING CONFIGURATION, while GSR still holds every
  // fabric flop and long before proc_sys_reset releases s_axi_aresetn -- so the
  // one pulse this primitive ever gives on a normally-configured part is gone
  // before this block can see it. The first version of this block gated its
  // capture on a synchronised DATAVALID and therefore never captured: silicon
  // on 0x3F1A560F read MAGIC=0x55535241, VALUE=0, VALID=0 with the AXSS word
  // proven present in the .bit (docs/evidence/2026-09-w2/usracc_20260922.txt).
  // The bench passed only because its model raised DATAVALID as a level after
  // reset; tests/usr_access_rd now models Figure 7-9 and reproduces the silicon
  // read against the old RTL (`make control-silicon`, MUST FAIL).
  //
  // So DATA is sampled directly. The fabric only ever runs after configuration
  // has completed, so by the time s_axi_aclk is ticking DATA already holds the
  // AXSS word -- there is no "not loaded yet" state to wait for. CFGCLK stays
  // unconnected: it stops after configuration and nothing here needs it.
  // ===========================================================================
  wire [31:0] usr_access_data;
  wire        usr_access_datavalid;

  USR_ACCESSE2 u_usr_accesse2 (
    .CFGCLK    (),                      // unused, see above
    .DATA      (usr_access_data),
    .DATAVALID (usr_access_datavalid)   // unused: a configuration-time pulse
  );
  // verilator lint_off UNUSED
  wire _unused_datavalid = &{1'b0, usr_access_datavalid, 1'b0};
  // verilator lint_on UNUSED

  // DATA comes from the configuration domain, not from s_axi_aclk, so it is
  // taken through a 2-FF synchroniser (ASYNC_REG) and only ACCEPTED when two
  // successive synchronised samples agree. For the normal post-configuration
  // constant that is the same word every clock; for a dynamic AXSS write
  // (JTAG/ICAPE3, UG570 "Advanced Uses") it means a word torn across the
  // update is never published -- VALUE moves straight from the old word to the
  // new one.
  //
  // VALID is set on the first accepted sample after reset and is then sticky: an
  // answer this block has given once it must not later withdraw. fill_q makes
  // sure the comparison is between two REAL samples, never between the reset
  // zeros of the pipeline (which would publish 0 as if it had been read).
  //
  // RE-DERIVED after a reset, not remembered across one. A peripheral reset --
  // including the watchdog reset -- clears the capture and the next four clocks
  // refill it from the primitive. The block can never report an identity it is
  // no longer reading, and every flop here stays on the one reset (VCS
  // Error-[ICPD] rejects an `initial` seed on a variable an always_ff drives).
  (* ASYNC_REG = "TRUE" *) logic [31:0] data_sync1_q;
  (* ASYNC_REG = "TRUE" *) logic [31:0] data_sync2_q;
  logic [31:0] data_sync3_q;
  logic [2:0]  fill_q;
  logic [31:0] usr_access_q;
  logic        usr_access_valid_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      data_sync1_q <= '0;
      data_sync2_q <= '0;
      data_sync3_q <= '0;
      fill_q       <= 3'b000;
    end else begin
      data_sync1_q <= usr_access_data;
      data_sync2_q <= data_sync1_q;
      data_sync3_q <= data_sync2_q;
      fill_q       <= {fill_q[1:0], 1'b1};
    end
  end

  wire sample_stable = fill_q[2] && (data_sync2_q == data_sync3_q);

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      usr_access_q       <= '0;
      usr_access_valid_q <= 1'b0;
    end else if (sample_stable) begin
      usr_access_q       <= data_sync3_q;
      usr_access_valid_q <= 1'b1;
    end
  end

  // ===========================================================================
  // Read data mux. Every offset that is not one of the three reads 0.
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_MAGIC:  axi_rdata_q <= USRACC_MAGIC_VALUE;
        IDX_VALUE:  axi_rdata_q <= usr_access_q;
        IDX_STATUS: axi_rdata_q <= {31'd0, usr_access_valid_q};
        default:    axi_rdata_q <= '0;
      endcase
    end
  end

endmodule
