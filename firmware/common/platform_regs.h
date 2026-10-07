/*
 * platform_regs.h — shared AXI4-Lite register contract for the MicroBlaze
 * shell coordinator firmware (A3).
 *
 * SOURCE OF TRUTH: docs/contracts/shell-regmap.md (v0.1, 2026-07-04). v0.1
 * folds in the blocks the Phase-0 spike found missing (OPEN_ISSUES.md
 * I5-I11: SWDBB/DBGBR/UARTBR/GPIO + real TELEM/GENCHK field tables) and the
 * DFXCTL.RM_ID/RM_STATUS RM-load-verify registers (I8). This header is the
 * "shared contract header" called out in the A3 tasking: every other
 * firmware/ module includes this instead of open-coding addresses. If a
 * base or bitfield changes, change it here first.
 *
 * Owner RTL is under fpga/shell/ip/ (A1). Widening a register within its block is
 * a firmware-local change; MOVING A BLOCK BASE IS AN A6 CHANGE (regmap v0.1
 * line 5).
 *
 * NOT YET COMPILABLE AGAINST A REAL BSP: once Vitis generates the platform
 * BSP from the A1 block-design XSA, cross-check every offset below against
 * xparameters.h / the IP's generated _hw.h and fix any drift. Several blocks
 * below (HWICAP) reuse *standard Xilinx IP* register
 * layouts reproduced here from memory/PG134/PG153 conventions for shape —
 * treat that block's offsets as illustrative until Vitis emits the
 * authoritative xhwicap_l.h for this BSP.
 *
 * HAL split for host-testability (firmware/test/): the four accessors below
 * (mps3_reg_{read,write,set_bits,clr_bits}32) are declared here with ONE
 * signature but TWO bodies, selected at compile time:
 *   - default (MicroBlaze / any real target build): `static inline`,
 *     straight `volatile uint32_t *` MMIO -- byte-for-byte what this file
 *     had before, unchanged semantics.
 *   - `-DMPS3_HAL_MOCK` (firmware/test/'s Makefile): plain extern function
 *     declarations, DEFINED by firmware/test/mock_regs.c against an
 *     in-memory register file the test can seed/inspect. Every other
 *     firmware/ .c file calls the exact same four functions either way --
 *     this is the "thin HAL shim" that lets swap_fsm.c/config_agent.c/
 *     overlay_store.c's real control-flow run host-side against mock
 *     register memory without any #ifdef in those files themselves.
 *   - `-DMPS3_HAL_UIO` (src/linux_harness/sw/harnessd/, the MicroBlaze V Linux
 *     harness): the SAME extern declarations as the mock, DEFINED by
 *     harnessd/hal_uio.c against the /dev/uioN windows the kernel maps for
 *     every harnessd-owned shell block. The base -> mapping lookup is by
 *     PHYSICAL base, so every MPS3_*_BASE below is used unchanged; an
 *     unmapped base is a loud fatal error, never a silent 0. See
 *     docs/planning/linux_lanes/HARNESSD_CONTRACT.md. The byte-packing and
 *     every other target default below follow the TARGET branch (UIO is real
 *     hardware), because they test MPS3_HAL_MOCK alone.
 */
#ifndef MPS3_PLATFORM_REGS_H
#define MPS3_PLATFORM_REGS_H

#include <stdint.h>
#include <string.h>  /* memcpy — the host-mock native branch of mps3_hwicap_pack_word() */

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------
 * Generic AXI4-Lite accessors — see the HAL-split note above.
 * ------------------------------------------------------------------------ */
#if defined(MPS3_HAL_MOCK) || defined(MPS3_HAL_UIO)

uint32_t mps3_reg_read32(uintptr_t base, uintptr_t off);
void     mps3_reg_write32(uintptr_t base, uintptr_t off, uint32_t val);
void     mps3_reg_set_bits32(uintptr_t base, uintptr_t off, uint32_t mask);
void     mps3_reg_clr_bits32(uintptr_t base, uintptr_t off, uint32_t mask);

#else /* real hardware target */

#define MPS3_REG32(base, off) \
    (*(volatile uint32_t *)((uintptr_t)(base) + (uintptr_t)(off)))

static inline uint32_t mps3_reg_read32(uintptr_t base, uintptr_t off)
{
    return MPS3_REG32(base, off);
}

static inline void mps3_reg_write32(uintptr_t base, uintptr_t off, uint32_t val)
{
    MPS3_REG32(base, off) = val;
}

static inline void mps3_reg_set_bits32(uintptr_t base, uintptr_t off, uint32_t mask)
{
    MPS3_REG32(base, off) = MPS3_REG32(base, off) | mask;
}

static inline void mps3_reg_clr_bits32(uintptr_t base, uintptr_t off, uint32_t mask)
{
    MPS3_REG32(base, off) = MPS3_REG32(base, off) & ~mask;
}

#endif /* MPS3_HAL_MOCK || MPS3_HAL_UIO */

/* ------------------------------------------------------------------------
 * THE REGISTER MAP BELOW IS GENERATED. Bases come from the shell block
 * design's assign_bd_address lines; register offsets come from the owning
 * CSR RTL's decode (or, for the four Xilinx blocks, from their product
 * guides, which is stated per block). Re-run `python3 tools/gen_regmap.py`
 * after any BD or CSR-RTL change; `make check-ci` fails if this file is
 * stale (scripts/harness_gates/check_generated_fresh.py).
 *
 * Editing a base or an offset HERE cannot change the hardware and will be
 * overwritten. Edit the BD or the RTL. Bit-field defines are NOT generated
 * -- they live outside the fences, beside the prose that explains them.
 * ------------------------------------------------------------------------ */
/* BEGIN GENERATED[regmap-bases] — gen_regmap.py — DO NOT EDIT BY HAND */
/* Block base addresses. Each is the -offset of an assign_bd_address line
 * in the shell block design; nothing here can claim a block the BD does not
 * instantiate, or omit one it does. */
#define MPS3_CLKRST_BASE    0x44A00000u  /* 64K, dut_clkrst_0 @ fpga/shell/bd/shell_bd.tcl:1596 */
#define MPS3_DFXCTL_BASE    0x44A10000u  /* 64K, dfx_ctl_0 @ fpga/shell/bd/shell_bd.tcl:1597 */
#define MPS3_HWICAP_BASE    0x44A20000u  /* 64K, axi_hwicap_0 @ fpga/shell/bd/shell_bd.tcl:1598 */
#define MPS3_VPHY_BASE      0x44A30000u  /* 4K, eth_mac_test_subsystem_0 @ fpga/shell/bd/shell_bd.tcl:1609 */
#define MPS3_USD_BASE       0x44A40000u  /* 64K, usd_spi_0 @ fpga/shell/bd/shell_bd.tcl:1599 */
#define MPS3_TELEM_BASE     0x44A50000u  /* 64K, telem_0 @ fpga/shell/bd/shell_bd.tcl:1600 */
#define MPS3_GENCHK_BASE    0x44A60000u  /* 4K, eth_mac_test_subsystem_0 @ fpga/shell/bd/shell_bd.tcl:1610 */
#define MPS3_JTAGBB_BASE    0x44A70000u  /* 64K, jtag_bb_0 @ fpga/shell/bd/shell_bd.tcl:1611 */
/* SWDBB: retired at the A6 SWD->JTAG cutover; shares the JTAGBB page. Same page as JTAGBB. */
#define MPS3_SWDBB_BASE     0x44A70000u  /* fpga/shell/ip/swd_bb/swd_bb.sv */
#define MPS3_DBGBR_BASE     0x44A80000u  /* 64K, debug_bridge_0 @ fpga/shell/bd/shell_bd.tcl:1612 */
#define MPS3_UARTBR_BASE    0x44A90000u  /* 64K, uart_bridge_0 @ fpga/shell/bd/shell_bd.tcl:1613 */
#define MPS3_GPIO_BASE      0x44AA0000u  /* 64K, board_gpio_0 @ fpga/shell/bd/shell_bd.tcl:1614 */
#define MPS3_MMCM_DRP_BASE  0x44AB0000u  /* 64K, clk_wiz_dut @ fpga/shell/bd/shell_bd.tcl:1615 */
#define MPS3_CLCD_BASE      0x44AC0000u  /* 64K, clcd_0 @ fpga/shell/bd/shell_bd.tcl:1616 */
#define MPS3_CLCDKVM_BASE   0x44AD0000u  /* 64K, clcd_kvm_0 @ fpga/shell/bd/shell_bd.tcl:1617 */
#define MPS3_TOUCH_BASE     0x44AE0000u  /* 64K, touch_iic_0 @ fpga/shell/bd/touch_iic_add.tcl:100 — GATED: built only with SHELL_TOUCH=1 */
#define MPS3_DUTEGR_BASE    0x44B20000u  /* 64K, dut_egress_0 @ fpga/shell/bd/shell_bd.tcl:1625 */
#define MPS3_USRACC_BASE    0x44B30000u  /* 64K, usr_access_rd_0 @ fpga/shell/bd/shell_bd.tcl:1637 */
#define MPS3_WDOG_BASE      0x44B40000u  /* 64K, axi_timebase_wdt_0 @ fpga/shell/bd/shell_bd.tcl:1638 */

/* Pages RESERVED by a block that is DESIGNED but in no block design yet.
 * No base is emitted for them -- nothing can drive a slave that is not in
 * the fabric, and a base for one is how a page gets claimed twice. They are
 * listed so the NEXT block takes a page nobody holds. The authority is
 * tools/gen_regmap.py's RESERVATIONS; every file that writes one of these
 * literals is listed there and checked against it by
 * tests/firmware_logic/test_regmap_conformance.py.
 *
 *   0x44AF0000 (64K)  AXIJTAG
 *       axi_jtag:1.0 AXI->JTAG shifter into the DUT SWJ-DP (staged carry-across; MUTUALLY EXCLUSIVE with the fielded JTAGBB @0x44A7)
 *
 *   0x44B00000 (64K)  MAGICID
 *       axi_gpio:2.0 all-inputs magic word 0x4A544147 ('JTAG') -- aperture-alive preflight (staged carry-across)
 *
 *   0x44B10000 (64K)  UART16550
 *       axi_uart16550:2.0 ns16550a console, regfile at +0x1000 (staged carry-across)
 *
 * Region 0x44A00000..0x44B50000: FULL -- a new block must extend REGION_HI, and say so.
 */
/* END GENERATED[regmap-bases] */

/* BEGIN GENERATED[regmap-offsets] — gen_regmap.py — DO NOT EDIT BY HAND */
/* Register offsets, one block per stanza. Bit-field defines for these are
 * NOT generated -- they live outside the fences, beside the prose that
 * explains them. */

/* ---- CLKRST @ 0x44A00000 — fpga/shell/ip/clkrst/dut_clkrst.sv ---- */
#define CLKRST_RESET_CTRL       0x00u
#define CLKRST_DUT_CLK_SEL      0x04u
#define CLKRST_DUT_CLK_DRP      0x08u
#define CLKRST_STATUS           0x0Cu  /* (ro) */

/* ---- DFXCTL @ 0x44A10000 — fpga/shell/ip/dfx_ctl/dfx_ctl.sv ---- */
#define DFXCTL_DECOUPLE         0x00u
#define DFXCTL_SHUTDOWN         0x04u
#define DFXCTL_STATUS           0x08u  /* (ro) */
#define DFXCTL_RM_ID            0x10u  /* (ro) */
#define DFXCTL_RM_STATUS        0x14u  /* (ro) */

/* ---- HWICAP @ 0x44A20000 — Xilinx AXI HWICAP (PG134) [vendor doc, not derived] ---- */
#define HWICAP_GIER             0x1Cu  /* global interrupt enable */
#define HWICAP_ISR              0x20u  /* interrupt status */
#define HWICAP_IER              0x28u  /* interrupt enable */
#define HWICAP_WF               0x100u  /* write FIFO — bitstream words, ICAP order */
#define HWICAP_RF               0x104u  /* read FIFO */
#define HWICAP_SZ               0x108u  /* transfer size, in 32-bit words */
#define HWICAP_CR               0x10Cu  /* control: [0] write (FIFO->ICAP), [1] read */
#define HWICAP_SR               0x110u  /* status: [0] done, [2] EOS */
#define HWICAP_WFV              0x114u  /* write FIFO vacancy, in words */
#define HWICAP_RFO              0x118u  /* read FIFO occupancy, in words */

/* ---- VPHY @ 0x44A30000 — fpga/ethernet/mdio_phy_model/mdio_phy_model.sv ---- */
#define VPHY_PHY_STATE          0x00u
#define VPHY_PHY_ID             0x04u
#define VPHY_LINK_EVENT         0x08u

/* ---- USD @ 0x44A40000 — fpga/shell/ip/usd_spi/usd_spi.sv ---- */
#define USD_ID                  0x00u  /* (ro) block-present witness 0x55534431 "USD1" */
#define USD_CTRL                0x04u  /* EN / CS / WIDE / CD_POL / CD_IGNORE */
#define USD_CLKDIV              0x08u  /* SCK = aclk / (2*(DIV+1)), reset 124 */
#define USD_DATA                0x0Cu  /* W: start a shift / R: last RX word */
#define USD_STATUS              0x10u  /* BUSY, CD, sticky CD_CHANGED/OVR/ABORT (W1C) */

/* ---- TELEM @ 0x44A50000 — fpga/shell/ip/telem/telem.sv ---- */
#define TELEM_CTRL              0x00u
#define TELEM_BUS_MV            0x04u  /* (ro) */
#define TELEM_CURR_UA           0x08u  /* (ro) */
#define TELEM_POWER_MW          0x0Cu  /* (ro) */
#define TELEM_STATUS            0x10u  /* (ro) */

/* ---- GENCHK @ 0x44A60000 — fpga/ethernet/gen_checker/gen_checker.sv ---- */
#define GENCHK_CTRL             0x00u
#define GENCHK_INJECT           0x04u
#define GENCHK_TX_CNT           0x08u  /* (ro) */
#define GENCHK_RX_CNT           0x0Cu  /* (ro) */
#define GENCHK_ERR_CNT          0x10u  /* (ro) */
#define GENCHK_DUT_IP           0x14u  /* (ro) DUT-IP sniffer */
#define GENCHK_DUT_STATUS       0x18u  /* (ro) DUT-IP sniffer */

/* ---- JTAGBB @ 0x44A70000 — fpga/shell/ip/jtag_bb/jtag_bb.sv ---- */
#define JTAGBB_DRIVE            0x00u
#define JTAGBB_SAMPLE           0x04u  /* (ro) */

/* ---- SWDBB @ 0x44A70000 — fpga/shell/ip/swd_bb/swd_bb.sv (legacy, retired at the A6 SWD->JTAG cutover; shares the JTAGBB page) ---- */
#define SWDBB_DRIVE             0x00u
#define SWDBB_SAMPLE            0x04u  /* (ro) */

/* ---- DBGBR @ 0x44A80000 — Xilinx Debug Bridge, AXI->BSCAN (PG245) [vendor doc, not derived] ---- */
#define DBGBR_LENGTH            0x00u  /* rw — bits to shift this chunk (1..32) */
#define DBGBR_TMS               0x04u  /* rw — TMS vector chunk, LSB shifts first */
#define DBGBR_TDI               0x08u  /* rw — TDI vector chunk, LSB shifts first */
#define DBGBR_TDO               0x0Cu  /* ro — captured TDO chunk */
#define DBGBR_CTRL              0x10u  /* [0] GO: write 1 to start, self-clears */

/* ---- UARTBR @ 0x44A90000 — fpga/shell/ip/uart_bridge/uart_bridge.sv ---- */
#define UARTBR_U0_TXRX          0x00u  /* W: U0_TX push / R: U0_RX pop */
#define UARTBR_U1_TXRX          0x08u  /* W: U1_TX push / R: U1_RX pop */
#define UARTBR_SWO_RX           0x10u  /* R: SWO_RX pop (ro) */
#define UARTBR_FIFO_STATUS      0x14u  /* (ro) */
#define UARTBR_SWO_CFG          0x18u  /* (added, see ambiguities) */

/* ---- GPIO @ 0x44AA0000 — fpga/shell/ip/board_gpio/board_gpio.sv ---- */
#define GPIO_IN                 0x00u  /* (ro) */
#define GPIO_OUT                0x04u
#define GPIO_OE                 0x08u
#define GPIO_OWN                0x0Cu

/* ---- MMCM_DRP @ 0x44AB0000 — Xilinx Clocking Wizard AXI4-Lite DRP (PG065) [vendor doc, not derived] ---- */
#define MMCM_DRP_SW_RESET       0x00u  /* [0] soft reset (self-clearing) */
#define MMCM_DRP_STATUS         0x04u  /* ro: [0] LOCKED */
#define MMCM_DRP_CFG_REG0       0x200u  /* [7:0] DIVCLK_DIVIDE, [15:8] CLKFBOUT_MULT, [25:16] frac */
#define MMCM_DRP_CFG_REG2       0x208u  /* [7:0] CLKOUT0_DIVIDE, [17:8] CLKOUT0_FRAC */
#define MMCM_DRP_LOAD           0x25Cu  /* [0] LOAD, [1] SEN — latch+apply CFG_REG* */

/* ---- CLCD @ 0x44AC0000 — fpga/shell/ip/clcd/clcd.sv ---- */
#define CLCD_CTRL               0x00u
#define CLCD_CMD                0x04u  /* (W1, RS=0) */
#define CLCD_DATA               0x08u  /* (W1, RS=1) */
#define CLCD_STATUS             0x0Cu  /* (ro) */
#define CLCD_READ               0x10u  /* (ro) */
#define CLCD_TIMING             0x14u

/* ---- CLCDKVM @ 0x44AD0000 — fpga/shell/ip/clcd_kvm/clcd_kvm.sv ---- */
#define CLCDKVM_CTRL            0x00u
#define CLCDKVM_STATUS          0x04u  /* (ro) */
#define CLCDKVM_EVENT           0x08u  /* (RW1C) */
#define CLCDKVM_PANEL_TMR       0x0Cu
#define CLCDKVM_TIMEOUT         0x10u
#define CLCDKVM_DEBOUNCE        0x14u
#define CLCDKVM_TUNNEL          0x18u  /* (ro) */

/* ---- TOUCH @ 0x44AE0000 — Xilinx AXI IIC (PG090) [vendor doc, not derived] — GATED: built only with SHELL_TOUCH=1 ---- */
#define TOUCH_IIC_GIE           0x1Cu  /* global interrupt enable */
#define TOUCH_IIC_ISR           0x20u  /* interrupt status (W1C) */
#define TOUCH_IIC_IER           0x28u  /* interrupt enable */
#define TOUCH_IIC_SOFTR         0x40u  /* soft reset — write the reset key 0x0000000A */
#define TOUCH_IIC_CR            0x100u  /* control */
#define TOUCH_IIC_SR            0x104u  /* status (ro) */
#define TOUCH_IIC_TX_FIFO       0x108u  /* transmit FIFO (data + dynamic start/stop) */
#define TOUCH_IIC_RX_FIFO       0x10Cu  /* receive FIFO (ro, destructive pop) */
#define TOUCH_IIC_ADR           0x110u  /* own slave address (slave mode) */
#define TOUCH_IIC_TX_FIFO_OCY   0x114u  /* TX FIFO occupancy (ro) */
#define TOUCH_IIC_RX_FIFO_OCY   0x118u  /* RX FIFO occupancy (ro) */
#define TOUCH_IIC_TEN_ADR       0x11Cu  /* 10-bit slave address (unused here) */
#define TOUCH_IIC_RX_FIFO_PIRQ  0x120u  /* RX FIFO programmable depth IRQ / dyn count */
#define TOUCH_IIC_GPO           0x124u  /* general-purpose output */

/* ---- DUTEGR @ 0x44B20000 — fpga/shell/ip/dut_egress/dut_egress.sv ---- */
#define DUTEGR_CTRL             0x00u  /* rw enable / flush / clear-counters */
#define DUTEGR_STATUS           0x04u  /* ro frame-ready, empty, full, sticky flags */
#define DUTEGR_LEVEL            0x08u  /* ro committed bytes and frames waiting */
#define DUTEGR_FRAME_LEN        0x0Cu  /* ro head frame bytes remaining and total */
#define DUTEGR_DATA             0x10u  /* ro destructive byte pop with valid/last */
#define DUTEGR_RX_FRAMES        0x14u  /* ro frames captured whole */
#define DUTEGR_DROP_FULL        0x18u  /* ro frames dropped for want of room */
#define DUTEGR_DROP_GIANT       0x1Cu  /* ro frames dropped for exceeding MAX_FRAME */
#define DUTEGR_TX_CTRL          0x20u  /* rw commit / abort / clear-counters / flush, RAW mode */
#define DUTEGR_TX_STATUS        0x24u  /* ro room, empty, full, staging, sticky flags */
#define DUTEGR_TX_SPACE         0x28u  /* ro free bytes and free frame slots */
#define DUTEGR_TX_DATA          0x2Cu  /* wo stage one byte (reads 0, no side effect) */
#define DUTEGR_TX_FRAMES        0x30u  /* ro frames handed to the bridge whole */
#define DUTEGR_TX_REJECT        0x34u  /* ro commits refused (empty, short, long, no room) */
#define DUTEGR_TX_FLUSHED       0x38u  /* ro committed frames discarded by TX_CTRL.FLUSH */

/* ---- USRACC @ 0x44B30000 — fpga/shell/ip/usr_access_rd/usr_access_rd.sv ---- */
#define USRACC_MAGIC            0x00u  /* (ro) block-present witness 0x55535241 "USRA" */
#define USRACC_VALUE            0x04u  /* (ro) AXSS USR_ACCESS word, HARNESS_VER32 */
#define USRACC_STATUS           0x08u  /* (ro) bit0 valid — a stable AXSS sample is held */

/* ---- WDOG @ 0x44B40000 — Xilinx AXI Timebase Watchdog Timer (PG128) [vendor doc, not derived] ---- */
#define WDOG_TWCSR0             0x00u  /* control/status 0: [3] WRS reset-status, [2] WDS 1st-expiry status (W1C — THE KICK), [1] EWDT1 enable */
#define WDOG_TWCSR1             0x04u  /* control/status 1: [0] EWDT2 — both enables must be set for the watchdog to run */
#define WDOG_TBR                0x08u  /* ro: the free-running timebase counter */
/* END GENERATED[regmap-offsets] */

/* ========================================================================
 * USRACC (0x44B3_0000) — the FABRIC's own build identity
 * (docs/VERSIONING_PLAN.md §3.4; fpga/shell/ip/usr_access_rd/)
 *
 * USRACC_VALUE is the AXSS configuration register, i.e. whatever
 * `set_property BITSTREAM.CONFIG.USR_ACCESS` put in the .bit this fabric is
 * running — which fpga/dfx/build_dfx.tcl sets to HARNESS_VER32, the same 32-bit
 * value scripts/gen_version.py compiles into the image. The two can only
 * disagree if the .bit and the image baked into it came from different builds.
 *
 * READ MAGIC FIRST, AND MEAN IT. Every unmapped page in this window reads back
 * 0, so on a shell WITHOUT this block USRACC_VALUE reads 0 — which is not an
 * obviously broken answer, it is a plausible build identity. MAGIC is how
 * firmware tells "no sensor" from "stamped zero", and mps3_fabric_usr_access_str()
 * is the only thing that should be doing the telling.
 * ======================================================================== */

/* "USRA". A read of anything else means this page has no USRACC behind it. */
#define USRACC_MAGIC_VALUE             0x55535241u

/* STATUS[0]: the USR_ACCESSE2 primitive has presented DATAVALID and the word
 * has been captured. Low means the value is not an answer yet. */
#define USRACC_STATUS_VALID            (1u << 0)

/* ========================================================================
 * WDOG (0x44B4_0000) — the shell watchdog
 * (xilinx.com:ip:axi_timebase_wdt:3.0, PG128;
 *  docs/planning/SERVICES_PARTITION.md §5)
 *
 * Two-stage: the FIRST expiry sets TWCSR0.WDS (a status bit firmware can read
 * and report); the SECOND asserts Timebase_WDT_Reset, which shell_bd.tcl wires
 * to proc_sys_reset_shell/aux_reset_in — a warm restart of the shell firmware.
 *
 * THE KICK is a W1C of TWCSR0.WDS. Both enable bits (TWCSR0.EWDT1 and
 * TWCSR1.EWDT2) must be set for the watchdog to run; it comes out of reset
 * DISABLED, which is deliberate — see shell_bd.tcl's note on why the watchdog
 * sits on its own reset (it is what stops a reset LOOP).
 * ======================================================================== */
/* CORRECTED 2026-09-23 (net-protocol v0.11 `reboot`, the first code to WRITE
 * these). The previous values -- WRS bit0, WDS bit1, EWDT1 bit2, EWDT2 bit2 --
 * were "[vendor doc, not derived]" and WRONG against the vendor's own driver,
 * Vitis 2024.1 embeddedsw wdttb_v5_8/src/xwdttb_hw.h:
 *     XWT_CSR0_WRS_MASK 0x8, XWT_CSR0_WDS_MASK 0x4, XWT_CSR0_EWDT1_MASK 0x2,
 *     XWT_CSRX_EWDT2_MASK 0x1 (written to TWCSR1 by XWdtTb_Start,
 *     xwdttb_config.c:194-198).
 * With the old numbers an "enable" wrote TWCSR0 bit 2 -- which is WDS, i.e. a
 * KICK -- and TWCSR1 bit 2, a reserved bit: the watchdog would never have run,
 * and `reboot` would have answered ok and never rebooted. Nothing wrote them
 * before, which is how they survived. The generated TWCSR0/TWCSR1 comments in
 * the fence above still carry the old bit numbers; their source is
 * tools/gen_regmap.py (and docs/contracts/shell-regmap.md:676) -- outside this
 * lane, fix handed to the lead. firmware/test/test_v011_dispatch.c pins these
 * four values to the driver's. */
#define WDOG_TWCSR0_WRS                (1u << 3)  /* W1C: last reset was ours */
#define WDOG_TWCSR0_WDS                (1u << 2)  /* W1C: 1st expiry — THE KICK */
#define WDOG_TWCSR0_EWDT1              (1u << 1)  /* enable, half 1 */
#define WDOG_TWCSR1_EWDT2              (1u << 0)  /* enable, half 2 (TWCSR1) */

/* ========================================================================
 * CLKRST (0x44A0_0000) — DUT clock (DRP) + 3 resets
 * ======================================================================== */

/* RESET_CTRL — 1 = released (per regmap: "1=released; async-assert/
 * sync-deassert in RTL"). Firmware writes 0 to assert, 1 to release. */
#define CLKRST_RESET_CTRL_DUT_RESETN   (1u << 0)
#define CLKRST_RESET_CTRL_RP_RESETN    (1u << 1)
#define CLKRST_RESET_CTRL_DBG_RESETN   (1u << 2)

/* DUT_CLK_SEL[7:0] = preset id; exact preset table (e.g. 25/50/100 MHz) is
 * firmware-policy, not yet enumerated in the contract — see clkrst/README. */
#define CLKRST_DUT_CLK_SEL_MASK        0x000000FFu

/* DUT_CLK_DRP[15:0] = raw MMCM DRP window (address+data multiplexed per the
 * clkrst RTL's DRP sequencer — exact packing is an A1/A3 detail, see
 * clkrst/clkrst.h for the assumed encoding). */
#define CLKRST_DUT_CLK_DRP_MASK        0x0000FFFFu

#define CLKRST_STATUS_MMCM_LOCKED      (1u << 0)  /* RO */
#define CLKRST_STATUS_DUT_CLK_ALIVE    (1u << 1)  /* RO */

/* ========================================================================
 * MMCM_DRP (0x44AB_0000) — Xilinx Clocking Wizard v6.0 AXI4-Lite dynamic
 * reconfiguration (clk_wiz_dut, the DUT clock). This is the block that
 * actually retunes the DUT MMCM; CLKRST.DUT_CLK_SEL is an inert scratch
 * register (dut_clkrst.sv stores it but the clk_wiz has no DRP pins to drive
 * — see the MPS3_MMCM_DRP_BASE note above).
 *
 * Offsets + field packing are the PG065 "clock configuration register"
 * layout for a clk_wiz built with the Dynamic Reconfiguration interface.
 * Reproduced here for SHAPE — same "confirm against the Vitis-generated
 * xclk_wiz_l.h / _hw.h once the BSP emits it" caveat as the HWICAP and QSPI
 * blocks above. The reconfiguration sequence (clkrst.c clkrst_mmcm_drp_apply):
 *   1. write CFG_REG0 = (CLKFBOUT_MULT<<8)|DIVCLK_DIVIDE   (the VCO M/D)
 *   2. write CFG_REG2 = CLKOUT0_DIVIDE                       (the output O)
 *   3. write LOAD = LOAD|SEN  (latch + apply the new M/D/O; the MMCM drops
 *      LOCKED, re-computes, then re-asserts LOCKED)
 *   4. poll LOCKED (the clk_wiz's own STATUS bit, and equivalently
 *      CLKRST.STATUS.mmcm_locked — clk_wiz_dut/locked is wired to
 *      dut_clkrst.mmcm_locked_i in shell_bd.tcl line 641).
 * The MMCM's current config is otherwise untouched (no SW_RESET is issued on
 * a retune — a full soft-reset would needlessly glitch the DUT clock).
 * ======================================================================== */

#define MMCM_DRP_STATUS_LOCKED  (1u << 0)  /* RO */
#define MMCM_DRP_LOAD_LOAD      (1u << 0)
#define MMCM_DRP_LOAD_SEN       (1u << 1)

/* ========================================================================
 * DFXCTL (0x44A1_0000) — decoupler + shutdown-mgr + RP reset gate + rm_id
 * readback (shell-regmap.md v0.1 — I8 RESOLVED)
 * ======================================================================== */

#define DFXCTL_DECOUPLE_EN        (1u << 0)  /* 1 = RP isolated; assert before load */
#define DFXCTL_SHUTDOWN_AXI       (1u << 0)  /* AXI Shutdown Manager quiesce */
#define DFXCTL_STATUS_DECOUPLED   (1u << 0)  /* RO */
#define DFXCTL_STATUS_RP_IN_RESET (1u << 1)  /* RO */

#define DFXCTL_RM_STATUS_RM_ID_VALID (1u << 0)  /* RO */
#define DFXCTL_RM_STATUS_DUT_LOCKUP  (1u << 1)  /* RO */

/* v0.1 RESOLVED (was AMBIGUITY(A6) #4 / I8): DFXCTL_RM_ID @0x10 is the real,
 * regmap-confirmed offset — shell-regmap.md "RM-load verify" now reads
 * DFXCTL.RM_ID (0x44A1_0010) + RM_STATUS.rm_id_valid, and reports the
 * confirmed id to the host. swap_fsm.c's step_verify() (I25) compares this
 * register's value against the target RM's numeric rm_id (carried in the
 * partial's own bitstream header, common/net_proto.h) — no separate
 * name<->id table needed in firmware; the wire header already carries it
 * numerically for exactly this reason (see coordinator/swap_fsm.c). The
 * older CFG_CRC placeholder (config-CRC monitor) is dropped: v0.1 gives no
 * such register, only rm_id_valid; a config CRC compare is left as a v2
 * "optional DFX Bitstream Monitor" per shell-regmap.md, not required for
 * v1 verify to be real. */

/* ========================================================================
 * HWICAP (0x44A2_0000) — AXI HWICAP -> ICAPE3 (PG134-shaped; see file
 * header note: confirm against the Vitis-generated xhwicap_l.h)
 * ======================================================================== */

/* CR bit0=Write (FIFO->ICAP), bit1=Read (ICAP->FIFO) — per the axi_hwicap driver
 * xhwicap_l.h (XHI_CR_WRITE_MASK=0x1, XHI_CR_READ_MASK=0x2). These were previously
 * SWAPPED here, so every write-trigger fired CR=0x2 (=READ): the WF never drained
 * to ICAP, the FIFO filled (WFV=0) and reconfig never happened (HW-confirmed on the
 * KU115 shell — the partial stall with RM_ID stuck at greybox). */
#define HWICAP_CR_WRITE   (1u << 0)
#define HWICAP_CR_READ    (1u << 1)
#define HWICAP_SR_DONE    (1u << 0)
#define HWICAP_SR_EOS     (1u << 2)

/* Byte packing for the RAM/QSPI HWICAP.WF writers (swap_fsm.c hwicap_push_chunk,
 * common/hwicap_writer.c). The DFX .bin stores each config word
 * BIG-endian (first file byte = MSB; the sync word is bytes AA 99 55 66) and the
 * MicroBlaze is little-endian, so a native `memcpy(&w,...)` byte-swaps the word
 * and ICAP never sees 0xAA995566. MSB-first (1) packs word=(b0<<24|b1<<16|b2<<8|b3)
 * to match the stream-direct partial path (swap_fsm.c icap_direct_write). Host-mock
 * test builds keep native packing (0) so the existing byte-pattern assertions stay
 * byte-identical; the real target defaults to MSB-first. Override with
 * -DMPS3_HWICAP_MSB_FIRST=<0|1> for HW byte-order bring-up (if the axi_hwicap core
 * turns out to byte-swap internally, force 0). */
#ifndef MPS3_HWICAP_MSB_FIRST
#  ifdef MPS3_HAL_MOCK
#    define MPS3_HWICAP_MSB_FIRST 0
#  else
#    define MPS3_HWICAP_MSB_FIRST 1
#  endif
#endif

/* HWICAP write-path protocol select (BD C_MODE mirror). 0 = LITE (default,
 * the current proven behaviour): NO write FIFO, one StartConfig (CR=WRITE) per
 * config word — matches the shell built with axi_hwicap C_MODE {1}
 * (XPAR_AXI_HWICAP_0_MODE 1) and the BSP driver's `#if (XPAR_HWICAP_0_MODE==1)`
 * one-word path. 1 = FIFO: poll HWICAP_WFV, push up to WFV words into HWICAP_WF,
 * kick a single StartConfig, poll CR until WRITE self-clears, refill — matches
 * the shell rebuilt with axi_hwicap C_MODE {0} + C_WRITE_FIFO_DEPTH and the
 * driver's `#else` WFV-batch path. DEFAULT 0 keeps every unset build (host tests
 * + the current-silicon ELF) byte-identical; set -DMPS3_HWICAP_FIFO=1 (Makefile
 * HWICAP_FIFO=1) only once the FIFO-mode bitstream is the one on the board. The
 * writers keep the SAME mps3_hwicap_pack_word() packing either way. */
#ifndef MPS3_HWICAP_FIFO
#  define MPS3_HWICAP_FIFO 0
#endif

/* THE one shared HWICAP word-packing primitive (R3). Every writer that turns
 * 4 .bin bytes into one 32-bit HWICAP.WF word MUST go through this so the
 * three code paths cannot drift apart: previously the stream-direct sink
 * (swap_fsm.c icap_direct_write) packed MSB-first while the RAM writer
 * (swap_fsm.c hwicap_push_chunk) and the QSPI/boot writer (overlay_store.c
 * overlay_store.c's writer) each carried their OWN copy of the pack expression —
 * exactly the shape where a re-introduced native memcpy on one path silently
 * byte-swaps every config word so ICAP never sees the 0xAA995566 sync word
 * (docs/OVER_THE_WIRE_RECONFIG_PLAN.md §I18: the .bin is BIG-endian, first
 * file byte = MSB; the MicroBlaze is little-endian, so a native memcpy is
 * WRONG on target). MPS3_HWICAP_MSB_FIRST is 1 on every real target build
 * (the memcpy branch is not even compiled there). The host-mock default (0,
 * native memcpy) is kept ONLY so the existing byte-pattern assertions in the
 * host tests stay byte-identical; firmware/test/test_hwicap_byte_lane.c
 * forces =1 to pin the big-endian order so a re-introduced byte-swap fails a
 * unit test, not a board bring-up. */
static inline uint32_t mps3_hwicap_pack_word(const uint8_t *p)
{
#if MPS3_HWICAP_MSB_FIRST
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];  /* first file byte -> MSB (I18) */
#else
    uint32_t w;
    memcpy(&w, p, 4);  /* host mock: native packing keeps byte-pattern tests identical */
    return w;
#endif
}

/* ========================================================================
 * VPHY (0x44A3_0000) — virtual-PHY register model + link events
 * ======================================================================== */

#define VPHY_PHY_STATE_LINK_UP     (1u << 0)
#define VPHY_PHY_STATE_SPEED100    (1u << 1)
#define VPHY_PHY_STATE_FULL_DUPLEX (1u << 2)

#define VPHY_LINK_EVENT_FORCE_DOWN (1u << 0)
#define VPHY_LINK_EVENT_PULSE      (1u << 1)

/* ========================================================================
 * USD (0x44A4_0000) — D13's usd_spi (user microSD, SPI mode). Offsets are
 * GENERATED above (MPS3_USD_BASE, USD_*); the bit fields live with the driver
 * in firmware/usd/usd_regs.h. The pad-less axi_quad_spi_0 (OVLSTORE) and its
 * SST26 bit fields are gone: nothing may issue a flash opcode at this page.
 * ======================================================================== */

/* ========================================================================
 * TELEM (0x44A5_0000) — INA228 I2C + status (shell-regmap.md v0.1 — I9
 * RESOLVED: real field table, pre-shadowed mV/µA/mW/status register block).
 * ======================================================================== */

#define TELEM_CTRL_ENABLE    (1u << 0)
#define TELEM_CTRL_ALARM_EN  (1u << 1)
#define TELEM_STATUS_ALARM   (1u << 0)  /* RO */
#define TELEM_STATUS_I2C_ERR (1u << 1)  /* RO */

/* ========================================================================
 * GENCHK (0x44A6_0000) — error-inject gen/checker control (shell-regmap.md
 * v0.1 field table; I10 RESOLVED). Driven by the `{"op":"macgen",...}` control
 * verb as of net-protocol.md v0.4 (coordinator_handle_macgen): CTRL sets
 * gen_en/chk_en, INJECT arms the next-frame fault (one-hot), TX/RX/ERR_CNT are
 * the read-only counters the verb reports back. The counters are RTL-owned and
 * CLEAR-ON-ENABLE-RISE (shell-regmap.md v0.4): the gen_checker zeroes all three
 * on a gen_en/chk_en 0->1 rising edge, so they are monotonic *within* an enabled
 * session but NOT across enable toggles — they are NOT free-running/globally
 * monotonic. macgen only READS them (there is no counter-clear register); a MAC
 * test enables once and snapshots deltas within that session.
 *
 * SEAM FLAG for the gen_checker RTL owner: the cocotb bench's DRAFT layout in
 * tests/common/regmap.py (GEN_CTRL[3:1]=mode / GEN_COUNT / CHK_STATUS packing
 * err+good counts / CHK_CLEAR) does NOT match this frozen block. The frozen
 * contract (this file + shell-regmap.md) is authoritative: separate CTRL(0x00)
 * + one-hot INJECT(0x04) + three 32-bit counters(0x08/0x0C/0x10). The real
 * gen_checker AXI decode must present THIS layout for macgen to work.
 * ======================================================================== */
/* DUT egress observability (Phase-3 CLCD DUT-IP row). The gen_checker snoops the
 * DUT's transmit stream and latches the source IPv4 + ethertype of the last frame
 * it saw; read by clcd.c's clcd_fmt_dut_ip(). RO -- the checker owns them. This is
 * the DUT's own IP, distinct from the harness's management IP (net_proto.h). */

#define GENCHK_CTRL_GEN_EN     (1u << 0)
#define GENCHK_CTRL_CHK_EN     (1u << 1)
#define GENCHK_INJECT_BAD_FCS  (1u << 0)
#define GENCHK_INJECT_RUNT     (1u << 1)
#define GENCHK_INJECT_GIANT    (1u << 2)
#define GENCHK_INJECT_IFG      (1u << 3)
#define GENCHK_INJECT_DRIBBLE  (1u << 4)
/* All valid INJECT fault bits (writing 0 = "none", i.e. no fault armed). */
#define GENCHK_INJECT_MASK     (GENCHK_INJECT_BAD_FCS | GENCHK_INJECT_RUNT | \
                                GENCHK_INJECT_GIANT   | GENCHK_INJECT_IFG  | \
                                GENCHK_INJECT_DRIBBLE)

/* DUT_STATUS fields. IP_SEEN gates DUT_IP: clear until the checker captures the
 * first DUT ARP/IP frame, so a CPU-less RM (eth_ss, no IP stack) reads "--" until
 * one is seen. The ethertype in the high half is a diagnostic aid (0x0800 IPv4,
 * 0x0806 ARP). */
#define GENCHK_DUT_STATUS_IP_SEEN     (1u << 0)   /* RO — DUT_IP is valid   */
#define GENCHK_DUT_STATUS_ETYPE_SHIFT 16          /* [31:16] last ethertype */

/* ========================================================================
 * SWDBB (0x44A7_0000) — SWD pin-wiggler (shell-regmap.md v0.1 — I5
 * RESOLVED). Backs the swd_server OpenOCD `remote_bitbang` translation;
 * drives swd_clk/swd_dio_o/swd_dio_oe, reads swd_dio_i
 * (docs/contracts/partition-pins.md SWD group). srst is CLKRST.dbg_resetn,
 * not this block.
 * ======================================================================== */

#define SWDBB_DRIVE_SWCLK      (1u << 0)
#define SWDBB_DRIVE_SWDIO_O    (1u << 1)
#define SWDBB_DRIVE_SWDIO_OE   (1u << 2)
#define SWDBB_SAMPLE_SWDIO_I   (1u << 0)  /* RO */

/* ========================================================================
 * JTAGBB (0x44A7_0000) — JTAG pin-wiggler (A6: SWD->JTAG cutover; jtag_bb
 * replaces swd_bb in the same 0x44A7 slot — shell-regmap.md JTAGBB section).
 * Backs the jtag_server OpenOCD `remote_bitbang` translation; drives
 * jtag_tck/jtag_tms/jtag_tdi, reads jtag_tdo (docs/contracts/partition-pins.md
 * JTAG group). srst is CLKRST.dbg_resetn, not this block. Same CSR layout as
 * SWDBB, JTAG pin names. Folded here from jtag_server.h's local placeholders.
 * ======================================================================== */

#define JTAGBB_DRIVE_TCK       (1u << 0)
#define JTAGBB_DRIVE_TMS       (1u << 1)
#define JTAGBB_DRIVE_TDI       (1u << 2)
#define JTAGBB_SAMPLE_TDO      (1u << 0)  /* RO */

/* ========================================================================
 * DBGBR (0x44A8_0000) — Xilinx Debug Bridge, AXI->BSCAN mode (shell-
 * regmap.md v0.1/v0.2 — I6 RESOLVED: base assigned, standard Debug Bridge
 * register layout, no custom fields). Must be in static (survives DFX
 * swaps).
 *
 * Offsets below are the Xilinx embedded-XVC reference layout for the
 * Debug Bridge in AXI-to-BSCAN mode, one <=32-bit shift chunk per CTRL kick.
 *
 * CONFIRMED 2026-07-09 (was ASSUMED-PENDING-BRING-UP). The IP ships no _hw.h
 * for Vitis to generate, but the layout is not ours to guess: it is fixed by
 * Xilinx's own AXI-to-BSCAN reference, XAPP1251 —
 * github.com/Xilinx/XilinxVirtualCable, jtag/zynq7000/XAPP1251/src/xvcServer.c:
 *
 *     typedef struct {
 *       uint32_t  length_offset;   // 0x00
 *       uint32_t  tms_offset;      // 0x04
 *       uint32_t  tdi_offset;      // 0x08
 *       uint32_t  tdo_offset;      // 0x0C
 *       uint32_t  ctrl_offset;     // 0x10
 *     } jtag_t;
 *
 *     ptr->length_offset = 32;  ptr->tms_offset = tms;  ptr->tdi_offset = tdi;
 *     ptr->ctrl_offset = 0x01;
 *     while (ptr->ctrl_offset) { }   // GO self-clears on completion
 *     tdo = ptr->tdo_offset;         // read ONLY after it clears
 *
 * So the sequencing per chunk is: write LENGTH (bits, 1..32) -> TMS word ->
 * TDI word -> CTRL=GO; poll CTRL until GO self-clears; read TDO word.
 * Both the offsets AND that ORDER are pinned by
 * firmware/test/test_xvc_server.c's test_dbgbr_xapp1251_conformance(), which
 * records the access trace — a refactor that kicks CTRL before staging TDI, or
 * reads TDO before GO clears, fails there rather than latching garbage on
 * silicon. (Our poll is bounded + fail-closed, unlike XAPP1251's unbounded
 * `while`; that is a deliberate improvement, not a deviation.)
 * ======================================================================== */

#define DBGBR_CTRL_GO (1u << 0)

/* ========================================================================
 * UARTBR (0x44A9_0000) — UART0/UART1/SWO AXIS<->MicroBlaze FIFO bridge
 * (shell-regmap.md v0.1 — I7 RESOLVED: ONE block, all three streams as
 * sub-offsets — NOT three separate 64 KiB pages as the earlier
 * AMBIGUITY(A6) #3 placeholders assumed).
 * ======================================================================== */
/* SWO_CFG @ 0x18 is NOT in regmap v0.1: the uart_bridge RTL added it at a spare
 * offset and its README flag #2 asks A6 to codify it into v0.2. The offsets and
 * fields here match that RTL. */

#define UARTBR_DATA_MASK       0x000000FFu  /* [7:0] data */
#define UARTBR_VALID           (1u << 8)     /* [8] valid */

/* FIFO_STATUS bit assignment — confirmed against the landed
 * fpga/shell/ip/uart_bridge RTL (its README register table; positions are
 * that file's choice, flagged for regmap v0.2 codification — flag #4). */
#define UARTBR_FIFO_STATUS_U0_TX_FULL  (1u << 0)
#define UARTBR_FIFO_STATUS_U0_RX_EMPTY (1u << 1)
#define UARTBR_FIFO_STATUS_U1_TX_FULL  (1u << 2)
#define UARTBR_FIFO_STATUS_U1_RX_EMPTY (1u << 3)
#define UARTBR_FIFO_STATUS_SWO_RX_EMPTY (1u << 4)

/* SWO_CFG fields (uart_bridge RTL): bit period = (divisor+1) dut_clk
 * cycles; divisor >= 7 recommended (>= 8x oversampling). Changing the
 * divisor requires toggling ENABLE off->on (the RTL captures it on the
 * synchronized enable's rising edge). Sticky RO bits clear while
 * ENABLE=0. */
#define UARTBR_SWO_CFG_DIVISOR_MASK  0x0000FFFFu  /* [15:0] */
#define UARTBR_SWO_CFG_ENABLE        (1u << 16)
#define UARTBR_SWO_CFG_OVERFLOW      (1u << 30)   /* RO, sticky */
#define UARTBR_SWO_CFG_FRAME_ERR     (1u << 31)   /* RO, sticky */

/* ========================================================================
 * GPIO (0x44AA_0000) — board GPIO/PMOD passthrough + host mux (shell-
 * regmap.md v0.1 — I4 RESOLVED). Default = the DUT's dut_gpio_* drives the
 * pads (OWN bit clear); host can override per-bit when no DUT owns them.
 * NGPIO is a build parameter (partition-pins.md: v0 = 16).
 * ======================================================================== */

/* ========================================================================
 * CLCD (0x44AC_0000) — QVGA HX8347-D 8080 parallel bus master (shell-
 * regmap.md v0.4). RESERVED: no slave exists at this page on the shipped
 * shell — see the MPS3_CLCD_BASE note above. The block streams {RS,byte}
 * pairs to the panel; it holds no framebuffer and no font (the HX8347-D's
 * on-chip GRAM is the framebuffer).
 *
 * CMD/DATA writes are DROPPED when STATUS.fifo_full is set — the slave does
 * not back-pressure awready, because clcd_poll() shares its superloop with
 * the lwIP timers and must never be able to stall the data bus. Poll STATUS.
 * ======================================================================== */

#define CLCD_CTRL_ENABLE      (1u << 0)
#define CLCD_CTRL_BACKLIGHT   (1u << 1)  /* CLCD_BL */
#define CLCD_CTRL_RESET_N     (1u << 2)  /* CLCD_RST; 0 = panel held in reset */
#define CLCD_CTRL_FIFO_RESET  (1u << 3)  /* self-clearing */
/* Arm one CLCD_RD panel read cycle (self-clearing; READ_PATH=1 builds only).
 * Then poll STATUS.busy and read CLCD_READ. Reading CLCD_READ has NO side
 * effect -- a CSR page dump over SWD/XVC must never drive the panel bus. */
#define CLCD_CTRL_READ_START  (1u << 4)

#define CLCD_STATUS_FIFO_FULL   (1u << 0)  /* RO */
#define CLCD_STATUS_FIFO_EMPTY  (1u << 1)  /* RO */
#define CLCD_STATUS_BUSY        (1u << 2)  /* RO */
#define CLCD_STATUS_LEVEL_SHIFT 8
#define CLCD_STATUS_LEVEL_MASK  0x0000FF00u

#define CLCD_READ_DATA_MASK  0x000000FFu
#define CLCD_READ_VALID      (1u << 8)

#define CLCD_TIMING_WR_LO_SHIFT     0
#define CLCD_TIMING_WR_HI_SHIFT     8
#define CLCD_TIMING_CS_SETUP_SHIFT  16

/* ========================================================================
 * CLCDKVM (0x44AD_0000) — the CLCD KVM: panel arbiter between the harness
 * (clcd_0) and a DUT-side accelerator reaching the shell over the dut_gpio
 * tunnel. RESERVED: no slave exists at this page on the shipped shell — see
 * the MPS3_CLCDKVM_BASE note above; gate every access behind
 * MPS3_HAS_CLCD_KVM.
 *
 * FROZEN CONTRACT: fpga/shell/ip/clcd_kvm/README.md §5, shell-regmap.md v0.5.
 * Decode width is 32 (shell_bd.tcl sets C_S_AXI_ADDR_WIDTH{32} on every custom
 * CSR block; the RTL default of 12 is NEVER what ships).
 *
 * NO REGISTER IN THIS PAGE HAS A READ SIDE EFFECT. EVENT is W1C — write a 1 to
 * clear a bit, write 0 to leave it; READING DOES NOT CLEAR. The platform dumps
 * whole CSR pages over SWD/XVC and tests/csr_decode_width/ reads every offset,
 * so a read-to-clear EVENT would silently destroy handover events on every
 * debugger attach. Every action in this block is armed by a WRITE
 * (CTRL.src_sel_we, CTRL.panel_rst_pulse, CTRL.force_switch).
 *
 * ALL TIMES ARE IN MICROSECONDS (1 us tick, TICK_DIV = CLK_HZ / 1e6 = 100 at
 * the shipped 100 MHz shell clock). A field written as 0 is floored to 1 us.
 * ======================================================================== */

/* Owner encoding, shared by CTRL.src_sel / STATUS.owner / STATUS.tgt_owner. */
#define CLCDKVM_OWNER_HARNESS  0u
#define CLCDKVM_OWNER_DUT      1u

/* ---- CTRL @ 0x00 — reset 0x0000_0000 ------------------------------------
 * [0] src_sel is WRITE-GATED: tgt_owner is updated from [0] ONLY on a write
 * that ALSO sets [16] src_sel_we. That is what makes a plain read-modify-write
 * of CTRL (e.g. to set the backlight) safe: without src_sel_we it can never
 * clobber an ownership change made by a concurrent USER_nPB1 press. ALWAYS use
 * clcd_kvm_ctrl_update() (firmware/clcd_kvm/) for a RMW, and
 * clcd_kvm_request_owner() to actually move ownership. */
#define CLCDKVM_CTRL_SRC_SEL         (1u << 0)  /* 0=HARNESS 1=DUT; W only with [16] */
#define CLCDKVM_CTRL_FORCE_HARNESS   (1u << 1)  /* pin ownership to HARNESS         */
#define CLCDKVM_CTRL_TIMEOUT_EN      (1u << 2)  /* RESET 1 — hung-owner timeout armed*/
#define CLCDKVM_CTRL_PB_EN           (1u << 3)  /* RESET 1 — the button works with
                                                 * NO firmware at all               */
#define CLCDKVM_CTRL_DUT_REQ_EN      (1u << 4)  /* RESET 0 — firmware opts the DUT in*/
#define CLCDKVM_CTRL_BACKLIGHT       (1u << 5)  /* KVM-sourced CLCD_BL (ACTIVE-HIGH) */
#define CLCDKVM_CTRL_PANEL_RST_N     (1u << 6)  /* KVM-sourced CLCD_RST (ACTIVE-LOW:
                                                 * 0 = panel held in reset)          */
#define CLCDKVM_CTRL_BL_RST_SRC      (1u << 7)  /* RESET 0 = BL/RST follow clcd_0's
                                                 * CTRL[1]/[2] (drop-in: the SHIPPED
                                                 * firmware lights the panel on the
                                                 * new bitstream, unchanged).
                                                 * 1 = they follow [5]/[6] above.
                                                 * The KVM's auto panel-reset
                                                 * sequencer overrides CLCD_RST in
                                                 * BOTH modes — that is the recovery
                                                 * lever, and it is pure hardware.   */
#define CLCDKVM_CTRL_PANEL_RST_PULSE (1u << 8)  /* W1P — arm ONE full panel-reset
                                                 * sequence (S_RST->S_SETTLE->S_GRANT)
                                                 * with NO owner change. Reads 0.    */
#define CLCDKVM_CTRL_FORCE_SWITCH    (1u << 9)  /* W1P — commit the pending switch
                                                 * NOW, skipping the outgoing drain
                                                 * gate. Reads 0.                    */
#define CLCDKVM_CTRL_SRC_SEL_WE      (1u << 16) /* W1P — write-enable for [0]. Reads 0*/

/* ---- STATUS @ 0x04 — RO, no side effects -------------------------------- */
#define CLCDKVM_STATUS_OWNER            (1u << 0)  /* COMMITTED owner (reaching pads) */
#define CLCDKVM_STATUS_SWITCH_PENDING   (1u << 1)  /* tgt_owner != owner (derived)    */
#define CLCDKVM_STATUS_TGT_OWNER        (1u << 2)  /* REQUESTED owner                 */
#define CLCDKVM_STATUS_PANEL_RST_ACTIVE (1u << 3)  /* CLCD_RST pad is low, any cause  */
#define CLCDKVM_STATUS_PANEL_SETTLING   (1u << 4)  /* FSM in S_SETTLE                 */
#define CLCDKVM_STATUS_GRANTING         (1u << 5)  /* FSM in S_GRANT                  */
#define CLCDKVM_STATUS_DRAINING         (1u << 6)  /* FSM in S_DRAIN                  */
#define CLCDKVM_STATUS_KVM_DRIVES_PADS  (1u << 7)  /* NEITHER source reaches the pads:
                                                    * the KVM is driving the idle
                                                    * pattern. Bytes pushed now are
                                                    * DISCARDED — do not stream.      */
#define CLCDKVM_STATUS_HARNESS_QUIET    (1u << 8)  /* live h_fifo_empty && !h_busy    */
#define CLCDKVM_STATUS_DUT_QUIET        (1u << 9)  /* live !dut_cs && !dut_busy       */
#define CLCDKVM_STATUS_DUT_REQ          (1u << 10) /* live tunnel req level           */
#define CLCDKVM_STATUS_PB_LEVEL         (1u << 11) /* DEBOUNCED USER_nPB1, 1=pressed  */
#define CLCDKVM_STATUS_PB_RAW           (1u << 12) /* undebounced, pressed sense      */
#define CLCDKVM_STATUS_DECOUPLED        (1u << 13) /* synchronised decouple_status    */
#define CLCDKVM_STATUS_RP_IN_RESET      (1u << 14) /* synchronised !rp_resetn         */
#define CLCDKVM_STATUS_INTERLOCK        (1u << 15) /* decoupled|rp_in_reset|force_harness*/
#define CLCDKVM_STATUS_STATE_SHIFT      16
#define CLCDKVM_STATUS_STATE_MASK       0x00070000u  /* [18:16] */

/* STATUS.state codes (fpga/shell/ip/clcd_kvm/README.md §7). 5-7 unreachable. */
#define CLCDKVM_STATE_OWN     0u
#define CLCDKVM_STATE_DRAIN   1u
#define CLCDKVM_STATE_RST     2u
#define CLCDKVM_STATE_SETTLE  3u
#define CLCDKVM_STATE_GRANT   4u

/* ---- EVENT @ 0x08 — RW1C, reset 0x0000_0000 -----------------------------
 * Sticky. Write 1 to clear a bit; write 0 to leave it set; READING DOES NOT
 * CLEAR (see the block note above).
 *
 * ===================== THE FIRMWARE HANDOVER RULE =========================
 * Re-initialise the panel and repaint EVERY cell whenever
 *     (EVENT.harness_gained | EVENT.panel_reset_done)
 * is set WHILE YOU OWN THE PANEL. Clear the bits with a W1C BEFORE you start
 * repainting, so a handover that races the repaint is not lost.
 *
 * harness_gained ALONE IS NOT SUFFICIENT. A DFX interlock (a partial
 * reconfiguration) firing mid-handover resets the panel and hands it back with
 * NO owner change — S_RST/S_SETTLE/S_GRANT are shared by the plain
 * CTRL.panel_rst_pulse path, where tgt_owner == owner. panel_reset_done fires
 * on EVERY completed reset sequence; harness_gained only on a real handover.
 * A gained-only rule leaves the harness painting into a panel whose GRAM,
 * window, MADCTL and pixel format were all just wiped. (README §5, §7.)
 * ========================================================================= */
#define CLCDKVM_EVENT_HARNESS_GAINED   (1u << 0)
#define CLCDKVM_EVENT_HARNESS_LOST     (1u << 1)
#define CLCDKVM_EVENT_DUT_GAINED       (1u << 2)
#define CLCDKVM_EVENT_DUT_LOST         (1u << 3)
#define CLCDKVM_EVENT_TIMEOUT_FIRED    (1u << 4)  /* a quiescence wait was preempted */
#define CLCDKVM_EVENT_FORCED_REVERT    (1u << 5)  /* the DFX interlock forced HARNESS */
#define CLCDKVM_EVENT_PB_TOGGLE        (1u << 6)  /* a debounced PB1 press was taken  */
#define CLCDKVM_EVENT_PANEL_RESET_DONE (1u << 7)  /* ANY reset sequence completed —
                                                   * INCLUDING one with no owner change*/
#define CLCDKVM_EVENT_ALL              0x000000FFu

/* The one rule above, as a mask. */
#define CLCDKVM_EVENT_REINIT_MASK \
    (CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE)

/* ---- PANEL_TMR @ 0x0C — RW (microseconds) ------------------------------- */
#define CLCDKVM_PANEL_TMR_RST_US_SHIFT     0
#define CLCDKVM_PANEL_TMR_RST_US_MASK      0x0000FFFFu  /* reset 2000 (2 ms)  */
#define CLCDKVM_PANEL_TMR_SETTLE_US_SHIFT  16
#define CLCDKVM_PANEL_TMR_SETTLE_US_MASK   0xFFFF0000u  /* reset 5000 (5 ms)  */

/* ---- TIMEOUT @ 0x10 / DEBOUNCE @ 0x14 — RW (microseconds) ---------------- */
#define CLCDKVM_TIMEOUT_US_MASK        0xFFFFFFFFu  /* reset 1000  (1 ms)  */
#define CLCDKVM_DEBOUNCE_US_MASK       0x0000FFFFu  /* reset 10000 (10 ms) */

/* ---- TUNNEL @ 0x18 — RO, no side effects, pure capture ------------------- */
#define CLCDKVM_TUNNEL_GPIO_O_SHIFT    0
#define CLCDKVM_TUNNEL_GPIO_O_MASK     0x0000FFFFu  /* [15:8] = the DUT's PD[7:0] */
#define CLCDKVM_TUNNEL_GPIO_OE_SHIFT   16
#define CLCDKVM_TUNNEL_GPIO_OE_MASK    0xFFFF0000u  /* [31:24] = ctrl/status byte */

#ifdef __cplusplus
}
#endif

#endif /* MPS3_PLATFORM_REGS_H */
