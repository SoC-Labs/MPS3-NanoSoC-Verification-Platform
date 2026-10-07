/*
 * swap_worker.h — one full DFX swap driven through /dev/mps3dfx.
 *
 * Runs in a FORKED child of mps3-ctrld (the 6900 loop must keep servicing
 * accept-then-EOF refusals while the swap is in flight — §2A.1 clause 3),
 * reports one swap_outcome over a pipe, exits. The parent delivers the
 * HELD 6900 response from the outcome.
 *
 * Sequence (SERVICE_DISPOSITION §4, v0.7 push-inside-swap ordering, and
 * the Wave-2 staged-pair superset):
 *   1. probe /dev/mps3dfx (missing device = fast honest failure)
 *   2. await the staged {clearing,partial} pair from mps3-pushd's spool —
 *      returns immediately if already staged (Wave-2 2A.3 pre-staging),
 *      otherwise waits with the 30 s RX-progress-armed idle timer
 *      (progress = rx.tmp growth / stage publication; never bounds a slow
 *      transfer)
 *   3. SWAP_BEGIN (decouple + shutdown + rp reset, confirm-polled in-driver)
 *   4. push the OUTGOING clearing: the promoted resident cache if one
 *      exists, else the incoming pair's clearing (first-swap fallback —
 *      the bare-metal shell boot-seeds greybox instead; documented delta)
 *   5. push the incoming partial STREAM_DIRECT (decouple containment),
 *      64 KiB chunks
 *   6. SWAP_FINISH: driver runs hostio4 hook (between stream and release,
 *      while decoupled+reset), release, RM_ID verify vs the partial's wire
 *      rm_id, re-isolate on mismatch
 *   7. capture GET_STATUS (EOS/SR/icap_bytes/hostio4) + MOCK_GET (mock
 *      runs only) for the log + diag
 *   8. success: promote incoming clearing -> resident cache, consume pair;
 *      any post-BEGIN failure also consumes the pair (one swap, one pair)
 *
 * Failure invariant is the driver's: RP parked decoupled + in reset.
 */
#ifndef MPS3_SWAP_WORKER_H
#define MPS3_SWAP_WORKER_H

#include <stdint.h>

typedef struct {
    const char *spool_dir;
    const char *dev_path;
    uint32_t    static_id;
    uint32_t    await_ms;       /* staged-pair idle timeout (30000 product) */
    int         mock;           /* configure the driver mock before the swap */
    int         mock_force;     /* mock only: present a FORCED rm_id ...     */
    uint32_t    mock_force_rm;  /* ... (wrong-id fault injection for tests)  */
} swap_worker_cfg_t;

typedef struct {
    uint32_t ok;
    uint32_t verified;
    uint32_t rm_id;          /* confirmed on success / resident on failure */
    int32_t  err;            /* engine code (<0) or -errno; 0 on success   */
    char     stage[16];      /* failing step, for the log                  */
    /* post-swap capture (GET_STATUS) */
    uint32_t eos_status;     /* 0 none / 1 seen / 2 timeout                */
    uint32_t sr_last;
    uint32_t hostio4_calls;
    uint64_t icap_delta;     /* bytes into ICAP for THIS swap              */
    uint32_t dfxctl_status;
    /* mock capture (mock runs only; mock_valid=0 on real hardware) */
    uint32_t mock_valid;
    uint32_t mock_words;
    uint32_t mock_stream_crc;
    uint32_t mock_saw_sync, mock_saw_desync;
} swap_outcome_t;

/* Runs the whole swap; always fills *out (fail-closed). Returns 0 if the
 * swap VERIFIED, -1 otherwise. Logs to stderr. */
int mps3_swap_worker_run(const swap_worker_cfg_t *cfg, swap_outcome_t *out);

#endif /* MPS3_SWAP_WORKER_H */
