/*
 * fake_overlay_store.h — test double for the overlay store glue
 * (firmware/overlay_store/overlay_store.h) that swap_fsm.c and coordinator.c
 * call. Used by the binaries that link swap_fsm.c / coordinator.c but NOT the
 * real glue + ovlstore_sd.c + a block device: the greybox seed, a scriptable
 * `usd` status / action / commit, and a scriptable swap source "usd" (so the
 * FSM's usd path is unit-testable on its own; the real store end to end is
 * test_usd_boot.c).
 *
 * Defaults (after fake_overlay_store_reset(), and before any setter): NO CARD
 * -- status {"present":false,"state":"none","text":"none","boot":"none"},
 * commit / clear / format refused OVLSD_ENOCARD, rescan OVLSD_ENOCARD, and no
 * default for the swap source. That is ctrl_echo's fixed scenario.
 */
#ifndef MPS3_FAKE_OVERLAY_STORE_H
#define MPS3_FAKE_OVERLAY_STORE_H

#include "../overlay_store/overlay_store.h"

#ifdef __cplusplus
extern "C" {
#endif

void fake_overlay_store_reset(void);

/* Sets the descriptor overlay_store_get_greybox_clearing() will hand back
 * on its next call. Passing rc != 0 makes the NEXT call fail (return
 * nonzero) instead, to test swap_fsm_init()'s "greybox unavailable" path. */
void fake_overlay_store_set_greybox(const overlay_manifest_info_t *info, int rc);

/* Scripts commit: overlay_store_commit_begin() returns begin_rc; when it is
 * OVLSD_OK, the NEXT overlay_store_commit_poll() returns final_rc (with `slot`
 * on OVLSD_OK). */
void fake_overlay_store_set_commit(int begin_rc, int final_rc, char slot);
/* The descriptor / live ids overlay_store_commit_begin() last received. */
const ovlstore_sd_desc_t *fake_overlay_store_last_commit_desc(void);
uint32_t fake_overlay_store_last_commit_live_rm(void);
uint32_t fake_overlay_store_last_commit_live_static(void);

/* Scripts the `usd` verb: the status struct returned, and what an action
 * returns (begin_rc; when OVLSD_BUSY, the parked poll then returns final_rc). */
void fake_overlay_store_set_status(const mps3_usd_t *st);
void fake_overlay_store_set_action(int begin_rc, int final_rc);
const char *fake_overlay_store_last_action(void);
const char *fake_overlay_store_last_confirm(void);

/* Scripts the swap source "usd": the default descriptor (rc != 0 = none), and
 * the partial / clearing bytes src_next() hands out, `chunk` bytes per call,
 * with `busy_every` BUSY answers between chunks (0 = never busy, 0xFFFFFFFF =
 * BUSY forever: a store that never delivers). fail_at >= 0:
 * src_next() returns fail_rc once that many bytes have been handed out (the
 * card went bad). NULL bytes = counters only. */
void fake_overlay_store_set_src(const ovlstore_sd_desc_t *desc, int rc);
void fake_overlay_store_set_src_bytes(const uint8_t *clearing, const uint8_t *partial,
                                      uint32_t chunk, uint32_t busy_every);
void fake_overlay_store_set_src_fail(ovlstore_sd_which_t which, int32_t fail_at, int fail_rc);
int  fake_overlay_store_src_begins(ovlstore_sd_which_t which);
int  fake_overlay_store_src_aborts(void);
int  fake_overlay_store_src_open(void);   /* 1 while a stream is open */

#ifdef __cplusplus
}
#endif

#endif /* MPS3_FAKE_OVERLAY_STORE_H */
