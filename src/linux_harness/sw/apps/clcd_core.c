/*
 * clcd_core.c — Linux port of firmware/clcd/clcd.c (see clcd_core.h for the
 * port contract and the documented deviations). The wire discipline — init
 * table order, 273-byte cell stream, FIFO-full poll before every write,
 * bounded per-pass budget — is byte-for-byte the silicon-proven driver's.
 */
#include <stdint.h>
#include <string.h>

#include "clcd_core.h"
#include "clcd_regs.h"
#include "font8x16.h"
#include "hx8347_init.h"

/* ---- RGB565 colours (proven set: white-on-black, red banner) ------------- */
#define CLCD_RGB565_WHITE 0xFFFFu
#define CLCD_RGB565_BLACK 0x0000u
#define CLCD_RGB565_RED   0xF800u

/* ---- HX8347-D GRAM addressing (board-proven since 2026-07-14) ------------ */
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
 * State
 * ========================================================================== */
static clcd_io_t     s_io;
static int           s_state;
static uint64_t      s_t0;
static unsigned      s_init_idx;
static uint32_t      s_delay_ms;
static uint64_t      s_last_refresh;
static int           s_force_refresh;
static unsigned      s_refresh_tick;

static char    s_shadow[CLCD_NCELLS];
static uint8_t s_inv[CLCD_ROWS];
static uint8_t s_dirty[CLCD_DIRTY_BYTES];
static unsigned s_dirty_count;

static uint8_t  s_cell_val[CLCD_CELL_BYTES];
static uint8_t  s_cell_rs[CLCD_CELL_BYTES];
static unsigned s_cell_len;
static unsigned s_cell_pos;

static clcd_status_t s_status;
static uint32_t s_bytes_last_pass;
static uint64_t s_bytes_total;

/* ==========================================================================
 * Small formatting helpers (kept libc-free like the origin, so behaviour is
 * identical and the pure functions stay trivially portable)
 * ========================================================================== */
static const char HEXU[] = "0123456789ABCDEF";

static char *ap_str(char *p, const char *s)
{
    while (*s) *p++ = *s++;
    return p;
}
static char *ap_u32(char *p, uint32_t v)
{
    char tmp[10];
    int n = 0;
    if (v == 0) { *p++ = '0'; return p; }
    while (v) { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n) *p++ = tmp[--n];
    return p;
}
static char *ap_u64(char *p, uint64_t v)
{
    char tmp[20];
    int n = 0;
    if (v == 0) { *p++ = '0'; return p; }
    while (v) { tmp[n++] = (char)('0' + (unsigned)(v % 10u)); v /= 10u; }
    while (n) *p++ = tmp[--n];
    return p;
}
static char *ap_hex2(char *p, uint8_t b)
{
    *p++ = HEXU[(b >> 4) & 0xF];
    *p++ = HEXU[b & 0xF];
    return p;
}

/* ==========================================================================
 * Pure formatters — ported verbatim (uptime widened to u64: Linux boards
 * outlive the 32-bit 49.7-day wrap; days clamp at 999 on the glass)
 * ========================================================================== */
void clcd_fmt_uptime(char *out, uint64_t ms)
{
    uint64_t sec  = ms / 1000u;
    uint64_t days = sec / 86400u; sec %= 86400u;
    uint32_t hh   = (uint32_t)(sec / 3600u);  sec %= 3600u;
    uint32_t mm   = (uint32_t)(sec / 60u);
    uint32_t ss   = (uint32_t)(sec % 60u);
    if (days > 999u) days = 999u;
    out[0]  = (char)('0' + (unsigned)((days / 100u) % 10u));
    out[1]  = (char)('0' + (unsigned)((days / 10u) % 10u));
    out[2]  = (char)('0' + (unsigned)(days % 10u));
    out[3]  = ':';
    out[4]  = (char)('0' + (hh / 10u) % 10u);
    out[5]  = (char)('0' + hh % 10u);
    out[6]  = ':';
    out[7]  = (char)('0' + (mm / 10u) % 10u);
    out[8]  = (char)('0' + mm % 10u);
    out[9]  = ':';
    out[10] = (char)('0' + (ss / 10u) % 10u);
    out[11] = (char)('0' + ss % 10u);
    out[12] = '\0';
}

void clcd_fmt_hex32(char *out, uint32_t v)
{
    out[0] = '0';
    out[1] = 'x';
    for (int i = 0; i < 8; i++)
        out[2 + i] = HEXU[(v >> ((7 - i) * 4)) & 0xF];
    out[10] = '\0';
}

/* rm_id -> name, keyed on the DESIGN half — table identical to the firmware's
 * (clcd.c:209-233). The tests re-assert the <= CLCD_RM_NAME_MAX bound over all
 * 65536 design ids, same discipline as firmware/test/test_clcd.c. */
const char *clcd_rm_name(uint32_t rm_id)
{
    switch (CLCD_RM_DESIGN(rm_id)) {
    case 0x0000u: return "greybox";
    case 0x0001u: return "nanosoc";
    case 0x0002u: return "eth_ss";
    case 0x0003u: return "nanosoc_multicore";
    case 0x0004u: return "uart_echo";
    case 0x001Eu: return "led";
    case 0x00A1u: return "regdemo_a";
    case 0x00B2u: return "regdemo_b";
    default: {
        static char b[16];
        char *p = b;
        *p++ = 'r'; *p++ = 'm'; *p++ = '?';
        for (int i = 0; i < 8; i++)
            *p++ = HEXU[(rm_id >> ((7 - i) * 4)) & 0xF];
        *p = '\0';
        return b;
    }
    }
}

const char *clcd_rm_caps(uint32_t rm_id)
{
    switch (CLCD_RM_DESIGN(rm_id)) {
    case 0x0000u: return "greybox: empty tie-off";
    case 0x0001u: return "1x Cortex-M0  no ETH  1x UART";
    case 0x0002u: return "no CPU  ETH MAC+PTP  no UART";
    case 0x0003u: return "2x CPU  ETH MAC+PTP  2x UART";
    case 0x0004u: return "no CPU  1x UART (echo)";
    case 0x001Eu: return "no CPU  LED demo";
    case 0x00A1u: return "no CPU  AHB register demo";
    case 0x00B2u: return "no CPU  AHB register demo";
    default:      return "design unknown";
    }
}

void clcd_fmt_rm_version(char *out, uint32_t rm_id)
{
    char *p = out;
    *p++ = 'v';
    p = ap_u32(p, CLCD_RM_VER_MAJOR(rm_id));
    *p++ = '.';
    p = ap_u32(p, CLCD_RM_VER_MINOR(rm_id));
    *p = '\0';
}

/* Frozen swap-state numbering -> short display names (fit "SWAP: " + 20). */
const char *clcd_swap_state_name(int state)
{
    switch (state) {
    case CLCD_SWAP_IDLE:                    return "IDLE";
    case CLCD_SWAP_GATE:                    return "GATE";
    case CLCD_SWAP_DECOUPLE_ASSERT:         return "DECOUPLE";
    case CLCD_SWAP_STREAM_CLEARING:         return "STREAM-CLR";
    case CLCD_SWAP_AWAIT_INCOMING_CLEARING: return "AWAIT-CLR";
    case CLCD_SWAP_AWAIT_PARTIAL:           return "AWAIT-PART";
    case CLCD_SWAP_STREAM_PARTIAL:          return "STREAM-PART";
    case CLCD_SWAP_VERIFY:                  return "VERIFY";
    case CLCD_SWAP_CACHE_CLEARING:          return "CACHE-CLR";
    case CLCD_SWAP_RELEASE:                 return "RELEASE";
    case CLCD_SWAP_DONE:                    return "DONE";
    case CLCD_SWAP_FAILED:                  return "FAILED";
    case CLCD_SWAP_REISOLATE:               return "REISOLATE";
    default:                                return "?";
    }
}

/* ==========================================================================
 * Shadow diff — ported verbatim
 * ========================================================================== */
unsigned clcd_diff_cells(const char *cur, const char *next,
                         const uint8_t *cur_inv, const uint8_t *next_inv,
                         uint8_t *dirty)
{
    unsigned n = 0;
    for (unsigned i = 0; i < CLCD_NCELLS; i++) {
        unsigned row = i / CLCD_COLS;
        int changed = (cur[i] != next[i]);
        if (cur_inv && next_inv && cur_inv[row] != next_inv[row])
            changed = 1;
        if (changed) {
            uint8_t bit = (uint8_t)(1u << (i & 7u));
            if (!(dirty[i >> 3] & bit)) {
                dirty[i >> 3] |= bit;
                n++;
            }
        }
    }
    return n;
}

/* ==========================================================================
 * Frame builder — PURE. The firmware §8 layout with the Linux deviations
 * documented in clcd_core.h.
 * ========================================================================== */
static void row_fill(char *next, unsigned r, char ch)
{
    memset(next + r * CLCD_COLS, ch, CLCD_COLS);
}
static void put_at(char *next, unsigned r, unsigned c, const char *s)
{
    unsigned i = c;
    while (*s && i < CLCD_COLS) {
        next[r * CLCD_COLS + i] = *s++;
        i++;
    }
}

void clcd_build_frame(const clcd_status_t *st, unsigned tick,
                      char out[], uint8_t inv_out[])
{
    char scratch[64];
    char *p;

    memset(out, ' ', CLCD_NCELLS);
    memset(inv_out, 0, CLCD_ROWS);

    /* Row 0: board name + platform ---------------------------------------- */
    put_at(out, 0, 0, st->board_name);
    put_at(out, 0, 20, "nanoSoC harness");

    /* Row 1: rule ----------------------------------------------------------*/
    row_fill(out, 1, '-');

    /* Row 2: resident RM. Linux source = mps3_dfx sysfs (last VERIFIED id);
     * never guess when the driver is absent or the id is unqualified. */
    put_at(out, 2, 0, "DUT : ");
    if (!st->dfx_present) {
        put_at(out, 2, CLCD_RM_NAME_COL, "NO DFX DRIVER");
    } else if (st->rm_id_valid) {
        put_at(out, 2, CLCD_RM_NAME_COL, clcd_rm_name(st->rm_id));
        clcd_fmt_rm_version(scratch, st->rm_id);
        put_at(out, 2, CLCD_RM_VER_COL, scratch);
    } else {
        put_at(out, 2, CLCD_RM_NAME_COL, "RM ID NOT VALID");
    }

    /* Row 3: swap engine state --------------------------------------------- */
    if (!st->dfx_present) {
        put_at(out, 3, 0, "SWAP: no dfx driver");
    } else {
        p = ap_str(scratch, "SWAP: ");
        p = ap_str(p, clcd_swap_state_name(st->swap_state));
        *p = '\0';
        put_at(out, 3, 0, scratch);
        if (st->swap_state == CLCD_SWAP_DONE)
            put_at(out, 3, 32, "last OK");
        else if (st->swap_state == CLCD_SWAP_FAILED ||
                 st->swap_state == CLCD_SWAP_REISOLATE)
            put_at(out, 3, 32, "last ERR");
    }

    /* Row 4: static_id ------------------------------------------------------*/
    p = ap_str(scratch, "SID : ");
    clcd_fmt_hex32(p, st->static_id);
    put_at(out, 4, 0, scratch);

    /* Row 5: IP + link/speed/duplex ------------------------------------------*/
    put_at(out, 5, 0, "NET : ");
    {
        p = scratch;
        p = ap_str(p, st->ip[0] ? st->ip : "no address");
        if (st->link_up == 1) {
            p = ap_str(p, "  UP ");
            if (st->speed_mbps > 0)
                p = ap_u32(p, (uint32_t)st->speed_mbps);
            else
                p = ap_str(p, "?");
            *p++ = '/';
            p = ap_str(p, st->full_duplex == 1 ? "FD" :
                          st->full_duplex == 0 ? "HD" : "??");
        } else if (st->link_up == 0) {
            p = ap_str(p, "  DOWN");
        } else {
            p = ap_str(p, "  NO NETDEV");
        }
        *p = '\0';
        put_at(out, 5, 6, scratch);
    }

    /* Row 6: uptime -----------------------------------------------------------*/
    put_at(out, 6, 0, "UP  : ");
    clcd_fmt_uptime(scratch, st->uptime_ms);
    put_at(out, 6, 6, scratch);

    /* Row 7: kernel identity (Linux deviation — see clcd_core.h) --------------*/
    put_at(out, 7, 0, "OS  : Linux ");
    put_at(out, 7, 12, st->kernel);

    /* Row 8: ICAP bytes + honest substitute counters ---------------------------*/
    p = ap_str(scratch, "ICAP: ");
    if (st->dfx_present)
        p = ap_u64(p, st->icap_bytes);
    else
        p = ap_str(p, "-");
    p = ap_str(p, " B  rxdrop ");
    p = ap_u64(p, st->rx_dropped);
    p = ap_str(p, " txerr ");
    p = ap_u64(p, st->tx_errors);
    *p = '\0';
    put_at(out, 8, 0, scratch);

    /* Row 9: loaded-design makeup ------------------------------------------------*/
    put_at(out, 9, 0, "CFG : ");
    if (!st->dfx_present)
        put_at(out, 9, 6, CLCD_RM_CAPS_NODRIVER);
    else if (st->rm_id_valid)
        put_at(out, 9, 6, clcd_rm_caps(st->rm_id));
    else
        put_at(out, 9, 6, CLCD_RM_CAPS_UNKNOWN);

    /* Rows 10-12: error banner (highest diagnostic value; hidden when healthy) */
    {
        const char *banner = 0;
        if (st->link_up == 0)          banner = "NETWORK LINK DOWN";
        else if (st->dfx_present &&
                 (st->swap_state == CLCD_SWAP_FAILED ||
                  st->swap_state == CLCD_SWAP_REISOLATE))
                                       banner = "LAST SWAP FAILED - RM NOT LOADED";
        else if (st->static_id == 0)   banner = "STATIC_ID NOT PROVISIONED";

        if (banner) {
            unsigned len = (unsigned)strlen(banner);
            unsigned col = (len < CLCD_COLS) ? (CLCD_COLS - len) / 2u : 0u;
            row_fill(out, 10, ' ');
            row_fill(out, 11, ' ');
            row_fill(out, 12, ' ');
            put_at(out, 11, col, banner);
            inv_out[10] = inv_out[11] = inv_out[12] = 1;
        }
    }

    /* Row 13: rule -----------------------------------------------------------*/
    row_fill(out, 13, '-');

    /* Row 14: MAC + heartbeat spinner ------------------------------------------*/
    if (st->mac_valid) {
        p = ap_str(scratch, "MAC ");
        for (int i = 0; i < 6; i++) {
            p = ap_hex2(p, st->mac[i]);
            if (i < 5) *p++ = ':';
        }
        *p = '\0';
        put_at(out, 14, 0, scratch);
    } else {
        put_at(out, 14, 0, "MAC --:--:--:--:--:--");
    }
    {
        static const char spin[4] = { '|', '/', '-', '\\' };
        char hb[5];
        hb[0] = 'h'; hb[1] = 'b'; hb[2] = ' ';
        hb[3] = spin[tick & 3u];
        hb[4] = '\0';
        put_at(out, 14, 34, hb);
    }
}

/* ==========================================================================
 * FIFO push — the single choke point (never writes into a full FIFO)
 * ========================================================================== */
static int try_push(uint32_t *budget, int is_data, uint8_t val)
{
    if (*budget == 0)
        return 0;
    uint32_t st = s_io.read32(s_io.ctx, CLCD_STATUS);
    if (st & CLCD_STATUS_FIFO_FULL)
        return -1;
    s_io.write32(s_io.ctx, is_data ? CLCD_DATA : CLCD_CMD, (uint32_t)val);
    (*budget)--;
    s_bytes_total++;
    return 1;
}

/* ==========================================================================
 * Cell expansion — ported verbatim (orientation-independent window math;
 * do NOT mirror coordinates, see hx8347_init.h)
 * ========================================================================== */
static void push_pair(unsigned *n, int rs, uint8_t val)
{
    s_cell_rs[*n]  = (uint8_t)rs;
    s_cell_val[*n] = val;
    (*n)++;
}

static void build_cell(unsigned cell)
{
    unsigned r = cell / CLCD_COLS;
    unsigned c = cell % CLCD_COLS;
    unsigned x0 = c * CLCD_GLYPH_W, x1 = x0 + CLCD_GLYPH_W - 1u;
    unsigned y0 = r * CLCD_GLYPH_H, y1 = y0 + CLCD_GLYPH_H - 1u;

    unsigned ch = (unsigned char)s_shadow[cell];
    if (ch < CLCD_FONT_FIRST || ch > CLCD_FONT_LAST)
        ch = (unsigned)' ';
    const uint8_t *glyph = font8x16[ch - CLCD_FONT_FIRST];

    int inv = s_inv[r] ? 1 : 0;
    uint16_t fg = CLCD_RGB565_WHITE;
    uint16_t bg = inv ? CLCD_RGB565_RED : CLCD_RGB565_BLACK;

    unsigned n = 0;
    push_pair(&n, 0, HX_REG_COL_START_HI); push_pair(&n, 1, (uint8_t)(x0 >> 8));
    push_pair(&n, 0, HX_REG_COL_START_LO); push_pair(&n, 1, (uint8_t)(x0 & 0xFF));
    push_pair(&n, 0, HX_REG_COL_END_HI);   push_pair(&n, 1, (uint8_t)(x1 >> 8));
    push_pair(&n, 0, HX_REG_COL_END_LO);   push_pair(&n, 1, (uint8_t)(x1 & 0xFF));
    push_pair(&n, 0, HX_REG_ROW_START_HI); push_pair(&n, 1, (uint8_t)(y0 >> 8));
    push_pair(&n, 0, HX_REG_ROW_START_LO); push_pair(&n, 1, (uint8_t)(y0 & 0xFF));
    push_pair(&n, 0, HX_REG_ROW_END_HI);   push_pair(&n, 1, (uint8_t)(y1 >> 8));
    push_pair(&n, 0, HX_REG_ROW_END_LO);   push_pair(&n, 1, (uint8_t)(y1 & 0xFF));
    push_pair(&n, 0, HX_REG_RAMWR);

    for (unsigned yy = 0; yy < CLCD_GLYPH_H; yy++) {
        uint8_t bits = glyph[yy];
        for (unsigned xx = 0; xx < CLCD_GLYPH_W; xx++) {
            uint16_t px = (bits & (0x80u >> xx)) ? fg : bg;
            push_pair(&n, 1, (uint8_t)(px >> 8));   /* RGB565 high byte first */
            push_pair(&n, 1, (uint8_t)(px & 0xFF));
        }
    }
    s_cell_len = n;
    s_cell_pos = 0;
}

static int next_dirty_cell(void)
{
    for (unsigned i = 0; i < CLCD_NCELLS; i++) {
        if (s_dirty[i >> 3] & (1u << (i & 7u)))
            return (int)i;
    }
    return -1;
}

/* ==========================================================================
 * Init streaming
 * ========================================================================== */
static void write_ctrl(uint32_t bits)
{
    s_io.write32(s_io.ctx, CLCD_CTRL, bits);
}

static void reformat(void)
{
    char    next[CLCD_NCELLS];
    uint8_t next_inv[CLCD_ROWS];

    clcd_build_frame(&s_status, s_refresh_tick, next, next_inv);
    s_dirty_count += clcd_diff_cells(s_shadow, next, s_inv, next_inv, s_dirty);
    memcpy(s_shadow, next, sizeof(s_shadow));
    memcpy(s_inv, next_inv, sizeof(s_inv));
    s_refresh_tick++;
}

static void step_init(uint32_t *budget, uint64_t now)
{
    while (s_init_idx < hx8347_init_len) {
        const hx8347_entry_t *e = &hx8347_init[s_init_idx];
        if (e->op == HX_DLY) {
            s_delay_ms = e->val;
            s_t0       = now;
            s_init_idx++;
            s_state = CLCD_ST_INIT_WAIT;
            return;
        }
        int rc = try_push(budget, (e->op == HX_DAT) ? 1 : 0, e->val);
        if (rc == 1) { s_init_idx++; continue; }
        return;   /* budget exhausted or FIFO full — resume next pass */
    }
    s_state         = CLCD_ST_IDLE;
    s_force_refresh = 1;   /* first draw paints the whole screen */
}

/* ==========================================================================
 * Public API
 * ========================================================================== */
void clcd_core_init(const clcd_io_t *io, const char *board_name)
{
    s_io = *io;
    s_state = CLCD_ST_RESET;
    s_t0 = 0;
    s_init_idx = 0;
    s_delay_ms = 0;
    s_last_refresh = 0;
    s_force_refresh = 1;
    s_refresh_tick = 0;
    s_dirty_count = 0;
    s_cell_len = 0;
    s_cell_pos = 0;
    s_bytes_last_pass = 0;
    s_bytes_total = 0;

    memset(s_shadow, 0, sizeof(s_shadow));   /* NUL != any printable glyph  */
    memset(s_inv, 0, sizeof(s_inv));
    memset(s_dirty, 0, sizeof(s_dirty));

    memset(&s_status, 0, sizeof(s_status));
    s_status.link_up = -1;
    s_status.speed_mbps = -1;
    s_status.full_duplex = -1;
    s_status.swap_state = -1;
    {
        const char *n = board_name ? board_name : MPS3_BOARD_NAME;
        unsigned i = 0;
        while (n[i] && i < sizeof(s_status.board_name) - 1u) {
            s_status.board_name[i] = n[i];
            i++;
        }
        s_status.board_name[i] = '\0';
    }
}

void clcd_core_update_status(const clcd_status_t *st)
{
    char keep[sizeof(s_status.board_name)];
    memcpy(keep, s_status.board_name, sizeof(keep));
    s_status = *st;
    if (s_status.board_name[0] == '\0')
        memcpy(s_status.board_name, keep, sizeof(keep));
}

int clcd_core_idle(void)
{
    return s_state == CLCD_ST_IDLE && s_dirty_count == 0;
}

void clcd_core_poll(uint64_t now)
{
    uint32_t budget = CLCD_BYTES_PER_PASS;

    switch (s_state) {

    case CLCD_ST_RESET:
        /* Program the proven 8080 strobe timing, enable the block + flush its
         * FIFO with the panel HELD IN RESET (reset_n=0), backlight off — the
         * exact silicon-proven sequence (CLCD_PANEL_FACTS.md §4/§6). */
        s_io.write32(s_io.ctx, CLCD_TIMING, CLCD_TIMING_PROVEN);
        write_ctrl(CLCD_CTRL_ENABLE | CLCD_CTRL_FIFO_RESET);
        s_t0    = now;
        s_state = CLCD_ST_RST_WAIT;
        break;

    case CLCD_ST_RST_WAIT:
        if (now - s_t0 >= CLCD_RESET_PULSE_MS) {
            /* Release reset + backlight ON (BL active-high, RST active-low —
             * both board-proven). The init table's leading {HX_DLY,5} owns the
             * post-reset settle. */
            write_ctrl(CLCD_CTRL_ENABLE | CLCD_CTRL_RESET_N | CLCD_CTRL_BACKLIGHT);
            s_init_idx = 0;
            s_state    = CLCD_ST_INIT;
        }
        break;

    case CLCD_ST_INIT:
        step_init(&budget, now);
        break;

    case CLCD_ST_INIT_WAIT:
        if (now - s_t0 >= s_delay_ms)
            s_state = CLCD_ST_INIT;
        break;

    case CLCD_ST_IDLE:
        if (s_force_refresh || now - s_last_refresh >= CLCD_REFRESH_MS) {
            s_force_refresh = 0;
            s_last_refresh  = now;
            reformat();
            if (s_dirty_count > 0)
                s_state = CLCD_ST_RENDER;
        }
        break;

    case CLCD_ST_RENDER:
        for (;;) {
            if (s_cell_pos >= s_cell_len) {
                int cell = next_dirty_cell();
                if (cell < 0) {
                    s_state = CLCD_ST_IDLE;
                    break;
                }
                build_cell((unsigned)cell);
                s_dirty[(unsigned)cell >> 3] &= (uint8_t)~(1u << ((unsigned)cell & 7u));
                if (s_dirty_count) s_dirty_count--;
            }
            int rc = try_push(&budget, s_cell_rs[s_cell_pos], s_cell_val[s_cell_pos]);
            if (rc == 1) { s_cell_pos++; continue; }
            break;   /* budget spent or FIFO full — resume next pass */
        }
        break;

    default:
        s_state = CLCD_ST_RESET;
        break;
    }

    s_bytes_last_pass = CLCD_BYTES_PER_PASS - budget;
}

/* ---- introspection -------------------------------------------------------- */
int      clcd_core_state(void)           { return s_state; }
uint32_t clcd_core_bytes_last_pass(void) { return s_bytes_last_pass; }
uint64_t clcd_core_bytes_total(void)     { return s_bytes_total; }
unsigned clcd_core_dirty_count(void)     { return s_dirty_count; }

void clcd_core_shadow(char out[], uint8_t inv[])
{
    memcpy(out, s_shadow, CLCD_NCELLS);
    memcpy(inv, s_inv, CLCD_ROWS);
}
