### -----------------------------------------------------------------------------
### fpga/rp/eth_ss/filelist.tcl — OOC-synth file list for rm_eth_ss.
###
### A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
### license.
###
### Companion to rp_eth_ss_wrapper.sv / eth_ss_bringup.sv (this directory).
### Resolves this RM's Vivado out-of-context synthesis sources:
###   1. This RM's own two files (eth_ss_bringup.sv, rp_eth_ss_wrapper.sv).
###   2. The MAC + PTP subsystem internals, consumed READ-ONLY via the source
###      repo's own recommended flist,
###      ${ETHMAC_AHB_HOME}/flist/ethmac_subsystem_apb.flist (OpenCores ethmac
###      + SoC Labs patches + HA1588 PTP + eth_rx_cksum + APB/WB bridges +
###      cmsdk_apb_slave_mux + ethmac_subsystem_apb.v).
###   3. The handful of extra files the AHB wrapper level needs on top of that
###      flist (mirrors what the upstream fpga/filelist_common.tcl adds):
###      cmsdk_ahb_to_apb.v, ethmac_subsystem_ahb.v, rmii_to_mii.v, plus
###      cmsdk_ahb_to_sram.v + sl_ahb_sram.v for this RM's internal DMA SRAM.
###
### Nothing under the source repos or the Arm IP library is copied or modified;
### this script only *reads* those trees (lab read-only policy).
###
### Usage (same convention as ../nanosoc/filelist.tcl):
###   vivado -mode batch -source fpga/rp/eth_ss/filelist.tcl
###   ... then, in the calling script/session:
###   synth_design -mode out_of_context -top rp_eth_ss_wrapper \
###                -part xcku115-flvb1760-1-c
### This script only populates the current fileset (`read_verilog`), sets
### `include_dirs` and the ETH_WISHBONE_B3 define — it deliberately does not
### call `create_project` or `synth_design` itself (ooc_synth.tcl does).
###
### Also runs standalone under plain `tclsh` (no Vivado) as a DRY RUN — the
### flist parser below is pure Tcl and prints the resolved lists instead of
### feeding them to `read_verilog` when no Vivado interpreter is detected.
###
### Env vars (set them in tools.env -- fpga/dfx/Makefile forwards them -- or
### export them before invoking Vivado). The two site paths have NO default:
###   ETH_SS_HOME       (REQUIRED, no default)
###     -- the ethernet-subsystem-ahb checkout this RM wraps. READ-ONLY.
###        CHECKOUT CHOICE (see README.md "Which checkout and why"): this is
###        the repo rm_list.tcl names; a standalone ethernet-mac-ahb checkout
###        at 3246c90 (2026-04-23) is OLDER and lacks eth_rx_cksum.v, which
###        ethmac_subsystem_ahb.v requires — it will NOT build this RM.
###   ETHMAC_AHB_HOME   (default $ETH_SS_HOME/ethernet-mac-ahb)
###     -- the ethernet-mac-ahb SUBMODULE INSIDE that checkout (037ac83), NOT
###        the standalone sibling repo, for the same eth_rx_cksum reason.
###   ARM_IP_LIBRARY_PATH (REQUIRED, no default) -- the read-only Arm IP library
###   ETHMAC_IP_DIR     (default $ARM_IP_LIBRARY_PATH/OpenCores-EthMAC)
###   HA1588_IP_DIR     (default $ARM_IP_LIBRARY_PATH/OpenCores-HA1588)
###   CMSDK_DIR         (default $ARM_IP_LIBRARY_PATH/BP210/
###                               BP210-BU-00000-r1p1-00rel0)
### The derived defaults match the source repo's own set_env.sh layout.
### -----------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Env var resolution
# ---------------------------------------------------------------------------
proc _soclabs_env_default {var default} {
    if {[info exists ::env($var)] && $::env($var) ne ""} {
        return $::env($var)
    }
    return $default
}

foreach {v} {ETH_SS_HOME ARM_IP_LIBRARY_PATH} {
    if { [_soclabs_env_default $v ""] eq "" } {
        error "filelist.tcl: $v is not set and has NO default (a default naming one\
 site's directory silently reads the wrong tree elsewhere). Set it in tools.env\
 (see tools.env.example) or export it before invoking Vivado."
    }
}
set ETH_SS_HOME [_soclabs_env_default ETH_SS_HOME ""]

set ::env(ETH_SS_HOME)         $ETH_SS_HOME
set ::env(ETHMAC_AHB_HOME)     [_soclabs_env_default ETHMAC_AHB_HOME \
    "$ETH_SS_HOME/ethernet-mac-ahb"]
set ::env(ARM_IP_LIBRARY_PATH) [_soclabs_env_default ARM_IP_LIBRARY_PATH ""]
set ::env(ETHMAC_IP_DIR)       [_soclabs_env_default ETHMAC_IP_DIR \
    "$::env(ARM_IP_LIBRARY_PATH)/OpenCores-EthMAC"]
set ::env(HA1588_IP_DIR)       [_soclabs_env_default HA1588_IP_DIR \
    "$::env(ARM_IP_LIBRARY_PATH)/OpenCores-HA1588"]
set ::env(CMSDK_DIR)           [_soclabs_env_default CMSDK_DIR \
    "$::env(ARM_IP_LIBRARY_PATH)/BP210/BP210-BU-00000-r1p1-00rel0"]

foreach {v} {ETH_SS_HOME ETHMAC_AHB_HOME ETHMAC_IP_DIR HA1588_IP_DIR CMSDK_DIR} {
    if { ![file isdirectory $::env($v)] } {
        error "filelist.tcl: $v does not exist: $::env($v)"
    }
}

# ---------------------------------------------------------------------------
# Pure-Tcl SoC Labs flist parser — same dialect and logic as
# ../nanosoc/filelist.tcl (VCS-style: `-f nested`, `+incdir+`, `+libext+`,
# `+define+`, `//`/`#` comments, `$(VAR)`/`${VAR}` expansion, bare paths).
# Duplicated here rather than sourced so each RM's filelist stays a
# self-contained single file (matching the rm_nanosoc precedent).
# ---------------------------------------------------------------------------
proc _soclabs_expand_vars {line} {
    set out $line
    while {[regexp {\$\(([A-Za-z_][A-Za-z0-9_]*)\)} $out -> var]} {
        if {![info exists ::env($var)]} {
            error "soclabs flist: \$($var) referenced but env($var) is not set (line: $line)"
        }
        set out [string map [list "\$($var)" $::env($var)] $out]
    }
    while {[regexp {\$\{([A-Za-z_][A-Za-z0-9_]*)\}} $out -> var]} {
        if {![info exists ::env($var)]} {
            error "soclabs flist: \${$var} referenced but env($var) is not set (line: $line)"
        }
        set out [string map [list "\$\{$var\}" $::env($var)] $out]
    }
    return $out
}

proc soclabs_flist_parse {path filesVar incdirVar defineVar seenVar} {
    upvar 1 $filesVar files
    upvar 1 $incdirVar incdirs
    upvar 1 $defineVar defines
    upvar 1 $seenVar seen

    set path [file normalize $path]
    if {[info exists seen($path)]} { return }
    set seen($path) 1

    if {![file exists $path]} {
        error "soclabs flist: file not found: $path"
    }

    set fh [open $path r]
    set data [read $fh]
    close $fh

    foreach raw [split $data "\n"] {
        set line [string trim $raw]
        if {$line eq ""} { continue }
        if {[string match "//*" $line]} { continue }
        if {[string match "#*" $line]} { continue }

        set line [_soclabs_expand_vars $line]

        if {[string match "-f *" $line]} {
            set nested [string trim [string range $line 2 end]]
            soclabs_flist_parse $nested files incdirs defines seen
            continue
        }
        if {[string match "+incdir+*" $line]} {
            lappend incdirs [string range $line 8 end]
            continue
        }
        if {[string match "+define+*" $line]} {
            lappend defines [string range $line 8 end]
            continue
        }
        if {[string match "+libext+*" $line]} {
            continue  ;# informational only — Vivado infers by extension
        }
        if {[string match "-y *" $line]} {
            continue  ;# library-dir directive; unused in this tree
        }
        if {[string match "-*" $line] || [string match "+*" $line]} {
            puts "WARNING: soclabs_flist_parse: unrecognized directive, skipping: $line"
            continue
        }
        if {![info exists seen($line)]} {
            set seen($line) 1
            lappend files $line
        }
    }
}

# ---------------------------------------------------------------------------
# 1. The subsystem-internals flist (MAC + patches + PTP + cksum + bridges +
#    APB wrapper). This is the SUBMODULE's copy — it differs from the
#    standalone ethernet-mac-ahb repo's (adds eth_rx_cksum.v, uses the
#    patched ptp_parser.v) — see the ETHMAC_AHB_HOME note in the header.
# ---------------------------------------------------------------------------
set _subsystem_flist "$::env(ETHMAC_AHB_HOME)/flist/ethmac_subsystem_apb.flist"

set _all_files   [list]
set _all_incdir  [list]
set _all_defines [list]
array set _seen {}

soclabs_flist_parse $_subsystem_flist _all_files _all_incdir _all_defines _seen

# ---------------------------------------------------------------------------
# 2. AHB-wrapper-level extras (mirrors upstream fpga/filelist_common.tcl's
#    additions, minus everything CPU/bootrom/region-related — this RM has no
#    CPU: eth_ss_bringup.sv is the bus master instead).
# ---------------------------------------------------------------------------
set _extra_files [list \
    "$::env(CMSDK_DIR)/logical/cmsdk_ahb_to_apb/verilog/cmsdk_ahb_to_apb.v" \
    "$::env(CMSDK_DIR)/logical/cmsdk_ahb_to_sram/verilog/cmsdk_ahb_to_sram.v" \
    "$ETH_SS_HOME/src/rtl/fpga_lib/sram/sl_ahb_sram.v" \
    "$ETH_SS_HOME/src/rtl/ethmac_subsystem_ahb.v" \
    "$::env(ETHMAC_AHB_HOME)/amba_wb_bridges/src/rtl/rmii_to_mii.v" \
]
foreach f $_extra_files {
    set fn [file normalize $f]
    if {![info exists _seen($fn)]} {
        set _seen($fn) 1
        lappend _all_files $f
    }
}

# ETH_SS_HOME/src/rtl on the include path (upstream filelist_common.tcl does
# the same; harmless if nothing ends up `including from it).
lappend _all_incdir "$ETH_SS_HOME/src/rtl"

# ---------------------------------------------------------------------------
# Existence check — hard-fail loudly on anything missing (no expected-missing
# generated files in this RM's fileset, unlike rm_nanosoc's bootrom).
# ---------------------------------------------------------------------------
set _missing [list]
foreach f $_all_files {
    if {![file exists $f]} { lappend _missing $f }
}
if {[llength $_missing] > 0} {
    puts "-------------------------------------------------------------------"
    puts "ERROR: [llength $_missing] required RTL file(s) not found on disk:"
    foreach m $_missing { puts "    - $m" }
    puts "Check ETH_SS_HOME / ETHMAC_AHB_HOME / ETHMAC_IP_DIR / HA1588_IP_DIR / CMSDK_DIR."
    puts "-------------------------------------------------------------------"
    error "filelist.tcl: [llength $_missing] required file(s) missing -- see above"
}

# ---------------------------------------------------------------------------
# This RM's own sources, resolved relative to this script's location.
# ---------------------------------------------------------------------------
set _rm_dir [file dirname [file normalize [info script]]]
set _rm_files [list \
    "$_rm_dir/eth_ss_bringup.sv" \
    "$_rm_dir/rp_eth_ss_wrapper.sv" \
]
foreach f $_rm_files {
    if {![file exists $f]} { error "filelist.tcl: RM source not found: $f" }
}

# ---------------------------------------------------------------------------
# Split by extension for read_verilog vs. read_verilog -sv.
# ---------------------------------------------------------------------------
set _v_files  [list]
set _sv_files [list]
foreach f $_all_files {
    if {[string match "*.sv" $f]} {
        lappend _sv_files $f
    } else {
        lappend _v_files $f
    }
}
foreach f $_rm_files { lappend _sv_files $f }

# The flist carries +define+ETH_WISHBONE_B3 (mandatory: selects the WB-B3
# protocol variant in the OpenCores MAC — upstream filelist_common.tcl sets
# the same). Ensure it's present even if the flist ever drops it.
if {[lsearch -exact $_all_defines "ETH_WISHBONE_B3"] < 0} {
    lappend _all_defines "ETH_WISHBONE_B3"
}

set ::RM_ETH_SS_FILES   [concat $_v_files $_sv_files]
set ::RM_ETH_SS_INCDIRS $_all_incdir
set ::RM_ETH_SS_DEFINES $_all_defines

puts "INFO: rm_eth_ss filelist resolved: [llength $_v_files] .v + [llength $_sv_files] .sv file(s), [llength $_all_incdir] include dir(s), defines: $_all_defines"

# ---------------------------------------------------------------------------
# Vivado / dry-run gate (same idiom as ../nanosoc/filelist.tcl).
# ---------------------------------------------------------------------------
if {[llength [info commands read_verilog]] > 0} {
    if {[llength $_v_files]  > 0} { read_verilog     $_v_files }
    if {[llength $_sv_files] > 0} { read_verilog -sv $_sv_files }
    if {[llength $_all_incdir] > 0} {
        set_property include_dirs $_all_incdir [current_fileset]
    }
    if {[llength $_all_defines] > 0} {
        set_property verilog_define $_all_defines [current_fileset]
    }
    puts "INFO: fileset populated. Caller should now run e.g.:"
    puts "  synth_design -mode out_of_context -top rp_eth_ss_wrapper -part xcku115-flvb1760-1-c"
} else {
    puts "INFO: no Vivado interpreter detected (read_verilog not a known command) -- dry run only."
    puts "INFO: resolved files:"
    foreach f $::RM_ETH_SS_FILES { puts "    $f" }
    puts "INFO: include dirs:"
    foreach d $::RM_ETH_SS_INCDIRS { puts "    $d" }
}
