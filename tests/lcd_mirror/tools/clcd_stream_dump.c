/*
 * clcd_stream_dump.c -- record the EXACT 8080 byte stream the harness's
 * firmware/clcd/clcd.c pushes into the CLCD block, scenario by scenario, plus
 * the cell shadow (what is on the glass) at the end of each scenario.
 *
 * It is the "host tool linking clcd.c with mock registers" of
 * docs/planning/linux_lanes/LCD_MIRROR_FPGA.md §8.3: the bench
 * (tests/lcd_mirror/harness_stream.py) replays the recorded stream through the
 * RTL snooper and checks the frame buffer against BOTH the golden model and an
 * INDEPENDENT oracle -- the shadow's characters font-rendered in Python -- so
 * neither the model nor the RTL can pass its own mistake.
 *
 * Nothing in firmware/ is copied or modified: this links the real clcd.c,
 * hx8347_init.c and firmware/test/mock_regs.c, with the same defines and the
 * same fake-symbol pattern as firmware/test/test_clcd.c and
 * firmware/clcd/tools/clcd_preview.c.
 *
 * Build (tests/lcd_mirror/Makefile `stream`, or by hand from the repo root):
 *   gcc -std=c11 -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS \
 *       -Ifirmware/common -Ifirmware/test \
 *       tests/lcd_mirror/tools/clcd_stream_dump.c firmware/clcd/clcd.c \
 *       firmware/clcd/hx8347_init.c firmware/test/mock_regs.c -o clcd_stream_dump
 *
 * Output (stdout), line oriented:
 *   # ...                      comment
 *   SCEN <name>                a scenario begins
 *   CTRL <hex32>               a CLCD CTRL write (bit 1 backlight, bit 2 reset_n)
 *   X <tok> <tok> ...          pushed bytes, each token = RS digit + 2 hex digits
 *   SHADOW <name>              the cell shadow follows (15 rows)
 *   ROW <r> <inv> <hex80>      40 cells as hex chars
 *   END
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "platform_regs.h"
#include "net_proto.h"
#include "diag.h"
#include "../../../firmware/coordinator/coordinator.h"
#include "../../../firmware/coordinator/swap_fsm.h"
#include "mock_regs.h"
#include "../../../firmware/clcd/clcd.h"

/* === symbols clcd.c links against (the firmware/test/ fake pattern) ========= */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;

static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }

static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }

static int      s_link_up;
static uint16_t s_anlpar;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *val_out)
{ if (val_out) *val_out = (reg == 0x05u) ? s_anlpar : 0u; return 0; }

static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

/* === the CLCD block: an infinite FIFO that logs every push =================
 * fifo_full is never set, so the driver pushes its full per-pass budget; the
 * byte ORDER is what matters, not the pacing (the pad BFM re-times it). */
static unsigned s_tok_on_line;

static void flush_line(void)
{
    if (s_tok_on_line) { putchar('\n'); s_tok_on_line = 0; }
}

static int clcd_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (!is_write) {
        *val = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u;
        return 1;
    }
    if (off == CLCD_CMD || off == CLCD_DATA) {
        if (s_tok_on_line == 0) fputs("X", stdout);
        printf(" %u%02x", (off == CLCD_DATA) ? 1u : 0u, (unsigned)(*val & 0xFFu));
        if (++s_tok_on_line == 64) flush_line();
        return 1;
    }
    if (off == CLCD_CTRL) {
        flush_line();
        printf("CTRL %08x\n", (unsigned)*val);
        return 1;
    }
    return 1;   /* TIMING etc.: consumed */
}

static void seed_rm_live(uint32_t rm_id)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     rm_id);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
}

static void seed_healthy(void)
{
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id     = 0x72BB0A36u;
    g_shell_state.current_rm_id = 0x01000001u;
    seed_rm_live(0x01000001u);
    s_icap_bytes = 1835072u;
    s_link_up = 1;
    s_anlpar  = (1u << 8);
    s_swap_res.valid = 1; s_swap_res.ok = 1; s_swap_res.verified = 1;
    s_swap_res.rm_id = 0x01000001u;
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
}

/* Poll until the driver is IDLE with nothing dirty and has pushed nothing for
 * a stretch of passes (so a render that was triggered has fully drained). */
static void settle(uint32_t dt_ms, unsigned max_passes)
{
    unsigned quiet = 0;
    for (unsigned i = 0; i < max_passes; i++) {
        mock_time_advance_ms(dt_ms);
        clcd_poll();
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0 &&
            clcd_test_bytes_last_pass() == 0) {
            if (++quiet >= 4) break;
        } else {
            quiet = 0;
        }
    }
    flush_line();
}

static void dump_shadow(const char *name)
{
    char grid[CLCD_NCELLS];
    uint8_t inv[CLCD_ROWS];
    clcd_test_shadow(grid, inv);
    printf("SHADOW %s\n", name);
    for (unsigned r = 0; r < CLCD_ROWS; r++) {
        printf("ROW %u %u ", r, inv[r] ? 1u : 0u);
        for (unsigned c = 0; c < CLCD_COLS; c++)
            printf("%02x", (unsigned)(unsigned char)grid[r * CLCD_COLS + c]);
        putchar('\n');
    }
}

int main(void)
{
    mock_regs_reset();
    seed_healthy();
    mock_regs_set_hook(MPS3_CLCD_BASE, clcd_hook, 0);
    mock_time_set_ms(171907000u);             /* uptime 001:23:45:07 */

    printf("# clcd_stream_dump v1 -- firmware/clcd/clcd.c through mock registers\n");

    /* 1. boot: reset pulse, the whole init table, then the full first frame. */
    printf("SCEN boot\n");
    clcd_init();
    settle(1, 200000);
    dump_shadow("boot");

    /* 2. the 1 Hz uptime tick: an incremental redraw of the changed cells. */
    printf("SCEN tick\n");
    mock_time_advance_ms(1000u);
    settle(1, 20000);
    dump_shadow("tick");

    /* 3. the Applications & Ports page. */
    printf("SCEN apps\n");
    clcd_page_set(CLCD_PAGE_APPS);
    settle(1, 20000);
    dump_shadow("apps");

    /* 4. back to the status page with the network link DOWN: the red
     *    inverted error banner rows. */
    printf("SCEN linkdown\n");
    clcd_page_set(CLCD_PAGE_STATUS);
    s_link_up = 0;
    s_anlpar  = 0;
    settle(1, 20000);
    dump_shadow("linkdown");

    /* 5. the KVM handover OSD banner (clcd_lose), the last harness frame
     *    before the DUT takes the panel. */
    printf("SCEN banner\n");
    s_link_up = 1;
    s_anlpar  = (1u << 8);
    clcd_lose();
    settle(1, 20000);
    dump_shadow("banner");

    printf("END\n");
    return 0;
}
