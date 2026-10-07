/*
 * clcd_regs.h — CLCD 8080 byte-stream master register contract, transcribed
 * from the main repo's firmware/common/platform_regs.h:545-572 (read-only) and
 * board-confirmed by docs/CLCD_PANEL_FACTS.md (§6/§8, panel lit 2026-07-14).
 *
 * Base on shell_linux: 0x44AC_0000, 64 KiB page, DT node clcd@44ac0000
 * ("soclabs,clcd-8080-1.0","generic-uio") — src/linux_harness/shell_linux.dts.
 * Reads have NO side effects at any offset (deliberate, regmap contract).
 */
#ifndef MPS3_APPS_CLCD_REGS_H
#define MPS3_APPS_CLCD_REGS_H

#define CLCD_CTRL    0x00u
#define CLCD_CMD     0x04u  /* W — push a COMMAND byte (RS=0)               */
#define CLCD_DATA    0x08u  /* W — push a DATA byte    (RS=1)               */
#define CLCD_STATUS  0x0Cu  /* RO                                           */
#define CLCD_READ    0x10u  /* RO — READ_PATH=0 on every shipped shell: 0   */
#define CLCD_TIMING  0x14u

#define CLCD_CTRL_ENABLE      (1u << 0)
#define CLCD_CTRL_BACKLIGHT   (1u << 1)  /* CLCD_BL — ACTIVE-HIGH (proven)   */
#define CLCD_CTRL_RESET_N     (1u << 2)  /* CLCD_RST — 0 = held in reset     */
#define CLCD_CTRL_FIFO_RESET  (1u << 3)  /* self-clearing                    */
#define CLCD_CTRL_READ_START  (1u << 4)  /* no-op while READ_PATH=0          */

#define CLCD_STATUS_FIFO_FULL   (1u << 0)  /* writes DROPPED while set —     */
#define CLCD_STATUS_FIFO_EMPTY  (1u << 1)  /* the writer must poll, never    */
#define CLCD_STATUS_BUSY        (1u << 2)  /* rely on back-pressure          */
#define CLCD_STATUS_LEVEL_SHIFT 8
#define CLCD_STATUS_LEVEL_MASK  0x0000FF00u

#define CLCD_TIMING_WR_LO_SHIFT     0
#define CLCD_TIMING_WR_HI_SHIFT     8
#define CLCD_TIMING_CS_SETUP_SHIFT  16

/* The proven 8080 strobe timing (CLCD_PANEL_FACTS.md §6): wr_lo=4, wr_hi=4,
 * cs_setup=2 s_axi_aclk cycles @100 MHz — the RTL reset defaults, re-written
 * at every panel reset exactly as the bare-metal driver does. */
#define CLCD_TIMING_PROVEN \
    ((4u << CLCD_TIMING_WR_LO_SHIFT) | \
     (4u << CLCD_TIMING_WR_HI_SHIFT) | \
     (2u << CLCD_TIMING_CS_SETUP_SHIFT))

#define CLCD_FIFO_DEPTH 128u   /* {RS,byte} entries (clcd.sv:55)            */

#endif /* MPS3_APPS_CLCD_REGS_H */
