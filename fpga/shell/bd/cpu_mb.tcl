###-----------------------------------------------------------------------------
### fpga/shell/bd/cpu_mb.tcl -- the CLASSIC MicroBlaze half of the shell's CPU
### seam (SHELL_CPU=mb, the default). The bare-metal coordinator subsystem.
###
### THIS IS A MOVE, NOT A REWRITE. Every line below was cut verbatim out of
### shell_bd.tcl (pre-seam, eafe787: SECTION 2 lines 371-499, the INTERRUPT net
### at 511, the M_AXI_DP hookup at 1567 and the LMB address block at 1688-1703)
### and is executed at the SAME point of create_root_design it used to occupy,
### in the same order, in the same scope. That is what makes the SHELL_CPU=mb BD
### IDENTICAL to the pre-seam one -- not merely equivalent: same cells, same
### CONFIG, same nets, same NET NAMES (Vivado names a net after its first
### connection, so reordering would rename them). tests/shell_cpu_seam proves it
### by dumping both BDs through fpga/shell/tools/bd_dump.tcl and diffing.
### The bare-metal static is the fielded fallback for the whole Linux
### programme (LINUX_HARNESS_PLAN_2026-09-23.md DL2/DL3); treat an edit here as
### an edit to a fielded shell -- it re-keys every overlay.
###
### HOW IT IS CALLED. shell_bd.tcl sets `cpu_stage` and `source`s this file once
### per stage, INSIDE create_root_design, so every variable of that proc
### ($shell_clk, $shell_mb_reset, $axi_interconnect_0, ...) is in scope and every
### variable set here ($microblaze_0, $local_ram, ...) is visible to the later
### stages. The stages and where shell_bd.tcl runs them:
###   core    SECTION 2 -- the CPU, its LMB memory and its debug module
###   intc    right after the (shared) axi_intc_0/xlconcat_intr are created
###   timer   right after the (shared) axi_timer_0/axi_uartlite_0 are created
###   axi     SECTION 6, between the interconnect's clocks and its master map
###   addr    SECTION 6, after the contract-window + TOUCH address lines
###   finish  last, before the caller's validate_bd_design
### A stage this CPU has nothing to do in is an empty branch, never an absent
### one: an unknown stage is an error, so a stage added to shell_bd.tcl cannot be
### silently skipped by one CPU file.
###
### The sibling is cpu_mbv.tcl (SHELL_CPU=mbv, MicroBlaze V + DDR4, Vivado
### 2026.1). What the two must agree on -- the cell names the rest of the BD
### wires to -- is listed in docs/planning/linux_lanes/SHELL_CONTRACT.md §1.
###-----------------------------------------------------------------------------

switch -exact -- $cpu_stage {

core {
        ###################################################################
        # SECTION 2 — MICROBLAZE BARE-METAL COORDINATOR SUBSYSTEM
        # (spec §4.1/§4.3 — bare-metal, NOT MicroBlaze-Linux; runs
        # config_agent/xvc_server/jtag_server/uart_over_eth/coordinator/clkrst/
        # overlay_store firmware, A3).
        ###################################################################

        set microblaze_0 [create_bd_cell -type ip -vlnv xilinx.com:ip:microblaze:11.0 microblaze_0]
        set_property -dict [list \
            CONFIG.C_USE_MMU {0} \
            CONFIG.C_USE_BARREL {1} \
            CONFIG.C_USE_HW_MUL {1} \
            CONFIG.C_USE_DIV {1} \
            CONFIG.C_USE_FPU {0} \
            CONFIG.C_DEBUG_ENABLED {1} \
            CONFIG.C_D_AXI {1} \
            CONFIG.C_D_LMB {1} \
            CONFIG.C_I_LMB {1} \
            CONFIG.C_ICACHE_ALWAYS_USED {0} \
            CONFIG.C_DCACHE_ALWAYS_USED {0} \
        ] $microblaze_0
        # MMU-less bare-metal per spec §1 non-goal ("Do not attempt a
        # MicroBlaze-Linux/PYNQ port... the MicroBlaze here is a bare-metal
        # agent"). C_D_AXI enables M_AXI_DP, the peripheral-facing master used
        # below for the entire shell-regmap.md fan-out.

        # -- Local memory: shared ILMB/DLMB BRAM, 256 KiB (spec: "local BRAM
        #    ~64-128KB" — doubled to 256 KiB for the lwIP+firmware image, see
        #    below) — standard MicroBlaze MMU-less BSP pattern. --
        set ilmb_v10 [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_v10:3.0 ilmb_v10]
        set dlmb_v10 [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_v10:3.0 dlmb_v10]
        set ilmb_bram_if_cntlr [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_bram_if_cntlr:4.0 ilmb_bram_if_cntlr]
        set dlmb_bram_if_cntlr [create_bd_cell -type ip -vlnv xilinx.com:ip:lmb_bram_if_cntlr:4.0 dlmb_bram_if_cntlr]
        set_property CONFIG.C_ECC {0} $ilmb_bram_if_cntlr
        set_property CONFIG.C_ECC {0} $dlmb_bram_if_cntlr
        set local_ram [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen:8.4 local_ram]
        set_property -dict [list \
            CONFIG.Memory_Type {True_Dual_Port_RAM} \
            CONFIG.Use_Byte_Write_Enable {true} \
            CONFIG.Byte_Size {9} \
            CONFIG.Write_Width_A {32} \
            CONFIG.Write_Depth_A {262144} \
            CONFIG.Write_Width_B {32} \
            CONFIG.Enable_B {Use_ENB_Pin} \
            CONFIG.Register_PortA_Output_of_Memory_Primitives {false} \
            CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
            CONFIG.Use_RSTA_Pin {true} \
            CONFIG.Use_RSTB_Pin {true} \
        ] $local_ram
        # 262144 x 32b = 1 MiB (doubled again from 131072=512 KiB; prior steps
        # 65536=256 KiB, 32768=128 KiB).
        #
        # Why 1 MiB. The swap needs TWO clearing buffers that COEXIST: the incoming
        # clearing sits in config_agent's receive slot while SWAP_STREAM_CLEARING
        # replays the outgoing one from swap_fsm's arena. So the firmware image is
        # `core + 2X` where X is the clearing size, core ~= 226,652 B. nanosoc's
        # clearing is 155,864 B (it grew from 117,684 B when firmware was baked into
        # the RM), and 226,652 + 2*155,864 = 538,380 > the 512 KiB LMB's 524,080
        # usable -- measured: "region `local_lmb' overflowed by 14152 bytes".
        #
        # At 512 KiB, X caps at 148,714 B, so nanosoc -- the DoD-critical RM -- could
        # not hold its own clearing and its swap-AWAY failed closed at
        # SWAP_STREAM_CLEARING; its clearing spilled to the QSPI flash, which has
        # never worked on silicon and which D16 (ARCHITECTURE_SPEC.md §15) says to
        # keep idle. 1 MiB lifts the cap to ~410 KiB and makes every RM QSPI-free.
        #
        # KU115 BRAM headroom is ample: 256 KiB used 64 RAMB36E2 (2.96% of 2160);
        # 512 KiB ~= 128 (~5.9%); 1 MiB ~= 256 (~11.9%), still leaving >1900 tiles
        # for the DFX RP + any ILA.
        #
        # *** FIRMWARE LOCKSTEP — the shell/firmware pair must move together ***
        #   - firmware/platform/Makefile:  LMB_KB 512 -> 1024
        #   - firmware/platform/lscript.ld.in: @LMB_LENGTH@ = LMB_KB*1024 - 0x50
        #     auto-tracks LMB_KB, so the linker length follows — BUT SEE THE DIAG
        #     MAILBOX WARNING BELOW.
        #   *** DIAG MAILBOX MOVES *** the fixed JTAG diag mailbox is TOP-anchored
        #   (lscript.ld.in `.mps3_diag (0x50 + @LMB_LENGTH@ - 0x80)`): 256 KiB ->
        #   0x3FF80, 512 KiB -> 0x7FF80, 1 MiB -> 0xFFF80. Any JTAG tooling that
        #   reads a HARD-CODED address (magic 0xD1A6C0DE) MUST be repointed.
        #   scripts/mps3_diag.tcl and scripts/harness_gates/tier3_csr_liveness.tcl
        #   scan a CANDIDATES list by magic; 0x000FFF80 is now in both, and
        #   scripts/harness_gates/check_diag_mailbox_parity.py asserts the list
        #   still contains the address implied by the CURRENT LMB_KB.
        # Drop Write_Depth_A back to 131072/65536/32768 ONLY if the firmware image
        # shrinks and BRAM pressure becomes tight — but then the ranges below AND
        # firmware LMB_KB AND the diag address must move in lockstep, and
        # scripts/harness_gates/check_clearing_fits.py will go red if a clearing no
        # longer fits.

        set mdm_1 [create_bd_cell -type ip -vlnv xilinx.com:ip:mdm:3.2 mdm_1]

        connect_bd_intf_net [get_bd_intf_pins microblaze_0/ILMB] [get_bd_intf_pins $ilmb_v10/LMB_M]
        connect_bd_intf_net [get_bd_intf_pins microblaze_0/DLMB] [get_bd_intf_pins $dlmb_v10/LMB_M]
        connect_bd_intf_net [get_bd_intf_pins $ilmb_v10/LMB_Sl_0] [get_bd_intf_pins $ilmb_bram_if_cntlr/SLMB]
        connect_bd_intf_net [get_bd_intf_pins $dlmb_v10/LMB_Sl_0] [get_bd_intf_pins $dlmb_bram_if_cntlr/SLMB]
        connect_bd_intf_net [get_bd_intf_pins $ilmb_bram_if_cntlr/BRAM_PORT] [get_bd_intf_pins $local_ram/BRAM_PORTA]
        connect_bd_intf_net [get_bd_intf_pins $dlmb_bram_if_cntlr/BRAM_PORT] [get_bd_intf_pins $local_ram/BRAM_PORTB]
        connect_bd_intf_net [get_bd_intf_pins microblaze_0/DEBUG] [get_bd_intf_pins $mdm_1/MBDEBUG_0]

        foreach p [list $ilmb_v10 $dlmb_v10 $ilmb_bram_if_cntlr $dlmb_bram_if_cntlr $mdm_1] {
            connect_bd_net $shell_clk [get_bd_pins $p/LMB_Clk] -quiet
            connect_bd_net $shell_clk [get_bd_pins $p/Clk] -quiet
            # CONFIRMED LIVE: lmb_v10/lmb_bram_if_cntlr's SYS_Rst/LMB_Rst pins are
            # ACTIVE_HIGH (standard MicroBlaze MMU-less LMB reset convention) —
            # feeding them proc_sys_reset's ACTIVE_LOW `peripheral_aresetn`
            # (shell_aresetn) is a real polarity bug, not just a pin-name miss;
            # validate_bd_design correctly ERRORs on it ([BD 41-238] POLARITY
            # mismatch). The fix is the same signal already used for
            # microblaze_0/Reset below: proc_sys_reset's dedicated ACTIVE_HIGH
            # `mb_reset` output (shell_mb_reset) — this is in fact the standard
            # Xilinx MicroBlaze-MMU-less BSP template pairing (mb_reset feeds
            # MicroBlaze + both LMBs; peripheral_aresetn/shell_aresetn is for the
            # AXI-Lite peripheral domain only).
            connect_bd_net $shell_mb_reset [get_bd_pins $p/SYS_Rst] -quiet
            connect_bd_net $shell_mb_reset [get_bd_pins $p/LMB_Rst] -quiet
        }
        connect_bd_net $shell_clk [get_bd_pins microblaze_0/Clk]
        connect_bd_net $shell_mb_reset [get_bd_pins microblaze_0/Reset]
        connect_bd_net $shell_clk [get_bd_pins $local_ram/clka] -quiet
        connect_bd_net $shell_clk [get_bd_pins $local_ram/clkb] -quiet
        # NOTE(integrator): the foreach loop above deliberately uses `-quiet`
        # because lmb_v10/lmb_bram_if_cntlr/mdm pin names vary slightly by exact
        # IP minor version (LMB_Clk vs Clk, SYS_Rst vs LMB_Rst) — harmless misses
        # are swallowed rather than erroring the whole script; CONFIRMED LIVE
        # that for lmb_v10/lmb_bram_if_cntlr both LMB_Clk (not bare Clk) and
        # SYS_Rst + LMB_Rst (both, not either/or) are real and get connected;
        # mdm_1 has none of these four (it needs no external clock/reset at all
        # — its Dbg_Clk_0/Debug_SYS_Rst etc. are internally-driven OUTPUTS that
        # ride the MBDEBUG_0 bus into microblaze_0, confirmed live).
}

intc {
    # The INTC's scalar irq drives the classic MicroBlaze's INTERRUPT pin.
        connect_bd_net [get_bd_pins axi_intc_0/irq] [get_bd_pins microblaze_0/INTERRUPT]
}

timer {
    # Nothing CPU-specific: the bare-metal firmware polls the timer and takes
    # every axi_timer_0 / axi_uartlite_0 default the shared code leaves.
}

axi {
        connect_bd_intf_net [get_bd_intf_pins microblaze_0/M_AXI_DP] [get_bd_intf_pins $axi_interconnect_0/S00_AXI]
}

addr {
        # -- MicroBlaze local program/data memory (SECTION 2's ilmb/dlmb BRAM
        #    controllers) — standard MMU-less MicroBlaze BSP mapping, both LMBs
        #    at 0x0 (separate ILMB/Instruction and DLMB/Data address spaces, so
        #    no overlap). CONFIRMED LIVE: validate_bd_design flags these two
        #    segments as CRITICAL WARNING "not assigned into address space" if
        #    omitted — this was a real gap in the original script (SECTION 2's
        #    local_ram cells were wired for clock/reset/BRAM_PORT but never
        #    given an address), not just a naming drift. 1 MiB (0x100000) matches
        #    local_ram's Write_Depth_A=262144 x 32b (SECTION 2). Both LMB
        #    controllers map the SAME physical BRAM (ilmb->BRAM_PORTA,
        #    dlmb->BRAM_PORTB, True_Dual_Port_RAM) so ILMB (Instruction) and DLMB
        #    (Data) see identical 1 MiB windows at 0x0 — must move together AND
        #    stay in lockstep with local_ram's depth + firmware LMB_KB=1024 (and
        #    the diag-mailbox address move, SECTION 2 note).
        assign_bd_address -offset 0x00000000 -range 1M [get_bd_addr_segs {ilmb_bram_if_cntlr/SLMB/Mem}]
        assign_bd_address -offset 0x00000000 -range 1M [get_bd_addr_segs {dlmb_bram_if_cntlr/SLMB/Mem}]
}

finish {
    # Nothing: every classic-MicroBlaze wire is made by the stages above.
}

default {
    error "cpu_mb.tcl: unknown cpu_stage '$cpu_stage' -- shell_bd.tcl added a seam point this CPU file does not handle"
}

}
