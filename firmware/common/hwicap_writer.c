/*
 * hwicap_writer.c — the single HWICAP write path. See hwicap_writer.h for why
 * this file exists and what it replaced.
 *
 * The LITE/FIFO split is confined to the helpers at the top (hwicap_lite_write /
 * hwicap_fifo_drain / put_one / the batch accumulator). Everything below them is
 * protocol-blind, so there is exactly one implementation of "stream these bytes
 * to the ICAP" and one place where a mode mismatch can be introduced.
 */
#include "hwicap_writer.h"
#include "platform_regs.h"

/* ---- stats (diag.h icap_bytes / the I18(3) SR capture) --------------------- */

static uint32_t s_bytes;     /* bytes CONFIRMED into HWICAP.WF, monotonic */
static uint32_t s_sr_last;   /* last NON-ZERO HWICAP_SR seen by the EOS wait */

uint32_t mps3_hwicap_bytes(void)   { return s_bytes; }
uint32_t mps3_hwicap_sr_last(void) { return s_sr_last; }

void mps3_hwicap_stats_reset(void)
{
    s_bytes = 0;
    s_sr_last = 0;
}

int mps3_hwicap_is_fifo(void)
{
#if MPS3_HWICAP_FIFO
    return 1;
#else
    return 0;
#endif
}

/* ---- byte packer (shared by both modes) ------------------------------------ */

/* Bytes staged into the current config word, MSB-first, carried across
 * mps3_hwicap_write() calls: config_agent hands over <=512-byte TCP chunks and a
 * config word can straddle two of them. */
static uint32_t s_word;    /* partial word so far, MSB-first (b0<<24|...) */
static uint32_t s_word_n;  /* bytes accumulated into s_word (0..3) */

/* ---- mode-specific primitives ---------------------------------------------- */
/*
 * THE TWO NAMES BELOW ARE A CONTRACT, NOT AN ACCIDENT.
 *
 * Exactly one of hwicap_lite_write() / hwicap_fifo_drain() is compiled into any
 * image, and both have EXTERNAL linkage on purpose so the name survives into the
 * ELF symbol table (a static one could be inlined away). That symbol IS the
 * built artifact's statement of which axi_hwicap write protocol it speaks, and
 * firmware/platform/verify_shell_image.py's "THE d28c292 TRAP" check reads it
 * to refuse a LITE firmware on a C_MODE{0} FIFO bitstream -- the mismatch that
 * pings, reports the right static_id, passes the acceptance gate and cannot load
 * a single RM. The names are inherited verbatim from the two writers this file
 * replaced (swap_fsm.c's and overlay_store.c's) precisely so that gate keeps
 * working, and it is now STRONGER: the symbols live in ONE translation unit, so
 * the gate's "AMBIGUOUS" verdict can no longer be produced by two files that
 * were compiled with different flags. Do not rename either without changing
 * verify_shell_image.py and firmware/platform/README.md in the same commit.
 */

#if MPS3_HWICAP_FIFO

int hwicap_fifo_drain(const uint32_t *words, uint32_t nwords, uint32_t *confirmed);

/* FIFO mode (axi_hwicap C_MODE {0}, a real write FIFO). The BSP driver's `#else`
 * (XPAR_..._MODE != 1) branch, xhwicap.c ~405-475: poll write-FIFO vacancy
 * (HWICAP_WFV), push up to WFV already-packed words into HWICAP_WF, kick ONE
 * StartConfig (CR=WRITE), poll CR until the WRITE bit self-clears, refill.
 * Batching many words per StartConfig is the entire point of FIFO mode.
 *
 * Words are written RAW — the caller already put them in ICAP order via
 * mps3_hwicap_pack_word() — so this never re-packs and the WF value is
 * byte-identical to what the LITE path would have written for the same bytes.
 *
 * `*confirmed` (optional) receives the number of words that actually reached the
 * ICAP, so a stall part-way through a long run is reported honestly instead of
 * as "none of it landed". Bounded + fail-closed on both spins. */
int hwicap_fifo_drain(const uint32_t *words, uint32_t nwords, uint32_t *confirmed)
{
    uint32_t done = 0;
    int rc = MPS3_HWICAP_OK;
    while (done < nwords) {
        uint32_t vacancy;
        uint32_t spins = 0;
        while ((vacancy = mps3_reg_read32(MPS3_HWICAP_BASE, HWICAP_WFV)) == 0) {
            if (++spins >= MPS3_HWICAP_DONE_POLL_MAX) {
                rc = MPS3_HWICAP_ERR; /* write FIFO never made room */
                goto out;
            }
        }
        uint32_t batch = nwords - done;
        if (batch > vacancy) {
            batch = vacancy;
        }
        for (uint32_t i = 0; i < batch; i++) {
            mps3_reg_write32(MPS3_HWICAP_BASE, HWICAP_WF, words[done + i]);
        }
        mps3_reg_write32(MPS3_HWICAP_BASE, HWICAP_CR, HWICAP_CR_WRITE); /* StartConfig */
        spins = 0;
        while (mps3_reg_read32(MPS3_HWICAP_BASE, HWICAP_CR) & HWICAP_CR_WRITE) {
            if (++spins >= MPS3_HWICAP_DONE_POLL_MAX) {
                rc = MPS3_HWICAP_ERR; /* CR.WRITE never self-cleared: batch did not drain */
                goto out;
            }
        }
        done += batch;
        s_bytes += batch * 4u;
    }
out:
    if (confirmed) {
        *confirmed = done;
    }
    return rc;
}

/* Batch accumulator for the incremental byte path (mps3_hwicap_write): each
 * completed word lands here and the batch drains a full run at a time rather
 * than one StartConfig per word. */
static uint32_t s_batch[MPS3_HWICAP_BATCH_WORDS];
static uint32_t s_batch_n;

static int put_one(uint32_t w)
{
    s_batch[s_batch_n++] = w;
    if (s_batch_n == MPS3_HWICAP_BATCH_WORDS) {
        return mps3_hwicap_flush();
    }
    return MPS3_HWICAP_OK;
}

int mps3_hwicap_flush(void)
{
    if (s_batch_n == 0) {
        return MPS3_HWICAP_OK;
    }
    uint32_t n = s_batch_n;
    s_batch_n = 0;   /* cleared first: a stalled drain must not be re-attempted */
    return hwicap_fifo_drain(s_batch, n, 0);
}

static void batch_discard(void) { s_batch_n = 0; }

#else /* LITE */

int hwicap_lite_write(uint32_t w);

/* LITE mode (axi_hwicap C_MODE {1}, XPAR_AXI_HWICAP_0_MODE=1): there is NO write
 * FIFO, so the FIFO-mode WF-batch protocol never drains (WFV maxes at 1). The
 * BSP's MODE==1 path (xhwicap.c: XHwIcap_FifoWrite -> XHwIcap_StartConfig ->
 * `while (CR & XHI_CR_WRITE_MASK)`) writes ONE word at a time. Proven on the
 * board via MDM pokes (AA995566/20000000 each cleared CR in 0 iterations).
 *
 * Fail-closed: MPS3_HWICAP_OK once CR.WRITE self-clears (the word reached ICAP),
 * MPS3_HWICAP_ERR if the bounded poll expires with CR.WRITE still set. */
int hwicap_lite_write(uint32_t w)
{
    mps3_reg_write32(MPS3_HWICAP_BASE, HWICAP_WF, w);
    mps3_reg_write32(MPS3_HWICAP_BASE, HWICAP_CR, HWICAP_CR_WRITE); /* StartConfig */
    uint32_t spins = 0;
    while (mps3_reg_read32(MPS3_HWICAP_BASE, HWICAP_CR) & HWICAP_CR_WRITE) {
        if (++spins >= MPS3_HWICAP_DONE_POLL_MAX) {
            return MPS3_HWICAP_ERR; /* CR.WRITE never cleared: the word did not reach ICAP */
        }
    }
    s_bytes += 4u;
    return MPS3_HWICAP_OK;
}

static int put_one(uint32_t w) { return hwicap_lite_write(w); }

int mps3_hwicap_flush(void)
{
    return MPS3_HWICAP_OK; /* nothing is ever buffered: each word self-drained */
}

static void batch_discard(void) { }

#endif /* MPS3_HWICAP_FIFO */

/* ---- protocol-blind API ---------------------------------------------------- */

void mps3_hwicap_begin(void)
{
    s_word = 0;
    s_word_n = 0;
    batch_discard();
}

void mps3_hwicap_abort(void)
{
    s_word = 0;
    s_word_n = 0;
    batch_discard();
}

int mps3_hwicap_write(const void *buf, uint32_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    if (p == 0 && len != 0) {
        return MPS3_HWICAP_ERR;
    }
    while (len > 0) {
        s_word = (s_word << 8) | (uint32_t)(*p++); /* first byte -> MSB (I18) */
        s_word_n++;
        len--;
        if (s_word_n == 4) {
            uint32_t w = s_word;
            s_word = 0;
            s_word_n = 0;
            if (put_one(w) != MPS3_HWICAP_OK) {
                return MPS3_HWICAP_ERR;
            }
        }
    }
    return MPS3_HWICAP_OK;
}

int mps3_hwicap_write_words(const uint8_t *bytes, uint32_t nwords, uint32_t *nwritten)
{
    if (nwritten) {
        *nwritten = 0;
    }
    if (bytes == 0 && nwords != 0) {
        return MPS3_HWICAP_ERR;
    }
#if MPS3_HWICAP_FIFO
    /* Pack a bounded window onto the stack and drain it, so an arbitrarily large
     * nwords never needs a big buffer AND nothing is left buffered on return. */
    uint32_t done = 0;
    while (done < nwords) {
        uint32_t win = nwords - done;
        if (win > MPS3_HWICAP_BATCH_WORDS) {
            win = MPS3_HWICAP_BATCH_WORDS;
        }
        uint32_t words[MPS3_HWICAP_BATCH_WORDS];
        for (uint32_t i = 0; i < win; i++) {
            words[i] = mps3_hwicap_pack_word(&bytes[4u * (done + i)]);
        }
        uint32_t confirmed = 0;
        int rc = hwicap_fifo_drain(words, win, &confirmed);
        done += confirmed;
        if (nwritten) {
            *nwritten = done;
        }
        if (rc != MPS3_HWICAP_OK) {
            return MPS3_HWICAP_ERR;
        }
    }
    return MPS3_HWICAP_OK;
#else
    for (uint32_t i = 0; i < nwords; i++) {
        if (put_one(mps3_hwicap_pack_word(&bytes[4u * i])) != MPS3_HWICAP_OK) {
            if (nwritten) {
                *nwritten = i; /* only i words made it into the ICAP before the stall */
            }
            return MPS3_HWICAP_ERR;
        }
    }
    if (nwritten) {
        *nwritten = nwords;
    }
    return MPS3_HWICAP_OK;
#endif
}

int mps3_hwicap_finish(void)
{
    if (s_word_n != 0) {
        return MPS3_HWICAP_ERR; /* non-word-aligned payload */
    }
    if (mps3_hwicap_flush() != MPS3_HWICAP_OK) {
        return MPS3_HWICAP_ERR;
    }
    /* Bounded end-of-sequence wait, FAIL-CLOSED (this was fail-OPEN once: the old
     * body broke out of the loop and returned success whether or not EOS ever
     * asserted, so a stuck ICAP that never reached end-of-sequence was reported
     * as a clean finish and the swap "verified" a load that never completed).
     * If a specific axi_hwicap build is found not to raise EOS post-DESYNC,
     * relax THIS gate deliberately with a HW note — never pass on timeout. */
    for (uint32_t i = 0; i < MPS3_HWICAP_DONE_POLL_MAX; i++) {
        uint32_t sr = mps3_reg_read32(MPS3_HWICAP_BASE, HWICAP_SR);
        /* I18(3) capture-only: keep the last NON-ZERO SR for a post-swap JTAG
         * read (an all-zero read is uninformative — the tx_last_status lesson,
         * 22ea891). Changes neither the read the gate does nor the bound. */
        if (sr != 0u) {
            s_sr_last = sr;
        }
        if (sr & HWICAP_SR_EOS) {
            return MPS3_HWICAP_OK;
        }
    }
    return MPS3_HWICAP_ERR_EOS;
}

uint32_t mps3_hwicap_pace_words(uint32_t cap)
{
#if MPS3_HWICAP_FIFO
    uint32_t vacancy = mps3_reg_read32(MPS3_HWICAP_BASE, HWICAP_WFV);
    return (vacancy < cap) ? vacancy : cap;
#else
    return cap; /* no write FIFO to pace against — see the header */
#endif
}

int mps3_hwicap_stream_mem(const void *data, uint32_t len)
{
    if (data == 0 || (len & 3u) != 0) {
        return MPS3_HWICAP_ERR;
    }
    return mps3_hwicap_write_words((const uint8_t *)data, len / 4u, 0);
}

int mps3_hwicap_stream_region(mps3_hwicap_read_fn rd, void *ctx,
                              uint32_t off, uint32_t len,
                              uint8_t *scratch, uint32_t scratch_len)
{
    if (rd == 0 || scratch == 0 || scratch_len == 0 ||
        (len & 3u) != 0 || (scratch_len & 3u) != 0) {
        return MPS3_HWICAP_ERR;
    }
    uint32_t done = 0;
    while (done < len) {
        uint32_t n = len - done;
        if (n > scratch_len) {
            n = scratch_len;
        }
        if (rd(off + done, scratch, n, ctx) != 0) {
            return MPS3_HWICAP_ERR;
        }
        if (mps3_hwicap_write_words(scratch, n / 4u, 0) != MPS3_HWICAP_OK) {
            return MPS3_HWICAP_ERR; /* stuck ICAP word -> fail closed */
        }
        done += n;
    }
    return MPS3_HWICAP_OK;
}
