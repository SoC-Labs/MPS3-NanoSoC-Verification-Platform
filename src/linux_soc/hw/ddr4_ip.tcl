### FOLDED 2026-09-23 into the shell's CPU seam (LINUX_HARNESS_PLAN_2026-09-23.md
### DL2): the MicroBlaze V shell now takes its DDR4/MBV configuration from
### fpga/shell/bd/ddr4_mig.tcl, cpu_mbv.tcl and fpga/shell/constraints/mbv/.
### This copy is the July linux_soc PoC's own, frozen as its reference build;
### change the seam copy, not this one.
###-----------------------------------------------------------------------------
### ddr4_ip.tcl — create + configure the Xilinx DDR4 controller IP
###               (xilinx.com:ip:ddr4:2.2), module "ddr4_0".
###
### PoC: DDR4 + MicroBlaze-V in the KU115 fabric (poc/ddr4_mbv/).
###
### This script is meant to be `source`d by a parent build script that has
### ALREADY created an in-memory Vivado project for part xcku115-flvb1760-1-c.
### It ONLY creates and configures the IP and generates its targets; it does not
### touch synthesis, implementation, the block design, or constraints.
###
### Package/pin assignment is NOT done here. There is no CONFIG parameter for
### package pins on this IP — the DDR4 pins are supplied by a separate user XDC,
### ddr4_pins.xdc (owned by another agent). Do not add pin LOCs to this script.
###
### Ports emitted by ddr4_0 for the parent to wire up:
###   c0_sys_clk_p / c0_sys_clk_n   differential 100 MHz reference (board OSC6)
###   sys_rst                       active-HIGH system reset in
###   c0_init_calib_complete        calibration-done flag out
###   c0_ddr4_ui_clk                user-interface clock out (= CK/4 = 200 MHz)
###   c0_ddr4_ui_clk_sync_rst       active-HIGH UI reset out
###   c0_ddr4_aresetn               active-LOW AXI-slave reset *** IN ***  -- MUST BE
###                                 DRIVEN (typically ~c0_ddr4_ui_clk_sync_rst). It is
###                                 an INPUT, not an output: this line used to say
###                                 "out", mbv_soc.tcl believed it and left the pin
###                                 dangling, IPI silently tied it to 1'b0 = reset
###                                 ASSERTED, and the MIG's AXI slave never returned a
###                                 single write response -- the CPU wedged on its
###                                 first DDR store while calibration still said OK.
###   C0_DDR4_S_AXI                 AXI4 slave (512b data, 32b addr, 4b id)
###   C0_DDR4                       the external DDR4 memory interface
###
### =====================  MEMORY-PART SELECTION  ==============================
### The MPS3 board's FITTED DIMM is Micron MTA4ATF51264HZ-2G3B1 (a SO-DIMM;
### DDR4-2400 CL17 — MPS3 TRM 100765 fig 2-18; the TRM prose "2GB31" is a
### typo). We configure the controller with the closest STOCK catalogue part
###                     MTA4ATF51264HZ-2G6  (MemoryType = SODIMMs)
### — same family and form factor as the fitted part. Re-verified against Vivado
### 2026.1's part database (line 69; the part survived the version bump, and the
### build asserts the read-back part/tCK anyway — see build.tcl _assert_mem_part):
###   <EDA-install>/Xilinx/Vivado/2026.1/Vivado/data/ip/xilinx/mem_v1_4/csv/ddr4_sdram/memparts.csv
###
###   * MTA4ATF51264HZ-2G6 is a SO-DIMM, 1-rank, x16 device, 4 GB, BG width 1.
###
###   * It matches the fitted MTA4ATF51264HZ FIELD-FOR-FIELD on every
###     controller-visible attribute (line 69 of memparts.csv):
###         Rank            = 1        CA Mirror       = 0
###         Data mask       = 1        Address width   = 17
###         Row width       = 16       Column width    = 10
###         Bank width      = 2        Bank group width= 1     <-- the key one
###         CS width        = 1        CKE width       = 1
###         ODT width       = 1        CK width        = 1
###         Memory density  = 4GB      Component densty= 8Gb
###         Device width    = 64       Min period      = 750 ps
###         Max period      = 1600 ps
###     The ONLY deviation is the speed-grade timing model: -2G6 is the
###     DDR4-2666 model, whereas the fitted part (MTA4ATF51264HZ-2G3B1 per
###     MPS3 TRM 100765 fig 2-18 — the TRM prose "2GB31" is a typo) is a
###     DDR4-2400 CL17 grade, tAA(min) = 14.16 ns. The *organisation*
###     (address/bank/row map) is identical, BUT the faster -2G6 timing model
###     is NOT harmless left to itself: at tCK = 1250 ps it lets the IP
###     auto-derive CL = 11 (11 x 1.25 ns = 13.75 ns < 14.16 ns), undershooting
###     the fitted die's rated tAA by ~0.4 ns — a JEDEC-on-paper violation.
###     The fitted part's official JEDEC down-bin at 1600 MT/s is CL = 12
###     (15.0 ns >= 14.16 ns), so CasLatency is PINNED to 12 below (read-back
###     verified like every other critical parameter). Found during the
###     2026-07-15 calibration bring-up (init_calib_complete stuck 0 on
###     silicon); the derived CL=11 was decoded from the built xci/MR0.
###
###   * A *custom* part is NOT required (so simulation stays available; a custom
###     part disables it — see xgui/ddr4_v2_2.tcl:593).
###
### -------------------------  HAZARD: DO NOT USE  ------------------------------
### Do NOT substitute the x8 sibling MTA8ATF51264HZ-2G1 (memparts.csv line 27).
### Despite the near-identical name it has a DIFFERENT organisation:
###       Bank group width = 2   (would drive bg[1] = pin F18, which this
###                               single-bank-group DIMM does NOT have)
###       Row width        = 15   (this DIMM has 16 row-address bits)
### It would produce a WRONG bank map and WRONG row addressing, and it would
### fail SILENTLY: build clean, simulate clean, then corrupt on the bench.
### Always confirm Bank group width = 1 and Row width = 16 for this board.
### =============================================================================
###
### =====================  TIMING TARGET  ======================================
### C0.DDR4_TimePeriod = 1250 ps  ->  tCK = 1.25 ns  ->  CK = 800 MHz  ->
###                                   1600 MT/s  ->  c0_ddr4_ui_clk = 800/4 = 200 MHz.
### Rationale (deliberately conservative — we are proving CALIBRATION, not
### chasing bandwidth):
###   * The MPS3 TRM caps the DDR4 interface at 1800 MT/s.
###   * memparts.csv gives this part a period range of [750, 1600] ps; tCK 1250
###     ps sits comfortably inside it (Max period 1600 ps = its slowest, Min 750
###     ps = its fastest supported tCK).
###   * On a -1 speed grade, time_periods.csv allows 2133 MT/s only in the
###     Group-1 HP banks and 1866 MT/s elsewhere; 1250 ps (1600 MT/s) is
###     comfortably legal in ANY bank group, so the PoC is not sensitive to
###     which bank the pins land in.
### Reference clock: C0.DDR4_InputClockPeriod = 10000 ps = 100 MHz differential
### on H19/H18 (board OSC6).
### =============================================================================
###
### Usage (from the parent script):
###   source [file join $poc_dir ddr4_ip.tcl]
###   # then check $::ddr4_cfg_warnings == 0 before trusting the timing.
###-----------------------------------------------------------------------------

### Global warning counter — the single most valuable output of this script.
### The parent MUST check this: a non-zero value means Vivado silently SNAPPED
### one or more requested parameters to a different legal value.
set ::ddr4_cfg_warnings 0

set ip_name  "ddr4_0"
set ip_vlnv  "xilinx.com:ip:ddr4:2.2"
set req_part "xcku115-flvb1760-1-c"

###-----------------------------------------------------------------------------
### 1. Guard: this configuration is only valid for the KU115 on the MPS3.
###-----------------------------------------------------------------------------
if {[llength [get_projects -quiet]] == 0} {
    error "ddr4_ip.tcl: no current Vivado project. This script must be sourced\
           by a parent that has already created an in-memory project for\
           part $req_part."
}
set cur_part [get_property PART [current_project]]
if {$cur_part ne $req_part} {
    error "ddr4_ip.tcl: current project part is '$cur_part', but this DDR4 IP\
           configuration is only valid for '$req_part'. Refusing to continue\
           (the part-specific timing/geometry would be wrong)."
}
puts "INFO: ddr4_ip.tcl — part OK ($cur_part); creating IP '$ip_name' ($ip_vlnv)."

###-----------------------------------------------------------------------------
### 2. Create the IP.
###-----------------------------------------------------------------------------
if {[llength [get_ipdefs -quiet -all $ip_vlnv]] == 0} {
    error "ddr4_ip.tcl: IP definition '$ip_vlnv' is not in the catalogue.\
           Is the DDR4 controller (mem_v1_4) installed for this Vivado?"
}
if {[current_bd_design -quiet] eq ""} {
    error "ddr4_ip.tcl: no block design is open. The parent must create_bd_design\
           before sourcing this file -- ddr4_0 must be a BD cell so that\
           mbv_soc.tcl can connect C0_DDR4_S_AXI to it."
}
if {[llength [get_bd_cells -quiet $ip_name]] > 0} {
    error "ddr4_ip.tcl: an IP named '$ip_name' already exists in the project.\
           Remove it before sourcing this script (create_ip would fail)."
}
create_bd_cell -type ip -vlnv $ip_vlnv $ip_name

###-----------------------------------------------------------------------------
### 3. Configuration.
###
### Single source of truth: $ddr4_cfg is a flat {key value key value ...} list
### used BOTH for the set_property -dict below AND for the read-back verify in
### step 4, so the two can never drift apart. Comments are attached with `;#`
### (a real dict literal {..} cannot carry Tcl comments — they'd become data).
###
### Every line cites where the parameter name comes from. Where a precise
### component.xml line number is not known it is cited as plain `component.xml`
### rather than inventing a number.
###-----------------------------------------------------------------------------
set ddr4_cfg [list]

### -- Memory part & type (stock SO-DIMM matching the fitted DIMM) --------------
### DO NOT change to the x8 MTA8ATF51264HZ-2G1 — it is BG width 2 / row 15 and
### fails silently on the bench. See the HAZARD note in the header.
lappend ddr4_cfg CONFIG.C0.DDR4_MemoryType {SODIMMs}               ;# component.xml — see MEMORY-PART SELECTION header
lappend ddr4_cfg CONFIG.C0.DDR4_MemoryPart {MTA4ATF51264HZ-2G6}    ;# component.xml — stock SO-DIMM matching the fitted MTA4ATF51264HZ field-for-field (BG=1, row=16)

### -- Clocking -----------------------------------------------------------------
lappend ddr4_cfg CONFIG.System_Clock {Differential}                ;# component.xml — NOT C0.-prefixed; board OSC6 diff pair H19/H18
lappend ddr4_cfg CONFIG.C0.DDR4_InputClockPeriod {10000}           ;# component.xml — 10000 ps = 100 MHz reference clock
lappend ddr4_cfg CONFIG.C0.DDR4_TimePeriod {1250}                  ;# component.xml — tCK 1250 ps -> 800 MHz CK -> 1600 MT/s (see TIMING TARGET header)

### -- Simulation ---------------------------------------------------------------
lappend ddr4_cfg CONFIG.Simulation_Mode {BFM}                      ;# component.xml — NOT C0.-prefixed; BFM = fast/abbreviated calibration

### -- AXI slave interface ------------------------------------------------------
lappend ddr4_cfg CONFIG.C0.DDR4_AxiSelection {true}               ;# component.xml — expose the AXI4 slave (C0_DDR4_S_AXI)
lappend ddr4_cfg CONFIG.C0.DDR4_AxiDataWidth {512}               ;# component.xml — native AXI width for x64 @ 4:1
lappend ddr4_cfg CONFIG.C0.DDR4_AxiIDWidth {4}                   ;# component.xml
lappend ddr4_cfg CONFIG.C0.DDR4_AxiAddressWidth {32}            ;# component.xml
### NARROW BURSTS ARE MANDATORY HERE -- DO NOT SET THIS false.
###
### The MIG's AXI slave is 512 bits wide (DDR4_AxiDataWidth above). The only
### master on it is the MicroBlaze-V, a 32-BIT master, behind an UPSIZING
### SmartConnect. So EVERY SINGLE STORE the CPU makes arrives here as a NARROW
### transfer -- 4 of the 64 byte lanes. Measured at the ddr4_0 slave pin in the
### fw_memtest co-sim, the very first store to DRAM is:
###     awaddr=80000000  awsize=2  awlen=0   wstrb=0x000000000000000f
###     awaddr=80000004  awsize=2  awlen=0   wstrb=0x00000000000000f0
### i.e. textbook-correct AXI narrow writes with correct byte enables.
###
### Setting this to {false} propagates C_S_AXI_SUPPORTS_NARROW_BURST=0 into the
### synthesised ddr4_v2_2_axi shim (confirmed: rtl/ip_top/..._ddr4.sv:265 read
### back `parameter C_S_AXI_SUPPORTS_NARROW_BURST = 0`), which OPTIMISES OUT the
### byte-lane/strobe steering the shim needs to honour those WSTRBs. The shim then
### commits whole 64-byte beats, so each 4-byte store SILENTLY CLOBBERS THE 60
### BYTES AROUND IT. The memtest sees it as a word written and acknowledged (the
### B response returns!) but reading back as 0x00000000.
###
### It was previously {false} with the comment "no narrow-burst support needed",
### which was simply wrong -- this design cannot issue anything BUT narrow bursts.
### The DDR4 module has DM pins (DDR4_DataMask=DM_NO_DBI below), so byte-granular
### writes are supported by the hardware; the shim just has to be told to use them.
lappend ddr4_cfg CONFIG.C0.DDR4_AxiNarrowBurst {true}          ;# component.xml — REQUIRED: a 32-bit master on a 512-bit slave issues narrow writes on every store

### -- DRAM geometry / features ------------------------------------------------
lappend ddr4_cfg CONFIG.C0.DDR4_DataWidth {64}                  ;# component.xml — x64 module
lappend ddr4_cfg CONFIG.C0.DDR4_Ecc {false}                    ;# component.xml — non-ECC module
lappend ddr4_cfg CONFIG.C0.DDR4_DataMask {DM_NO_DBI}          ;# component.xml — data mask on, no DBI
lappend ddr4_cfg CONFIG.C0.DDR4_Slot {Single}                ;# component.xml — single slot
lappend ddr4_cfg CONFIG.C0.DDR4_MemoryVoltage {1.2V}        ;# component.xml — DDR4 nominal 1.2 V
lappend ddr4_cfg CONFIG.C0.DDR4_isCustom {false}           ;# component.xml — stock catalogue part (custom would forbid sim)
lappend ddr4_cfg CONFIG.C0.DDR4_Specify_MandD {false}     ;# component.xml — let the IP derive M and D from the periods

### -- CAS latency: pin to the fitted part's official 1600 MT/s down-bin --------
### DO NOT revert this to auto-derived. The configured stock part (-2G6) carries
### a DDR4-2666 timing model, so left to itself the IP derives CL=11 at
### tCK=1250 ps (confirmed in a built xci: MR0=13'b0001100010000 -> CL=11).
### 11 x 1.25 ns = 13.75 ns UNDERSHOOTS the fitted -2G3B1 die's rated
### tAA(min)=14.16 ns (DDR4-2400 CL17). JEDEC's official down-bin for a 2400
### part at 1600 MT/s is CL=12 (15.0 ns >= 14.16 ns). See the MEMORY-PART
### SELECTION header. C0.DDR4_CasWriteLatency stays auto-derived: the IP's
### CWL=11 (from MR2, decoded the same way) is a legal JEDEC second-set value
### at 1600 MT/s and CWL has no tAA-equivalent bound to violate.
### If Vivado ever snaps this (read-back below is CRITICAL + fatal), the legal
### CL list for the part/tCK has changed — re-derive from the part datasheet,
### do not just delete the line.
lappend ddr4_cfg CONFIG.C0.DDR4_CasLatency {12}                    ;# component.xml — fitted -2G3B1 (DDR4-2400 CL17) official down-bin at 1600 MT/s; auto would give an out-of-spec CL=11

set_property -dict $ddr4_cfg [get_bd_cells $ip_name]

###-----------------------------------------------------------------------------
### 4. Verify — read every parameter back and compare to what we asked for.
###
### Vivado SILENTLY snaps illegal / unsupported values to the nearest legal one
### (DDR4_TimePeriod and DDR4_InputClockPeriod are the classic offenders). A
### read-back mismatch means the controller will NOT run at the intended
### settings, so we make it loud and count it in ::ddr4_cfg_warnings.
###
### The values in $ddr4_cfg are all written in the IP's canonical form
### (true/false, bare integers, exact strings) so an equal read-back compares
### exactly; anything that differs is a genuine snap, not cosmetic.
###-----------------------------------------------------------------------------
set ddr4_critical [list \
    CONFIG.C0.DDR4_TimePeriod \
    CONFIG.C0.DDR4_InputClockPeriod \
    CONFIG.C0.DDR4_MemoryPart \
    CONFIG.C0.DDR4_MemoryType \
    CONFIG.C0.DDR4_CasLatency \
    CONFIG.C0.DDR4_AxiDataWidth \
    CONFIG.C0.DDR4_AxiNarrowBurst \
    CONFIG.Simulation_Mode]

puts "INFO: ddr4_ip.tcl — verifying [expr {[llength $ddr4_cfg] / 2}] parameters against read-back..."
set ddr4_critical_snaps {}
foreach {k want} $ddr4_cfg {
    set got [get_property $k [get_bd_cells $ip_name]]
    if {$got ne $want} {
        incr ::ddr4_cfg_warnings
        if {[lsearch -exact $ddr4_critical $k] >= 0} {
            puts "############################################################"
            puts "## WARNING (CRITICAL): Vivado SNAPPED a DDR4 parameter"
            puts "##   parameter : $k"
            puts "##   requested : $want"
            puts "##   read-back : $got"
            puts "##   -> the controller will NOT behave as intended."
            puts "############################################################"
            lappend ddr4_critical_snaps "$k requested=$want read-back=$got"
        } else {
            puts "WARNING: ddr4_ip.tcl — '$k' read back as '$got' (requested '$want')"
        }
    }
}
### A snapped CRITICAL parameter means the controller is NOT the one we
### intended to build (wrong CL, wrong tCK, wrong part...). No parent script
### checked ::ddr4_cfg_warnings in practice, so a snap here used to survive to
### the bench looking like a mystery hardware bug. Fail the build instead.
if {[llength $ddr4_critical_snaps] > 0} {
    error "ddr4_ip.tcl: Vivado snapped [llength $ddr4_critical_snaps] CRITICAL DDR4 parameter(s): [join $ddr4_critical_snaps {; }] — refusing to continue with an unintended controller configuration."
}

###-----------------------------------------------------------------------------
### 4b. MAKE THE *SIMULATION MODEL* HONOUR BYTE ENABLES.
###
### CONFIG.C0.DDR4_AxiNarrowBurst (above) controls the SYNTHESISED shim. It does
### NOT control the TLM/BFM model used in simulation: that is driven by a separate
### bus-interface parameter on the C0_DDR4_S_AXI interface, which Xilinx generates
### as 0 for this IP *even when the IP itself is configured for narrow bursts*.
### Verified on a build whose xci had DDR4_AxiNarrowBurst=true and whose synth shim
### correctly read C_S_AXI_SUPPORTS_NARROW_BURST=1, while:
###   linux_soc_ddr4_0_0.xml  BUSIFPARAM_VALUE.C0_DDR4_S_AXI.SUPPORTS_NARROW_BURST = 0
###                           <xilinx:parameterUsage>simulation.tlm</xilinx:parameterUsage>
###   linux_soc_ddr4_0_0.cpp  addLong("SUPPORTS_NARROW_BURST", "0")
### With that, the xaximm_pin2xtlm transactor assumes every beat is full-width and
### DISCARDS WSTRB, so each 4-byte CPU store commits all 64 bytes of its beat and
### wipes its neighbours -- the memtest's read-back then returns 0x00000000 for a
### word the CPU demonstrably wrote and got a B response for.
###
### This is a fidelity defect of the vendor's SIM MODEL, not of the design: real
### DDR4 honours byte enables through the DM pins (DDR4_DataMask=DM_NO_DBI). Left
### as-is, NO correct design could ever pass a byte-granular memtest here -- the
### model would corrupt it. The parameter is declared valuePermission="bd", so the
### block design is allowed to set it; we set it to match the hardware.
###
### This changes ONLY the memory model's byte-enable handling. The DUT -- CPU,
### caches, SmartConnect, address map, the MIG's own RTL -- is untouched, and every
### check in the memtest still has to pass on its own merits.
set ddr4_saxi [get_bd_intf_pins -quiet $ip_name/C0_DDR4_S_AXI]
if {$ddr4_saxi eq ""} {
    error "ddr4_ip.tcl: $ip_name/C0_DDR4_S_AXI interface pin not found -- cannot set SUPPORTS_NARROW_BURST."
}
set_property CONFIG.SUPPORTS_NARROW_BURST {1} $ddr4_saxi
set snb [get_property CONFIG.SUPPORTS_NARROW_BURST $ddr4_saxi]
if {$snb ne "1"} {
    puts "############################################################"
    puts "## WARNING (CRITICAL): C0_DDR4_S_AXI.SUPPORTS_NARROW_BURST"
    puts "##   read back as '$snb', wanted '1'."
    puts "##   The DDR4 BFM will DISCARD WSTRB on the CPU's 4-byte stores"
    puts "##   and the memtest read-back will be corrupt (reads 0) even"
    puts "##   though the hardware is correct. See the header above."
    puts "############################################################"
    incr ::ddr4_cfg_warnings
} else {
    puts "INFO: ddr4_ip.tcl — C0_DDR4_S_AXI.SUPPORTS_NARROW_BURST = $snb (BFM will honour WSTRB)"
}

###-----------------------------------------------------------------------------
### 5. Generate the IP targets (needed for both synthesis and simulation, plus
###    the HDL instantiation template the parent uses to wire the ports).
###-----------------------------------------------------------------------------
# generate_target is NOT run here: ddr4_0 is a BD cell, and output products are
# materialised by the parent when it generates the whole block design.

###-----------------------------------------------------------------------------
### 6. Summary. tCK is read back (not assumed) so the derived rates reflect what
###    the IP actually holds, even if a snap occurred above.
###-----------------------------------------------------------------------------
set s_tck   [get_property CONFIG.C0.DDR4_TimePeriod   [get_bd_cells $ip_name]]
set s_type  [get_property CONFIG.C0.DDR4_MemoryType   [get_bd_cells $ip_name]]
set s_part  [get_property CONFIG.C0.DDR4_MemoryPart   [get_bd_cells $ip_name]]
set s_axiw  [get_property CONFIG.C0.DDR4_AxiDataWidth [get_bd_cells $ip_name]]
set s_sim   [get_property CONFIG.Simulation_Mode      [get_bd_cells $ip_name]]
set s_ckmhz [expr {1.0e6 / double($s_tck)}]
set s_mts   [expr {2.0 * $s_ckmhz}]
set s_uimhz [expr {$s_ckmhz / 4.0}]

puts "==================== DDR4 IP ($ip_name) summary ===================="
puts [format "  FPGA part      : %s" $req_part]
puts [format "  Memory part    : %s  (%s)" $s_part $s_type]
puts [format "  tCK            : %s ps  (CK = %.1f MHz)" $s_tck $s_ckmhz]
puts [format "  Data rate      : %.0f MT/s" $s_mts]
puts [format "  UI clock       : %.1f MHz  (= CK/4)" $s_uimhz]
puts [format "  AXI data width : %s bit" $s_axiw]
puts [format "  Simulation mode: %s" $s_sim]
puts [format "  Config warnings: %d" $::ddr4_cfg_warnings]
puts "===================================================================="
if {$::ddr4_cfg_warnings > 0} {
    puts "WARNING: ddr4_ip.tcl finished with $::ddr4_cfg_warnings snapped\
          parameter(s); the parent build should treat the timing as UNTRUSTED."
}
