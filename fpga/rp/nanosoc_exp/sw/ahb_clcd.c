/*
 * ahb_clcd.c -- the DUT-side driver for the reference accelerator `ahb_clcd`.
 * ===========================================================================
 * THIS IS TEACHING CODE. Read it as the worked example of driving your own
 * accelerator in the nanosoc expansion socket (fpga/rp/nanosoc_exp/). It runs on
 * nanosoc's Cortex-M0 and puts a picture on the on-board panel -- press
 * USER_nPB[1] on the board and this picture replaces the shell's status screen.
 * ===========================================================================
 *
 * WHAT IT DOES, in one paragraph: it programs the block's 8080 bus timing, streams
 * the Himax HX8347-D initialisation sequence to the panel, and fills the screen.
 * Then it does the WHOLE thing again, forever. The interesting part is WHY it
 * repeats -- read section 3 below before you "optimise" the re-init away.
 *
 * The panel's actual register values (the init sequence, the pixel format, the
 * GRAM window opcodes) are NOT in this file and NOT in the RTL -- they are the
 * SAME board-proven data table the shell driver uses,
 * firmware/clcd/hx8347_init.c, with its provenance record in
 * firmware/clcd/PANEL_PROVENANCE.md. Reusing it means your accelerator inherits a
 * table that is already lighting the panel on the bench; do NOT transcribe a new
 * one from a datasheet. Your block is protocol-agnostic: it knows 8080 bus cycles
 * and nothing about Himax registers.
 *
 * Contract: fpga/rp/nanosoc_exp/README.md (the socket) and
 * docs/contracts/dut-display-tunnel.md (how the bytes reach the panel).
 */
#include <stdint.h>

#include "ahb_clcd.h"

/* The init table seam, shared verbatim with the shell driver. The DUT firmware
 * build links firmware/clcd/hx8347_init.c (a bus-agnostic {op,val} array) and
 * puts firmware/clcd/ on the include path. HX_CMD/HX_DAT/HX_DLY are its opcodes. */
#include "hx8347_init.h"

/* GRAM addressing (the runtime window + write opcodes -- panel facts, the same
 * ones the shell renderer emits, firmware/clcd/clcd.c). Command byte, then data. */
#define HX_REG_COL_START_HI 0x02u
#define HX_REG_COL_START_LO 0x03u
#define HX_REG_COL_END_HI   0x04u
#define HX_REG_COL_END_LO   0x05u
#define HX_REG_ROW_START_HI 0x06u
#define HX_REG_ROW_START_LO 0x07u
#define HX_REG_ROW_END_HI   0x08u
#define HX_REG_ROW_END_LO   0x09u
#define HX_REG_RAMWR        0x22u

/* ==========================================================================
 * 0. The bus -- four register accessors. This is the ENTIRE hardware interface.
 *
 * A load/store to EXP_BASE+off is a single AHB-Lite transfer to your block. On a
 * Cortex-M0 these compile to LDR/STR; the bus matrix routes them to `exp_*` and
 * your slave's `hreadyout` completes them (README §4 -- the one rule).
 * ========================================================================== */
static inline void     reg_wr(uint32_t off, uint32_t v) { *(volatile uint32_t *)(uintptr_t)(EXP_BASE + off) = v; }
static inline uint32_t reg_rd(uint32_t off)             { return *(volatile uint32_t *)(uintptr_t)(EXP_BASE + off); }

/* ==========================================================================
 * 1. Pushing one {RS,byte} to the panel.
 *
 * RS=0 => the byte is a COMMAND (a register index); RS=1 => it is DATA (a
 * register value, or a pixel byte). That two-signal stream is the whole 8080
 * protocol from your side -- the panel's on-chip frame memory (GRAM) IS the
 * framebuffer, so you never need a BRAM, video timing or a DMA engine.
 *
 * Writes into a full FIFO are DROPPED, not stalled (README §4 -- back-pressuring
 * the bus on a FIFO deadlocks the CPU). So we POLL fifo_full and spin here until
 * there is room. That spin is safe: the block drains autonomously at ~480 ns/byte
 * whether or not the CPU is looking, so fifo_full always clears on its own.
 * ========================================================================== */
static void push(unsigned rs, uint8_t byte)
{
    while (reg_rd(AHB_CLCD_STATUS) & AHB_CLCD_STATUS_FIFO_FULL)
        ; /* wait for room -- the FIFO drains on its own, so this always ends */
    reg_wr(rs ? AHB_CLCD_DATA : AHB_CLCD_CMD, byte);
}

static void cmd(uint8_t c)  { push(0u, c); }   /* a register index */
static void dat(uint8_t d)  { push(1u, d); }   /* a datum / pixel byte */

/* A rough busy-wait, in loop iterations. The init table's HX_DLY entries are in
 * MILLISECONDS; on this bare-metal DUT we may block (unlike the shell's
 * cooperative driver, which must never stall its network superloop). This does
 * not need to be accurate -- the panel's minimums are microseconds; a few ms of
 * slop is harmless. Tune LOOPS_PER_MS to your nanosoc clock if you care. */
#define LOOPS_PER_MS 5000u
static void delay_ms(unsigned ms)
{
    volatile unsigned n = ms * LOOPS_PER_MS;
    while (n--) { /* spin */ }
}

/* ==========================================================================
 * 2. Bring the panel up: program the bus timing, then stream the init table.
 *
 * We do NOT reset the panel here, and we CANNOT: CLCD_BL and CLCD_RST are owned
 * by the shell's KVM and there are no reset bits in the tunnel by design (that is
 * what makes a hung DUT always recoverable). We do not need them -- the KVM
 * hard-resets the panel for us on every handover (see section 3).
 * ========================================================================== */
static void panel_init(void)
{
    /* Sane 8080 timing (satisfies the CDC floor with margin) + enable + flush. */
    reg_wr(AHB_CLCD_TIMING, AHB_CLCD_TIMING_DEFAULT);
    reg_wr(AHB_CLCD_CTRL, AHB_CLCD_CTRL_ENABLE | AHB_CLCD_CTRL_FIFO_RESET);

    /* Stream the shared, board-proven HX8347-D init sequence. We just walk the
     * table: HX_CMD -> a command byte, HX_DAT -> a data byte, HX_DLY -> wait. We
     * never inspect the values -- that is the whole point of the protocol/panel
     * split (README §7). */
    for (unsigned i = 0; i < hx8347_init_len; i++) {
        uint8_t op  = hx8347_init[i].op;
        uint8_t val = hx8347_init[i].val;
        if      (op == HX_CMD) cmd(val);
        else if (op == HX_DAT) dat(val);
        else if (op == HX_DLY) delay_ms(val);
    }
}

/* Set the GRAM address window (col x0..x1, row y0..y1) then open it for writing.
 * The panel auto-increments through the window as you push pixel bytes, so a fill
 * is: set the window, RAMWR, then stream W*H pixels. */
static void set_window(unsigned x0, unsigned y0, unsigned x1, unsigned y1)
{
    cmd(HX_REG_COL_START_HI); dat((uint8_t)(x0 >> 8)); cmd(HX_REG_COL_START_LO); dat((uint8_t)x0);
    cmd(HX_REG_COL_END_HI);   dat((uint8_t)(x1 >> 8)); cmd(HX_REG_COL_END_LO);   dat((uint8_t)x1);
    cmd(HX_REG_ROW_START_HI); dat((uint8_t)(y0 >> 8)); cmd(HX_REG_ROW_START_LO); dat((uint8_t)y0);
    cmd(HX_REG_ROW_END_HI);   dat((uint8_t)(y1 >> 8)); cmd(HX_REG_ROW_END_LO);   dat((uint8_t)y1);
    cmd(HX_REG_RAMWR);        /* GRAM write; pixels follow as DATA bytes */
}

/* Fill an axis-aligned box with one RGB565 colour. RGB565 is sent HIGH byte
 * first (firmware/clcd/clcd.c, Himax DS Fig 5.22). */
static void fill_rect(unsigned x, unsigned y, unsigned w, unsigned h, uint16_t colour)
{
    set_window(x, y, x + w - 1u, y + h - 1u);
    for (unsigned i = 0; i < w * h; i++) {
        dat((uint8_t)(colour >> 8));
        dat((uint8_t)(colour & 0xFFu));
    }
}

/* Draw one recognisable frame: a blue field with three primary-colour bars. The
 * bars double as the RGB-vs-BGR bench check (CLCD_PANEL_FACTS.md §7.1 -- the one
 * open colour question): if the "RED" bar shows blue, the channels are swapped. */
static void draw_demo_frame(void)
{
    fill_rect(0, 0, AHB_CLCD_WIDTH, AHB_CLCD_HEIGHT, AHB_CLCD_RGB565(0, 0, 40));   /* dim blue field */
    fill_rect(40,  60, 60, 120, AHB_CLCD_RGB565(255, 0,   0));            /* RED   */
    fill_rect(130, 60, 60, 120, AHB_CLCD_RGB565(0,   255, 0));            /* GREEN */
    fill_rect(220, 60, 60, 120, AHB_CLCD_RGB565(0,   0,   255));          /* BLUE  */
}

/* ==========================================================================
 * 3. The main loop -- and WHY it re-initialises every time round.
 *
 * There is NO grant signal in v1: the shell never tells the DUT that it owns the
 * panel (dut_gpio_i[15:0] is fully allocated -- README §6 / tunnel §6). So this
 * driver CANNOT wait to be told "you have the panel now". Instead it just draws,
 * unconditionally, on a periodic loop -- and it re-runs the FULL init sequence
 * each pass, NOT once at boot.
 *
 * The reason is subtle and load-bearing: EVERY handover HARD-RESETS THE PANEL
 * (the KVM must, because the panel IS the framebuffer and its state -- GRAM,
 * window, MADCTL, pixel format -- is not shared between the two owners). So the
 * moment the button hands the panel to us, its configuration is GONE. If we had
 * initialised once at boot and only redrawn, we would be streaming pixels into an
 * un-configured, freshly-reset panel and the screen would stay blank.
 *
 * Re-initialising every pass converges with NO grant wire and NO state to
 * reconcile: whether or not we were ever granted the panel, whether or not we
 * noticed, one loop iteration after we get it the panel is correctly configured
 * and painted. Bytes we push while the harness still owns the panel are silently
 * discarded by the KVM -- they cost a few bus cycles and nothing else.
 *
 * >>> DO NOT "optimise" the re-init out of the loop. <<< The next reader will be
 * tempted (it looks wasteful). But move panel_init() to before the loop and the
 * DUT's screen stays BLANK after the very first handover, on silicon only, in a
 * way no simulation of this block alone will reproduce.
 * ========================================================================== */
void ahb_clcd_demo(void)
{
    /* Politely ask for the panel. Harmless if ignored: it is only a request
     * source when the harness has set CLCDKVM.CTRL.dut_req_en (off by default),
     * and the USER_nPB[1] button works regardless -- so never DEPEND on this. */
    reg_wr(AHB_CLCD_CTRL, AHB_CLCD_CTRL_ENABLE | AHB_CLCD_CTRL_REQ);

    for (;;) {
        panel_init();        /* re-configure the (possibly just-reset) panel */
        draw_demo_frame();   /* ...and repaint it */
        /* Loop straight back: a full 320x240 repaint at ~2 MB/s is ~75 ms, which
         * is the whole refresh period -- no explicit delay is needed. */
    }
}
