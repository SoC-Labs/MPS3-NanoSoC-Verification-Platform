###############################################################################
# create_platform.tcl — xsct (Vitis 2024.1 classic scripting API) headless
# build: shell_harness.xsa -> platform + standalone BSP + lwip220 -> app ->
# MicroBlaze ELF. This is the A3 target-build entry point; see
# firmware/platform/README.md for the narrative + the BSP/lwIP setting
# rationale table (keep the two in sync), and BUILD_RESULT.txt for the last
# run's evidence.
#
# Usage (from the repo root; settings64.sh NOT required for xsct itself):
#   /apps/Xilinx/Vitis/2024.1/bin/xsct -nodisp firmware/platform/create_platform.tcl \
#       build/shell_proj_256k/shell_harness.xsa build/vitis_fw
#
# This script builds the PLATFORM + BSP only (display-free hsi commands).
# The ELF itself comes from firmware/platform/Makefile afterwards — the
# LMB_KB / BLOB choices live there (see the trailer comment for why the
# xsct app commands are unusable headless).
#
# The workspace (<out_ws>) is DELETED and rebuilt every run (CI-style,
# deterministic); it lives under the repo's gitignored build/ tree.
###############################################################################

if { $argc < 2 } {
    puts "usage: xsct -nodisp create_platform.tcl <shell_harness.xsa> <out_ws>"
    exit 1
}
set xsa_path  [file normalize [lindex $argv 0]]
set ws        [file normalize [lindex $argv 1]]

set script_dir [file dirname [file normalize [info script]]]
set fw_dir     [file normalize "$script_dir/.."]

set plat_name   "shell_platform"
set domain_name "standalone_domain"
set proc_name   "microblaze_0"    ;# BD cell name (fpga/shell/bd/shell_bd.tcl)
set console     "axi_uartlite_0"  ;# MB console UARTLite instance (BD cell)

if { ![file isfile $xsa_path] } {
    puts "ERROR: XSA not found: $xsa_path"
    puts "       (gitignored build artifact — regenerate per fpga/shell/build_results_*/RESULT.txt)"
    exit 1
}

# Fresh workspace every run.
file delete -force $ws
file mkdir $ws
setws $ws

###############################################################################
# 1. Platform + standalone domain
###############################################################################
puts ">>> platform create -hw $xsa_path"
platform create -name $plat_name -hw $xsa_path -out $ws
domain create -name $domain_name -proc $proc_name -os standalone
platform active $plat_name

###############################################################################
# 2. BSP configuration
#
# lwip220 (lwIP 2.2.0), RAW API, poll-model, sized for the 128 KiB LMB.
# DEVIATION from the tasking's "lwip213": 2024.1's classic-flow catalog
# only PUBLISHES lwip220 (lwip213_v1_1 sits on disk but `bsp setlib
# lwip213` is refused — "Library not available in the Repository",
# confirmed live 2026-07-07; available list = lwip220 + xil* only). The
# parameter set below is name-identical between the two .mld files, and
# nothing in this firmware uses a 2.2-only feature.
#
# every non-default is listed here with its budget rationale (mirrored in
# README.md's table; the generated lwipopts.h is the artifact to diff):
#   api_mode RAW_API        no RTOS/sockets (firmware/README.md build model)
#   dhcp off (default)      static 192.168.10.101 (net-protocol.md; D8=DHCP option)
#   mem_size 16384          lwIP heap: tcp_write(COPY) staging; default 128K
#                           would alone consume the whole LMB
#   memp_n_pbuf 16          (was 8) RAM-pbuf headers — raised with tcp_wnd so a
#                           16 KiB in-flight window has enough pbuf headers
#   memp_n_tcp_pcb 12       (default 32) 7 services x 1 client + margin
#   memp_n_tcp_pcb_listen 8 (default 8, explicit) 7 listen ports + 1
#   memp_n_tcp_seg 48       (default 256) must be >= the Xilinx ports hardwired
#                           TCP_SND_QUEUELEN = 16*TCP_SND_BUF/TCP_MSS = 44
#                           (lwip_sanity_check hit live; ~28 B/seg)
#   memp_n_udp_pcb 4        TFTP :69 + ephemeral TIDs
#   pbuf_pool_size 16       (was 8) RX pool: 16 x 1700 ~ 27 KiB. Raised to hold a
#                           16 KiB TCP_WND of in-flight RX (~10 full-MSS pbufs) so
#                           the bigger window doesn't drop frames under burst.
#   tcp_snd_buf 4096        (default 8192) per-conn send buffer (shell only ever
#                           sends tiny segments: the 1-byte windowed grant + the
#                           swap response — no bulk TX, so 4096 is ample)
#   tcp_wnd 16384           (was 2048) per-conn RX window. THROUGHPUT fix for
#                           over-the-wire reconfig: the windowed 1-byte grant
#                           egresses only after an RTO (~1-4 s) on this LAN9220
#                           TX-while-RX path, so cost is ~1 RTO PER WINDOW. An 8x
#                           bigger window (= ACK_WINDOW) means 8x fewer windows =
#                           8x fewer grant-RTOs. Keep ACK_WINDOW == tcp_wnd (the
#                           firmware/platform Makefile default) so a full app
#                           window fits the TCP window with no mid-window reopen
#                           (the ACK_WINDOW>TCP_WND mid-window-reopen stall).
#   tcp_queue_ooseq 0       drop out-of-order segs instead of buffering (low mem)
#   ip_reassembly/ip_frag 0 no fragmented traffic in the contract; saves the
#                           reass buffer + timer
#   no_sys_no_timers true   (default, explicit) manual tcp_tmr()/etharp_tmr()
#                           from the superloop (net_if_lwip.c mps3_net_lwip_tmr)
#   lwip_tcp_keepalive true  (was false) REAP A DEAD PEER'S SESSION. A client
#                           that connects to 6910/6900 and dies WITHOUT a FIN
#                           (killed process / yanked cable) BEFORE or BETWEEN
#                           swaps leaves the pcb ESTABLISHED forever; with
#                           config_agent single-session this refuses every
#                           later client until a JTAG reload (see
#                           docs/QSPI_CLEARING_CACHE_HW_FINDINGS.md "dead client
#                           wedges the shell"). Turning this ON makes lwIP HONOR
#                           the per-pcb keep_idle/keep_intvl/keep_cnt that
#                           net_if_lwip.c's accept path sets (otherwise the
#                           fixed ~2 h TCP_MAXIDLE applies and the fields are
#                           compiled out of struct tcp_pcb). NO extra timer is
#                           needed: keepalive lives in tcp_slowtmr(), already
#                           driven by the manual tcp_tmr() above. HEADER-ONLY
#                           EDIT IS SILENTLY IGNORED — liblwip*.a must be
#                           REBUILT (platform generate) for this to take effect.
###############################################################################
# --- local patched lwip220 sw-repo -----------------------------------------
# BOTH lwip213 and lwip220 hard-fail their DRC when no *Xilinx* EMAC IP is
# addressable ("lwIP requires atleast one EMAC ... connected to the
# interrupt controller", confirmed live 2026-07-07) — but this design's MAC
# is a LAN9220 behind axi_emc with its own bare-metal driver
# (firmware/smsc911x) and its own netif glue (net_if_lwip.c); the lwIP CORE
# builds fine without any Xilinx adapter (the DRC's own zero-EMAC branch
# already writes a valid Makefile.config before erroring). So: copy the
# library into a build-local sw-repo and soften that one `error` to a
# warning. The tool install is never modified (read-only vendor collateral);
# the patched copy is regenerated into the gitignored workspace every run.
set lwip_upstream "/apps/Xilinx/Vitis/2024.1/data/embeddedsw/ThirdParty/sw_services/lwip220_v1_0"
set local_repo    "$ws/local_sw_repo"
file mkdir "$local_repo/ThirdParty/sw_services"
file copy -force $lwip_upstream "$local_repo/ThirdParty/sw_services/lwip220_v1_0"

set drc_tcl "$local_repo/ThirdParty/sw_services/lwip220_v1_0/data/lwip220.tcl"
set f [open $drc_tcl r]; set drc [read $f]; close $f
set n1 [regsub {error "ERROR: No Ethernet MAC cores} $drc \
            {puts "WARNING (local-repo patch, fw/platform/create_platform.tcl): No Xilinx EMAC cores} drc]
set n2 [regsub {interrupt pin connected to the interrupt controller\.\\n" "" "MDT_ERROR"} $drc \
            {interrupt pin connected to the interrupt controller. Proceeding: this design uses a non-Xilinx MAC (LAN9220/axi_emc) with its own netif driver.\\n"} drc]
if { $n1 != 1 || $n2 != 1 } {
    puts "ERROR: lwip220 DRC patch did not apply (n1=$n1 n2=$n2) — upstream lwip220.tcl changed; re-derive the patch"
    exit 1
}
set f [open $drc_tcl w]; puts -nonewline $f $drc; close $f

repo -set $local_repo
repo -scan
# ----------------------------------------------------------------------------

puts ">>> BSP: lwip220 + standalone config"
bsp setlib -name lwip220

bsp config api_mode              "RAW_API"
bsp config mem_size              "16384"
bsp config memp_n_pbuf           "16"
bsp config memp_n_tcp_pcb        "12"
bsp config memp_n_tcp_pcb_listen "8"
bsp config memp_n_tcp_seg        "48"
bsp config memp_n_udp_pcb        "4"
bsp config pbuf_pool_size        "16"
bsp config tcp_snd_buf           "4096"
bsp config tcp_wnd               "16384"
bsp config tcp_queue_ooseq       "0"
bsp config ip_reassembly         "0"
bsp config ip_frag               "0"
bsp config no_sys_no_timers     "true"
bsp config lwip_dhcp             "false"
bsp config lwip_stats            "false"
bsp config lwip_tcp_keepalive    "true"

# MB console on the shell's physical UARTLite (xil_printf boot banner /
# diagnostics — NOT the DUT consoles, those relay via uart_over_eth).
bsp config stdin  $console
bsp config stdout $console

puts ">>> platform generate"
platform generate

###############################################################################
# 3. Done — the app itself is built by firmware/platform/Makefile (mb-gcc
#    directly against this BSP). The classic `app create`/`app build` xsct
#    commands are NOT used: they talk to an Eclipse "XSDx" backend that
#    needs an X display even under xsct (confirmed live 2026-07-07 on a
#    headless host: "Unable to init server: Could not connect"), while the
#    hsi-side platform/BSP commands above are display-free. See
#    README.md "Build (repro)" and the Makefile header.
###############################################################################
puts "BSP ready: $ws/shell_platform/microblaze_0/standalone_domain/bsp/microblaze_0"
puts "Next: make -C firmware/platform elf WS=$ws   (defaults: LMB_KB=256 BLOB=real; see Makefile header)"
puts "DONE"
