/*
 * ovlstore_phase.c — strong override of overlay_store's weak mps3_ovlstore_phase()
 * hook, publishing the current QSPI phase into the JTAG diagnostic mailbox.
 * See ovlstore_phase.h for the design rationale (the two consumers of the value).
 *
 * BUILD NOTE: this platform TU is folded into main.c's compile via a single
 * `#include "ovlstore_phase.c"` at the bottom of main.c, NOT added to
 * firmware/platform/Makefile's SRCS. That Makefile is concurrently owned by the
 * MF-1 clearing-size workstream; rather than risk a collision on its source list,
 * the override rides main.c's translation unit (it is compiled exactly once — it
 * is not in SRCS, so there is no duplicate definition). The host test links this
 * file standalone instead. If Makefile ownership frees up, prefer promoting this
 * to a first-class SRCS entry and dropping the include from main.c.
 */
#include <stdint.h>

#include "../../overlay_store/overlay_store.h"  /* canonical mps3_ovlstore_phase() prototype + OVL_PHASE_* */
#include "../../common/diag.h"                   /* g_mps3_diag mailbox */
#include "ovlstore_phase.h"

/* Last (phase, detail) stamped by overlay_store.c, held so main.c's per-poll
 * gather can re-thread it through mps3_diag_publish() (whole-struct copy). */
static volatile uint32_t s_ovl_phase  = OVL_PHASE_IDLE;
static volatile uint32_t s_ovl_detail = 0u;

/* STRONG definition — overrides the WEAK no-op in overlay_store.c. Records the
 * phase at the CALL (overlay_store.c stamps it immediately BEFORE each blocking
 * step, so this captures the last phase REACHED, not only completed steps) and
 * ALSO writes it straight into the mailbox. The direct mailbox write is the
 * point: when a QSPI step wedges, the superloop never reaches main.c's
 * bottom-of-loop diag gather, so without stamping the mailbox here the wedged
 * phase would never become JTAG-visible. Two 32-bit stores; no CS/opcode/SPI —
 * safe to call from anywhere in the QSPI path. */
void mps3_ovlstore_phase(uint32_t phase, uint32_t detail)
{
    s_ovl_phase  = phase;
    s_ovl_detail = detail;
    /* Direct into the fixed-address mailbox so the phase is readable over
     * JTAG-MDM mid-wedge. magic/version are stamped by mps3_diag_init(); on the
     * boot-load path that runs before the first stamp only if main.c ordered it
     * so (it does — see main.c). Single-threaded superloop, no ISRs, so this
     * cannot race the per-poll publish. */
    g_mps3_diag.ovlstore_phase  = phase;
    g_mps3_diag.ovlstore_detail = detail;
}

void mps3_ovlstore_phase_get(uint32_t *phase, uint32_t *detail)
{
    if (phase)  *phase  = s_ovl_phase;
    if (detail) *detail = s_ovl_detail;
}
