/*
 * test_swap_qspi_free.c — pins the QSPI-FREE swap configuration (RETARGETED for
 * D13: the SST26 overlay-store backend and its QSPI staging sinks are DELETED, so
 * coordinator_init() registers only the ICAP-direct sink; this binary registers
 * TRIP-WIRE sinks in the two old QSPI seams and asserts neither is ever begun,
 * written or finished -- "zero flash ops" became "not one byte into a store
 * sink"). With
 * config_agent's clearing RAM slot and swap_fsm's resident clearing arena both
 * sized >= the largest real clearing (nanosoc's 117,684 B, from
 * fpga/dfx/overlay/nanosoc/manifest.json), a FULL a->b->a swap-away cycle
 * completes through the shipping code with ZERO flash erase and ZERO flash
 * program ops, and the on-part block protection is NEVER unlocked. This is the
 * board-free, link-time + host-harness evidence for docs/
 * QSPI_CLEARING_CACHE_HANDBACK.md §7 ("a QSPI-free configuration exists").
 *
 * Why no QSPI write occurs — the three code facts this binary exercises:
 *
 *   1. config_agent.c:359 (`if (s_payload_expect > s_dst->cap)`) routes a
 *      payload to a SINK only when it OVERFLOWS the slot's RAM buffer. The
 *      clearing slot's cap is MPS3_CFG_AGENT_CLEARING_RAM_BYTES; built here at
 *      117,684, nanosoc's 117,684 B clearing is NOT > cap, so it stays on the
 *      RAM fast path and the QSPI clearing-STAGE sink is never begun.
 *
 *   2. config_agent.c:362-368 prefers the ICAP-DIRECT sink (Path 3, straight to
 *      HWICAP.WF) for a large PARTIAL over the QSPI A/B-slot sink — so the
 *      >RAM-threshold partials in this cycle stream to the ICAP, never to flash.
 *      All three sinks are wired here exactly as coordinator_init() wires them
 *      (QSPI partial + QSPI clearing + ICAP-direct); the QSPI write paths are
 *      AVAILABLE and simply never invoked.
 *
 *   3. swap_fsm.c step_cache_clearing() copies the just-verified incoming
 *      clearing into the resident RAM arena (MPS3_SWAP_CLEARING_ARENA_BYTES); a
 *      subsequent swap-AWAY replays it from RAM at SWAP_STREAM_CLEARING
 *      (every resident clearing is RAM since D13). The arena must fit the
 *      clearing or the copy is refused and the clearing marked invalid, failing
 *      the NEXT swap-away closed — which the negative-control binary proves.
 *
 * Two binaries from this one source, differing ONLY in the resident arena size
 * (chosen at compile time; the buffer is a fixed-size array, so it cannot vary
 * within one binary — hence a second binary, per the harness note):
 *
 *   - test_swap_qspi_free            arena = 117,684  -> POSITIVE: the full
 *                                    greybox->a->b->a cycle succeeds, a's large
 *                                    clearing replays from RAM, zero flash writes.
 *   - test_swap_qspi_free_tinyarena  arena =   4,096  -> NEGATIVE CONTROL: a's
 *                                    117,684 B clearing RAM-stages in
 *                                    config_agent's big slot but does NOT fit the
 *                                    tiny arena, so swap-away fails CLOSED at
 *                                    SWAP_STREAM_CLEARING (the exact silicon bug:
 *                                    "a 51,952 B clearing against a 4,096 B
 *                                    arena, so regdemo_b -> regdemo_a could never
 *                                    work") — and STILL writes zero flash (it
 *                                    fails closed, it does not fall back to QSPI).
 * The mode is selected at RUNTIME from the real arena capacity, so the SAME
 * code drives both and the divergence is observable, not hard-coded.
 *
 * Links: config_agent.c, swap_fsm.c, swap_fsm_transitions.c, hwicap_writer.c,
 * common/{crc32,net_proto,net_if}.c, mock_regs.c, fake_net_if.c,
 * fake_overlay_store.c (the greybox seed). Defines g_shell_state itself (no
 * coordinator.c).
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/swap_fsm.h"
#include "../coordinator/coordinator.h"
#include "../config_agent/config_agent.h"
#include "../overlay_store/overlay_store.h"
#include "../common/platform_regs.h"
#include "../common/crc32.h"
#include "../common/net_if.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_overlay_store.h"

mps3_shell_state_t g_shell_state; /* swap_fsm.c's extern -- defined here */

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

/* This one source builds two binaries (positive + tiny-arena negative control);
 * the Makefile passes each its own name so the per-binary summary line reads
 * "<binary>: N checks passed" (the host harness banner check keys on it). */
#ifndef QSPI_FREE_TEST_NAME
#define QSPI_FREE_TEST_NAME "test_swap_qspi_free"
#endif

/* ---- greybox blob (the shell-image seed of the current-clearing cache) ----- */
static const uint8_t k_greybox_bin[16] = {
    0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,
    0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF,
};

/* ---- TRIP-WIRE sinks in the two retired QSPI seams --------------------------- */
static int s_trip_calls;
static int  trip_begin(uint32_t n)               { (void)n; s_trip_calls++; return -1; }
static int  trip_write(const void *b, uint32_t n) { (void)b; (void)n; s_trip_calls++; return -1; }
static int  trip_finish(uint32_t crc)            { (void)crc; s_trip_calls++; return -1; }
static void trip_abort(void)                     { s_trip_calls++; }
static const mps3_cfg_agent_qspi_sink_t k_trip = { trip_begin, trip_write, trip_finish,
                                                   trip_abort, NULL };

/* nanosoc's REAL clearing size (fpga/dfx/overlay/nanosoc/manifest.json:
 * clearing.len = 117,684 = the LARGEST clearing of any current RM). This is the
 * size the QSPI-free build sizes CLEARING_RAM_BYTES + the arena to. */
#define NANO_CLEAR_BYTES  117684u
#define NANO_CLEAR_WORDS  (NANO_CLEAR_BYTES / 4u)   /* 29421 */

/* ---- HWICAP word capture --------------------------------------------------- */
#define ICAP_CAP 40000
static uint32_t s_icap_words[ICAP_CAP];
static int      s_icap_count;

static int hwicap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write && off == HWICAP_WF) {
        if (s_icap_count < ICAP_CAP) {
            s_icap_words[s_icap_count] = *val;
        }
        s_icap_count++;
        return 1;
    }
    if (!is_write && off == HWICAP_CR) {
        /* Lite mode: the per-word StartConfig WRITE bit self-clears the instant
         * the word reaches ICAP — model an instant drain (CR poll exits at once). */
        *val = 0;
        return 1;
    }
    if (!is_write && off == HWICAP_SR) {
        /* Healthy core: DONE + EOS always set, so the ICAP-direct finish's
         * bounded post-DESYNC EOS wait completes immediately. */
        *val = HWICAP_SR_DONE | HWICAP_SR_EOS;
        return 1;
    }
    return 0; /* WFV reads, SZ/CR writes -> plain mock slots */
}

/* Native-LE packing (seed + byte index) — the byte order the RAM/QSPI clearing
 * writers produce (matches test_swap_e2e_net / test_swap_clearing_cache). The
 * ICAP-direct PARTIAL sink packs MSB-first, but this binary only word-verifies
 * the RAM-replayed CLEARING, which is native-LE. */
static uint32_t word_of_pattern(uint8_t seed, int word_idx)
{
    uint8_t b[4] = { (uint8_t)(seed + 4 * word_idx), (uint8_t)(seed + 4 * word_idx + 1),
                     (uint8_t)(seed + 4 * word_idx + 2), (uint8_t)(seed + 4 * word_idx + 3) };
    uint32_t w;
    memcpy(&w, b, 4);
    return w;
}

/* ---- boot: the QSPI-free wiring, byte-for-byte what coordinator_init() does -- */
static void boot(void)
{
    mock_regs_reset();
    fake_net_reset();
    mock_regs_set_hook(MPS3_HWICAP_BASE, hwicap_hook, 0);
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
    s_icap_count = 0;
    memset(&g_shell_state, 0, sizeof(g_shell_state));

    fake_overlay_store_reset();
    overlay_manifest_info_t grey = { .static_id = TEST_STATIC_ID, .rm_id = 0,
                                     .clear_len_words = 4, .clear_crc32 = 0,
                                     .clear_data = k_greybox_bin };
    fake_overlay_store_set_greybox(&grey, 0);
    swap_fsm_init();            /* seeds g_current_rm_clearing from the greybox (RAM) */
    config_agent_init();
    config_agent_set_running_static_id(TEST_STATIC_ID);
    /* coordinator_init() registers ONLY the ICAP-direct sink since D13. The two
     * retired QSPI seams get TRIP-WIRES: the point of this test is that nothing
     * ever reaches them. */
    s_trip_calls = 0;
    config_agent_set_qspi_sink(&k_trip);
    config_agent_set_qspi_clearing_sink(&k_trip);
    config_agent_set_icap_direct_sink(swap_fsm_icap_direct_sink());
}

/* ---- wire framing (same bytes the host pusher emits) ----------------------- */
static uint8_t s_frame[128 * 1024];   /* room for nanosoc's 117,684-byte clearing + header */

static int build_frame(uint8_t *out, mps3_bin_kind_t kind, uint32_t rm_id,
                       uint32_t len_words, uint8_t seed)
{
    uint32_t nbytes = len_words * 4u;
    uint8_t *payload = out + MPS3_BITSTREAM_HDR_WIRE_SIZE;
    for (uint32_t i = 0; i < nbytes; i++) {
        payload[i] = (uint8_t)(seed + i);
    }
    mps3_bitstream_hdr_t hdr;
    memset(&hdr, 0, sizeof(hdr));
    memcpy(hdr.magic, MPS3_BITSTREAM_MAGIC, 4);
    hdr.ver = MPS3_BITSTREAM_VER;
    hdr.kind = (uint8_t)kind;
    hdr.static_id = TEST_STATIC_ID;
    hdr.rm_id = rm_id;
    hdr.len_words = len_words;
    hdr.crc32 = mps3_crc32(payload, nbytes);
    mps3_bitstream_hdr_pack(&hdr, out);
    return (int)(MPS3_BITSTREAM_HDR_WIRE_SIZE + nbytes);
}

/* The rm_id the mock DFXCTL.RM_ID reads back at SWAP_VERIFY (set per swap). */
static uint32_t s_verify_rm_id;

/* One superloop tick: inject the vendor-IP confirmations the mocks can't
 * self-drive (keyed on FSM state), then step BOTH polls. Same pattern as
 * test_swap_icap_direct's pump(). */
static void pump(int n)
{
    for (int i = 0; i < n; i++) {
        switch (swap_fsm_state()) {
        case SWAP_DECOUPLE_ASSERT:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        case SWAP_VERIFY:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, s_verify_rm_id);
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
            break;
        case SWAP_RELEASE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        case SWAP_REISOLATE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        default:
            break;
        }
        swap_fsm_poll();
        config_agent_poll();
    }
}

/* Feed one frame over 6910 the way a real TCP push arrives: chunked into pieces
 * (the fake_net TCP ring is 32 KiB; nanosoc's 117 KiB clearing MUST be fed in
 * pieces) with the receive side drained between them. Each pump() also advances
 * the swap FSM, so a CLEARING RAM-stages during SWAP_AWAIT_INCOMING_CLEARING and
 * a PARTIAL streams ICAP-direct during SWAP_AWAIT_PARTIAL, exactly as the real
 * over-the-wire flow interleaves receipt with the FSM. */
static void feed_frame_chunked(mps3_bin_kind_t kind, uint32_t rm_id,
                               uint32_t len_words, uint8_t seed)
{
    int len = build_frame(s_frame, kind, rm_id, len_words, seed);
    int cli = fake_net_connect(MPS3_PORT_RAW_PUSH);
    assert(cli >= 0);
    int off = 0;
    while (off < len) {
        int want = len - off;
        if (want > 4096) want = 4096;
        int sent = fake_net_send(cli, s_frame + off, want);
        assert(sent >= 0);
        off += sent;
        pump(4); /* drain: 4 KiB/poll * 4 = 16 KiB >> the 4 KiB just sent */
    }
    fake_net_close(cli);
    pump(400); /* EOF -> finish_payload -> hand-off -> FSM runs through VERIFY.. */
}

#define PUMP_MAX 200000

/* Arm a swap to `rm_name`/`rm_id`, then drive it to a terminal state, pushing
 * the incoming pair through the SAME seams the FSM waits on:
 *   - a LARGE clearing (clear_words) RAM-staged during SWAP_AWAIT_INCOMING_CLEARING;
 *   - a LARGE partial (part_words) ICAP-direct during SWAP_AWAIT_PARTIAL.
 * If the swap fails BEFORE reaching SWAP_AWAIT_INCOMING_CLEARING (the
 * tiny-arena swap-away, which fails closed at SWAP_STREAM_CLEARING), the pushes
 * never happen and it returns idle with a failed result. The caller asserts the
 * outcome. */
static void drive_swap(const char *rm_name, uint32_t rm_id,
                       uint32_t clear_words, uint8_t clear_seed,
                       uint32_t part_words, uint8_t part_seed)
{
    s_verify_rm_id = rm_id;
    CHECK(swap_fsm_start(rm_name, "tcp") == 0);

    int clearing_pushed = 0, partial_pushed = 0;
    for (int i = 0; i < PUMP_MAX && !swap_fsm_idle(); i++) {
        mps3_swap_state_t st = swap_fsm_state();
        if (st == SWAP_AWAIT_INCOMING_CLEARING && !clearing_pushed) {
            feed_frame_chunked(MPS3_BIN_KIND_CLEARING, rm_id, clear_words, clear_seed);
            clearing_pushed = 1;
            continue;
        }
        if (st == SWAP_AWAIT_PARTIAL && !partial_pushed) {
            feed_frame_chunked(MPS3_BIN_KIND_PARTIAL, rm_id, part_words, part_seed);
            partial_pushed = 1;
            continue;
        }
        pump(1);
    }
}

/* Not one call reached a (retired) QSPI store seam: no begin, no byte, no
 * finish, no abort. */
static void assert_zero_flash_writes(void)
{
    CHECK(s_trip_calls == 0);
}

/* ---- the a/b RMs ----------------------------------------------------------- */
/* 'a' carries nanosoc's FULL 117,684 B clearing (the one that used to be
 * rejected / uncacheable); 'b' carries a modest clearing. Both partials are
 * >RAM-threshold so they take the ICAP-direct path (never QSPI). */
/* A_RM_ID / B_RM_ID are ARBITRARY OPAQUE PAYLOADS, not real design ids: this test
 * only needs two distinct values to tell partial A from partial B. Do NOT relabel
 * them as a named design's rm_id -- under the v2 encoding
 * ({major,minor,design_id}, docs/VERSIONING_PLAN.md) a real id changes on every
 * version bump, so such a label rots immediately. (These used to be captioned
 * "nanosoc's" / "led's", which became false the moment those RMs went to v1.0.) */
#define A_RM_ID       0x00000001u   /* opaque payload; only A != B matters */
#define A_CLEAR_WORDS NANO_CLEAR_WORDS
#define A_CLEAR_SEED  0x50
#define A_PART_WORDS  1200u         /* 4800 B > 4096 RAM threshold -> ICAP-direct */
#define A_PART_SEED   0x40

#define B_RM_ID       0x0000001Eu   /* opaque payload; only A != B matters */
#define B_CLEAR_WORDS 16u           /* 64 B  <= threshold -> RAM fast path */
#define B_CLEAR_SEED  0x90
#define B_PART_WORDS  1300u         /* 5200 B > 4096 -> ICAP-direct */
#define B_PART_SEED   0x70

/* ============================================================================
 * The QSPI-free a->b->a swap-away cycle (POSITIVE mode, big arena), with the
 * NEGATIVE-CONTROL divergence (tiny arena) selected from the real arena size.
 * ========================================================================== */
static void test_qspi_free_swap_away_cycle(void)
{
    boot();

    uint32_t arena_cap = 0;
    (void)swap_fsm_clearing_stage_buffer(&arena_cap);
    const int arena_big = (arena_cap >= NANO_CLEAR_BYTES);

    /* Both binaries size config_agent's clearing slot to hold the full clearing,
     * so nanosoc's 117,684 B clearing RAM-stages (fact 1: never routed to QSPI). */
    CHECK(MPS3_CFG_AGENT_CLEARING_RAM_BYTES >= NANO_CLEAR_BYTES);

    /* -- SWAP 1: greybox -> a. Establishes a's FULL clearing as resident. ----- */
    drive_swap("nanosoc", A_RM_ID, A_CLEAR_WORDS, A_CLEAR_SEED, A_PART_WORDS, A_PART_SEED);
    CHECK(swap_fsm_idle());
    CHECK(swap_fsm_last_result()->ok && swap_fsm_last_result()->verified);
    CHECK(swap_fsm_last_result()->rm_id == A_RM_ID);
    /* nanosoc's 117,684 B clearing stayed on the RAM path. */
    assert_zero_flash_writes();

    if (arena_big) {
        /* a's clearing is resident IN RAM (real bytes in the arena). */
        CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == A_RM_ID);
        CHECK(g_current_rm_clearing.len_words == A_CLEAR_WORDS);
        CHECK(g_current_rm_clearing.data == swap_fsm_clearing_stage_buffer(NULL));
        CHECK(((const uint8_t *)g_current_rm_clearing.data)[0] == (uint8_t)A_CLEAR_SEED);

        /* -- SWAP 2: a -> b. THE swap-AWAY: a's 117,684 B resident clearing is
         * the OUTGOING clearing and must replay FROM RAM at SWAP_STREAM_CLEARING
         * This is the case the retired QSPI clearing-cache was invented to
         * enable — done here with no QSPI at all. --------------------------- */
        s_icap_count = 0;
        drive_swap("led", B_RM_ID, B_CLEAR_WORDS, B_CLEAR_SEED, B_PART_WORDS, B_PART_SEED);
        CHECK(swap_fsm_idle());
        CHECK(swap_fsm_last_result()->ok && swap_fsm_last_result()->verified);
        CHECK(swap_fsm_last_result()->rm_id == B_RM_ID);

        /* The outgoing clearing streamed to HWICAP FIRST, and its first
         * NANO_CLEAR_WORDS words are byte-exact a's clearing, native-LE — i.e.
         * replayed straight from the resident RAM arena, not any flash copy. */
        CHECK(s_icap_count >= (int)A_CLEAR_WORDS);
        for (uint32_t i = 0; i < A_CLEAR_WORDS; i++) {
            assert(s_icap_words[i] == word_of_pattern(A_CLEAR_SEED, (int)i));
        }
        s_checks++; /* the 29,421-word replay-from-RAM compare held */

        /* b is now resident (RAM fast path). */
        CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == B_RM_ID);
        CHECK(g_current_rm_clearing.len_words == B_CLEAR_WORDS);
        CHECK(g_current_rm_clearing.data != NULL);

        /* -- SWAP 3: b -> a. Completes the a->b->a cycle: a's FULL clearing is
         * received + RAM-staged AGAIN and becomes resident again. ----------- */
        s_icap_count = 0;
        drive_swap("nanosoc", A_RM_ID, A_CLEAR_WORDS, A_CLEAR_SEED, A_PART_WORDS, A_PART_SEED);
        CHECK(swap_fsm_idle());
        CHECK(swap_fsm_last_result()->ok && swap_fsm_last_result()->verified);
        CHECK(swap_fsm_last_result()->rm_id == A_RM_ID);
        /* a is resident again, still in RAM, full size. */
        CHECK(g_current_rm_clearing.valid && g_current_rm_clearing.rm_id == A_RM_ID);
        CHECK(g_current_rm_clearing.len_words == A_CLEAR_WORDS);
        CHECK(g_current_rm_clearing.data != NULL);

        /* ***** THE CORE ASSERTION: not one call into a store seam across the
         * ENTIRE cycle. */
        assert_zero_flash_writes();

        printf("  [positive] arena=%u: greybox->a->b->a completed, "
               "a's %u B clearing replayed from RAM, 0 flash writes\n",
               (unsigned)arena_cap, (unsigned)NANO_CLEAR_BYTES);
    } else {
        /* NEGATIVE CONTROL (arena too small): swap 1 still SUCCEEDED (its
         * outgoing greybox streamed, a's partial verified) — but a's 117,684 B
         * clearing did NOT fit the tiny arena, so step_cache_clearing() refused
         * the copy and marked the current clearing INVALID. */
        CHECK(!g_current_rm_clearing.valid);

        /* -- SWAP 2 (a -> b) must FAIL CLOSED at SWAP_STREAM_CLEARING: the
         * outgoing clearing is unavailable (this is the silicon bug the arena
         * fixes). It fails BEFORE awaiting any incoming pair, so nothing is
         * pushed. -------------------------------------------------------------- */
        s_icap_count = 0;
        drive_swap("led", B_RM_ID, B_CLEAR_WORDS, B_CLEAR_SEED, B_PART_WORDS, B_PART_SEED);
        CHECK(swap_fsm_idle());
        CHECK(!swap_fsm_last_result()->ok);        /* fail-closed, not success */
        CHECK(!swap_fsm_last_result()->verified);
        CHECK(swap_fsm_state() == SWAP_IDLE);       /* terminal SWAP_FAILED -> IDLE */

        /* The failure did NOT fall back to a flash write: it fails closed, so the
         * teeth of the positive test's zero-write assertion are visible — swap-away
         * only worked in the positive binary BECAUSE the arena held the clearing. */
        assert_zero_flash_writes();

        printf("  [negative] arena=%u < %u: swap-away failed CLOSED at "
               "STREAM_CLEARING (clearing did not fit the arena), 0 flash writes\n",
               (unsigned)arena_cap, (unsigned)NANO_CLEAR_BYTES);
    }
}

int main(void)
{
    test_qspi_free_swap_away_cycle();
    printf("%s: %d checks passed\n", QSPI_FREE_TEST_NAME, s_checks);
    return 0;
}
