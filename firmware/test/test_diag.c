/*
 * test_diag.c — the JTAG diagnostic mailbox had NO tests, and it cost us twice.
 *
 * mps3_diag_publish() used to copy 24 fields by hand. A `tx_last_status` field
 * was added to the struct, gathered in main.c and read over JTAG -- and read back
 * 0x00000000, because nobody added the 25th assignment. An evening went into
 * chasing a phantom before the diagnostic itself turned out to be the liar.
 * mps3_diag_snapshot() had drifted the same way, independently: it omitted
 * tx_last_status too, so the field was visible over JTAG but invisible to the
 * 6900 diag verb.
 *
 * An unverified diagnostic is worse than none: it does not merely fail to help,
 * it actively misdirects.
 *
 * These tests are deliberately FIELD-COUNT AGNOSTIC. They never name a field, so
 * adding one to mps3_diag_t cannot silently escape them -- which is exactly the
 * property the hand-written copies lacked.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../common/diag.h"
#include "../overlay_store/overlay_store.h"   /* OVL_PHASE_* + mps3_ovlstore_phase() */
#include "../platform/src/ovlstore_phase.h"   /* the strong override's getter */

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The two header words are owned by the mailbox, not by the publisher. */
static void restore_header(mps3_diag_t *v)
{
    v->magic   = MPS3_DIAG_MAGIC;
    v->version = MPS3_DIAG_VERSION;
}

static void test_init_zeroes_every_field(void)
{
    mps3_diag_t out;

    /* Poison the snapshot target so a no-op snapshot cannot pass. */
    memset(&out, 0x5A, sizeof(out));

    mps3_diag_init();
    mps3_diag_snapshot(&out);

    CHECK(out.magic == MPS3_DIAG_MAGIC);
    CHECK(out.version == MPS3_DIAG_VERSION);

    /* Everything else must be zero. tx_last_status specifically was NOT zeroed
     * by the old hand-written init, so a soft restart left a stale value in the
     * mailbox and the next reader trusted it. */
    mps3_diag_t zeroed;
    memset(&zeroed, 0, sizeof(zeroed));
    restore_header(&zeroed);
    CHECK(memcmp(&out, &zeroed, sizeof(out)) == 0);
}

static void test_publish_snapshot_round_trip_loses_no_field(void)
{
    mps3_diag_t v;
    mps3_diag_t out;

    /* Fill EVERY byte with a non-zero pattern. A dropped field shows up as a
     * zero where the pattern should be -- precisely the observed symptom
     * (tx_last_status read back 0x00000000). */
    memset(&v, 0xA5, sizeof(v));

    mps3_diag_init();
    mps3_diag_publish(&v);

    memset(&out, 0x5A, sizeof(out));   /* poison, so a no-op snapshot fails */
    mps3_diag_snapshot(&out);

    /* The publisher does not own the header; the mailbox does. */
    CHECK(out.magic == MPS3_DIAG_MAGIC);
    CHECK(out.version == MPS3_DIAG_VERSION);

    /* Every other byte must survive publish() -> snapshot() unchanged. */
    restore_header(&v);
    CHECK(memcmp(&out, &v, sizeof(out)) == 0);
}

static void test_publish_cannot_forge_the_header(void)
{
    /* A caller that fills its struct with garbage (or with a stale header read
     * back from a previous snapshot) must not be able to change magic/version.
     * The magic is what the JTAG reader scans for to FIND the mailbox and to
     * identify the right MicroBlaze target among several; a corrupted magic
     * makes the shell undebuggable. */
    mps3_diag_t v;
    memset(&v, 0xFF, sizeof(v));
    v.magic   = 0xDEADBEEFu;
    v.version = 0x99u;

    mps3_diag_init();
    mps3_diag_publish(&v);

    mps3_diag_t out;
    mps3_diag_snapshot(&out);
    CHECK(out.magic == MPS3_DIAG_MAGIC);
    CHECK(out.version == MPS3_DIAG_VERSION);
}

static void test_second_publish_overwrites_cleanly(void)
{
    /* publish() is a whole-struct copy, so a field that goes back to zero must
     * actually go back to zero -- a hand-written copy that skipped a field would
     * leave the previous value latched, which reads as a stuck counter. */
    mps3_diag_t a, b, out;
    memset(&a, 0xA5, sizeof(a));
    memset(&b, 0x00, sizeof(b));

    mps3_diag_init();
    mps3_diag_publish(&a);
    mps3_diag_publish(&b);
    mps3_diag_snapshot(&out);

    restore_header(&b);
    CHECK(memcmp(&out, &b, sizeof(out)) == 0);
}

/* -----------------------------------------------------------------------------
 * v6 QSPI/overlay-store phase hook (platform/src/ovlstore_phase.c). This is what
 * turns a field QSPI wedge into a one-board-run diagnosis, so it has the same
 * "the diagnostic must not lie" bar as everything above. Two independent halves,
 * each the "field added but never threaded" bug class that bit tx_last_status:
 *   1. the strong override must RECORD what overlay_store.c stamps AND write it
 *      straight into the mailbox at the call (so a mid-wedge phase is visible
 *      even though the per-poll gather is stalled);
 *   2. main.c's per-poll gather must re-thread that value through publish() or
 *      the whole-struct copy clobbers it back to IDLE.
 * (The struct fields themselves are already covered by the field-count-agnostic
 * round-trip above -- adding ovlstore_phase/detail to mps3_diag_t cannot escape
 * test_publish_snapshot_round_trip_loses_no_field. This adds the threading.)
 * -------------------------------------------------------------------------- */

static void test_ovlstore_phase_override_records_and_publishes_directly(void)
{
    mps3_diag_init();

    /* Exactly what overlay_store.c stamps immediately before erasing a sector. */
    mps3_ovlstore_phase(OVL_PHASE_ERASE_SECTOR, 0x00780000u);

    uint32_t p = 0, d = 0;
    mps3_ovlstore_phase_get(&p, &d);
    CHECK(p == OVL_PHASE_ERASE_SECTOR);
    CHECK(d == 0x00780000u);

    /* The wedge-visibility property: the override writes the mailbox DIRECTLY, so
     * with the superloop stalled inside the erase (gather not running) the phase
     * is still there for the JTAG reader. Drop that write and this reads IDLE. */
    CHECK(g_mps3_diag.ovlstore_phase  == OVL_PHASE_ERASE_SECTOR);
    CHECK(g_mps3_diag.ovlstore_detail == 0x00780000u);

    /* "The last phase before a hang is what's visible": a later stamp wins. */
    mps3_ovlstore_phase(OVL_PHASE_WAIT_READY, 0x00780000u);
    mps3_ovlstore_phase_get(&p, &d);
    CHECK(p == OVL_PHASE_WAIT_READY);
    CHECK(g_mps3_diag.ovlstore_phase == OVL_PHASE_WAIT_READY);
}

static void test_ovlstore_phase_survives_publish_only_when_threaded(void)
{
    mps3_diag_init();
    mps3_ovlstore_phase(OVL_PHASE_STREAM, 0x00010000u);

    /* CORRECT gather (what main.c does): getter -> v -> publish. The phase must
     * survive publish()'s whole-struct copy into the snapshot. */
    mps3_diag_t v = {0};
    mps3_ovlstore_phase_get(&v.ovlstore_phase, &v.ovlstore_detail);
    mps3_diag_publish(&v);

    mps3_diag_t out;
    mps3_diag_snapshot(&out);
    CHECK(out.ovlstore_phase  == OVL_PHASE_STREAM);
    CHECK(out.ovlstore_detail == 0x00010000u);

    /* BUGGY gather: the field is in the struct but main.c forgot to thread the
     * getter (v.ovlstore_phase left 0). publish()'s whole-struct copy then
     * clobbers the mailbox to IDLE -- the JTAG reader would see IDLE during a live
     * stream. Asserting the clobber proves the getter line in main.c is
     * load-bearing (this IS the tx_last_status defect: added, never threaded). */
    mps3_diag_t buggy = {0};   /* forgot mps3_ovlstore_phase_get() */
    mps3_diag_publish(&buggy);
    mps3_diag_snapshot(&out);
    CHECK(out.ovlstore_phase == OVL_PHASE_IDLE);   /* clobbered to 0, as the bug would */
}

int main(void)
{
    test_init_zeroes_every_field();
    test_publish_snapshot_round_trip_loses_no_field();
    test_publish_cannot_forge_the_header();
    test_second_publish_overwrites_cleanly();
    test_ovlstore_phase_override_records_and_publishes_directly();
    test_ovlstore_phase_survives_publish_only_when_threaded();
    printf("test_diag: %d checks passed\n", s_checks);
    return 0;
}
