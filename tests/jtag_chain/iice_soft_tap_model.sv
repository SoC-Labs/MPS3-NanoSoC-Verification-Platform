//-----------------------------------------------------------------------------
// iice_soft_tap_model.sv — a behavioural IEEE 1149.1 TAP standing in for the
// Synopsys Identify SOFT JTAG TAP that `device jtagport soft` inserts.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
// Copyright (C) 2026, SoC Labs (www.soclabs.org)
//-----------------------------------------------------------------------------
// THIS IS A MODEL, AND SAYING SO IS THE POINT
// ===========================================
// The real soft TAP is emitted by the Identify instrumentor from
//   /eda/synopsys/2022-23/RHELx86/SFPGA_2022.09-SP2/fpga/lib/di/hw_gen/ip/
//   jtag_interface_core.v
// whose body is vendor-obfuscated and which is not ours to copy, vendor or
// simulate outside the tool. So the OTHER TAP in this bench's chain is the
// REAL Arm SoC-400 SWJ-DP RTL, and THIS one is a model.
//
// Its parameters are not invented. They are read off the debugger's own device
// table, which is plain Tcl and is the table Identify uses to auto-detect a
// chain:
//
//   .../SFPGA_2022.09-SP2/identify/lib/share/contrib/syn_idcodes.tcl:741-742
//       # Synopsys Soft JTAG
//       idcode add -quiet 00010000011000111110010011001101 SoftJTAG 5 \
//           -family soft-jtag
//
// The `idcode add` argument order is <idcode-binary-MSB-first> <deviceName>
// <instructionRegisterWidth> (identify_debug_env_reference.pdf p.46), so:
//
//       IR LENGTH = 5
//       IDCODE    = 32'b0001_0000_0110_0011_1110_0100_1100_1101 = 32'h1063E4CD
//
// and the instruction opcodes come from the debugger binary's own VHDL-shaped
// symbol strings (`strings identify_debugger_shell`):
//
//       instruction_VENDORID   ... := "00000"
//       instruction_HCR_CHAIN  ... := "00010"
//       instruction_IDHW_CHAIN ... := "00011"
//       instruction_BYPASS     ... := "11111"
//
// WHAT THIS MODEL THEREFORE PROVES, AND WHAT IT DOES NOT
//   PROVES     the CHAIN: that a 5-bit-IR TAP and the real 4-bit-IR SWJ-DP,
//              sharing TCK/TMS with TDI/TDO cascaded, can both be addressed —
//              IR/DR concatenation order, BYPASS padding, IDCODE ordering, and
//              that a DAP access still works with a second TAP in the path.
//   DOES NOT   prove anything about Identify's real internal logic, its sample
//              buffer, or its download protocol. Those are the debugger's
//              business and are exercised board-side, not here.
//
// NO TRST. The real soft TAP has exactly four pins — the instrumentor's own
// message lists tck/tms/tdi/tdo and nothing else — so this model has no
// asynchronous reset input either, and reaching Test-Logic-Reset is done the
// only way the chain allows: five TCK with TMS high.
//-----------------------------------------------------------------------------
`timescale 1ns / 1ps

module iice_soft_tap_model #(
    // From syn_idcodes.tcl:741-742. Do not "tidy" these into literals whose
    // provenance is lost.
    parameter [31:0] IDCODE  = 32'h1063_E4CD,
    parameter int    IR_LEN  = 5
) (
    input  wire tck,
    input  wire tms,
    input  wire tdi,
    output wire tdo,

    // Observation only — never driven back into the chain. Lets the bench
    // assert what the model believes without reaching into its guts by name.
    output wire [IR_LEN-1:0] obs_ir,
    output wire [31:0]       obs_idhw,
    output wire [15:0]       obs_hcr
);

    // ---- instruction opcodes (see header for provenance) --------------------
    localparam [IR_LEN-1:0] IR_VENDORID   = 5'b00000;  // 32-bit IDCODE DR
    localparam [IR_LEN-1:0] IR_HCR_CHAIN  = 5'b00010;  // 16-bit control DR
    localparam [IR_LEN-1:0] IR_IDHW_CHAIN = 5'b00011;  // 32-bit data DR
    localparam [IR_LEN-1:0] IR_BYPASS     = 5'b11111;  // 1-bit DR (IEEE: all 1s)

    // IEEE 1149.1: Capture-IR loads a fixed pattern whose two LSBs are 01.
    // OpenOCD checks exactly that by default (-ircapture 0x01 / -irmask 0x03,
    // openocd src/jtag/tcl.c:411-412), which is why this is 5'b00001 and not 0.
    localparam [IR_LEN-1:0] IR_CAPTURE_VALUE = 5'b00001;

    // ---- 1149.1 controller states ------------------------------------------
    localparam [3:0]
        S_TLR = 4'd0,  S_RTI  = 4'd1,
        S_SELDR = 4'd2, S_CAPDR = 4'd3, S_SHDR = 4'd4, S_E1DR = 4'd5,
        S_PADR  = 4'd6, S_E2DR  = 4'd7, S_UPDR = 4'd8,
        S_SELIR = 4'd9, S_CAPIR = 4'd10, S_SHIR = 4'd11, S_E1IR = 4'd12,
        S_PAIR  = 4'd13, S_E2IR = 4'd14, S_UPIR = 4'd15;

    reg [3:0] state = S_TLR;

    reg [IR_LEN-1:0] ir       = IR_VENDORID;   // TLR selects IDCODE
    reg [IR_LEN-1:0] ir_shift = {IR_LEN{1'b0}};

    reg [31:0] dr_shift = 32'h0;
    reg [5:0]  dr_len   = 6'd32;

    // The two Identify-side registers a host actually writes. IDHW_CHAIN is the
    // one this bench uses to prove "a real Identify-TAP shift got through with
    // the DAP in BYPASS": write a pattern, update it, scan it back.
    reg [31:0] idhw = 32'h0;
    reg [15:0] hcr  = 16'h0;

    assign obs_ir   = ir;
    assign obs_idhw = idhw;
    assign obs_hcr  = hcr;

    // TDO is COMBINATIONAL on the shift register's LSB and driven only in the
    // two Shift states — 0 elsewhere. That is what makes the host's
    // "write TCK=0, read TDO, write TCK=1" bit loop align: the value read while
    // TCK is low is the bit the upcoming rising edge will shift out.
    //
    // Driving 0 outside Shift is safe in a daisy chain PRECISELY because TMS
    // and TCK are shared: the downstream TAP only samples its TDI while it is
    // itself in a Shift state, which is the same instant this one is driving a
    // real value.
    assign tdo = (state == S_SHDR) ? dr_shift[0]
               : (state == S_SHIR) ? ir_shift[0]
               : 1'b0;

    // Shift the DR right by one and drop `bit_in` into the top of the ACTIVE
    // width (not bit 31): a 1-bit BYPASS register must behave like one bit.
    // Written as a function so there is exactly one non-blocking assignment to
    // dr_shift and no reliance on last-write-wins between a full-vector and a
    // bit-select assignment in the same block.
    function automatic [31:0] dr_shift_in(input [31:0] cur,
                                          input        bit_in,
                                          input [5:0]  len);
        reg [4:0] top;
        reg [31:0] nxt;
        begin
            top = len[4:0] - 5'd1;   // len is 1..32; len==32 -> top==31
            nxt = cur >> 1;
            nxt[top] = bit_in;
            dr_shift_in = nxt;
        end
    endfunction

    function automatic [5:0] dr_width(input [IR_LEN-1:0] instr);
        case (instr)
            IR_VENDORID:   dr_width = 6'd32;
            IR_IDHW_CHAIN: dr_width = 6'd32;
            IR_HCR_CHAIN:  dr_width = 6'd16;
            default:       dr_width = 6'd1;   // BYPASS, and every unused opcode
        endcase
    endfunction

    function automatic [3:0] next_state(input [3:0] cur, input tms_i);
        case (cur)
            S_TLR:   next_state = tms_i ? S_TLR   : S_RTI;
            S_RTI:   next_state = tms_i ? S_SELDR : S_RTI;
            S_SELDR: next_state = tms_i ? S_SELIR : S_CAPDR;
            S_CAPDR: next_state = tms_i ? S_E1DR  : S_SHDR;
            S_SHDR:  next_state = tms_i ? S_E1DR  : S_SHDR;
            S_E1DR:  next_state = tms_i ? S_UPDR  : S_PADR;
            S_PADR:  next_state = tms_i ? S_E2DR  : S_PADR;
            S_E2DR:  next_state = tms_i ? S_UPDR  : S_SHDR;
            S_UPDR:  next_state = tms_i ? S_SELDR : S_RTI;
            S_SELIR: next_state = tms_i ? S_TLR   : S_CAPIR;
            S_CAPIR: next_state = tms_i ? S_E1IR  : S_SHIR;
            S_SHIR:  next_state = tms_i ? S_E1IR  : S_SHIR;
            S_E1IR:  next_state = tms_i ? S_UPIR  : S_PAIR;
            S_PAIR:  next_state = tms_i ? S_E2IR  : S_PAIR;
            S_E2IR:  next_state = tms_i ? S_UPIR  : S_SHIR;
            S_UPIR:  next_state = tms_i ? S_SELDR : S_RTI;
            default: next_state = S_TLR;
        endcase
    endfunction

    // The action of a state happens on the rising edge at which the controller
    // IS IN that state; the state then advances. So Capture-DR loads the DR on
    // the very edge that enters Shift-DR, and the first TDO read taken in
    // Shift-DR is DR bit 0 — which is what makes an IDCODE read come out
    // aligned instead of off by one.
    always @(posedge tck) begin
        case (state)
            S_CAPDR: begin
                dr_len <= dr_width(ir);
                case (ir)
                    IR_VENDORID:   dr_shift <= IDCODE;
                    IR_IDHW_CHAIN: dr_shift <= idhw;
                    IR_HCR_CHAIN:  dr_shift <= {16'h0, hcr};
                    default:       dr_shift <= 32'h0;   // BYPASS captures 0
                endcase
            end
            S_SHDR: dr_shift <= dr_shift_in(dr_shift, tdi, dr_len);
            S_UPDR: begin
                case (ir)
                    IR_IDHW_CHAIN: idhw <= dr_shift;
                    IR_HCR_CHAIN:  hcr  <= dr_shift[15:0];
                    default:       ;   // IDCODE and BYPASS have no update action
                endcase
            end
            S_CAPIR: ir_shift <= IR_CAPTURE_VALUE;
            S_SHIR:  ir_shift <= {tdi, ir_shift[IR_LEN-1:1]};
            S_UPIR:  ir <= ir_shift;
            default: ;
        endcase

        // Next-state: the standard 1149.1 controller graph.
        state <= next_state(state, tms);

        // Test-Logic-Reset reloads the IDCODE instruction (1149.1 section 6.2.1):
        // a bare five-TMS-high reset therefore makes every compliant TAP in the
        // chain present its 32-bit ID on the next DR scan, which is exactly how
        // both OpenOCD and Identify auto-detect a chain. Keyed on the NEXT state
        // so it fires on ENTERING TLR, not one edge later.
        if (next_state(state, tms) == S_TLR) begin
            ir     <= IR_VENDORID;
            dr_len <= 6'd32;
        end
    end

endmodule
