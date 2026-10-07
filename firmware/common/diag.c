/*
 * diag.c — the fixed-address diagnostic mailbox (see diag.h).
 */
#include <string.h>
#include "diag.h"

/* The counter block. On the real target the linker script
 * (firmware/platform/lscript.ld.in) places the `.mps3_diag` section at a FIXED
 * DMEM address at the top of the LMB (0x3FF80 for the 256 KiB build, v5 — the
 * struct grew to 128 B so the top-anchored base moved down from 0x3FFC0) so it
 * can be read over JTAG-MDM after the board wedges. `used` keeps --gc-sections from
 * dropping it; `volatile` because the MDM reads it asynchronously while the
 * superloop publishes into it. On the host test build the section attribute is
 * inert (ordinary placement). */
volatile mps3_diag_t g_mps3_diag __attribute__((used, section(".mps3_diag")));

void mps3_diag_init(void)
{
    /* memset, not a field list. A hand-written list of zeroes is the same bug
     * generator as the copies below: `tx_last_status` was never zeroed here, so
     * a soft restart left a stale value in the mailbox. */
    /* The cast is REQUIRED, not cosmetic: g_mps3_diag is volatile (the MDM
     * reads it asynchronously) and memset() takes a plain void *, so an
     * uncast &g_mps3_diag silently discards the qualifier
     * (-Wdiscarded-qualifiers, now -Werror). Zeroing the whole struct once
     * at init has no ordering requirement against the MDM, so dropping
     * volatile for this one call is safe -- unlike a per-field publish. */
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    g_mps3_diag.magic   = MPS3_DIAG_MAGIC;
    g_mps3_diag.version = MPS3_DIAG_VERSION;
}

void mps3_diag_publish(const mps3_diag_t *v)
{
    /* WHOLE-STRUCT copy. This was 24 hand-written field assignments, and it cost
     * us: a `tx_last_status` field added to the struct, published from main.c and
     * read over JTAG, silently read back 0x00000000 -- because nobody added the
     * 25th line here. Chasing that phantom burned an evening. An unverified
     * diagnostic is worse than none.
     *
     * `v` carries no header, so preserve ours across the copy. Now a new field
     * cannot be forgotten: adding it to the struct is sufficient. */
    const uint32_t magic   = g_mps3_diag.magic;
    const uint32_t version = g_mps3_diag.version;

    g_mps3_diag         = *v;
    g_mps3_diag.magic   = magic;
    g_mps3_diag.version = version;
}

void mps3_diag_snapshot(mps3_diag_t *out)
{
    /* Same reason as publish(). This copy had ALREADY drifted: it omitted
     * tx_last_status, so the field was visible over JTAG but invisible to the
     * 6900 diag verb -- the same defect, in a second place, undetected. */
    *out = g_mps3_diag;
}
