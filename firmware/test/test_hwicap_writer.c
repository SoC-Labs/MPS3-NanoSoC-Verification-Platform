/*
 * test_hwicap_writer.c — the ONE HWICAP writer (common/hwicap_writer.c), in
 * BOTH write-path modes, against a BEHAVIOURAL axi_hwicap core model.
 *
 * WHY A CORE MODEL AND NOT THE PLAIN REGISTER FILE. Every other binary in this
 * harness drives HWICAP through mock_regs.c's flat (base,offset)->value slots
 * with HWICAP_WFV poked to a constant 1024. Against that, a writer that ignores
 * write-FIFO vacancy entirely, or one that overwrites a word still pending in a
 * LITE core's single holding register, is INDISTINGUISHABLE from a correct one:
 * the slot array accepts an unbounded number of HWICAP_WF writes and forgets all
 * but the last. So the FIFO half of the writer — the half the FIELDED shell
 * actually runs (PRODUCT=1 => HWICAP_FIFO=1) — had no gate that could fail, and
 * neither did the LITE half's one-word-at-a-time discipline. Both halves were
 * compiled ONLY by the MicroBlaze target build; no host binary defined
 * MPS3_HWICAP_FIFO at all.
 *
 * THE MODEL (fake_icap below) is one write FIFO of configurable depth, which is
 * both cores:
 *   depth 1  = axi_hwicap C_MODE {1} (LITE): a single holding register. A second
 *              HWICAP_WF write with no StartConfig in between LOSES the first
 *              word — the model counts that as an overrun instead of silently
 *              overwriting, which is what the flat register file did.
 *   depth D  = axi_hwicap C_MODE {0} (FIFO) + C_WRITE_FIFO_DEPTH: a WF write
 *              while the FIFO is full LOSES the word. A writer that does not
 *              pace itself on HWICAP_WFV therefore drops config words, and a
 *              dropped config word is a partial that never loads.
 * CR bit0 (StartConfig) drains whatever is in the FIFO to the ICAP, records the
 * batch size, and self-clears after `drain_delay` CR reads (so the bounded
 * CR-poll loop is genuinely exercised rather than short-circuited).
 *
 * The model also counts HWICAP_WFV READS, which is the crispest statement of
 * which protocol a writer is speaking:
 *   LITE  — never reads WFV (there is no vacancy to pace against) and issues
 *           exactly one StartConfig per word.
 *   FIFO  — always reads WFV before a batch, and issues strictly FEWER
 *           StartConfigs than words for any run longer than one batch.
 * That pair is what the two former writers each had to get right SEPARATELY.
 *
 * Built TWICE from this one source: test_hwicap_writer (LITE, the default) and
 * test_hwicap_writer_fifo (-DMPS3_HWICAP_FIFO=1). Each asserts the protocol its
 * build is supposed to speak, so neither can pass by accident.
 *
 * Links: common/hwicap_writer.c, mock_regs.c. -DMPS3_HAL_MOCK.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/platform_regs.h"
#include "../common/hwicap_writer.h"
#include "mock_regs.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ---- behavioural axi_hwicap core model ------------------------------------ */

#define ICAP_FIFO_MAX  1024u
#define ICAP_WORD_CAP  8192u
#define ICAP_BATCH_CAP 8192u

typedef struct {
    uint32_t depth;        /* write-FIFO depth: 1 = LITE core, >1 = FIFO core */
    uint32_t drain_delay;  /* CR reads with WRITE still set before it self-clears */
    uint32_t sr;           /* what HWICAP_SR reads back */

    uint32_t fifo_occ;
    uint32_t staged[ICAP_FIFO_MAX];
    int      cr_write;
    uint32_t drain_left;

    /* observations */
    uint32_t words[ICAP_WORD_CAP];   /* words that actually reached the ICAP */
    uint32_t nwords;
    uint32_t batches[ICAP_BATCH_CAP];/* words per StartConfig */
    uint32_t nbatches;
    uint32_t overruns;               /* WF write with no room -> word LOST */
    uint32_t wfv_reads;              /* how often the writer paced itself */
    uint32_t cr_reads;
} fake_icap_t;

static fake_icap_t s_icap;

static void icap_model_init(uint32_t depth, uint32_t drain_delay, uint32_t sr)
{
    memset(&s_icap, 0, sizeof(s_icap));
    s_icap.depth = depth;
    s_icap.drain_delay = drain_delay;
    s_icap.sr = sr;
}

static int icap_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    (void)ctx; (void)base;
    if (is_write) {
        if (off == HWICAP_WF) {
            if (s_icap.fifo_occ >= s_icap.depth) {
                s_icap.overruns++;   /* no room: the word is LOST, not queued */
            } else if (s_icap.fifo_occ < ICAP_FIFO_MAX) {
                s_icap.staged[s_icap.fifo_occ++] = *val;
            }
            return 1;
        }
        if (off == HWICAP_CR) {
            if (*val & HWICAP_CR_WRITE) {           /* StartConfig */
                uint32_t n = s_icap.fifo_occ;
                for (uint32_t i = 0; i < n; i++) {
                    if (s_icap.nwords < ICAP_WORD_CAP) {
                        s_icap.words[s_icap.nwords] = s_icap.staged[i];
                    }
                    s_icap.nwords++;
                }
                if (s_icap.nbatches < ICAP_BATCH_CAP) {
                    s_icap.batches[s_icap.nbatches] = n;
                }
                s_icap.nbatches++;
                s_icap.fifo_occ = 0;
                s_icap.cr_write = 1;
                s_icap.drain_left = s_icap.drain_delay;
            }
            return 1;
        }
        return 1; /* swallow every other HWICAP write */
    }
    if (off == HWICAP_WFV) {
        s_icap.wfv_reads++;
        *val = s_icap.depth - s_icap.fifo_occ;
        return 1;
    }
    if (off == HWICAP_CR) {
        s_icap.cr_reads++;
        if (s_icap.cr_write) {
            if (s_icap.drain_left > 0) {
                s_icap.drain_left--;
                *val = HWICAP_CR_WRITE;   /* still draining */
                return 1;
            }
            s_icap.cr_write = 0;
        }
        *val = 0;
        return 1;
    }
    if (off == HWICAP_SR) {
        *val = s_icap.sr;
        return 1;
    }
    *val = 0;
    return 1;
}

/* A core sized for the protocol this image speaks: a LITE build must drive a
 * one-word core, a FIFO build a real FIFO. `drain_delay` > 0 on both so the
 * bounded CR-poll loop is really walked. */
static void setup(uint32_t sr)
{
    mock_regs_reset();
    icap_model_init(mps3_hwicap_is_fifo() ? 8u : 1u, /*drain_delay=*/3u, sr);
    mock_regs_set_hook(MPS3_HWICAP_BASE, icap_hook, 0);
    mps3_hwicap_stats_reset();
    mps3_hwicap_begin();
}

/* ---- test payload ---------------------------------------------------------- */

/* A real DFX .bin prefix shape: config words are BIG-endian on disk, so the
 * first file byte is the word's MSB (I18). */
static const uint8_t kbin[] = {
    0xFF, 0xFF, 0xFF, 0xFF,   /* dummy pad     */
    0xAA, 0x99, 0x55, 0x66,   /* SYNC WORD     */
    0x20, 0x00, 0x00, 0x00,   /* Type-1 NOP    */
    0x30, 0x02, 0x20, 0x01,   /* write-to-CMD  */
};
#define KWORDS (sizeof(kbin) / 4u)

static uint32_t be_word(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

#define BIG_WORDS 700u
static uint8_t s_big[BIG_WORDS * 4u];

static void fill_big(void)
{
    for (uint32_t i = 0; i < sizeof(s_big); i++) {
        s_big[i] = (uint8_t)(0x11u + i);
    }
}

static int words_match(const uint8_t *bytes, uint32_t nwords, uint32_t first)
{
    if (s_icap.nwords != first + nwords) {
        return 0;
    }
    for (uint32_t i = 0; i < nwords; i++) {
        if (s_icap.words[first + i] != be_word(&bytes[4u * i])) {
            return 0;
        }
    }
    return 1;
}

/* ---- 1. the protocol this build speaks ------------------------------------- */

/* The single most important assertion in this file: the image has ONE answer to
 * "which axi_hwicap write protocol am I", and it is the one the build asked for.
 * Two writers in two translation units could not be asked this question at all —
 * and a build that defined MPS3_HWICAP_FIFO for only one of them linked, ran and
 * passed the whole suite. */
static void test_build_flag_and_protocol_agree(void)
{
#if MPS3_HWICAP_FIFO
    CHECK(mps3_hwicap_is_fifo() == 1);
#else
    CHECK(mps3_hwicap_is_fifo() == 0);
#endif

    fill_big();
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write_words(s_big, BIG_WORDS, 0) == MPS3_HWICAP_OK);

    /* Every word reached the ICAP, in order, and NONE was lost to a full FIFO
     * or a clobbered holding register. */
    CHECK(s_icap.overruns == 0);
    CHECK(words_match(s_big, BIG_WORDS, 0));

    if (mps3_hwicap_is_fifo()) {
        /* FIFO: paces on WFV, and batches -- strictly fewer StartConfigs than
         * words. One StartConfig per word here would mean a LITE writer had been
         * built into a FIFO image. */
        CHECK(s_icap.wfv_reads > 0);
        CHECK(s_icap.nbatches < BIG_WORDS);
        CHECK(s_icap.nbatches == (BIG_WORDS + 7u) / 8u); /* depth-8 core */
        for (uint32_t i = 0; i < s_icap.nbatches; i++) {
            CHECK(s_icap.batches[i] >= 1u && s_icap.batches[i] <= 8u);
        }
    } else {
        /* LITE: there is no vacancy to pace against, so the writer must never
         * read WFV, and each word gets its own StartConfig. */
        CHECK(s_icap.wfv_reads == 0);
        CHECK(s_icap.nbatches == BIG_WORDS);
        for (uint32_t i = 0; i < s_icap.nbatches; i++) {
            CHECK(s_icap.batches[i] == 1u);
        }
    }
    /* Either way the CR-poll loop was really walked (drain_delay=3). */
    CHECK(s_icap.cr_reads >= s_icap.nbatches);
}

/* ---- 2. byte-lane order + word ordering ------------------------------------ */

static void test_word_order_msb_first(void)
{
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write_words(kbin, KWORDS, 0) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_flush() == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == KWORDS);
    /* The sync word specifically must reach WF as 0xAA995566 -- a native memcpy
     * on the little-endian MicroBlaze would make it 0x669955AA and ICAP would
     * never lock. (test_hwicap_byte_lane.c pins the primitive itself; this pins
     * that the unified writer still goes through it.) */
    CHECK(s_icap.words[1] == 0xAA995566u);
    CHECK(words_match(kbin, KWORDS, 0));
}

/* A config word STRADDLING several write() calls -- the case config_agent's
 * <=512-byte TCP chunks create, and the reason the packer's partial word is
 * carried in writer state. Split 1 + 2 + 1 bytes across three calls. */
static void test_word_straddles_write_calls(void)
{
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write(&kbin[4], 1) == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == 0);   /* incomplete word: nothing may be emitted yet */
    CHECK(mps3_hwicap_write(&kbin[5], 2) == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == 0);
    CHECK(mps3_hwicap_write(&kbin[7], 1) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_flush() == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == 1);
    CHECK(s_icap.words[0] == 0xAA995566u);
    CHECK(s_icap.overruns == 0);
}

/* write() and write_words() are the SAME writer: the same bytes must produce a
 * byte-identical WF sequence whichever entry point delivers them. This is the
 * cross-entry-point invariant the two old writers could only satisfy by
 * coincidence. */
static void test_entry_points_agree(void)
{
    uint32_t via_write[BIG_WORDS];
    uint32_t nwrite;

    fill_big();
    setup(HWICAP_SR_EOS);
    /* 37-byte dribbles: word boundaries fall inside calls. */
    for (uint32_t off = 0; off < sizeof(s_big); ) {
        uint32_t n = sizeof(s_big) - off;
        if (n > 37u) { n = 37u; }
        CHECK(mps3_hwicap_write(&s_big[off], n) == MPS3_HWICAP_OK);
        off += n;
    }
    CHECK(mps3_hwicap_flush() == MPS3_HWICAP_OK);
    nwrite = s_icap.nwords;
    CHECK(nwrite == BIG_WORDS);
    memcpy(via_write, s_icap.words, nwrite * sizeof(uint32_t));

    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write_words(s_big, BIG_WORDS, 0) == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == nwrite);
    CHECK(memcmp(via_write, s_icap.words, nwrite * sizeof(uint32_t)) == 0);

    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_stream_mem(s_big, sizeof(s_big)) == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == nwrite);
    CHECK(memcmp(via_write, s_icap.words, nwrite * sizeof(uint32_t)) == 0);
    CHECK(s_icap.overruns == 0);
}

/* ---- 3. the stream-from-region convenience --------------------------------- */

static uint32_t s_region_reads;

static int region_reader(uint32_t off, uint8_t *buf, uint32_t len, void *ctx)
{
    (void)ctx;
    s_region_reads++;
    if (off + len > sizeof(s_big)) {
        return -1;
    }
    memcpy(buf, &s_big[off], len);
    return 0;
}

static int region_reader_fails(uint32_t off, uint8_t *buf, uint32_t len, void *ctx)
{
    (void)off; (void)buf; (void)len; (void)ctx;
    return -1;
}

static void test_stream_region(void)
{
    uint8_t scratch[256];
    fill_big();
    setup(HWICAP_SR_EOS);
    s_region_reads = 0;
    CHECK(mps3_hwicap_stream_region(region_reader, 0, 0, sizeof(s_big),
                                    scratch, sizeof(scratch)) == MPS3_HWICAP_OK);
    CHECK(s_region_reads == (sizeof(s_big) + sizeof(scratch) - 1u) / sizeof(scratch));
    CHECK(words_match(s_big, BIG_WORDS, 0));
    CHECK(s_icap.overruns == 0);

    /* A failing byte source must fail the stream closed, not stream garbage. */
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_stream_region(region_reader_fails, 0, 0, sizeof(s_big),
                                    scratch, sizeof(scratch)) == MPS3_HWICAP_ERR);
    CHECK(s_icap.nwords == 0);

    /* Argument validation: a non-word-multiple length or scratch is refused
     * before a single word is written. */
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_stream_region(region_reader, 0, 0, 6u, scratch, sizeof(scratch))
          == MPS3_HWICAP_ERR);
    CHECK(mps3_hwicap_stream_region(region_reader, 0, 0, 8u, scratch, 3u)
          == MPS3_HWICAP_ERR);
    CHECK(mps3_hwicap_stream_region(0, 0, 0, 8u, scratch, 4u) == MPS3_HWICAP_ERR);
    CHECK(mps3_hwicap_stream_mem(s_big, 6u) == MPS3_HWICAP_ERR);
    CHECK(mps3_hwicap_stream_mem(0, 8u) == MPS3_HWICAP_ERR);
    CHECK(s_icap.nwords == 0);
}

/* ---- 4. fail-closed ------------------------------------------------------- */

/* CR.WRITE that NEVER self-clears: the bounded poll must expire and the write
 * must report failure -- and report how many words really got in, so the FSM's
 * s_words_done does not step over an unwritten word. (A `drain_delay` larger
 * than MPS3_HWICAP_DONE_POLL_MAX is a permanently stuck ICAP.) */
static void test_stuck_cr_fails_closed(void)
{
    fill_big();
    setup(HWICAP_SR_EOS);
    s_icap.drain_delay = MPS3_HWICAP_DONE_POLL_MAX + 1u;

    uint32_t written = 0xDEADBEEFu;
    CHECK(mps3_hwicap_write_words(s_big, BIG_WORDS, &written) == MPS3_HWICAP_ERR);
    /* Nothing gets past the very first stalled StartConfig. */
    CHECK(written == 0);
    CHECK(mps3_hwicap_bytes() == 0);

    /* The incremental path fails closed on the same stall. */
    setup(HWICAP_SR_EOS);
    s_icap.drain_delay = MPS3_HWICAP_DONE_POLL_MAX + 1u;
    int rc = mps3_hwicap_write(s_big, sizeof(s_big));
    if (mps3_hwicap_is_fifo()) {
        /* FIFO buffers, so the stall surfaces at the auto-flush or at flush(). */
        CHECK(rc == MPS3_HWICAP_OK || rc == MPS3_HWICAP_ERR);
        if (rc == MPS3_HWICAP_OK) {
            CHECK(mps3_hwicap_flush() == MPS3_HWICAP_ERR);
        }
    } else {
        CHECK(rc == MPS3_HWICAP_ERR);
    }
    CHECK(mps3_hwicap_bytes() == 0);
}

/* A NULL byte source with a non-zero length is a caller bug, not a stream. */
static void test_null_source_refused(void)
{
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write(0, 4u) == MPS3_HWICAP_ERR);
    CHECK(mps3_hwicap_write_words(0, 4u, 0) == MPS3_HWICAP_ERR);
    CHECK(mps3_hwicap_write(0, 0u) == MPS3_HWICAP_OK);   /* empty run is fine */
    CHECK(s_icap.nwords == 0);
}

/* ---- 5. finish(): flush + the post-DESYNC EOS gate ------------------------- */

static void test_finish_eos_gate(void)
{
    /* EOS asserted -> clean finish, and everything buffered is in the ICAP. */
    fill_big();
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write(s_big, sizeof(s_big)) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_finish() == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == BIG_WORDS);        /* finish() drained the tail */
    CHECK(mps3_hwicap_bytes() == BIG_WORDS * 4u);
    CHECK(mps3_hwicap_sr_last() == HWICAP_SR_EOS);

    /* EOS never asserts -> ERR_EOS, distinct from every other failure because
     * the caller records it as an EOS verdict on the diag mailbox. The bytes DID
     * get in; it is the end-of-sequence that never happened. */
    setup(/*sr=*/HWICAP_SR_DONE);
    CHECK(mps3_hwicap_write(s_big, sizeof(s_big)) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_finish() == MPS3_HWICAP_ERR_EOS);
    CHECK(s_icap.nwords == BIG_WORDS);
    CHECK(mps3_hwicap_sr_last() == HWICAP_SR_DONE);

    /* An all-zero SR is not recorded (it tells you nothing -- the tx_last_status
     * lesson): sr_last keeps the last NON-ZERO word. */
    setup(/*sr=*/0u);
    CHECK(mps3_hwicap_finish() == MPS3_HWICAP_ERR_EOS);
    CHECK(mps3_hwicap_sr_last() == 0u);

    /* A non-word-aligned tail is refused, and is NOT an EOS verdict. */
    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_write(kbin, 6u) == MPS3_HWICAP_OK); /* 1 word + 2 bytes */
    CHECK(mps3_hwicap_finish() == MPS3_HWICAP_ERR);
}

/* ---- 6. abort discards, begin re-arms -------------------------------------- */

static void test_abort_discards_unflushed(void)
{
    setup(HWICAP_SR_EOS);
    /* Two whole words plus a partial one. In FIFO mode both whole words sit in
     * the batch accumulator (batch = 256 words, nothing auto-flushes yet); in
     * LITE mode they have already self-drained. Either way the PARTIAL word must
     * never reach the ICAP, and an abort must not flush anything new. */
    CHECK(mps3_hwicap_write(kbin, 10u) == MPS3_HWICAP_OK);
    uint32_t before = s_icap.nwords;
    CHECK(before == (mps3_hwicap_is_fifo() ? 0u : 2u));
    mps3_hwicap_abort();
    CHECK(s_icap.nwords == before);            /* abort NEVER writes */

    /* begin() re-arms: the aborted partial word is gone, so the next stream
     * starts on a word boundary rather than inheriting two stale bytes. */
    mps3_hwicap_begin();
    CHECK(mps3_hwicap_write(kbin, 4u) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_flush() == MPS3_HWICAP_OK);
    CHECK(s_icap.nwords == before + 1u);
    CHECK(s_icap.words[before] == be_word(&kbin[0]));
}

/* ---- 7. pace_words(): the writer's "ready" query ---------------------------- */

static void test_pace_words(void)
{
    setup(HWICAP_SR_EOS);
    if (mps3_hwicap_is_fifo()) {
        CHECK(mps3_hwicap_pace_words(256u) == 8u);   /* capped by the depth-8 FIFO */
        CHECK(mps3_hwicap_pace_words(4u) == 4u);     /* capped by the caller */
        /* A FULL FIFO yields a zero budget -- "no room this poll, come back",
         * which is exactly how the FSM keeps one superloop iteration short. */
        s_icap.fifo_occ = s_icap.depth;
        CHECK(mps3_hwicap_pace_words(256u) == 0u);
        s_icap.fifo_occ = 0;
    } else {
        /* No write FIFO to pace against: the budget is the caller's cap and WFV
         * is never consulted. A LITE build that paced on WFV would throttle a
         * QSPI->ICAP stream to a vacancy that does not mean vacancy. */
        uint32_t reads = s_icap.wfv_reads;
        CHECK(mps3_hwicap_pace_words(256u) == 256u);
        CHECK(mps3_hwicap_pace_words(1u) == 1u);
        CHECK(s_icap.wfv_reads == reads);
    }
}

/* ---- 8. the diag counter (diag.h icap_bytes) ------------------------------- */

/* "total bytes written to HWICAP.WF" must mean TOTAL: every entry point, every
 * byte source. This is the observable the two old writers disagreed on --
 * swap_fsm.c's copy counted, overlay_store.c's did not, so a QSPI-sourced or
 * boot-load stream of any size reported zero. */
static void test_bytes_counts_every_entry_point(void)
{
    uint8_t scratch[256];
    fill_big();

    setup(HWICAP_SR_EOS);
    CHECK(mps3_hwicap_bytes() == 0);
    CHECK(mps3_hwicap_write_words(kbin, KWORDS, 0) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_flush() == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_bytes() == KWORDS * 4u);

    CHECK(mps3_hwicap_write(kbin, sizeof(kbin)) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_flush() == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_bytes() == KWORDS * 8u);

    CHECK(mps3_hwicap_stream_mem(kbin, sizeof(kbin)) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_bytes() == KWORDS * 12u);

    CHECK(mps3_hwicap_stream_region(region_reader, 0, 0, sizeof(s_big),
                                    scratch, sizeof(scratch)) == MPS3_HWICAP_OK);
    CHECK(mps3_hwicap_bytes() == KWORDS * 12u + BIG_WORDS * 4u);

    /* Monotonic across streams; only the test hook resets it. */
    CHECK(4u * s_icap.nwords == mps3_hwicap_bytes());
}

int main(void)
{
    test_build_flag_and_protocol_agree();
    test_word_order_msb_first();
    test_word_straddles_write_calls();
    test_entry_points_agree();
    test_stream_region();
    test_stuck_cr_fails_closed();
    test_null_source_refused();
    test_finish_eos_gate();
    test_abort_discards_unflushed();
    test_pace_words();
    test_bytes_counts_every_entry_point();
    printf("%s: %d checks passed (%s write path)\n",
           HWICAP_WRITER_TEST_NAME, s_checks,
           mps3_hwicap_is_fifo() ? "FIFO" : "LITE");
    return 0;
}
