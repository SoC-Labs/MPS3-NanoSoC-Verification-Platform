/*
 * stage0_flow.c -- see stage0_flow.h. Portable: no MMIO, no libc, no physical
 * address. Everything board-specific arrives through struct s0_flow_ops.
 */
#include "stage0_flow.h"
#include "stage0_fmt.h"

/* ---- console lines ------------------------------------------------------- */

static void l_emit(const struct s0_flow_ops *ops, struct s0_line *l)
{
    if (ops->log)
        ops->log(ops->ctx, l->b);
    s0l_init(l);
}

static void say(const struct s0_flow_ops *ops, const char *a, const char *b)
{
    struct s0_line l;
    s0l_init(&l);
    s0l_str(&l, a);
    s0l_str(&l, b);
    l_emit(ops, &l);
}

const char *s0_rescue_reason_text(uint32_t rr)
{
    switch (rr) {
    case S0_RR_NONE:      return "none";
    case S0_RR_DDR:       return "ddr calib fail";
    case S0_RR_NOCARD:    return "no card";
    case S0_RR_NOHW:      return "no usd_spi block";
    case S0_RR_UNSUP:     return "card unsupported";
    case S0_RR_SDERR:     return "card error";
    case S0_RR_NOLAYOUT:  return "card has no stage0 slots";
    case S0_RR_BADSLOTS:  return "no valid slot";
    case S0_RR_EXHAUSTED: return "slots exhausted (unconfirmed boots)";
    case S0_RR_WDOG:      return "stage0 watchdog loop";
    default:              return "unknown";
    }
}

/* ---- status block ---------------------------------------------------------- */

static int status_valid(const struct s0_status *st)
{
    return st->magic == S0_STATUS_MAGIC && st->version == S0_STATUS_VERSION &&
           st->size == S0_STATUS_BYTES && st->magic_end == S0_STATUS_MAGIC;
}

/* This run still owes the cold settle (set by s0_status_open, cleared once
 * s0_boot_select has run it): never settle twice in one run. */
static uint32_t s_settle_due;

void s0_status_note_calib_drops(struct s0_status *st, uint32_t n)
{
    uint32_t d = S0_ENTRY_CALIB_DROPS(st->entry) + n;
    if (d > 0xFFu)
        d = 0xFFu;
    st->entry = (st->entry & ~(0xFFu << 16)) | (d << 16);
}

void s0_status_note_settle(struct s0_status *st, uint32_t drops, uint32_t cal)
{
    s0_status_note_calib_drops(st, drops);
    if (cal & 1u)
        st->entry |= S0_ENTRY_CAL_AT_SETTLE;
    if (cal & 2u)
        st->entry |= S0_ENTRY_CAL_ROSE;
}

static void note_ddr_recovery(struct s0_status *st)
{
    uint32_t r = S0_ENTRY_DDR_RECOVER(st->entry);
    if (r < 0xFu)
        r += 1u;
    st->entry = (st->entry & ~(0xFu << 24)) | (r << 24);
}

uint32_t s0_status_open(struct s0_status *st, const struct s0_build_ids *ids,
                        uint32_t reset_cause, uint32_t boot_limit)
{
    uint32_t kind;
    st->last_verdict = S0_VD_NONE;
    st->verdict_from = S0_FROM_NONE;
    if (!status_valid(st)) {
        /* Reconfiguration (BRAM outside the baked image is zero) or garbage:
         * everything, NOINIT counters included, starts from zero. This is the
         * COLD entry: the MCC may still be setting clocks up (settle owed). */
        volatile uint32_t *w = (volatile uint32_t *)st;
        for (uint32_t i = 0; i < S0_STATUS_BYTES / 4u; ++i)
            w[i] = 0u;
        st->magic = S0_STATUS_MAGIC;
        st->version = S0_STATUS_VERSION;
        st->size = S0_STATUS_BYTES;
        st->magic_end = S0_STATUS_MAGIC;
        kind = S0_EK_COLD;
        st->entry = S0_ENTRY_SETTLE_PENDING | S0_EK_COLD;
    } else {
        /* The previous run, as it was when it was reset: where a silent hang
         * happened (the hart frozen on a DDR transaction) survives PB0 and the
         * watchdog, so the next entry can say where it was. */
        uint32_t prev = st->entry;
        uint32_t run = S0_ENTRY_WDOG_RUN(prev);
        st->prev_phase = S0_PREV(st->phase, st->subphase);
        st->prev_uptime_ms = st->uptime_ms;
        if ((reset_cause & S0_RESET_WRS) && st->phase != S0_PH_HANDOFF) {
            kind = S0_EK_WDOG;      /* stage0 itself stalled: count it */
            if (run < 0xFFu)
                run += 1u;
        } else {
            /* a hand-off, or any reset that is not the watchdog, breaks the run */
            kind = (reset_cause & S0_RESET_WRS) ? S0_EK_WDOG_LINUX : S0_EK_WARM;
            run = 0u;
        }
        if ((prev & S0_ENTRY_SETTLE_PENDING) && kind == S0_EK_WARM)
            kind = S0_EK_RESETTLE;  /* reset inside the MCC window: settle again */
        st->entry = (prev & S0_ENTRY_SETTLE_PENDING) | (run << 8) | kind;
    }
    s_settle_due = (st->entry & S0_ENTRY_SETTLE_PENDING) ? 1u : 0u;

    if (kind != S0_EK_COLD && st->att_from != S0_FROM_NONE) {
        /* Warm restart after a hand-off: judge that attempt exactly once. */
        uint32_t from = st->att_from;
        st->verdict_from = from;
        if (st->att_confirm == S0_CONFIRM_MAGIC) {
            st->last_verdict = S0_VD_CONFIRMED;
            if (from == S0_FROM_A)
                st->fails_a = 0u;
            else if (from == S0_FROM_B)
                st->fails_b = 0u;
            else {
                /* A confirmed RESCUE boot is a healthy Linux that may well
                 * have rewritten the card: give both slots a fresh start. */
                st->fails_a = 0u;
                st->fails_b = 0u;
            }
        } else {
            st->last_verdict = S0_VD_UNCONFIRMED;
            if (from == S0_FROM_A)
                st->fails_a += 1u;
            else if (from == S0_FROM_B)
                st->fails_b += 1u;
            /* an unconfirmed rescue boot costs no slot anything */
        }
        st->att_from = S0_FROM_NONE;    /* judged: never count it twice */
    }

    st->boot_count += 1u;
    st->build_id = ids->build_id;
    st->fabric_static_id = ids->fabric_static_id;
    st->fabric_ver32 = ids->fabric_ver32;
    st->reset_cause = reset_cause;
    st->boot_limit = boot_limit;
    st->phase = S0_PH_RESET;
    st->booted_from = S0_FROM_NONE;
    st->last_error = 0u;
    st->ddr_calib = S0_DDR_UNKNOWN;
    st->sd_result = S0_SD_NOTRIED;
    st->sd_detail = 0u;
    st->slot_a_rc = S0_ENOTTRIED;
    st->slot_b_rc = S0_ENOTTRIED;
    st->default_slot = S0_FROM_A;
    st->rescue_reason = S0_RR_NONE;
    st->rescue_state = S0_RS_OFF;
    st->rescue_bytes = 0u;
    st->rescue_last_rc = S0_ENOTTRIED;
    st->entry_pc = 0u;
    st->entry_a0 = 0u;
    st->entry_a1 = 0u;
    st->image_hdr_crc = 0u;
    st->handoff_ms = 0u;
    st->heartbeat = 0u;
    st->uptime_ms = 0u;
    st->net_rc = 0u;
    st->sd_rd_fails = 0u;
    st->sd_rd_last = 0u;
    st->subphase = S0_SUBPHASE(S0_SP_NONE, 0u);
    st->ddr_ok_ms = 0u;
    return kind;
}

void s0_status_note_handoff(struct s0_status *st, int from,
                            const struct s0_result *res, uint32_t now_ms)
{
    st->booted_from = (uint32_t)from;
    st->entry_pc = res->entry_pc;
    st->entry_a0 = res->entry_a0;
    st->entry_a1 = res->entry_a1;
    st->image_hdr_crc = res->header_crc32;
    st->handoff_ms = now_ms;
    st->uptime_ms = now_ms;
    if (from == S0_FROM_A)
        st->n_boot_a += 1u;
    else if (from == S0_FROM_B)
        st->n_boot_b += 1u;
    else if (from == S0_FROM_RESCUE)
        st->n_boot_rescue += 1u;
    if ((from == S0_FROM_A || from == S0_FROM_B) && (uint32_t)from != st->default_slot) {
        /* a fallback is the non-default slot booting because the default was
         * TRIED (or limited) and lost -- not because the card lacks it */
        uint32_t drc = st->default_slot == S0_FROM_B ? st->slot_b_rc : st->slot_a_rc;
        if (drc != S0_OK && drc != S0_ENOSLOT)
            st->n_fallback += 1u;
    }
    /* The attempt: pending until Linux writes S0_CONFIRM_MAGIC. Clear the
     * confirm word BEFORE naming the attempt, so no reader can pair this
     * attempt with a stale confirmation. */
    st->att_confirm = 0u;
    st->att_from = (uint32_t)from;
    st->phase = S0_PH_HANDOFF;   /* last: a reader seeing HANDOFF sees the rest */
}

/* ---- slot reader ------------------------------------------------------------- */

static uint8_t  s_blk[S0_BLOCK_BYTES];
static uint32_t s_blk_lba;
static uint32_t s_blk_valid;
static uint8_t  s_cfg[S0_BLOCK_BYTES];   /* boot-select copy 0 */

void s0_slot_cache_invalidate(void)
{
    s_blk_valid = 0u;
}

static int cached_block(const struct s0_flow_ops *ops, uint32_t lba)
{
    if (s_blk_valid && s_blk_lba == lba)
        return 0;
    s_blk_valid = 0u;
    if (ops->sd_read_blocks(ops->ctx, lba, 1u, s_blk) != 0)
        return -1;
    s_blk_lba = lba;
    s_blk_valid = 1u;
    return 0;
}

/* Before every card read op of a slot load and every in-DDR CRC chunk: say
 * where we are, re-check the DDR calib bit (the target kicks the watchdog in
 * the same call) and enforce the slot's time bound. 0 or an S0_* code. */
static int slot_gate(const struct s0_slot_src *src, uint32_t sp, uint32_t detail)
{
    const struct s0_flow_ops *ops = src->ops;
    if (src->st)
        src->st->subphase = S0_SUBPHASE(sp, detail);
    if (ops->guard && !ops->guard(ops->ctx))
        return S0_EDDR;
    if (ops->now_ms && ops->now_ms(ops->ctx) - src->t0_ms >= S0_SLOT_TIME_MS)
        return S0_ESLOW;
    return 0;
}

static int slot_chunk(void *ctx, uint32_t region)
{
    return slot_gate((const struct s0_slot_src *)ctx, S0_SP_CRC, region);
}

int s0_slot_read(uint32_t src_off, void *dst, uint32_t len, void *ctx)
{
    const struct s0_slot_src *src = (const struct s0_slot_src *)ctx;
    uint32_t nblk = src->slot.nblocks;
    if (nblk > S0_IMAGE_MAX / S0_BLOCK_BYTES)
        nblk = S0_IMAGE_MAX / S0_BLOCK_BYTES;
    uint32_t limit = nblk * S0_BLOCK_BYTES;          /* <= 64 MiB, no overflow */
    if (src->slot.first_lba == 0u || src_off > limit || len > limit - src_off)
        return -1;                                   /* truncated slot */

    uint8_t *d = (uint8_t *)dst;
    uint32_t lba = src->slot.first_lba + src_off / S0_BLOCK_BYTES;
    uint32_t boff = src_off % S0_BLOCK_BYTES;
    while (len != 0u) {
        uint32_t n;
        int g = slot_gate(src, S0_SP_SLOT_LOAD, lba - src->slot.first_lba);
        if (g != 0)
            return g;
        if (boff != 0u || len < S0_BLOCK_BYTES) {
            if (cached_block(src->ops, lba) != 0)
                return -1;
            n = S0_BLOCK_BYTES - boff;
            if (n > len)
                n = len;
            for (uint32_t i = 0; i < n; ++i)
                d[i] = s_blk[boff + i];
            if (boff + n == S0_BLOCK_BYTES)
                lba += 1u;
            boff = 0u;
        } else {
            /* one usd.c op at a time, each behind the gate above */
            uint32_t nb = len / S0_BLOCK_BYTES;
            if (nb > S0_RD_CHUNK_BLOCKS)
                nb = S0_RD_CHUNK_BLOCKS;
            if (src->ops->sd_read_blocks(src->ops->ctx, lba, nb, d) != 0)
                return -1;
            n = nb * S0_BLOCK_BYTES;
            lba += nb;
        }
        d += n;
        len -= n;
    }
    return 0;
}

/* ---- the boot order ------------------------------------------------------------ */

/* The per-attempt fields, reset before every s0_boot_select (the first call
 * finds them as s0_status_open left them; a recovery call must not inherit
 * the failed attempt's verdicts). */
static void attempt_reset(struct s0_status *st)
{
    st->ddr_calib = S0_DDR_UNKNOWN;
    st->sd_result = S0_SD_NOTRIED;
    st->sd_detail = 0u;
    st->slot_a_rc = S0_ENOTTRIED;
    st->slot_b_rc = S0_ENOTTRIED;
    st->default_slot = S0_FROM_A;
    st->rescue_reason = S0_RR_NONE;
    st->ddr_ok_ms = 0u;
}

/* The DDR gate (ops->ddr_calib holds the bit for S0_CALIB_HOLD_MS on the
 * target); ddr_ok_ms = when it passed. */
static int ddr_gate(struct s0_status *st, const struct s0_flow_ops *ops)
{
    st->phase = S0_PH_DDR;
    st->subphase = S0_SUBPHASE(S0_SP_DDR_WAIT, 0u);
    int d = ops->ddr_calib(ops->ctx);
    st->ddr_calib = (uint32_t)d;
    if ((d == S0_DDR_OK || d == S0_DDR_IMPLIED) && ops->now_ms) {
        uint32_t t = ops->now_ms(ops->ctx);
        st->ddr_ok_ms = t ? t : 1u;
    }
    return d;
}

static int try_slot(struct s0_status *st, const struct s0_flow_ops *ops,
                    const struct s0_slot *slot, uint32_t from, struct s0_result *out)
{
    uint32_t fails = from == S0_FROM_A ? st->fails_a : st->fails_b;
    int rc;

    if (slot->first_lba == 0u) {
        rc = S0_ENOSLOT;
    } else if (st->boot_limit != 0u && fails >= st->boot_limit) {
        rc = S0_ELIMIT;          /* handed off boot_limit times, never confirmed */
    } else {
        struct s0_slot_src src = { ops, *slot, st, ops->now_ms ? ops->now_ms(ops->ctx) : 0u };
        struct s0_backend be = { s0_slot_read, ops->addr_to_ptr, &src };
        st->phase = from == S0_FROM_A ? S0_PH_SLOT_A : S0_PH_SLOT_B;
        st->subphase = S0_SUBPHASE(S0_SP_SLOT_LOAD, 0u);
        rc = s0_load_guarded(&be, slot_chunk, out);
    }
    if (from == S0_FROM_A)
        st->slot_a_rc = (uint32_t)rc;
    else
        st->slot_b_rc = (uint32_t)rc;
    if (rc != S0_OK)
        st->last_error = S0_LAST_ERROR(from == S0_FROM_A ? S0_ES_SLOT_A : S0_ES_SLOT_B, rc);

    struct s0_line l;
    s0l_init(&l);
    s0l_str(&l, from == S0_FROM_A ? "stage0: slot A: " : "stage0: slot B: ");
    s0l_str(&l, s0_strerror(rc));
    if (rc == S0_OK) {
        s0l_str(&l, " hdr_crc=");
        s0l_hex(&l, out->header_crc32);
    } else if (rc == S0_ELIMIT) {
        s0l_str(&l, " (");
        s0l_dec(&l, fails);
        s0l_str(&l, " unconfirmed boots)");
    }
    l_emit(ops, &l);
    return rc;
}

static uint32_t sd_reason(int r)
{
    switch (r) {
    case S0_SD_NOCARD: return S0_RR_NOCARD;
    case S0_SD_NOHW:   return S0_RR_NOHW;
    case S0_SD_UNSUP:  return S0_RR_UNSUP;
    case S0_SD_NOMBR:  return S0_RR_NOLAYOUT;
    default:           return S0_RR_SDERR;
    }
}

int s0_boot_select(struct s0_status *st, const struct s0_flow_ops *ops,
                   struct s0_result *out, int *ddr_ok)
{
    static const char *const sdtxt[] = {
        "not tried", "ready", "no card", "no usd_spi block", "unsupported card",
        "init error", "init timeout", "no MBR", "skipped",
    };
    struct s0_slot slots[S0_NUM_SLOTS];
    uint32_t card_blocks = 0u, detail = 0u;

    /* A second call in one run: rescue saw the calib bit come good again. */
    if (st->ddr_calib == S0_DDR_FAIL || st->ddr_calib == S0_DDR_LOST)
        note_ddr_recovery(st);
    attempt_reset(st);

    /* 0. stage0 keeps being reset by its own watchdog before any hand-off:
     * whatever stalls it (a DDR transaction, a card op) must not loop. The
     * DDR gate still runs -- TELEM only -- so rescue knows whether a push may
     * be staged; the card is not touched. */
    uint32_t run = S0_ENTRY_WDOG_RUN(st->entry);
    if (S0_WDOG_LOOP_LIMIT != 0u && run >= S0_WDOG_LOOP_LIMIT) {
        int dl = ddr_gate(st, ops);
        *ddr_ok = (dl == S0_DDR_OK || dl == S0_DDR_IMPLIED);
        st->sd_result = S0_SD_SKIPPED;
        st->last_error = S0_LAST_ERROR(S0_ES_WDOG, run);
        st->rescue_reason = S0_RR_WDOG;
        struct s0_line l;
        s0l_init(&l);
        s0l_str(&l, "stage0: ");
        s0l_dec(&l, run);
        s0l_str(&l, " watchdog restarts in a row before a hand-off -- straight to rescue");
        l_emit(ops, &l);
        return S0_FROM_NONE;
    }

    /* 0b. a cold entry: the MCC is still setting clocks up. Touch nothing. */
    if (s_settle_due) {
        s_settle_due = 0u;
        if (ops->settle && S0_COLD_SETTLE_MS != 0u) {
            struct s0_line l;
            st->phase = S0_PH_DDR;
            st->subphase = S0_SUBPHASE(S0_SP_SETTLE, S0_COLD_SETTLE_MS);
            s0l_init(&l);
            s0l_str(&l, "stage0: cold entry: settling ");
            s0l_dec(&l, S0_COLD_SETTLE_MS);
            s0l_str(&l, " ms before DDR / card (MCC clock setup)");
            l_emit(ops, &l);
            ops->settle(ops->ctx, S0_COLD_SETTLE_MS);
        }
        st->entry &= ~S0_ENTRY_SETTLE_PENDING;
    }

    /* 1. DDR -- before ANY DDR access: an access before calibration stalls
     * forever (SHELL_CONTRACT §5). */
    int d = ddr_gate(st, ops);
    *ddr_ok = (d == S0_DDR_OK || d == S0_DDR_IMPLIED);
    if (!*ddr_ok) {
        st->sd_result = S0_SD_SKIPPED;
        st->last_error = S0_LAST_ERROR(S0_ES_DDR, d);
        st->rescue_reason = S0_RR_DDR;
        say(ops, "stage0: ddr calib fail -- DDR4 never calibrated; nothing can be staged", 0);
        return S0_FROM_NONE;
    }
    say(ops, d == S0_DDR_OK ? "stage0: DDR4 calib=1"
                            : "stage0: DDR4 calib not checked (CALIB=none build)", 0);

    /* 2. the card: init + MBR */
    st->phase = S0_PH_SD;
    st->subphase = S0_SUBPHASE(S0_SP_CARD_INIT, 0u);
    s0_slot_cache_invalidate();
    int r = ops->sd_init(ops->ctx, &card_blocks, &detail);
    st->sd_detail = detail;
    st->subphase = S0_SUBPHASE(S0_SP_CARD_META, 0u);
    if (r == S0_SD_READY) {
        if (cached_block(ops, 0u) != 0)
            r = S0_SD_ERROR;
        else if (s0_mbr_parse(s_blk, card_blocks, slots) != 0)
            r = S0_SD_NOMBR;
    }
    st->sd_result = (uint32_t)r;
    if (r != S0_SD_READY) {
        if (r != S0_SD_NOCARD)            /* an empty slot is not an error */
            st->last_error = S0_LAST_ERROR(S0_ES_SD, r);
        st->rescue_reason = sd_reason(r);
        say(ops, "stage0: uSD: ",
            (uint32_t)r < sizeof sdtxt / sizeof sdtxt[0] ? sdtxt[r] : "?");
        return S0_FROM_NONE;
    }

    /* the default slot (Linux's choice; A when the card has none) */
    uint32_t seq = 0u, def = S0_FROM_A;
    if (ops->sd_read_blocks(ops->ctx, S0_BOOTCFG_LBA0, 1u, s_cfg) == 0 &&
        cached_block(ops, S0_BOOTCFG_LBA1) == 0)
        def = s0_bootcfg_pick(s_cfg, s_blk, &seq);
    st->default_slot = def;
    if (seq != st->cfg_seq) {
        /* The card's boot selection changed (Linux wrote new slots and/or a
         * new default): earlier failures were about other images. */
        st->fails_a = 0u;
        st->fails_b = 0u;
        st->cfg_seq = seq;
    }
    {
        struct s0_line l;
        s0l_init(&l);
        s0l_str(&l, "stage0: uSD: ready, default slot ");
        s0l_str(&l, def == S0_FROM_B ? "B" : "A");
        s0l_str(&l, " (seq ");
        s0l_dec(&l, seq);
        s0l_str(&l, ")");
        l_emit(ops, &l);
    }

    /* 3/4. default slot, then the other. A calib drop mid-load (S0_EDDR)
     * ends the pass: once the gate holds again the slots are retried, up to
     * S0_DDR_RETRIES times; otherwise the DDR is lost -> rescue, which keeps
     * polling the bit. A slot over its time bound (S0_ESLOW) is just a bad
     * slot: the other one is tried. */
    uint32_t order[2] = { def, def == S0_FROM_B ? S0_FROM_A : S0_FROM_B };
    for (uint32_t pass = 0u;; ++pass) {
        int lost = 0;
        for (uint32_t i = 0; i < 2u && !lost; ++i) {
            const struct s0_slot *sl = &slots[order[i] == S0_FROM_A ? 0 : 1];
            int rc = try_slot(st, ops, sl, order[i], out);
            if (rc == S0_OK)
                return (int)order[i];
            lost = rc == S0_EDDR;
        }
        if (!lost)
            break;
        s0_status_note_calib_drops(st, 1u);
        say(ops, "stage0: DDR4 calib DROPPED during the slot load", 0);
        if (pass < S0_DDR_RETRIES) {
            d = ddr_gate(st, ops);
            if (d == S0_DDR_OK || d == S0_DDR_IMPLIED) {
                say(ops, "stage0: DDR4 calib holds again -- retrying the slots", 0);
                continue;
            }
        }
        st->ddr_calib = S0_DDR_LOST;
        *ddr_ok = 0;
        st->last_error = S0_LAST_ERROR(S0_ES_DDR, S0_DDR_LOST);
        st->rescue_reason = S0_RR_DDR;
        say(ops, "stage0: ddr calib lost -- rescue (it keeps polling the bit)", 0);
        return S0_FROM_NONE;
    }

    /* 5. why rescue */
    uint32_t ra = st->slot_a_rc, rb = st->slot_b_rc;
    if (ra == S0_ENOSLOT && rb == S0_ENOSLOT)
        st->rescue_reason = S0_RR_NOLAYOUT;
    else if ((ra == S0_ELIMIT || ra == S0_ENOSLOT) && (rb == S0_ELIMIT || rb == S0_ENOSLOT))
        st->rescue_reason = S0_RR_EXHAUSTED;
    else
        st->rescue_reason = S0_RR_BADSLOTS;
    return S0_FROM_NONE;
}
