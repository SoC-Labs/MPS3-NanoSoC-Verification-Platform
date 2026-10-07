/*
 * test_usd_boot.c — THE D13 END TO END, board-free: the REAL coordinator +
 * coordinator_net (6900) + config_agent (6910) + swap FSM + overlay store glue +
 * the BARE-METAL engine (overlay_store_bm.c) + ovlstore_sd + ovl_bdev_usd +
 * usd.c, against fake_usd.c (a register-level usd_spi with an SD card behind it)
 * and fake_net_if.c (the sockets). One simulated superloop pass = every poll
 * the target runs, then 1 ms of mock time.
 *
 *   1. no card: the power-on decision is "none" within the grace period, with
 *      ZERO DATA-register writes (the pads never driven) and the network up;
 *   2. a factory card: FOREIGN, read-only; plain format refused by NAME; the
 *      explicit wipe ("erase-all", held) makes it EMPTY;
 *   3. commit, PARKED: the control connection is held while the pair is pushed
 *      over 6910 (a request sent meanwhile is answered only after); a partial
 *      pushed first, a CRC the request did not declare, and the wrong live
 *      rm_id are refused by name; nothing reaches the ICAP; the answer is the
 *      slot;
 *   4. the power-on load: after a RECONFIGURATION the default loads through the
 *      swap FSM's "usd" source -- the outgoing greybox clearing, then the card's
 *      partial word for word into HWICAP, VERIFY, and the card's clearing into
 *      the RAM arena -- while the control channel answers (`usd` says pending,
 *      a host `swap` is refused as mid-swap);
 *   5. ONCE PER CONFIGURATION: a WDOG warm restart (BRAM, and so the .data
 *      latch, kept) never reloads and still reports "loaded";
 *   6. PB1 held at the boot check: "skipped", nothing loaded, and still skipped
 *      after a warm restart;
 *   7. a restart that interrupts a load mid-stream: "failed:aborted", never
 *      retried in that configuration;
 *   8. a default minted for another shell: STALE, never read, never loaded --
 *      and committing over it (the re-keyed board's recovery) works.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../config_agent/config_agent.h"
#include "../overlay_store/overlay_store.h"
#include "../overlay_store/ovlstore_sd.h"
#include "../common/net_proto.h"
#include "../common/platform_regs.h"
#include "../common/hwicap_writer.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_usd.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define SHELL_A 0xA1B2C3D4u
#define SHELL_B 0x5EED5EEDu
#define RM_X    0x0100001Eu

/* ---- the shell's identity (a strong override of coordinator.c's weak one) --- */
static uint32_t s_static = SHELL_A;
uint32_t mps3_shell_static_id(void) { return s_static; }

/* ---- the greybox blob overlay_store_bm.c hands the FSM ------------------------ */
const uint8_t  mps3_greybox_clearing_bin[16] = { 0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,
                                                 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF };
const uint32_t mps3_greybox_clearing_len_words = 4;
const uint32_t mps3_greybox_clearing_crc32 = 0;
const uint32_t mps3_greybox_clearing_rm_id = 0;
const uint32_t mps3_greybox_clearing_static_id = SHELL_A;

void overlay_store_bm_test_reconfigure(void);   /* overlay_store_bm.c test hook */

/* ---- HWICAP capture ------------------------------------------------------------ */
#define ICAP_CAP 8192
static uint32_t s_icap[ICAP_CAP];
static int      s_icap_n;

static int hwicap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        if (s_icap_n < ICAP_CAP) s_icap[s_icap_n] = *val;
        s_icap_n++;
        return 1;
    }
    if (!is_write && off == HWICAP_CR) { *val = 0; return 1; }
    if (!is_write && off == HWICAP_SR) { *val = HWICAP_SR_DONE | HWICAP_SR_EOS; return 1; }
    return 0;
}

/* ---- the fabric: the RM a load would leave in the RP ---------------------------- */
static uint32_t s_fabric_rm = RM_X;

static void dfx_choreography(void)
{
    switch (swap_fsm_state()) {
    case SWAP_DECOUPLE_ASSERT:
    case SWAP_REISOLATE:
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                       DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
        break;
    case SWAP_RELEASE:
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
        break;
    case SWAP_VERIFY:
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, s_fabric_rm);
        mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
        break;
    default:
        break;
    }
}

/* ONE SUPERLOOP PASS: what the target's service table runs, then 1 ms. */
static void pass(void)
{
    dfx_choreography();
    swap_fsm_poll();
    config_agent_poll();
    coordinator_net_poll();
    overlay_store_service();
    mock_time_advance_ms(1);
    fake_usd_tick();
}

static void passes(int n)
{
    for (int i = 0; i < n; i++) pass();
}

static ovlstore_sd_state_t store_state(void)
{
    return ovlstore_sd_state(overlay_store_test_store());
}

static int run_until_state(ovlstore_sd_state_t st, int max)
{
    for (int i = 0; i < max; i++) {
        if (store_state() == st && !overlay_store_busy()) return 1;
        pass();
    }
    return 0;
}

static int run_until_decided(int max)
{
    for (int i = 0; i < max; i++) {
        if (strcmp(overlay_store_boot_text(), "pending") != 0) return 1;
        pass();
    }
    return 0;
}

/* ---- the 6900 control channel ----------------------------------------------------- */
static int s_ctl = -1;

static void ctl_open(void)
{
    s_ctl = fake_net_connect(MPS3_PORT_CONTROL);
    assert(s_ctl >= 0);
    pass();
}

static void ctl_send(const char *line)
{
    char buf[1024];
    int n = snprintf(buf, sizeof buf, "%s\n", line);
    CHECK(fake_net_send(s_ctl, buf, n) == n);
}

/* Up to `max` passes for one whole line; returns its length (0 = none yet). */
static int ctl_line(char *out, int cap, int max)
{
    int n = 0;
    for (int i = 0; i < max; i++) {
        char c;
        while (n < cap - 1 && fake_net_recv(s_ctl, &c, 1) == 1) {
            out[n++] = c;
            if (c == '\n') {
                out[n] = '\0';
                return n;
            }
        }
        pass();
    }
    out[n] = '\0';
    CHECK(n == 0);   /* a partial line is never left hanging */
    return 0;
}

static void ctl_req(const char *line, char *out, int cap)
{
    ctl_send(line);
    CHECK(ctl_line(out, cap, 200000) > 0);
}

static void expect(const char *got, const char *want)
{
    if (strcmp(got, want) != 0) {
        printf("  got:  %s  want: %s", got, want);
    }
    CHECK(strcmp(got, want) == 0);
}

/* ---- the pair ----------------------------------------------------------------------- */
#define CLEAR_W 256u    /* 1 KiB  */
#define PART_W  1024u   /* 4 KiB  */
static uint8_t  s_clear_frame[64 + CLEAR_W * 4u], s_part_frame[64 + PART_W * 4u];
static int      s_clear_len, s_part_len;
static uint32_t s_clear_crc, s_part_crc;

static int build(uint8_t *out, uint8_t kind, uint32_t rm, uint32_t sid, uint32_t words,
                 uint8_t seed, uint32_t *crc_out)
{
    uint32_t n = words * 4u;
    uint8_t *p = out + MPS3_BITSTREAM_HDR_WIRE_SIZE;
    for (uint32_t i = 0; i < n; i++) p[i] = (uint8_t)(seed + i * 13u + (i >> 8));
    mps3_bitstream_hdr_t h;
    memset(&h, 0, sizeof h);
    memcpy(h.magic, MPS3_BITSTREAM_MAGIC, 4);
    h.ver = MPS3_BITSTREAM_VER;
    h.kind = kind;
    h.static_id = sid;
    h.rm_id = rm;
    h.len_words = words;
    h.crc32 = mps3_crc32(p, n);
    *crc_out = h.crc32;
    mps3_bitstream_hdr_pack(&h, out);
    return (int)(MPS3_BITSTREAM_HDR_WIRE_SIZE + n);
}

static void build_pair(uint32_t sid, uint8_t seed)
{
    s_clear_len = build(s_clear_frame, MPS3_BIN_KIND_CLEARING, RM_X, sid, CLEAR_W, seed, &s_clear_crc);
    s_part_len = build(s_part_frame, MPS3_BIN_KIND_PARTIAL, RM_X, sid, PART_W,
                       (uint8_t)(seed + 0x40u), &s_part_crc);
}

static void commit_line(char *out, size_t cap, uint32_t sid, uint32_t rm, uint32_t ccrc)
{
    snprintf(out, cap,
             "{\"op\":\"commit\",\"rm\":\"x\",\"src\":\"tcp\",\"rm_id\":\"0x%08x\","
             "\"static_id\":\"0x%08x\",\"clear_len\":%u,\"clear_crc\":\"0x%08x\","
             "\"part_len\":%u,\"part_crc\":\"0x%08x\"}",
             (unsigned)rm, (unsigned)sid, (unsigned)(CLEAR_W * 4u), (unsigned)ccrc,
             (unsigned)(PART_W * 4u), (unsigned)s_part_crc);
}

/* One push over 6910, passes in between (the superloop runs while it arrives). */
static void push(const uint8_t *f, int len)
{
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    int off = 0;
    while (off < len) {
        int want = len - off;
        if (want > 2048) want = 2048;
        int sent = fake_net_send(cli, f + off, want);
        assert(sent >= 0);
        off += sent;
        passes(4);
    }
    fake_net_close(cli);
    for (int i = 0; i < 200000 && !fake_net_fw_closed(cli); i++) pass();
    CHECK(fake_net_fw_closed(cli));
}

/* ---- power-on / warm restart ---------------------------------------------------------- */
enum { CARD_NONE, CARD_NEW, CARD_KEEP };
static uint32_t s_usd_id = 0x55534431u;   /* the block's ID register ("USD1") */

static void rp_greybox(void)
{
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
}

/* An FPGA (re)configuration: BRAM re-initialised from the bitstream (the .data
 * latch back to FRESH), every register at reset, the RP holding the greybox. */
static void reconfigure(int card, int pb1_held)
{
    mock_regs_reset();
    if (card == CARD_KEEP) {
        fake_usd_power_cycle();
    } else {
        fake_usd_reset();
        fake_usd_set_id(s_usd_id);
        if (card == CARD_NEW) fake_usd_insert();
    }
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, pb1_held ? CLCDKVM_STATUS_PB_LEVEL : 0u);
    rp_greybox();
    fake_net_reset();
    s_icap_n = 0;
    overlay_store_bm_test_reconfigure();
    coordinator_init();
    ctl_open();
}

/* A WDOG warm restart: the MicroBlaze (and its peripherals) reset, BRAM KEPT,
 * the fabric's RP untouched, the card in the slot. */
static void warm_restart(void)
{
    fake_usd_power_cycle();
    fake_net_reset();
    s_icap_n = 0;
    coordinator_init();
    ctl_open();
}

/* ---- 1 ---------------------------------------------------------------------------- */
static void test_no_card(void)
{
    char out[1024];
    printf("- no card: \"none\" within the grace period, pads never driven\n");
    reconfigure(CARD_NONE, 0);
    ctl_req("{\"op\":\"usd\"}", out, sizeof out);          /* answers at once */
    expect(out, "{\"ok\":true,\"present\":false,\"state\":\"none\",\"text\":\"none\","
                "\"boot\":\"pending\"}\n");
    CHECK(run_until_decided(OVLSTORE_BOOT_GRACE_MS + 10));
    CHECK(strcmp(overlay_store_boot_text(), "none") == 0);
    ctl_req("{\"op\":\"usd\"}", out, sizeof out);
    expect(out, "{\"ok\":true,\"present\":false,\"state\":\"none\",\"text\":\"none\","
                "\"boot\":\"none\"}\n");
    passes(500);
    CHECK(fake_usd_data_writes() == 0u && fake_usd_en_writes() == 0u);
    CHECK(!fake_usd_pads_ever_driven());
    CHECK(s_icap_n == 0 && swap_fsm_idle());
    CHECK(overlay_store_diag_word() == 0xB0070004u);          /* latched: none */
    /* no hw: a fabric without usd_spi (ID mismatch) says so */
    s_usd_id = 0u;
    reconfigure(CARD_NONE, 0);
    s_usd_id = 0x55534431u;
    passes(200);
    ctl_req("{\"op\":\"usd\"}", out, sizeof out);
    expect(out, "{\"ok\":true,\"present\":false,\"state\":\"no_hw\",\"text\":\"no hw\","
                "\"boot\":\"none\"}\n");
    CHECK(fake_usd_page_writes() == 0u);
}

/* ---- 2 + 3 ---------------------------------------------------------------------------- */
static void test_wipe_and_commit(void)
{
    char out[1024], line[512];
    uint32_t cc0;
    printf("- factory card: foreign; plain format refused; the wipe makes it empty\n");
    reconfigure(CARD_NEW, 0);
    cc0 = overlay_store_usd_change_count();
    CHECK(run_until_state(OVLSD_FOREIGN, 20000));
    CHECK(overlay_store_usd_change_count() != cc0);            /* the CLCD was told */
    CHECK(strcmp(overlay_store_usd_text(), "foreign") == 0);
    CHECK(run_until_decided(100));
    CHECK(strcmp(overlay_store_boot_text(), "none") == 0);

    uint32_t w0 = fake_usd_blocks_written();
    ctl_req("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase\"}", out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"filesystem present\"}\n");
    ctl_req("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase all\"}", out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"confirm required\"}\n");
    ctl_req("{\"op\":\"usd\",\"action\":\"format\"}", out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"confirm required\"}\n");
    ctl_req("{\"op\":\"usd\",\"action\":\"clear\"}", out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"foreign\"}\n");
    ctl_req("{\"op\":\"usd\",\"action\":\"defrag\"}", out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"bad args\"}\n");
    CHECK(fake_usd_blocks_written() == w0);                    /* FOREIGN: zero writes */

    ctl_req("{\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase-all\"}", out, sizeof out);
    expect(out, "{\"ok\":true,\"state\":\"empty\"}\n");
    CHECK(strcmp(overlay_store_usd_text(), "empty") == 0);

    printf("- commit: parked; the pair over 6910; refusals by name; nothing to ICAP\n");
    build_pair(SHELL_A, 0x21);
    /* the RM "running" is X */
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, RM_X);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);

    commit_line(line, sizeof line, SHELL_A, RM_X + 1u, s_clear_crc);   /* not what runs */
    ctl_req(line, out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"rm mismatch\"}\n");
    commit_line(line, sizeof line, SHELL_B, RM_X, s_clear_crc);        /* another shell */
    ctl_req(line, out, sizeof out);
    expect(out, "{\"ok\":false,\"err\":\"stale key\"}\n");

    /* a partial pushed first */
    commit_line(line, sizeof line, SHELL_A, RM_X, s_clear_crc);
    ctl_send(line);
    CHECK(ctl_line(out, sizeof out, 50) == 0);                         /* PARKED */
    push(s_part_frame, s_part_len);
    CHECK(ctl_line(out, sizeof out, 2000) > 0);
    expect(out, "{\"ok\":false,\"err\":\"bad args\"}\n");

    /* a clearing whose CRC is not the one the request declared */
    commit_line(line, sizeof line, SHELL_A, RM_X, s_clear_crc ^ 1u);
    ctl_send(line);
    CHECK(ctl_line(out, sizeof out, 50) == 0);
    push(s_clear_frame, s_clear_len);
    CHECK(ctl_line(out, sizeof out, 2000) > 0);
    expect(out, "{\"ok\":false,\"err\":\"crc\"}\n");
    CHECK(strcmp(overlay_store_usd_text(), "empty") == 0);             /* old state kept */

    /* the real thing: parked; a second request waits its turn */
    int icap0 = s_icap_n;
    commit_line(line, sizeof line, SHELL_A, RM_X, s_clear_crc);
    ctl_send(line);
    ctl_send("{\"op\":\"ping\"}");
    CHECK(ctl_line(out, sizeof out, 50) == 0);
    push(s_clear_frame, s_clear_len);
    CHECK(ctl_line(out, sizeof out, 20) == 0);                         /* still parked */
    push(s_part_frame, s_part_len);
    CHECK(ctl_line(out, sizeof out, 200000) > 0);
    expect(out, "{\"ok\":true,\"slot\":\"A\"}\n");
    CHECK(ctl_line(out, sizeof out, 100) > 0);                         /* then the ping */
    CHECK(strstr(out, "\"shell_id\":\"0xa1b2c3d4\"") != NULL);
    CHECK(s_icap_n == icap0);                                          /* NOTHING to the ICAP */
    CHECK(swap_fsm_idle());

    ctl_req("{\"op\":\"usd\"}", out, sizeof out);
    expect(out, "{\"ok\":true,\"present\":true,\"state\":\"valid\",\"text\":\"0x0100001E [A]\","
                "\"card_mb\":7580,\"default\":{\"rm_id\":\"0x0100001e\",\"static_id\":\"0xa1b2c3d4\","
                "\"slot\":\"A\"},\"boot\":\"none\"}\n");
    CHECK(strlen(overlay_store_usd_text()) <= 16u);
}

/* ---- 4 + 5 ---------------------------------------------------------------------------- */
static void test_power_on_load_once_per_configuration(void)
{
    char out[1024];
    printf("- power-on load: the default through the swap FSM's \"usd\" source\n");
    s_fabric_rm = RM_X;
    reconfigure(CARD_KEEP, 0);
    uint32_t done0 = swap_fsm_completed();

    /* wait until the load is running, then use the control channel mid-load */
    int saw_loading = 0;
    for (int i = 0; i < 200000 && strcmp(overlay_store_boot_text(), "pending") == 0; i++) {
        pass();
        if (!saw_loading && swap_fsm_state() == SWAP_STREAM_PARTIAL) {
            saw_loading = 1;
            ctl_req("{\"op\":\"usd\"}", out, sizeof out);
            CHECK(strstr(out, "\"boot\":\"pending\"") != NULL);
            ctl_req("{\"op\":\"swap\",\"rm\":\"led\",\"src\":\"tcp\"}", out, sizeof out);
            expect(out, "{\"ok\":false,\"err\":\"swap already in progress\"}\n");
            char line[512];
            commit_line(line, sizeof line, SHELL_A, RM_X, s_clear_crc);
            ctl_req(line, out, sizeof out);
            expect(out, "{\"ok\":false,\"err\":\"store busy\"}\n");
            ctl_req("{\"op\":\"usd\",\"action\":\"clear\"}", out, sizeof out);
            expect(out, "{\"ok\":false,\"err\":\"store busy\"}\n");
        }
    }
    CHECK(saw_loading);
    CHECK(strcmp(overlay_store_boot_text(), "loaded") == 0);
    CHECK(swap_fsm_completed() == done0 + 1u);
    CHECK(swap_fsm_last_result()->ok && swap_fsm_last_result()->rm_id == RM_X);
    CHECK(g_shell_state.current_rm_id == RM_X);

    /* the ICAP saw the greybox clearing, then the card's partial, word for word */
    CHECK(s_icap_n == (int)(4u + PART_W));
    for (int i = 0; i < 4; i++) {
        CHECK(s_icap[i] == mps3_hwicap_pack_word(&mps3_greybox_clearing_bin[4 * i]));
    }
    for (uint32_t i = 0; i < PART_W; i++) {
        assert(s_icap[4u + i] ==
               mps3_hwicap_pack_word(&s_part_frame[MPS3_BITSTREAM_HDR_WIRE_SIZE + 4u * i]));
    }
    s_checks++;
    /* the card's clearing is the resident one, in the RAM arena */
    CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == RM_X);
    CHECK(g_current_rm_clearing.len_words == CLEAR_W);
    CHECK(g_current_rm_clearing.data == swap_fsm_clearing_stage_buffer(NULL));
    CHECK(memcmp(g_current_rm_clearing.data, s_clear_frame + MPS3_BITSTREAM_HDR_WIRE_SIZE,
                 CLEAR_W * 4u) == 0);
    CHECK(overlay_store_diag_word() == 0xB0070002u);
    ctl_req("{\"op\":\"usd\"}", out, sizeof out);
    CHECK(strstr(out, "\"boot\":\"loaded\"") != NULL);
    passes(1000);

    printf("- once per configuration: a WDOG warm restart never reloads\n");
    for (int k = 0; k < 2; k++) {
        warm_restart();
        passes(40000);                      /* card init + verify + far past the grace */
        CHECK(s_icap_n == 0);               /* not one word */
        CHECK(swap_fsm_idle());
        CHECK(strcmp(overlay_store_boot_text(), "loaded") == 0);   /* what was decided */
        CHECK(store_state() == OVLSD_VALID);
    }
}

/* ---- 6 ---------------------------------------------------------------------------- */
static void test_pb1_skips(void)
{
    char out[1024];
    printf("- PB1 held at the boot check: skipped, and still skipped after a restart\n");
    reconfigure(CARD_KEEP, /*pb1=*/1);
    CHECK(run_until_decided(10));
    CHECK(strcmp(overlay_store_boot_text(), "skipped") == 0);
    mock_regs_poke(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS, 0u);   /* released: a level, sampled once */
    CHECK(run_until_state(OVLSD_VALID, 60000));
    passes(2000);
    CHECK(s_icap_n == 0 && swap_fsm_idle());
    CHECK(strcmp(overlay_store_usd_text(), "skipped") == 0);
    ctl_req("{\"op\":\"usd\"}", out, sizeof out);
    CHECK(strstr(out, "\"text\":\"skipped\"") != NULL && strstr(out, "\"boot\":\"skipped\"") != NULL);
    warm_restart();
    passes(40000);
    CHECK(s_icap_n == 0);
    CHECK(strcmp(overlay_store_boot_text(), "skipped") == 0);
}

/* ---- 7 ---------------------------------------------------------------------------- */
static void test_interrupted_load_never_retries(void)
{
    printf("- a restart mid-load: failed:aborted, never retried in that configuration\n");
    reconfigure(CARD_KEEP, 0);
    int i;
    for (i = 0; i < 200000 && swap_fsm_state() != SWAP_STREAM_PARTIAL; i++) pass();
    CHECK(swap_fsm_state() == SWAP_STREAM_PARTIAL);
    warm_restart();
    passes(40000);
    CHECK(strcmp(overlay_store_boot_text(), "failed:aborted") == 0);
    CHECK(s_icap_n == 0 && swap_fsm_idle());
}

/* ---- 8 ---------------------------------------------------------------------------- */
static void test_stale_default(void)
{
    char out[1024], line[512];
    printf("- a default minted for another shell: stale, never loaded; commit over it\n");
    s_static = SHELL_B;
    reconfigure(CARD_KEEP, 0);
    CHECK(run_until_decided(60000));
    CHECK(strcmp(overlay_store_boot_text(), "none") == 0);
    CHECK(store_state() == OVLSD_STALE);
    CHECK(strcmp(overlay_store_usd_text(), "stale key") == 0);
    passes(2000);
    CHECK(s_icap_n == 0 && swap_fsm_idle());
    ctl_req("{\"op\":\"usd\"}", out, sizeof out);
    expect(out, "{\"ok\":true,\"present\":true,\"state\":\"stale\",\"text\":\"stale key\","
                "\"card_mb\":7580,\"default\":{\"rm_id\":\"0x0100001e\",\"static_id\":\"0xa1b2c3d4\","
                "\"slot\":\"A\"},\"boot\":\"none\"}\n");

    /* the re-keyed board's recovery: commit the running pair over it */
    build_pair(SHELL_B, 0x77);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, RM_X);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    commit_line(line, sizeof line, SHELL_B, RM_X, s_clear_crc);
    ctl_send(line);
    CHECK(ctl_line(out, sizeof out, 20) == 0);
    push(s_clear_frame, s_clear_len);
    push(s_part_frame, s_part_len);
    CHECK(ctl_line(out, sizeof out, 200000) > 0);
    expect(out, "{\"ok\":true,\"slot\":\"B\"}\n");        /* the stale one's other slot */
    CHECK(store_state() == OVLSD_VALID);
    CHECK(strcmp(overlay_store_usd_text(), "0x0100001E [B]") == 0);
    s_static = SHELL_A;
}

int main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    test_no_card();
    test_wipe_and_commit();
    test_power_on_load_once_per_configuration();
    test_pb1_skips();
    test_interrupted_load_never_retries();
    test_stale_default();
    CHECK(fake_usd_store_overflows() == 0u);
    CHECK(fake_usd_busy_violations() == 0u && fake_usd_protocol_errors() == 0u);
    printf("test_usd_boot: %d checks passed\n", s_checks);
    return 0;
}
