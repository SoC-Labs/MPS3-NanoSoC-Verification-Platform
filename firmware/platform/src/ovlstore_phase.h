/*
 * ovlstore_phase.h — platform-layer STRONG override of overlay_store's weak
 * mps3_ovlstore_phase() hook (overlay_store.h "QSPI-path phase hook", defect C
 * from docs/QSPI_CLEARING_CACHE_HW_FINDINGS.md: "a hang must be locatable").
 *
 * overlay_store.c stamps mps3_ovlstore_phase(phase, detail) immediately BEFORE
 * every blocking QSPI step (erase/program/RDSR-spin/CRC/promote/stream) and
 * OVL_PHASE_IDLE on completion, with detail = the flash offset. Its own default
 * body is a WEAK no-op so that module stays diag-free and portable. THIS file is
 * the intended "board-side diag backend" that strongly overrides it to publish
 * (phase, detail) into the JTAG-readable diagnostic mailbox (diag.h), so a QSPI
 * wedge on silicon reports WHICH phase + WHICH flash offset it stuck at — even
 * though the superloop is stalled and the per-poll diag gather is not running.
 *
 * Two consumers of the recorded value, both load-bearing:
 *   1. The override writes g_mps3_diag.ovlstore_{phase,detail} DIRECTLY at the
 *      call (see ovlstore_phase.c) — this is what makes a mid-blocking-op phase
 *      visible over JTAG when the superloop never reaches its bottom-of-loop
 *      gather (the wedge case).
 *   2. main.c's per-poll gather reads mps3_ovlstore_phase_get() into its diag
 *      snapshot `v` so mps3_diag_publish()'s WHOLE-STRUCT copy does not clobber
 *      the phase back to 0 between updates (publish overwrites every field).
 *
 * Host-testable: this TU has ZERO Xilinx/register dependency (only <stdint.h>,
 * overlay_store.h for the canonical prototype, and diag.h for the mailbox), so
 * firmware/test/test_diag.c links it directly and exercises the real override.
 */
#ifndef MPS3_OVLSTORE_PHASE_H
#define MPS3_OVLSTORE_PHASE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Read back the last (phase, detail) stamped by the strong override. main.c's
 * per-poll diag gather calls this so the published mailbox keeps the current
 * phase across mps3_diag_publish()'s whole-struct overwrite. Either pointer may
 * be NULL. Before any stamp, reports (OVL_PHASE_IDLE, 0). */
void mps3_ovlstore_phase_get(uint32_t *phase, uint32_t *detail);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_OVLSTORE_PHASE_H */
