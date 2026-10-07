# -----------------------------------------------------------------------------
# ooc_synth_synplify.tcl — turn the Synplify/Identify EDIF into the
# out-of-context checkpoint the existing DFX flow already consumes.
#
#   vivado -mode batch -source ooc_synth_synplify.tcl
#
# Env (all set by fpga/rp/nanosoc_iice/Makefile):
#   OUT_DIR         the Synplify work dir; also where rm_nanosoc_synth.dcp lands
#   RM_IICE_DIR     fpga/rp/nanosoc_iice
#   RM_NANOSOC_DIR  fpga/rp/nanosoc            (for nanosoc_ooc.xdc)
#   REPO_ROOT       repo root                  (for docs/contracts/partition-pins.md)
#   SYNTH_TOP       rp_nanosoc_iice_core       (the Synplify top)
#   SYNPLIFY_REV    rev_1_identify | rev_1
#   IICE            1 => instrumented: wrap the 51-port EDIF in the 47-port shim
#                   0 => plain:        the 47-port EDIF IS the RM top
#
# ZERO CHANGES to fpga/dfx/build_dfx.tcl. Its ensure_rm_synth_dcp only wants a
# file at <out_dir>/<rm_key>_synth.dcp (build_dfx.tcl:138-141), and
# build_config then does `read_checkpoint -cell $rp_inst`. The cell's top MODULE
# NAME is irrelevant to that; only the PIN LIST has to match the locked static's
# RP cell. So this script's one real obligation is: emit a checkpoint whose top
# carries exactly the 47 ports / 148 bits of docs/contracts/partition-pins.md.
# It ASSERTS that at the end rather than hoping.
#
# ---------------------------------------------------------------------------
# WHY THE TWO BRANCHES EXIST — the port-count problem, and its resolution
# ---------------------------------------------------------------------------
# `device jtagport soft` makes the Identify instrumentor ADD four external ports
# to the SYNTHESIS TOP. Verbatim tool message (hwg_comm_gen_c::
# PrintJTAGUserMessage(), Identify 2022.09-SP2):
#
#   "The following external ports have been added to your design for
#    communication:
#        identify_jtag_tck : Identify Testport TCK
#        identify_jtag_tms : Identify Testport TMS
#        identify_jtag_tdi : Identify Testport TDI
#        identify_jtag_tdo : Identify Testport TDO"
#
# It does NOT reuse pre-declared nets of those names. So an instrumented EDIF
# has 47 + 4 = 51 ports, and there is no RTL scope inside the synthesis top in
# which to tie the four to anything. The tie therefore happens ABOVE the EDIF,
# on the Vivado side: read_edif the 51-port core, read_verilog the 47-port shim
# that instantiates it and splices identify_jtag_* into the ONE jtag_* partition
# wire-set as a 1149.1 daisy chain (rp_nanosoc_iice_shim.sv,
# docs/planning/IICE_JTAG_CHAIN.md), and synth_design the shim.
#
# REBASED 2026-09-10: this used to say "the four existing swd_* partition pins".
# That group no longer exists — the A6 SWD->JTAG cutover replaced it, and the
# then-fielded boundary (mint 0xA8C1C535) has jtag_tck/tms/tdi/tdo instead. The soft
# TAP can no longer be given a wire-set of its own, because the DUT's SoC-400
# SWJ-DP is on the only one there is; hence the chain.
#
# The plain (IICE=0) branch has no such problem and takes the cheaper
# link_design path, which is also the honest A/B baseline for "did swapping
# Vivado synthesis for Synplify change the RM?" (plan Phase 3).
# -----------------------------------------------------------------------------

set part xcku115-flvb1760-1-c

proc need {name} {
    global env
    if { ![info exists env($name)] || $env($name) eq "" } {
        error "ooc_synth_synplify.tcl: env $name is not set"
    }
    return $env($name)
}

set OUT_DIR   [need OUT_DIR]
set RM_IICE   [need RM_IICE_DIR]
set RM_NANO   [need RM_NANOSOC_DIR]
set REPO_ROOT [need REPO_ROOT]
set SYNTH_TOP [need SYNTH_TOP]
set REV       [need SYNPLIFY_REV]
set IICE      [need IICE]

# =============================================================================
# 1. Locate the EDIF — and re-assert the filename trap.
#
# TRAP, PROVEN: the EDIF must be named exactly <top_module>.edf. Anything else
# and link_design dies with
#   [Project 1-68] No files found to match top module
# Case-sensitive on Linux (.edf, not .EDF or .edn). gen_prj.tcl emits
# `project -result_file "./<rev>/<top>.edf"` for exactly this reason; this check
# catches the case where somebody edits the generated .prj by hand.
# =============================================================================
set edf [file join $OUT_DIR $REV "${SYNTH_TOP}.edf"]
if { ![file exists $edf] } {
    set found [glob -nocomplain -directory [file join $OUT_DIR $REV] *.edf *.edn *.EDF]
    error "no EDIF at $edf — run `make synth` first.\
 Files present in [file join $OUT_DIR $REV]: [expr {[llength $found] ? $found : {none}}].\
 If an EDIF exists under a DIFFERENT name, that is the [Project 1-68] trap:\
 rename it to ${SYNTH_TOP}.edf or fix `project -result_file` in the .prj."
}
if { [file tail $edf] ne "${SYNTH_TOP}.edf" } {
    error "EDIF basename [file tail $edf] != ${SYNTH_TOP}.edf"
}
puts "INFO: EDIF        : $edf ([file size $edf] bytes)"

# =============================================================================
# 2. Contract-derived port audit helper.
#
# Parses docs/contracts/partition-pins.md rather than restating it, so this
# check cannot drift from the contract. Same table regex as
# fpga/dfx/pin_check.py (which remains THE authority, at source level; this is
# the netlist-level twin).
# =============================================================================
proc contract_ports { md_path ngpio } {
    if { ![file exists $md_path] } { error "contract not found: $md_path" }
    set fh [open $md_path r]; set txt [read $fh]; close $fh
    set out [dict create]
    foreach line [split $txt "\n"] {
        if { [regexp {^\|\s*`(\w+)`\s*\|\s*([IO])\s*\|\s*(\S+)\s*\|} $line -> nm dir w] } {
            if { $w eq "NGPIO" } { set w $ngpio }
            if { ![string is integer -strict $w] } { continue }
            dict set out $nm $w
        }
    }
    return $out
}

proc audit_ports { md_path ngpio } {
    set want [contract_ports $md_path $ngpio]
    # Collapse the netlist's bit-blasted port names back to name -> width.
    set got [dict create]
    foreach p [get_ports -quiet *] {
        set n [get_property NAME $p]
        regsub {\[\d+\]$} $n {} n
        dict incr got $n 1
    }
    set errs {}
    dict for {nm w} $want {
        if { ![dict exists $got $nm] } {
            lappend errs "MISSING port $nm (\[$w\])"
        } elseif { [dict get $got $nm] != $w } {
            lappend errs "port $nm is [dict get $got $nm] bits, contract says $w"
        }
    }
    dict for {nm w} $got {
        if { ![dict exists $want $nm] } { lappend errs "EXTRA port $nm (\[$w\]) not in the contract" }
    }
    set nbits 0
    dict for {nm w} $got { incr nbits $w }
    set wbits 0
    dict for {nm w} $want { incr wbits $w }
    puts "INFO: boundary audit: [dict size $got] ports / $nbits bits\
 (contract: [dict size $want] / $wbits)"
    if { [llength $errs] } {
        error "BOUNDARY AUDIT FAILED — this checkpoint would be rejected by\
 pr_verify (or, worse, pushed onto silicon against a static it does not\
 match):\n  [join $errs "\n  "]"
    }
    puts "INFO: boundary audit PASS — [dict size $got] ports / $nbits bits, contract-conformant."
}

set contract_md [file join $REPO_ROOT docs contracts partition-pins.md]

# =============================================================================
# 3. EDIF -> OOC checkpoint
# =============================================================================
create_project -in_memory -part $part
read_edif $edf

if { $IICE } {
    # --- instrumented: 51-port EDIF core wrapped by the 47-port shim ---------
    #
    # PROVEN RECIPE (2026-07-29, xcku115-flvb1760-1-c, Vivado 2024.1, on the
    # rm_led throwaway of `make probe-softtap` + `make probe-dcp`):
    #
    #     read_edif   <core>.edf                 <- 51-port instrumented netlist
    #     read_verilog -sv <core>_bb.sv          <- (* black_box *) 51-port stub
    #     read_verilog -sv <shim>.sv             <- 47-port RTL parent
    #     synth_design -mode out_of_context -top <shim>
    #
    #   => 1290 cells (1288 from the standalone-linked EDIF + the shim's 2),
    #      47 ports / 148 bits, IS_BLACKBOX(u_core) == 0, no BSCANE2, no BUFG.
    #
    # THE BLACK-BOX STUB IS NOT OPTIONAL. Two measured dead ends:
    #   * read_edif + read_verilog(shim) + synth_design, no stub
    #       -> ERROR: [Synth 8-439] module 'rp_nanosoc_iice_core' not found
    #   * read_edif + read_verilog(shim) + link_design -top <shim>
    #       -> ERROR: [Project 1-68] No files found to match top module
    #          (link_design needs a NETLIST for the top; the shim is RTL. This is
    #          the same [Project 1-68] family as the .edf-filename trap, from a
    #          completely different cause — do not confuse the two.)
    #   * link_design the core standalone, write_checkpoint, then
    #     read_checkpoint -cell u_core into a separately-synthesised shim
    #       -> ERROR: [Project 1-9] Cannot open structural netlist because no
    #          structural source files were specified.
    #
    # If all of this ever breaks, the remaining fallback is the plan's R1 one:
    # accept 51 partition pins and pay ONE re-mint (13 files, full shell
    # rebuild, static_id re-key, all 9 overlays re-keyed, sd_install + a
    # physical power-cycle).
    set shim [file join $RM_IICE rp_nanosoc_iice_shim.sv]
    if { ![file exists $shim] } { error "missing shim $shim" }

    # The black-box stub is DERIVED from rp_nanosoc_iice_core.sv (+ the four
    # ports the instrumentor adds), so it cannot drift from the shim's
    # instantiation. Generate it if absent rather than making the caller
    # remember a separate step.
    set bb [file join $OUT_DIR lint rp_nanosoc_iice_core_bb.sv]
    if { ![file exists $bb] } {
        set gen [file join $RM_IICE lint gen_lint_stubs.py]
        puts "INFO: generating the black-box stub: $gen"
        if { [catch {exec python3 $gen $REPO_ROOT [file join $OUT_DIR lint]} m] } {
            error "could not generate $bb:\n$m"
        }
    }
    if { ![file exists $bb] } { error "still no black-box stub at $bb" }

    read_verilog -sv $bb
    read_verilog -sv $shim
    puts "INFO: mode        : INSTRUMENTED — shim rp_nanosoc_iice_shim over EDIF cell $SYNTH_TOP"
    synth_design -mode out_of_context -top rp_nanosoc_iice_shim -part $part

    # The black box MUST be filled. An empty one would sail through every gate
    # below (47 ports, no BSCANE2, timing trivially met) and produce a partial
    # bitstream containing NO DUT.
    set bbcell [get_cells -quiet u_core]
    if { $bbcell eq "" } { error "no u_core cell after synth_design" }
    if { [get_property IS_BLACKBOX $bbcell] } {
        error "u_core is STILL A BLACK BOX after synth_design — the EDIF was not\
 linked in. This checkpoint would be an empty DUT that passes every other gate.\
 Check that read_edif ran BEFORE read_verilog and that the EDIF's top cell is\
 named exactly $SYNTH_TOP."
    }
    set ncells [llength [get_cells -quiet -hierarchical]]
    if { $ncells < 1000 } {
        error "only $ncells cells in the linked design — nanosoc is ~18,000. The\
 EDIF is almost certainly not linked in."
    }
    puts "INFO: u_core filled from EDIF: $ncells cells hierarchically"
} else {
    # --- plain: the EDIF top IS the RM top -----------------------------------
    puts "INFO: mode        : PLAIN (uninstrumented) — linking EDIF top $SYNTH_TOP directly"
    link_design -top $SYNTH_TOP -part $part -mode out_of_context
}

# =============================================================================
# 4. Socketed OOC timing constraints (docs/contracts/partition-timing.md).
#
# Additive + guarded on existence, exactly like fpga/rp/nanosoc/ooc_synth.tcl
# lines 128-143. Deliberately reuses fpga/rp/nanosoc/nanosoc_ooc.xdc — the SAME
# file the Vivado baseline reads — so the staged checkpoint carries identical
# create_clock/timing constraints and report_timing_summary is comparable
# between the two flows instead of analysing a different constraint set.
# (nanosoc_iice.fdc is the SYNTHESIS-side twin of that XDC and was already
# consumed by Synplify; this is the CHECKPOINT side.)
# =============================================================================
set ooc_xdc [file join $RM_NANO nanosoc_ooc.xdc]
if { [file exists $ooc_xdc] } {
    puts "INFO: reading socketed OOC timing XDC $ooc_xdc (partition-timing.md)"
    read_xdc $ooc_xdc
    puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
} else {
    error "missing $ooc_xdc — refusing to write a clockless checkpoint silently.\
 If a clockless OOC run is genuinely wanted, say so explicitly."
}

# =============================================================================
# 5. Gates + reports
# =============================================================================
audit_ports $contract_md 16

# BSCANE2 must NOT be present. `check-no-bscan` in the Makefile is the primary,
# cheap, EDIF-text gate (the INVERSION of hello_ident/run.sh:39); this is the
# netlist-level backstop, and it is the one that would catch a BSCANE2 arriving
# from somewhere other than the EDIF text.
set bad [get_cells -quiet -hierarchical -filter {REF_NAME =~ "BSCANE2"}]
if { [llength $bad] } {
    error "BSCANE2 in the RM netlist: $bad. That is HDPR-16 (\"Illegal logic\
 inside reconfigurable cell\") waiting to happen, and there is no BSCAN site in\
 the RP pblock anyway. Cause: `device jtagport builtin` instead of `soft` in\
 the .idc."
}
puts "INFO: no BSCANE2 in the netlist (gate PASS)"

# -----------------------------------------------------------------------------
# No global clock buffers, no clock managers. The RP pblock has ZERO sites for
# any of them (fpga/dfx/dfx_floorplan.xdc:73-95 ranges are SLICE / DSP48E2 /
# RAMB18 / RAMB36 only; sections 7-10 of util_rm_nanosoc.rpt -- I/O, CLOCK,
# ADVANCED, CONFIGURATION -- are empty tables). One inferred BUFG anywhere in
# the RM is fatal at placement.
#
# The sweep is a WILDCARD, not a fixed list: BUFGCE, BUFGCE_DIV, BUFGCTRL,
# BUFG_GT, BUFGMUX*, MMCME*, PLLE* all matter and the family names change
# between device generations.
#
# TWO COMPLETELY DIFFERENT CAUSES, and the fix for one is useless against the
# other. Do not conflate them:
#
#   (a) IICE-inserted. Identify puts `BUFG clkbuf(.I(tck))` on the soft TAP's
#       clock unless told not to. Fix = `device xilinxinsertbufg 0` +
#       `device skewfree 1` in the .idc. `make synth` already refuses to run
#       without both, so this cause should be impossible here — if it appears
#       anyway, the .idc that actually reached <rev>/identify.idc is not the one
#       you think it is.
#
#   (b) SYNPLIFY-INFERRED, on a DESIGN net. Synplify automatically promotes
#       high-fanout CLOCK AND RESET nets onto global buffers; Vivado does not.
#       Nothing in the .idc affects this. Fix = the syn_noclockbuf attribute,
#       applied globally in nanosoc_iice.fdc (see that file's "Global buffer
#       inference" block). MEASURED, 2026-07-29, on the real 240-source DUT:
#         u_rm/u_nanosoc/u_qspi_flash_0/u_top_ahb_qspi/u_qspi_clock_div/
#             partial_QSPI_SCLK_i_cb            <- divided QSPI clock
#         u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/u_core_prmu/u_rstctrl/
#             u_core_hresetn_sync/rst_sync2_n_buf   <- reset synchroniser
#       Both are design logic. The first version of this guard blamed the .idc
#       and was flatly wrong.
#
# So classify by hierarchy: anything under Identify's own logic
# (syn_identify_core*, comm_block_INST, identify*) is (a); everything else is
# (b).
# -----------------------------------------------------------------------------
proc is_iice_cell { name } {
    foreach pat {*syn_identify_core* *comm_block_INST* *identify*} {
        if { [string match $pat $name] } { return 1 }
    }
    return 0
}

set clkcells [get_cells -quiet -hierarchical -filter \
    {REF_NAME =~ "BUFG*" || REF_NAME =~ "MMCM*" || REF_NAME =~ "PLL*"}]
if { [llength $clkcells] } {
    set iice_hits {}
    set design_hits {}
    foreach c $clkcells {
        set nm [get_property NAME $c]
        set rf [get_property REF_NAME $c]
        if { [is_iice_cell $nm] } {
            lappend iice_hits "$rf  $nm"
        } else {
            lappend design_hits "$rf  $nm"
        }
    }
    set msg "[llength $clkcells] global clock buffer / clock manager cell(s) in\
 the RM netlist. The RP pblock has ZERO sites for these, so this checkpoint\
 cannot be placed.\n"
    if { [llength $iice_hits] } {
        append msg "\n  (a) IICE-INSERTED — [llength $iice_hits] cell(s). These sit\
 inside Identify's own logic.\n      FIX: the .idc must set BOTH\
 `device xilinxinsertbufg 0` AND `device skewfree 1`.\n      Check\
 <rev>/identify.idc, not the file you passed as IDC=.\n"
        foreach h $iice_hits { append msg "        $h\n" }
    }
    if { [llength $design_hits] } {
        append msg "\n  (b) SYNPLIFY-INFERRED ON DESIGN NETS — [llength $design_hits]\
 cell(s). NOTHING in the .idc affects these: Synplify automatically promotes\
 high-fanout clock AND reset nets onto global buffers, and Vivado does not.\n\
      FIX: `define_global_attribute syn_noclockbuf {1}` in\
 fpga/rp/nanosoc_iice/nanosoc_iice.fdc (already present — if you are reading\
 this, either the FDC was not consumed by Synplify, or these particular nets\
 escaped the global attribute and need a per-net\
 `define_attribute {n:<path>} syn_noclockbuf {1}`; see that file's\
 \"Global buffer inference\" block for the exact syntax and the fallback list).\n"
        foreach h $design_hits { append msg "        $h\n" }
    }
    error $msg
}
puts "INFO: no BUFG*/MMCM*/PLL* in the netlist (gate PASS)"

report_utilization    -file [file join $OUT_DIR util_rm_nanosoc.rpt]
report_timing_summary -file [file join $OUT_DIR timing_rm_nanosoc.rpt]

# R2 evidence, cheap: dump the RAMB INIT strings so a human can diff BRAM
# preload content against the Vivado-synth baseline. $readmemh -> INIT_*
# propagation through a SYNPLIFY EDIF is UNVERIFIED and the failure mode is
# silent (a DUT that boots from garbage).
# --- IMEM ROM payload gate (R2, now mechanical) -------------------------------
# R2 was CLOSED on 2026-07-29: the Synplify EDIF's IMEM INIT_00 came out
# byte-identical to the Vivado baseline's
#   u_nanosoc/u_ss_cpu/u_region_imem_0/u_mem/u_sram/mem_reg_0
# with vsrc_override/sl_fpga_rom_word.v in place. That proof is worth keeping,
# because the failure mode is SILENT (a DUT booting from garbage) and because
# every future synthesis-option change -- syn_noclockbuf included -- is a chance
# to break it without breaking anything else.
#
# So: assert it. EXPECT_IMEM_INIT_00 defaults in the Makefile to the proven
# string. Set EXPECT_IMEM_INIT_00= (empty) to report without gating.
# SET MEMBERSHIP, not a single named cell. The first version of this gate picked
# "the IMEM RAMB cell" by name and FALSE-POSITIVED immediately: a 16 KiB IMEM
# maps to SEVERAL RAMB cells, only one of which holds address 0, and the cell
# NAMES DIFFER BETWEEN THE TWO FLOWS -- Vivado calls it
#   .../u_sram/mem_reg_0
# and Synplify calls it
#   .../u_sram/mem_1_mem_1_0_0
# so keying on the baseline's name silently selected a different (zero-filled,
# high-address) slice and reported the ROM as broken when it was byte-perfect.
#
# The naming-independent assertion is: the expected payload must appear as the
# INIT_00 of AT LEAST ONE RAMB cell in the IMEM hierarchy. That holds whatever
# the tool calls its cells and whatever order it packs them in.
proc init_hex { s } {
    # "256'hDEAD..." / "256'bBinary..." / bare hex -> uppercase hex digits, or ""
    # for a binary-formatted value (which we compare as-is instead).
    if { [regexp {'[hH]([0-9a-fA-F]+)$} $s -> h] } { return [string toupper $h] }
    if { [regexp {^[0-9a-fA-F]+$} $s] }            { return [string toupper $s] }
    return ""
}

set imem_cells [get_cells -quiet -hierarchical -filter \
    {REF_NAME =~ "RAMB*" && NAME =~ "*u_region_imem_0*"}]
set want ""
if { [info exists env(EXPECT_IMEM_INIT_00)] } { set want $env(EXPECT_IMEM_INIT_00) }
set want_h [init_hex $want]

if { [llength $imem_cells] == 0 } {
    if { $want ne "" } {
        error "no RAMB cell matched *u_region_imem_0* but EXPECT_IMEM_INIT_00 is\
 set, i.e. a preloaded IMEM was expected and there is no IMEM block RAM at all.\
 Either RAM_PRELOAD was lost from the file list, or the IMEM spilled out of\
 block RAM (check syn_ramstyle in vsrc_override/sl_fpga_rom_word.v)."
    }
    puts "WARNING: no RAMB cell matched *u_region_imem_0* — IMEM ROM payload not checked."
} else {
    puts "INFO: IMEM ROM payload — [llength $imem_cells] RAMB cell(s) in the IMEM:"
    set hit ""
    foreach c [lsort $imem_cells] {
        set got [get_property INIT_00 $c]
        set mark "  "
        if { $want_h ne "" && [init_hex $got] eq $want_h } { set mark "->" ; set hit $c }
        puts "INFO: $mark [get_property NAME $c]"
        puts "INFO:      INIT_00 = $got"
    }
    if { $want eq "" } {
        puts "INFO:   (EXPECT_IMEM_INIT_00 unset/empty — reported, not gated)"
    } elseif { $hit ne "" } {
        puts "INFO:   R2 gate PASS — the proven Vivado-baseline INIT_00 payload is\
 present, on [get_property NAME $hit]."
    } else {
        error "IMEM ROM PAYLOAD MISSING.\n  expected INIT_00: $want\n\
 It is not the INIT_00 of ANY of the [llength $imem_cells] RAMB cells in the IMEM\
 (all values listed above). Something in the synthesis options, the file list, or\
 vsrc_override/sl_fpga_rom_word.v has altered the \$readmemh -> BRAM INIT path.\
 This is the SILENT failure mode: the DUT would boot from garbage and every other\
 gate here would still pass. Do NOT ship this checkpoint.\n\
 Before assuming a regression, check the EDIF directly — it is plain text:\n\
   grep -c \"[string range $want_h 0 15]\" <the .edf>\n\
 If the payload IS in the EDIF, the fault is in this gate, not the build. If the\
 firmware image was changed on purpose, update EXPECT_IMEM_INIT_00 in\
 fpga/rp/nanosoc_iice/Makefile in the same commit."
    }
}

set fh [open [file join $OUT_DIR bram_init_audit.txt] w]
set nram 0
foreach c [lsort [get_cells -quiet -hierarchical -filter {REF_NAME =~ "RAMB*"}]] {
    incr nram
    set nz 0
    foreach prop [lsort [list_property $c]] {
        if { ![string match "INIT_*" $prop] } { continue }
        set v [get_property $prop $c]
        if { [regexp {[1-9a-fA-F]} $v] } { incr nz }
    }
    puts $fh "[get_property REF_NAME $c]\t$nz\t$c"
}
close $fh
puts "INFO: BRAM INIT audit: $nram RAMB cells -> $OUT_DIR/bram_init_audit.txt\
 (column 2 = count of NON-ZERO INIT_* strings; a preloaded IMEM must have\
 non-zero entries. R2 gate: diff this against the Vivado baseline.)"

set dcp [file join $OUT_DIR rm_nanosoc_synth.dcp]
write_checkpoint -force $dcp
puts "INFO: wrote $dcp"
puts "RM_NANOSOC_IICE_SYNTH_COMPLETE"
