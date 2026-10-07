/*
 * test_stage0_flow.c -- the stage0 boot ORDER and the status block, on the host.
 *
 * Links the REAL stage0_core.c + stage0_flow.c and drives s0_status_open() /
 * s0_boot_select() / s0_status_note_handoff() through an ops table backed by an
 * in-memory card (the §6 layout of STAGE0_CONTRACT.md) and a simulated DDR.
 * The status block is one static struct that survives between "runs", exactly
 * as the LMB survives a watchdog warm restart; zeroing it is a reconfiguration.
 *
 * Cases: cold/warm open + fabric identity; 1-region and 4-region slot boots;
 * slot A bad magic / bad payload CRC / truncated / out-of-window -> slot B;
 * both bad; no MBR; no stage0 slots; no card (no reads, not an error); DDR
 * calibration fail (card never touched); default slot B from the boot-select
 * sector, torn copies, seq ordering; TRY-ONCE-THEN-CONFIRM end to end (A x2
 * unconfirmed -> B -> B x2 -> rescue "slots exhausted"; confirm resets; rescue
 * confirm resets both; seq change resets; reconfiguration resets; an attempt is
 * judged exactly once); boot_limit 0; and that the flow never writes the card.
 *
 * COLD-BOOT HARDENING (S0-COLDFIX): with the optional ops wired (settle, guard,
 * now_ms) and an injectable "DDR stall" (the fake card read longjmps out, as a
 * frozen hart does until the watchdog resets it) and "cold entry" / watchdog
 * reset_cause: a cold entry settles before any DDR or card access and warm
 * ones do not; a reset inside the settle settles again; two pre-hand-off
 * watchdog restarts go straight to rescue without touching the card, and any
 * other reset breaks the run; a calib drop mid-load retries the slots or
 * rescues "ddr calib fail" (S0_DDR_LOST); a DDR recovery in the same run
 * reboots without a second settle; a slot over S0_SLOT_TIME_MS falls to the
 * other; and the new status words (subphase, entry, prev_*, ddr_ok_ms).
 */
#include <setjmp.h>
#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_flow.h"

/* ---- the simulated board ------------------------------------------------------- */

#define CARD_BLOCKS   (256u * 2048u)          /* 256 MiB, calloc'd lazily */
#define DDR_BASE      0x80000000u
#define DDR_SPAN      0x10000000u            /* 256 MiB of the 1 GiB */
#define LBA_A         67584u
#define LBA_B         198656u
#define SLOT_BLOCKS   131072u

static uint8_t *g_card;
static uint8_t *g_ddr;
static int      g_ddr_state = S0_DDR_OK;
static int      g_sd_state  = S0_SD_READY;
static unsigned g_sd_inits, g_sd_reads, g_sd_writes;
static int      g_verbose;

static int op_ddr(void *c) { (void)c; return g_ddr_state; }

static int op_sd_init(void *c, uint32_t *blocks, uint32_t *detail)
{
    (void)c;
    g_sd_inits++;
    *blocks = g_sd_state == S0_SD_READY ? CARD_BLOCKS : 0u;
    *detail = 0x00030000u;
    return g_sd_state;
}

static int op_sd_read(void *c, uint32_t lba, uint32_t n, void *dst)
{
    (void)c;
    g_sd_reads++;
    if (lba >= CARD_BLOCKS || n > CARD_BLOCKS - lba)
        return -1;
    memcpy(dst, g_card + (size_t)lba * 512u, (size_t)n * 512u);
    return 0;
}

/* the target's rule: destinations only inside [0x8000_0000, 0xB000_0000) --
 * here the simulated span, which is inside that window */
static void *op_a2p(uint32_t a, uint32_t len, void *c)
{
    (void)c;
    if (!s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len))
        return NULL;
    return g_ddr + (a - DDR_BASE);
}

static void op_log(void *c, const char *line)
{
    (void)c;
    if (g_verbose)
        printf("    | %s\n", line);
}

static const struct s0_flow_ops OPS = { op_ddr, op_sd_init, op_sd_read, op_a2p, op_log, NULL,
                                        NULL, NULL, NULL };

static struct s0_status ST;

/* ---- the hardened platform: settle / guard / clock + a DDR stall -------------- */

static char     g_ev[4096];            /* event trace: S settle, D ddr, I init, R read */
static unsigned g_ev_n;
static uint32_t g_clock;               /* the ms clock ops->now_ms reads */
static uint32_t g_settles, g_settle_ms, g_ddr_calls, g_guards;
static uint32_t g_guard_fail_at;       /* the Nth guard call reports a calib drop (0 = never) */
static uint32_t g_guard_fail_n;        /* ... for this many calls */
static uint32_t g_ms_per_read;         /* clock cost of one card read */
static uint32_t g_slow_lba_lo, g_slow_lba_hi, g_slow_ms;   /* reads in [lo,hi) cost g_slow_ms */
static int      g_ddr_script[8];       /* successive op_ddr results (0 = g_ddr_state) */
static unsigned g_ddr_script_i;
static uint32_t g_stall_lba;           /* a read at/after this LBA in slot A stalls (0 = off) */
static int      g_settle_reset;        /* the settle is cut short by a reset */
static jmp_buf  g_reset;               /* "the board reset" */

static void ev(char c) { if (g_ev_n < sizeof g_ev - 1u) { g_ev[g_ev_n++] = c; g_ev[g_ev_n] = 0; } }

static int hop_ddr(void *c)
{
    (void)c;
    ev('D');
    g_ddr_calls++;
    g_clock += 1000u;                  /* the target's hold */
    if (g_ddr_script_i < 8u && g_ddr_script[g_ddr_script_i])
        return g_ddr_script[g_ddr_script_i++];
    return g_ddr_state;
}
static int hop_sd_init(void *c, uint32_t *b, uint32_t *d) { ev('I'); return op_sd_init(c, b, d); }
static int hop_sd_read(void *c, uint32_t lba, uint32_t n, void *dst)
{
    ev('R');
    g_clock += g_ms_per_read;
    if (lba >= g_slow_lba_lo && lba < g_slow_lba_hi)
        g_clock += g_slow_ms;
    if (g_stall_lba && lba >= g_stall_lba && lba < LBA_A + SLOT_BLOCKS)
        longjmp(g_reset, 1);           /* the hart froze; the watchdog resets it */
    return op_sd_read(c, lba, n, dst);
}
static void hop_settle(void *c, uint32_t ms)
{
    (void)c;
    ev('S');
    g_settles++;
    g_settle_ms = ms;
    if (g_settle_reset) {
        g_settle_reset = 0;
        g_clock += ms / 2u;
        longjmp(g_reset, 1);           /* an MMCM lock loss half way through */
    }
    g_clock += ms;
}
static uint32_t g_guard_fail_sub;      /* ST.subphase when the guard first reported a drop */
static int hop_guard(void *c)
{
    (void)c;
    g_guards++;
    if (g_guard_fail_at && g_guards >= g_guard_fail_at && g_guards < g_guard_fail_at + g_guard_fail_n) {
        if (!g_guard_fail_sub)
            g_guard_fail_sub = ST.subphase;
        return 0;
    }
    return 1;
}
static uint32_t hop_now(void *c) { (void)c; return g_clock; }

static const struct s0_flow_ops HOPS = { hop_ddr, hop_sd_init, hop_sd_read, op_a2p, op_log, NULL,
                                         hop_settle, hop_guard, hop_now };

static void hreset_model(void)
{
    g_ev_n = 0; g_ev[0] = 0;
    g_clock = 0; g_settles = g_settle_ms = g_ddr_calls = g_guards = 0;
    g_guard_fail_at = g_guard_fail_n = 0; g_guard_fail_sub = 0;
    g_ms_per_read = 0; g_slow_lba_lo = g_slow_lba_hi = g_slow_ms = 0;
    memset(g_ddr_script, 0, sizeof g_ddr_script); g_ddr_script_i = 0;
    g_stall_lba = 0; g_settle_reset = 0;
    g_sd_inits = g_sd_reads = 0;
}
static const struct s0_build_ids IDS = { 0xEAFE7878u, 0x3F1A560Fu, 0x0B0C0D0Eu };

/* ---- card building ------------------------------------------------------------------- */

static uint8_t g_img[1u << 20];
static uint8_t g_pay[4][200000];

static void card_reset(void)
{
    memset(g_card, 0, 4u * 1024u * 1024u);           /* MBR + boot-select area */
    memset(g_card + (size_t)LBA_A * 512u, 0, 1u << 20);
    memset(g_card + (size_t)LBA_B * 512u, 0, 1u << 20);
}

static void card_mbr(uint32_t a_blocks, uint32_t b_blocks)
{
    struct tu_part p[4] = {
        { a_blocks ? 0x7F : 0, LBA_A, a_blocks },
        { b_blocks ? 0x7F : 0, LBA_B, b_blocks },
        { 0x83, 329728u, 65536u },
        { 0xDA, 2048u, 65536u },
    };
    tu_build_mbr(g_card, p);
}

static void card_bootsel(int copy, uint32_t seq, uint32_t def)
{
    tu_build_bootsel(g_card + 512u * (copy ? 2u : 1u), seq, def);
}

/* one-region image with payload seed s at 0x80000000; returns its length */
static uint32_t put_1region(uint32_t lba, uint32_t len, uint32_t seed, uint32_t pc)
{
    tu_fill(g_pay[0], len, seed);
    struct tu_region r = { g_pay[0], len, DDR_BASE };
    uint32_t n = tu_build_image(g_img, sizeof g_img, &r, 1, pc, 0, 0);
    memcpy(g_card + (size_t)lba * 512u, g_img, n);
    return n;
}

static uint32_t put_4region(uint32_t lba)
{
    static const uint32_t len[4] = { 4096, 180000, 1024, 150001 };
    static const uint32_t dst[4] = { 0x80000000u, 0x80400000u, 0x82200000u, 0x84000000u };
    struct tu_region r[4];
    for (int i = 0; i < 4; i++) {
        tu_fill(g_pay[i], len[i], 100u + (uint32_t)i);
        r[i].data = g_pay[i];
        r[i].len = len[i];
        r[i].dst = dst[i];
    }
    uint32_t n = tu_build_image(g_img, sizeof g_img, r, 4, 0x80000000u, 0, 0x82200000u);
    memcpy(g_card + (size_t)lba * 512u, g_img, n);
    return n;
}

static int ddr_holds(uint32_t addr, const uint8_t *p, uint32_t n)
{
    return memcmp(g_ddr + (addr - DDR_BASE), p, n) == 0;
}

/* one stage0 run: open + select (+ hand-off on success) */
static int run(struct s0_result *res, int *ddr_ok)
{
    s0_status_open(&ST, &IDS, 0u, S0_BOOT_LIMIT);
    memset(g_ddr, 0, 1u << 20);
    int from = s0_boot_select(&ST, &OPS, res, ddr_ok);
    if (from != S0_FROM_NONE)
        s0_status_note_handoff(&ST, from, res, 1234u);
    return from;
}

static void reconfigure(void) { memset(&ST, 0, sizeof ST); }

/* One HARDENED stage0 run: entry with reset_cause, then the boot order; a
 * "reset" (longjmp) inside it returns -1 and leaves the block as the hart
 * left it. *kind = the entry kind. */
static int hrun(uint32_t cause, uint32_t *kind, struct s0_result *res, int *ok)
{
    static volatile int from;
    g_clock = 0;                                     /* ms since this entry */
    *kind = s0_status_open(&ST, &IDS, cause, S0_BOOT_LIMIT);
    memset(g_ddr, 0, 1u << 20);
    if (setjmp(g_reset))
        return -1;
    from = s0_boot_select(&ST, &HOPS, res, ok);
    if (from != S0_FROM_NONE)
        s0_status_note_handoff(&ST, from, res, g_clock);
    return from;
}

/* ---- tests ------------------------------------------------------------------------------ */

static void t_open(void)
{
    printf("test: status block cold open, warm open, identity, per-run reset\n");
    memset(&ST, 0xA5, sizeof ST);                    /* garbage */
    s0_status_open(&ST, &IDS, 0x8u, 2u);
    CHECK(ST.magic == S0_STATUS_MAGIC && ST.magic_end == S0_STATUS_MAGIC, "magic");
    CHECK(ST.version == 1u && ST.size == 256u, "version/size");
    CHECK(ST.boot_count == 1u, "boot_count %u", ST.boot_count);
    CHECK(ST.fabric_static_id == 0x3F1A560Fu && ST.fabric_ver32 == 0x0B0C0D0Eu,
          "fabric identity stamped");
    CHECK(ST.build_id == 0xEAFE7878u && ST.reset_cause == 0x8u && ST.boot_limit == 2u, "ids");
    CHECK(ST.fails_a == 0 && ST.n_boot_a == 0 && ST.prev_phase == 0 && ST.prev_uptime_ms == 0,
          "cold zeroes NOINIT + prev_*");
    CHECK(S0_ENTRY_KIND(ST.entry) == S0_EK_COLD && (ST.entry & S0_ENTRY_SETTLE_PENDING) &&
          S0_ENTRY_WDOG_RUN(ST.entry) == 0u, "cold entry, settle pending (entry 0x%X)", ST.entry);
    CHECK(ST.slot_a_rc == S0_ENOTTRIED && ST.slot_b_rc == S0_ENOTTRIED, "not tried");
    ST.n_boot_a = 7; ST.slot_a_rc = 0; ST.phase = 5;
    s0_status_open(&ST, &IDS, 0u, 2u);
    CHECK(ST.boot_count == 2u && ST.n_boot_a == 7u, "warm keeps NOINIT");
    CHECK(ST.slot_a_rc == S0_ENOTTRIED && ST.phase == S0_PH_RESET, "warm resets per-run");
    ST.magic_end = 0;                                /* torn */
    s0_status_open(&ST, &IDS, 0u, 2u);
    CHECK(ST.boot_count == 1u && ST.n_boot_a == 0u, "bad magic_end = re-initialise");
}

static void t_slot_boots(void)
{
    struct s0_result res;
    int ok;
    printf("test: 1-region and 4-region images boot from slot A\n");
    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 150001, 1, 0x80000000u);
    CHECK(run(&res, &ok) == S0_FROM_A, "1-region A");
    CHECK(ok && ST.slot_a_rc == S0_OK && ST.slot_b_rc == S0_ENOTTRIED, "rcs");
    CHECK(ddr_holds(DDR_BASE, g_pay[0], 150001), "payload in DDR");
    CHECK(res.entry_pc == 0x80000000u && res.entry_a1 == 0u, "hand-off");
    CHECK(ST.phase == S0_PH_HANDOFF && ST.booted_from == S0_FROM_A && ST.att_from == S0_FROM_A &&
          ST.att_confirm == 0u && ST.n_boot_a == 1u && ST.image_hdr_crc == res.header_crc32,
          "status after hand-off");
    CHECK(ST.default_slot == S0_FROM_A && ST.cfg_seq == 0u, "no boot-select = A");

    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_4region(LBA_A);
    CHECK(run(&res, &ok) == S0_FROM_A, "4-region A");
    CHECK(res.entry_a1 == 0x82200000u, "a1 = dtb");
    CHECK(ddr_holds(0x80400000u, g_pay[1], 180000) && ddr_holds(0x84000000u, g_pay[3], 150001),
          "4 regions placed");
    CHECK(g_sd_writes == 0u, "no card writes");
}

static void t_fallback(void)
{
    struct s0_result res;
    int ok;
    printf("test: slot A bad (magic / payload CRC / truncated / out of window) -> slot B\n");

    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 5000, 2, 0x80000000u);
    put_1region(LBA_B, 7000, 3, 0x80000000u);
    g_card[(size_t)LBA_A * 512u] ^= 0xFF;                    /* magic */
    CHECK(run(&res, &ok) == S0_FROM_B, "bad magic -> B");
    CHECK(ST.slot_a_rc == S0_EMAGIC && ST.slot_b_rc == S0_OK && ST.n_fallback == 1u, "rc/fallback");
    CHECK(ddr_holds(DDR_BASE, g_pay[0], 7000), "B's payload");

    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 5000, 2, 0x80000000u);
    put_1region(LBA_B, 7000, 3, 0x80000000u);
    g_card[(size_t)LBA_A * 512u + 4096u + 17u] ^= 1u;         /* payload byte */
    CHECK(run(&res, &ok) == S0_FROM_B && ST.slot_a_rc == S0_ECRC, "bad payload CRC -> B");
    CHECK(ST.last_error == S0_LAST_ERROR(S0_ES_SLOT_A, S0_ECRC), "last_error");

    reconfigure(); card_reset();
    put_1region(LBA_A, 60000, 2, 0x80000000u);                /* needs 126 blocks */
    put_1region(LBA_B, 7000, 3, 0x80000000u);
    card_mbr(100u, SLOT_BLOCKS);                               /* partition too small */
    CHECK(run(&res, &ok) == S0_FROM_B && ST.slot_a_rc == S0_EREAD, "truncated slot -> B");

    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 5000, 2, 0x70000000u);
    {   /* region destination outside the DDR window */
        tu_fill(g_pay[0], 5000, 2);
        struct tu_region r = { g_pay[0], 5000, 0x70000000u };
        uint32_t n = tu_build_image(g_img, sizeof g_img, &r, 1, 0x80000000u, 0, 0);
        memcpy(g_card + (size_t)LBA_A * 512u, g_img, n);
    }
    put_1region(LBA_B, 7000, 3, 0x80000000u);
    CHECK(run(&res, &ok) == S0_FROM_B && ST.slot_a_rc == S0_EREAD, "out-of-window -> B");

    printf("test: both slots bad -> rescue \"no valid slot\"\n");
    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 5000, 2, 0x80000000u);
    g_card[(size_t)LBA_A * 512u + 12u] ^= 0x10u;              /* entry_pc -> table CRC */
    CHECK(run(&res, &ok) == S0_FROM_NONE, "none");
    CHECK(ST.slot_a_rc == S0_EHDRCRC && ST.slot_b_rc == S0_EMAGIC, "rcs %u %u", ST.slot_a_rc, ST.slot_b_rc);
    CHECK(ST.rescue_reason == S0_RR_BADSLOTS, "reason %u", ST.rescue_reason);
    CHECK(ST.phase != S0_PH_HANDOFF && ST.booted_from == S0_FROM_NONE, "no hand-off");
}

static void t_card_states(void)
{
    struct s0_result res;
    int ok;
    printf("test: no MBR / no stage0 slots / no card / DDR calibration fail\n");

    reconfigure(); card_reset();                              /* blank card */
    CHECK(run(&res, &ok) == S0_FROM_NONE && ST.sd_result == S0_SD_NOMBR, "no MBR");
    CHECK(ST.rescue_reason == S0_RR_NOLAYOUT, "reason blank card");

    reconfigure(); card_reset(); card_mbr(0, 0);              /* foreign: no 0x7F */
    CHECK(run(&res, &ok) == S0_FROM_NONE && ST.slot_a_rc == S0_ENOSLOT &&
          ST.slot_b_rc == S0_ENOSLOT && ST.rescue_reason == S0_RR_NOLAYOUT, "no stage0 slots");

    reconfigure(); g_sd_state = S0_SD_NOCARD; g_sd_reads = 0;
    CHECK(run(&res, &ok) == S0_FROM_NONE, "no card");
    CHECK(ST.sd_result == S0_SD_NOCARD && ST.rescue_reason == S0_RR_NOCARD, "reason no card");
    CHECK(ST.last_error == 0u, "an empty slot is not an error");
    CHECK(g_sd_reads == 0u, "no reads with no card");
    CHECK(ok, "DDR fine");
    g_sd_state = S0_SD_READY;

    reconfigure(); g_ddr_state = S0_DDR_FAIL; g_sd_inits = 0;
    CHECK(run(&res, &ok) == S0_FROM_NONE && !ok, "ddr fail");
    CHECK(g_sd_inits == 0u, "card never touched without DDR");
    CHECK(ST.ddr_calib == S0_DDR_FAIL && ST.sd_result == S0_SD_SKIPPED &&
          ST.rescue_reason == S0_RR_DDR, "ddr fail recorded");
    CHECK(strcmp(s0_rescue_reason_text(ST.rescue_reason), "ddr calib fail") == 0, "reason text");
    g_ddr_state = S0_DDR_IMPLIED;
    card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS); put_1region(LBA_A, 5000, 2, 0x80000000u);
    CHECK(run(&res, &ok) == S0_FROM_A && ok && ST.ddr_calib == S0_DDR_IMPLIED, "CALIB=none boots");
    g_ddr_state = S0_DDR_OK;
}

static void t_default_slot(void)
{
    struct s0_result res;
    int ok;
    printf("test: boot-select sector: default B, torn copy, seq order, invalid\n");
    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 5000, 2, 0x80000000u);
    put_1region(LBA_B, 7000, 3, 0x80000000u);
    card_bootsel(0, 5, 2);
    CHECK(run(&res, &ok) == S0_FROM_B && ST.default_slot == S0_FROM_B && ST.cfg_seq == 5u, "default B");
    CHECK(ST.slot_a_rc == S0_ENOTTRIED && ST.n_fallback == 0u, "A not tried, not a fallback");

    card_bootsel(1, 6, 1);                                    /* newer copy says A */
    CHECK(run(&res, &ok) == S0_FROM_A && ST.cfg_seq == 6u, "higher seq wins");
    g_card[2u * 512u + 40u] ^= 1u;                            /* tear the newer copy */
    CHECK(run(&res, &ok) == S0_FROM_B && ST.cfg_seq == 5u, "torn copy -> the other");
    card_bootsel(1, 7, 3);                                    /* invalid slot value */
    CHECK(run(&res, &ok) == S0_FROM_B, "invalid default ignored");

    card_bootsel(0, 9, 2);
    g_card[(size_t)LBA_B * 512u + 4096u] ^= 1u;               /* default B broken */
    CHECK(run(&res, &ok) == S0_FROM_A && ST.slot_b_rc == S0_ECRC, "default B bad -> A");
    CHECK(ST.n_fallback >= 1u, "that is a fallback");
}

static void t_confirm(void)
{
    struct s0_result res;
    int ok;
    printf("test: try-once-then-confirm (N=%u)\n", (unsigned)S0_BOOT_LIMIT);
    reconfigure(); card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 5000, 2, 0x80000000u);
    put_1region(LBA_B, 7000, 3, 0x80000000u);

    CHECK(run(&res, &ok) == S0_FROM_A, "attempt 1: A");
    /* watchdog restart, Linux never confirmed */
    CHECK(run(&res, &ok) == S0_FROM_A, "attempt 2: A again");
    CHECK(ST.fails_a == 1u && ST.last_verdict == S0_VD_UNCONFIRMED && ST.verdict_from == S0_FROM_A,
          "one failure counted");
    CHECK(run(&res, &ok) == S0_FROM_B, "A exhausted -> B");
    CHECK(ST.fails_a == 2u && ST.slot_a_rc == S0_ELIMIT && ST.n_fallback == 1u, "limit + fallback");
    CHECK(run(&res, &ok) == S0_FROM_B && ST.fails_b == 1u, "B retry");
    CHECK(run(&res, &ok) == S0_FROM_NONE, "both exhausted -> rescue");
    CHECK(ST.rescue_reason == S0_RR_EXHAUSTED && ST.fails_b == 2u, "reason exhausted");
    CHECK(ST.att_from == S0_FROM_NONE, "nothing pending");
    CHECK(run(&res, &ok) == S0_FROM_NONE && ST.fails_b == 2u, "judged once: no double count");

    printf("test: a confirmed boot clears its counter\n");
    reconfigure();
    CHECK(run(&res, &ok) == S0_FROM_A, "A");
    CHECK(run(&res, &ok) == S0_FROM_A && ST.fails_a == 1u, "A unconfirmed once");
    ST.att_confirm = S0_CONFIRM_MAGIC;                        /* harnessd is healthy */
    CHECK(run(&res, &ok) == S0_FROM_A, "A");
    CHECK(ST.fails_a == 0u && ST.last_verdict == S0_VD_CONFIRMED, "confirmed -> 0");
    ST.att_confirm = 0x12345678u;                             /* not the magic */
    run(&res, &ok);
    CHECK(ST.fails_a == 1u, "a wrong confirm value does not count");

    printf("test: a confirmed rescue boot, a new boot-select seq, a reconfiguration reset both\n");
    ST.fails_a = 2; ST.fails_b = 2;
    s0_status_note_handoff(&ST, S0_FROM_RESCUE, &res, 5u);
    ST.att_confirm = S0_CONFIRM_MAGIC;
    CHECK(run(&res, &ok) == S0_FROM_A && ST.fails_b == 0u, "rescue confirm resets both");
    ST.fails_a = 2; ST.fails_b = 2; ST.att_from = 0;
    card_bootsel(0, 11, 1);
    CHECK(run(&res, &ok) == S0_FROM_A && ST.cfg_seq == 11u, "new seq resets both");
    ST.fails_a = 2; ST.fails_b = 2;
    reconfigure();
    CHECK(run(&res, &ok) == S0_FROM_A && ST.boot_count == 1u, "power cycle clears everything");
    s0_status_note_handoff(&ST, S0_FROM_RESCUE, &res, 5u);
    CHECK(run(&res, &ok) == S0_FROM_A && ST.fails_a == 0u && ST.fails_b == 0u,
          "an unconfirmed rescue boot costs no slot");

    printf("test: boot_limit 0 never skips a slot\n");
    reconfigure();
    for (int i = 0; i < 5; i++) {
        s0_status_open(&ST, &IDS, 0u, 0u);
        int from = s0_boot_select(&ST, &OPS, &res, &ok);
        CHECK(from == S0_FROM_A, "run %d A", i);
        s0_status_note_handoff(&ST, from, &res, 1u);
    }
    s0_status_open(&ST, &IDS, 0u, 0u);
    CHECK(ST.fails_a == 5u, "still counted: %u", ST.fails_a);
}

/* ---- cold-boot hardening ------------------------------------------------------------ */

static void hcard(void)
{
    card_reset(); card_mbr(SLOT_BLOCKS, SLOT_BLOCKS);
    put_1region(LBA_A, 150001, 1, 0x80000000u);      /* ~294 blocks: ~37 guarded reads */
    put_1region(LBA_B, 7000, 3, 0x80000000u);
}

static void t_cold_settle(void)
{
    struct s0_result res;
    int ok;
    uint32_t k;
    printf("test: COLD entry settles before any DDR / card access; warm entries do not\n");
    reconfigure(); hcard(); hreset_model();
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && k == S0_EK_COLD, "cold boot from A (kind %u)", k);
    CHECK(g_settles == 1u && g_settle_ms == S0_COLD_SETTLE_MS && S0_COLD_SETTLE_MS == 10000u,
          "one settle of %u ms", g_settle_ms);
    CHECK(g_ev[0] == 'S' && strchr(g_ev + 1, 'S') == NULL, "the settle came FIRST: trace %.12s...", g_ev);
    CHECK(g_ev[1] == 'D' && g_ev[2] == 'I', "then the DDR gate, then the card (%.6s)", g_ev);
    CHECK(!(ST.entry & S0_ENTRY_SETTLE_PENDING), "settle no longer pending");
    CHECK(ST.ddr_ok_ms >= S0_COLD_SETTLE_MS + 1000u, "ddr_ok_ms %u >= settle + hold", ST.ddr_ok_ms);
    CHECK(g_guards > 30u, "every card read op of the load was guarded (%u)", g_guards);
    CHECK((ST.subphase >> 24) == S0_SP_CRC, "last subphase: the CRC (0x%X)", ST.subphase);

    printf("test: warm entries (PB0 / reboot after the hand-off) do not settle\n");
    uint32_t ho_ms = ST.handoff_ms;
    hreset_model();
    ST.att_confirm = S0_CONFIRM_MAGIC;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && k == S0_EK_WARM, "warm boot (kind %u)", k);
    CHECK(g_settles == 0u && g_ev[0] == 'D', "no settle: straight to the gate (%.4s)", g_ev);
    CHECK(ST.ddr_ok_ms == 1000u, "ddr_ok_ms %u = the hold only", ST.ddr_ok_ms);
    CHECK(S0_PREV_PHASE(ST.prev_phase) == S0_PH_HANDOFF && ST.prev_uptime_ms == ho_ms && ho_ms >= 11000u,
          "prev_phase = the hand-off (0x%X), prev_uptime = its hand-off ms (%u)",
          ST.prev_phase, ST.prev_uptime_ms);
    hreset_model();
    CHECK(hrun(S0_RESET_WRS | 0x12340u, &k, &res, &ok) == S0_FROM_A && k == S0_EK_WDOG_LINUX,
          "a watchdog AFTER a hand-off is Linux's (kind %u)", k);
    CHECK(S0_ENTRY_WDOG_RUN(ST.entry) == 0u && g_settles == 0u && ST.fails_a == 1u,
          "not counted here; try-once-then-confirm counted it (fails_a %u)", ST.fails_a);

    printf("test: a reset INSIDE the cold settle (MMCM lock loss, no watchdog) settles again\n");
    reconfigure(); hcard(); hreset_model();
    g_settle_reset = 1;
    CHECK(hrun(0u, &k, &res, &ok) == -1 && k == S0_EK_COLD, "reset half way through the settle");
    CHECK(ST.prev_phase == 0u && (ST.entry & S0_ENTRY_SETTLE_PENDING), "still pending");
    hreset_model();
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && k == S0_EK_RESETTLE, "re-settle entry (kind %u)", k);
    CHECK(g_settles == 1u && g_ev[0] == 'S', "settled again before anything else");
    CHECK(S0_PREV_PHASE(ST.prev_phase) == S0_PH_DDR && S0_PREV_SP(ST.prev_phase) == S0_SP_SETTLE &&
          S0_PREV_DETAIL(ST.prev_phase) == S0_COLD_SETTLE_MS, "prev: ddr/settle (0x%X)", ST.prev_phase);
    CHECK(ST.label_lo == 0u && ST.label_hi == 0u,
          "0xEC / 0xF0: s0_status_open never writes the label (stage0.c publishes it)");
    hreset_model();
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && k == S0_EK_WARM && g_settles == 0u,
          "after a completed settle: plain warm");
}

static void t_wdog_loop(void)
{
    struct s0_result res;
    int ok;
    uint32_t k;
    printf("test: a DDR STALL in slot A (the hart freezes; the watchdog resets it) x2 -> rescue\n");
    reconfigure(); hcard(); hreset_model();
    g_stall_lba = LBA_A + 100u;
    CHECK(hrun(0u, &k, &res, &ok) == -1 && k == S0_EK_COLD, "cold boot stalls in the load");
    CHECK(ST.phase == S0_PH_SLOT_A && (ST.subphase >> 24) == S0_SP_SLOT_LOAD &&
          (ST.subphase & 0xFFFFFFu) >= 100u, "the block shows where: 0x%X", ST.subphase);
    uint32_t hb_sub = ST.subphase;
    ST.heartbeat = 777u; ST.uptime_ms = 12345u;     /* what the target's poll hook left */

    hreset_model();
    g_stall_lba = LBA_A + 100u;
    CHECK(hrun(S0_RESET_WRS, &k, &res, &ok) == -1 && k == S0_EK_WDOG, "watchdog entry 1 (kind %u)", k);
    CHECK(S0_ENTRY_WDOG_RUN(ST.entry) == 1u && g_settles == 0u, "run 1, no settle");
    CHECK(ST.prev_phase == S0_PREV(S0_PH_SLOT_A, hb_sub) && S0_PREV_SP(ST.prev_phase) == S0_SP_SLOT_LOAD &&
          S0_PREV_DETAIL(ST.prev_phase) == (hb_sub & 0xFFFFu) && ST.prev_uptime_ms == 12345u,
          "prev_* = the stalled run (0x%X)", ST.prev_phase);
    CHECK(S0_PREV(S0_PH_SLOT_B, S0_SUBPHASE(S0_SP_SLOT_LOAD, 131071u)) ==
          (0xFFFF0000u | (S0_SP_SLOT_LOAD << 8) | S0_PH_SLOT_B), "a block index past 0xFFFF saturates");

    hreset_model();
    g_stall_lba = LBA_A + 100u;                      /* still broken: never reached */
    CHECK(hrun(S0_RESET_WRS, &k, &res, &ok) == S0_FROM_NONE && S0_ENTRY_WDOG_RUN(ST.entry) == 2u,
          "the 2nd pre-hand-off watchdog restart: straight to rescue (run %u)",
          S0_ENTRY_WDOG_RUN(ST.entry));
    CHECK(ST.rescue_reason == S0_RR_WDOG && strcmp(s0_rescue_reason_text(ST.rescue_reason),
          "stage0 watchdog loop") == 0, "reason: stage0 watchdog loop");
    CHECK(g_sd_inits == 0u && g_sd_reads == 0u && g_settles == 0u, "card never touched");
    CHECK(g_ddr_calls == 1u && ok && ST.ddr_calib == S0_DDR_OK, "the DDR gate still ran: pushes allowed");
    CHECK(ST.last_error == S0_LAST_ERROR(S0_ES_WDOG, 2u) && ST.sd_result == S0_SD_SKIPPED,
          "last_error 0x%X", ST.last_error);

    printf("test: any other reset breaks the watchdog run (PB0 -> the normal path)\n");
    hreset_model();
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && k == S0_EK_WARM && S0_ENTRY_WDOG_RUN(ST.entry) == 0u,
          "PB0: warm, run 0, boots A");
    ST.att_confirm = S0_CONFIRM_MAGIC;
    hreset_model();
    g_stall_lba = LBA_A + 5u;
    CHECK(hrun(0u, &k, &res, &ok) == -1, "stall");
    hreset_model();
    g_stall_lba = LBA_A + 5u;
    CHECK(hrun(S0_RESET_WRS, &k, &res, &ok) == -1 && S0_ENTRY_WDOG_RUN(ST.entry) == 1u, "run 1");
    hreset_model();
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && S0_ENTRY_WDOG_RUN(ST.entry) == 0u,
          "a non-watchdog reset in between: back to 0, boots");
    hreset_model();
    CHECK(hrun(S0_RESET_WRS, &k, &res, &ok) == S0_FROM_A && k == S0_EK_WDOG_LINUX &&
          S0_ENTRY_WDOG_RUN(ST.entry) == 0u, "a hand-off in between: the run restarts");
}

static void t_calib_drop(void)
{
    struct s0_result res;
    int ok;
    uint32_t k;
    printf("test: calib drops mid-load: gate holds again -> slots retried -> boots\n");
    reconfigure(); hcard(); hreset_model();
    g_guard_fail_at = 10u; g_guard_fail_n = 1u;      /* one glitch at the 10th read op */
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A, "booted A on the retry");
    CHECK(g_ddr_calls == 2u && S0_ENTRY_CALIB_DROPS(ST.entry) == 1u,
          "gate re-run once (%u), one drop counted (0x%X)", g_ddr_calls, ST.entry);
    tu_fill(g_pay[0], 150001, 1);
    CHECK(ddr_holds(DDR_BASE, g_pay[0], 150001), "the whole payload, reloaded and CRC-checked");

    printf("test: calib drops during the in-DDR CRC pass (s0_load_guarded's hook) -> retried\n");
    reconfigure(); hcard(); hreset_model();
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A, "clean run");
    uint32_t last_gate = g_guards;                   /* the last gate = the last CRC chunk */
    reconfigure(); hreset_model();
    g_guard_fail_at = last_gate; g_guard_fail_n = 1u;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A && g_ddr_calls == 2u,
          "the drop hit the CRC, the slots were retried (%u gate runs)", g_ddr_calls);
    CHECK((g_guard_fail_sub >> 24) == S0_SP_CRC, "it was the CRC pass (subphase 0x%X)", g_guard_fail_sub);
    CHECK(g_guards == 2u * last_gate, "the CRC ran in %u-byte chunks, each gated (%u / %u)",
          (unsigned)S0_CRC_CHUNK, g_guards, last_gate);

    printf("test: calib drops and the gate does not hold again -> rescue, DDR LOST\n");
    reconfigure(); hcard(); hreset_model();
    g_guard_fail_at = 10u; g_guard_fail_n = 1u;
    g_ddr_script[0] = S0_DDR_OK; g_ddr_script[1] = S0_DDR_FAIL;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_NONE && !ok, "rescue, no DDR");
    CHECK(ST.ddr_calib == S0_DDR_LOST && ST.rescue_reason == S0_RR_DDR &&
          ST.slot_a_rc == S0_EDDR && ST.slot_b_rc == S0_ENOTTRIED,
          "ddr_calib %u, reason %u, A %u, B not tried", ST.ddr_calib, ST.rescue_reason, ST.slot_a_rc);
    CHECK(ST.last_error == S0_LAST_ERROR(S0_ES_DDR, S0_DDR_LOST), "last_error 0x%X", ST.last_error);

    printf("test: rescue sees calib come good -> the SAME run boots, no second settle\n");
    hreset_model();
    int from = s0_boot_select(&ST, &HOPS, &res, &ok);      /* main's retry, no s0_status_open */
    CHECK(from == S0_FROM_A && ok && g_settles == 0u, "recovered boot from A, no settle");
    CHECK(S0_ENTRY_DDR_RECOVER(ST.entry) == 1u && ST.ddr_calib == S0_DDR_OK &&
          ST.rescue_reason == S0_RR_NONE && ST.slot_b_rc == S0_ENOTTRIED,
          "one recovery counted, per-attempt fields reset (0x%X)", ST.entry);

    printf("test: calib drops on every retry too -> DDR LOST after S0_DDR_RETRIES\n");
    reconfigure(); hcard(); hreset_model();
    g_guard_fail_at = 5u; g_guard_fail_n = 1000000u;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_NONE && ST.ddr_calib == S0_DDR_LOST &&
          g_ddr_calls == 1u + S0_DDR_RETRIES, "bounded: %u gate runs", g_ddr_calls);

    printf("test: DDR calib FAIL at the gate -> rescue; recovery in the same run boots\n");
    reconfigure(); hcard(); hreset_model();
    g_ddr_script[0] = S0_DDR_FAIL;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_NONE && !ok && ST.ddr_calib == S0_DDR_FAIL, "ddr fail");
    CHECK(g_sd_inits == 0u && g_settles == 1u, "settled, card never touched");
    CHECK(s0_boot_select(&ST, &HOPS, &res, &ok) == S0_FROM_A && g_settles == 1u &&
          S0_ENTRY_DDR_RECOVER(ST.entry) == 1u, "recovered without a second settle");
}

static void t_slot_time(void)
{
    struct s0_result res;
    int ok;
    uint32_t k;
    printf("test: F2: slot A over S0_SLOT_TIME_MS -> slot B\n");
    reconfigure(); hcard(); hreset_model();
    g_slow_lba_lo = LBA_A; g_slow_lba_hi = LBA_A + SLOT_BLOCKS; g_slow_ms = 20000u;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_B, "B booted");
    CHECK(ST.slot_a_rc == S0_ESLOW && strcmp(s0_strerror(S0_ESLOW), "slot load time limit") == 0,
          "A: %s", s0_strerror((int)ST.slot_a_rc));
    CHECK(ST.n_fallback == 1u, "a fallback");
    reconfigure(); hcard(); hreset_model();
    g_slow_lba_lo = LBA_A; g_slow_lba_hi = LBA_B + SLOT_BLOCKS; g_slow_ms = 100000u;
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_NONE && ST.slot_b_rc == S0_ESLOW &&
          ST.rescue_reason == S0_RR_BADSLOTS, "both slow -> rescue");
    reconfigure(); hcard(); hreset_model();
    g_ms_per_read = 400u;                                   /* 37 ops x 0.4 s: fine */
    CHECK(hrun(0u, &k, &res, &ok) == S0_FROM_A, "a slow-but-bounded load still boots");
}

int main(int argc, char **argv)
{
    g_verbose = argc > 1 && strcmp(argv[1], "-v") == 0;
    g_card = calloc(CARD_BLOCKS, 512u);
    g_ddr = calloc(1, DDR_SPAN);
    if (!g_card || !g_ddr) {
        printf("out of memory\n");
        return 2;
    }
    t_open();
    t_slot_boots();
    t_fallback();
    t_card_states();
    t_default_slot();
    t_confirm();
    t_cold_settle();
    t_wdog_loop();
    t_calib_drop();
    t_slot_time();
    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 flow %s\n", g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
