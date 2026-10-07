###-----------------------------------------------------------------------------
### package_csr_ip.tcl — IP-XACT packaging helper for the six real shell CSR
### RTL blocks (fpga/shell/ip/*/*.sv, owned by another agent — READ-ONLY,
### never modified here).
###
### WHY THIS EXISTS (first real Vivado 2024.1 run, W-BD-VALIDATE wave):
### shell_bd.tcl originally added these six blocks with
### `create_bd_cell -type module -reference <name>` ("Add Module" — a raw
### RTL cell with no IP-XACT metadata), betting that Vivado would
### auto-infer their s_axi_* ports as an AXI4-Lite bus interface anyway.
### That bet was wrong in a more fundamental way than expected: it is not an
### interface-inference problem at all — `-type module -reference` flatly
### REFUSES a SystemVerilog top file in 2024.1:
###   ERROR: [filemgmt 56-195] Reference '<name>' contains top file
###   '.../<name>.sv' of type SystemVerilog. This type is not allowed as the
###   top file in the reference.
### (Confirmed live; re-tagging the same file as plain `Verilog` instead just
### fails to parse — these modules genuinely use `logic`/`always_ff`/etc.)
### There is no scalar-pin/property workaround for this; a `-type module`
### BD cell can only ever be plain Verilog or VHDL. The only supported path
### for a SystemVerilog RTL block in a 2024.1 BD is a real IP-XACT component
### (`-type ip`), which is exactly what this script produces.
###
### CONFIRMED LIVE (2024.1, this environment): `ipx::package_project
### -import_files` on a single-module project DOES auto-infer the AXI4-Lite
### bus interface from the standard `s_axi_*` naming convention, with NO
### extra Tcl needed beyond -import_files itself — no ipx::add_bus_interface/
### ipx::infer_bus_interface calls required. The inferred artifacts are:
###   - bus interface name:  `s_axi`     (lowercase — NOT `S_AXI`)
###   - address-block name:  `reg0`      (lowercase — NOT `Reg`)
###   - clock/reset interfaces `s_axi_aclk` / `s_axi_aresetn` also inferred
###     and cross-linked (ASSOCIATED_BUSIF/ASSOCIATED_RESET) automatically.
### The address block's RANGE scales correctly off C_S_AXI_ADDR_WIDTH once
### instantiated in a BD (verified: default 12-bit param -> 0x1000 range;
### overridden to 32 in shell_bd.tcl -> 0x1_0000_0000 range, `assign_bd_address
### -range 64K` still carves its normal window out of that as usual).
###
### Usage (sourced, not run standalone): provides
###   soclabs_package_csr_ip { part_name repo_root shell_dir {build_root {}} {force 0} }
### which (re)packages all six blocks into $repo_root/<name>/ (an IP-XACT
### component per block, vlnv soclabs.org:user:<name>:1.0) using disposable
### per-block sub-projects under $build_root (defaults to a temp dir next to
### $repo_root — NOT part of the checked-in repo tree; only $repo_root's
### contents are the durable artifact). Idempotent: skips a block whose
### $repo_root/<name>/component.xml already exists unless $force is set.
###-----------------------------------------------------------------------------

proc soclabs_package_csr_ip { part_name repo_root shell_dir {build_root {}} {force 0} } {
    if { $build_root eq "" } {
        set build_root [file join $repo_root ".pkg_build"]
    }
    file mkdir $repo_root
    file mkdir $build_root

    # name -> {top_module list_of_source_files}
    set blocks [dict create]
    dict set blocks dut_clkrst    [list dut_clkrst    [list [file join $shell_dir "ip" "clkrst"      "dut_clkrst.sv"]]]
    dict set blocks dfx_ctl       [list dfx_ctl       [list [file join $shell_dir "ip" "dfx_ctl"      "dfx_ctl.sv"]]]
    dict set blocks board_gpio    [list board_gpio    [list [file join $shell_dir "ip" "board_gpio"   "board_gpio.sv"]]]
    dict set blocks swd_bb        [list swd_bb        [list [file join $shell_dir "ip" "swd_bb"       "swd_bb.sv"]]]
    dict set blocks jtag_bb       [list jtag_bb       [list [file join $shell_dir "ip" "jtag_bb"      "jtag_bb.sv"]]]
    dict set blocks telem         [list telem         [list [file join $shell_dir "ip" "telem"        "telem.sv"]]]
    dict set blocks uart_bridge   [list uart_bridge   [list \
        [file join $shell_dir "ip" "uart_bridge" "uart_bridge.sv"] \
        [file join $shell_dir "ip" "uart_bridge" "uartbr_async_fifo.sv"] \
        [file join $shell_dir "ip" "uart_bridge" "swo_uart_rx.sv"] \
    ]]
    # clcd — AXI4-Lite front end + clcd_core (the FIFO+8080 FSM the Wave 0-3
    # refactor split out for KVM reuse). clcd.sv instantiates clcd_core, so
    # BOTH files MUST be in the packaged fileset — packaging clcd.sv alone makes
    # synth fail "module 'clcd_core' not found" (validate_bd_design does not
    # catch it: it elaborates structure, not RTL). Multi-file like uart_bridge.
    dict set blocks clcd          [list clcd          [list \
        [file join $shell_dir "ip" "clcd" "clcd.sv"] \
        [file join $shell_dir "ip" "clcd" "clcd_core.sv"] \
    ]]
    # clcd_kvm — CLCD-KVM ownership arbiter (Wave 4). Single SV file, no
    # submodules (like clcd). Its many discrete non-AXI ports (h_*, dut_gpio_*,
    # panel pads, user_npb1, decouple_status, rp_resetn, owner_o) stay scalar —
    # the strip loop below removes any auto-inferred interface except s_axi.
    dict set blocks clcd_kvm      [list clcd_kvm      [list [file join $shell_dir "ip" "clcd_kvm"     "clcd_kvm.sv"]]]

    # dut_egress — the DUT Ethernet RETURN path (DUTEGR @0x44B2_0000). Like
    # uart_bridge, its top `include's its helper (dutegr_cfifo.sv, the
    # commit/rollback dual-clock FIFO), so BOTH files must be in the packaged
    # fileset: packaging the top alone would pass validate_bd_design and then
    # fail in synth with "module 'dutegr_cfifo' not found" (the clcd/clcd_core
    # precedent, and the same trap uart_bridge's entry documents). Listing the
    # helper is also what puts it under the mtime staleness check below -- omit
    # it and an edit to the FIFO alone would silently ship stale RTL.
    dict set blocks dut_egress    [list dut_egress    [list \
        [file join $shell_dir "ip" "dut_egress" "dut_egress.sv"] \
        [file join $shell_dir "ip" "dut_egress" "dutegr_cfifo.sv"] \
    ]]

    # usr_access_rd — USRACC @0x44B3_0000, the fabric's own build identity
    # (docs/VERSIONING_PLAN.md §3.4). Single SV file, no submodules, standard
    # s_axi_* surface -> the default keep-list applies.
    #
    # IT CONTAINS A XILINX PRIMITIVE (USR_ACCESSE2), which is new for this repo's
    # packaged IP. That needs no special handling here -- unisim primitives are
    # resolved by the SYNTHESIS tool, not by packaging, and the component's
    # fileset is just the .sv. What it DOES mean is that this block cannot be
    # simulated against the packaged output without a model; tests/usr_access_rd
    # supplies one (usr_accesse2_model.sv) and elaborates the ip/ source, which
    # is the same file.
    dict set blocks usr_access_rd [list usr_access_rd [list [file join $shell_dir "ip" "usr_access_rd" "usr_access_rd.sv"]]]

    # usd_spi — USD @0x44A4_0000, the SPI-mode master for the USER microSD slot
    # (docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.1; replaces the pad-less
    # axi_quad_spi_0 on that page). Single SV file, no submodules, standard
    # s_axi_* surface -> the default keep-list applies. Its pad ports
    # (usd_clk_o/oe, usd_cmd_o/oe, usd_dat0_i, usd_dat3_o/oe, usd_ncd_i) must
    # stay SCALAR: if -import_files infers a clock interface off `usd_clk_o`,
    # the strip loop below removes it like any other non-s_axi inference.
    dict set blocks usd_spi       [list usd_spi       [list [file join $shell_dir "ip" "usd_spi"      "usd_spi.sv"]]]

    # eth_mac_test_subsystem — the ARCHITECTURE_SPEC §8 virtual-PHY / MAC
    # verification subsystem (SECTION 5 of shell_bd.tcl). NOTE the source tree:
    # fpga/ethernet/ is a SIBLING of fpga/shell/, not a subdir, so its paths are
    # built from [file dirname $shell_dir] -- the proc's $repo_root argument is
    # the IP-XACT OUTPUT repo (fpga/shell/ip_packaged), not the git root.
    #
    # An ASSEMBLY module: it instantiates five separately-compiled blocks, so all
    # five MUST be in the fileset (the clcd/clcd_core precedent above -- packaging
    # the top alone fails ~28 min into synth with [Synth 8-439], and
    # validate_bd_design does NOT catch it). mdio_slave.sv + phy_reg_model.sv are
    # `include'd by mdio_phy_model.sv rather than instantiated; they are listed
    # anyway because (a) Vivado's compile-order analysis marks them
    # <isIncludeFile>true</> by itself (proven by ip_packaged/uart_bridge/), and
    # (b) listing them is what puts them under the staleness check below -- omit
    # them and an edit to mdio_slave.sv alone would silently ship stale RTL.
    #
    # Third element = the per-block bus-interface KEEP-LIST (see the strip loop).
    # Measured by fpga/shell/ip_packaged/pkg_smoke_eth.tcl, not guessed: this
    # block exposes TWO PREFIXED AXI4-Lite surfaces, which -import_files does
    # infer correctly as s_axi_vphy / s_axi_genchk. The four AXI-Stream ports
    # (mgmt_s/mgmt_m/uplink_s/uplink_m) are deliberately NOT kept -- they are
    # stripped to scalars because neither endpoint exists in the BD yet, and a
    # dangling scalar input can be tied off with an xlconstant while a dangling
    # bus interface cannot.
    set eth_dir [file join [file dirname $shell_dir] "ethernet"]
    dict set blocks eth_mac_test_subsystem [list eth_mac_test_subsystem [list \
        [file join $eth_dir "eth_mac_test_subsystem.sv"] \
        [file join $eth_dir "rmii_phy_if"      "rmii_phy_if.sv"] \
        [file join $eth_dir "link_partner_mac" "link_partner_mac.sv"] \
        [file join $eth_dir "bridge"           "eth_bridge_3port.sv"] \
        [file join $eth_dir "gen_checker"      "gen_checker.sv"] \
        [file join $eth_dir "mdio_phy_model"   "mdio_phy_model.sv"] \
        [file join $eth_dir "mdio_phy_model"   "mdio_slave.sv"] \
        [file join $eth_dir "mdio_phy_model"   "phy_reg_model.sv"] \
    ] {s_axi_vphy s_axi_genchk s_axi_vphy_aclk s_axi_vphy_aresetn}]

    if { [catch {current_project} orig_proj] } {
        set orig_proj ""
    }

    dict for {name spec} $blocks {
        set comp_dir [file join $repo_root $name]
        lassign $spec top_module srcs keep_busifs
        # Default keep-list = the AXI4-Lite trio the single-surface CSR blocks
        # expose. A block with prefixed/multiple AXI surfaces supplies its own
        # third dict element. `lassign` leaves a missing element as "", so every
        # pre-existing block keeps its exact previous behaviour -- load-bearing:
        # any change to their packaged output would re-key the shell bitstream.
        if { $keep_busifs eq "" } {
            set keep_busifs {s_axi s_axi_aclk s_axi_aresetn}
        }
        foreach f $srcs {
            if { ![file exists $f] } {
                error "package_csr_ip: missing expected RTL source for $name: $f"
            }
        }

        # STALENESS CHECK (was: skip whenever component.xml existed).
        #
        # The shell synthesizes from ip_packaged/, not from ip/. The old
        # unconditional skip meant an edit to ip/<name>/*.sv was silently
        # DROPPED on rebuild -- you got a new static_id for a bitstream whose
        # RTL had not changed. That very nearly shipped a no-op rebuild of the
        # R4 (ASYNC_REG) + R7 (reset) wave. Fail loudly instead: repackage
        # whenever any source is newer than the packaged component.xml.
        set comp_xml [file join $comp_dir "component.xml"]
        if { !$force && [file exists $comp_xml] } {
            set comp_mtime [file mtime $comp_xml]
            set stale_src ""
            set stale_why ""
            foreach f $srcs {
                if { [file mtime $f] > $comp_mtime } {
                    set stale_src $f
                    set stale_why "is newer than component.xml"
                    break
                }
            }

            # FILE-LIST CHECK — mtime alone is NOT sufficient, and this bit us.
            #
            # An mtime scan cannot see a source ADDED to $srcs whose own mtime
            # PREDATES the last packaging. That is exactly how the clcd_core
            # split shipped: clcd.sv was refactored to instantiate clcd_core and
            # clcd_core.sv was added to clcd's fileset, but clcd_core.sv was
            # older than the already-packaged component.xml -> every source
            # looked "up to date" -> skip -> the packaged IP still contained
            # clcd.sv ALONE. validate_bd_design passes (it does not elaborate
            # the packaged RTL), so the dangling submodule only surfaced ~28
            # minutes into the shell BD synth as
            #   ERROR: [Synth 8-439] module 'clcd_core' not found
            # Repackage whenever a listed source is absent from the packaged
            # fileset, regardless of mtime.
            if { $stale_src eq "" } {
                foreach f $srcs {
                    if { ![file exists [file join $comp_dir "src" [file tail $f]]] } {
                        set stale_src $f
                        set stale_why "is absent from the packaged fileset"
                        break
                    }
                }
            }

            if { $stale_src eq "" } {
                puts "INFO: package_csr_ip — $name up to date at $comp_dir (skip)"
                continue
            }
            puts "INFO: package_csr_ip — $name is STALE ([file tail $stale_src] $stale_why)\
                  -> repackaging"
        }

        puts "INFO: package_csr_ip — packaging $name (top=$top_module) -> $comp_dir"
        set pkg_proj_dir [file join $build_root "pkg_${name}_proj"]
        file delete -force $pkg_proj_dir
        create_project "pkg_${name}" $pkg_proj_dir -part $part_name -force
        add_files -norecurse $srcs
        set_property file_type SystemVerilog [get_files -of_objects [get_filesets sources_1]]
        set_property top $top_module [current_fileset]
        update_compile_order -fileset sources_1

        file delete -force $comp_dir
        ipx::package_project -root_dir $comp_dir -vendor soclabs.org -library user \
            -taxonomy /UserIP -import_files -set_current true
        set core [ipx::current_core]
        set_property display_name $name $core
        set_property description "soclabs mps3-nanosoc-platform shell block: $name (real RTL, not a stub)" $core

        # Auto-inference (see this file's header) also fires on OTHER
        # standard naming conventions beyond AXI4-Lite — confirmed live:
        # uart_bridge's `uart_tx_tdata_i/uart_tx_tvalid_i/uart_tx_tready_o`
        # etc. (its four scalar console/SWO AXI-Stream-shaped ports) get
        # auto-grouped into real `axis_rtl` bus interfaces
        # (uart_tx_i/uart_rx_o/uart1_tx_i/uart1_rx_o) even though this
        # design deliberately wires them as plain scalars straight to the RP
        # partition-pin boundary (partition-pins.md: "every signal crossing
        # the RP boundary is a slow scalar or low-rate AXI-Stream", wired at
        # the signal level, not as a Vivado bus interface). Left in place,
        # those phantom interfaces have no ASSOCIATED_BUSIF clock and
        # validate_bd_design flags each with CRITICAL WARNING [BD 41-967]
        # "not associated to any clock pin" once the module is instantiated
        # and its scalar ports individually connected. Strip every inferred
        # bus interface except the real AXI4-Lite one so these blocks expose
        # plain scalar ports for anything shell_bd.tcl wires by hand.
        foreach bi [ipx::get_bus_interfaces -of_objects $core] {
            set bi_name [get_property NAME $bi]
            if { $bi_name ni $keep_busifs } {
                puts "INFO: package_csr_ip — $name: removing auto-inferred bus interface '$bi_name' (keeping it scalar)"
                ipx::remove_bus_interface $bi_name $core
            }
        }

        # --- eth_mac_test_subsystem clock/reset metadata --------------------
        # MEASURED by pkg_smoke_eth.tcl (2026-07-24), not assumed. -import_files
        # inferred both AXI-Lite surfaces correctly but left the clocking
        # INCOMPLETE:
        #   refclk_i            NOT inferred as a clock interface at all
        #   rst_i               NOT inferred as a reset interface at all
        #   s_axi_vphy_aresetn  ASSOCIATED_BUSIF=<none>
        # gen_checker FUSES its control and datapath clocks onto refclk_i, so
        # s_axi_genchk has no aclk/aresetn port of its own. With refclk_i not
        # even being a clock interface, s_axi_genchk would be associated to
        # NOTHING and validate_bd_design raises [BD 41-967] "AXI interface not
        # associated to any clock pin" -- the exact failure this file's header
        # documents for the phantom-interface case.
        if { $name eq "eth_mac_test_subsystem" } {
            ipx::infer_bus_interface refclk_i xilinx.com:signal:clock_rtl:1.0 $core
            ipx::infer_bus_interface rst_i    xilinx.com:signal:reset_rtl:1.0 $core

            # Helper: add-or-update, since add_bus_parameter errors if present.
            proc _pkg_set_busparam { core busif param value } {
                set bi [ipx::get_bus_interfaces $busif -of_objects $core]
                if { [ipx::get_bus_parameters $param -of_objects $bi -quiet] eq "" } {
                    ipx::add_bus_parameter $param $bi
                }
                set_property value $value \
                    [ipx::get_bus_parameters $param -of_objects $bi]
            }

            # rst_i is ACTIVE-HIGH (eth_mac_test_subsystem.sv declares it so);
            # Vivado's reset inference defaults to ACTIVE_LOW for a name without
            # an _n/resetn suffix, so state it explicitly or the BD flags a
            # polarity mismatch against proc_sys_reset's peripheral_reset.
            _pkg_set_busparam $core rst_i POLARITY ACTIVE_HIGH

            # Point the datapath clock/reset at the GENCHK surface.
            _pkg_set_busparam $core refclk_i ASSOCIATED_BUSIF s_axi_genchk
            _pkg_set_busparam $core refclk_i ASSOCIATED_RESET rst_i

            # VPHY's reset: link it through the CLOCK's ASSOCIATED_RESET, NOT by
            # giving the reset its own ASSOCIATED_BUSIF. Setting the latter looks
            # like the obvious fix for the smoke's "ASSOCIATED_BUSIF=<none>" but
            # is WRONG: Vivado then counts the reset pin as a second clock-pin
            # for that bus and raises CRITICAL WARNING [BD 41-1732] "associated
            # with multiple clock-pins". Caught by validate_bd_design, 2026-07-24.
            _pkg_set_busparam $core s_axi_vphy_aclk ASSOCIATED_RESET s_axi_vphy_aresetn

            puts "INFO: package_csr_ip — $name: clock/reset metadata completed (refclk_i+rst_i inferred, s_axi_genchk associated, rst_i ACTIVE_HIGH)"
        }

        ipx::create_xgui_files $core
        ipx::update_checksums $core
        ipx::save_core $core

        close_project
        if { $orig_proj ne "" } {
            current_project $orig_proj
        }
    }
}
