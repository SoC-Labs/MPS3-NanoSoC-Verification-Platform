/*
 * service.h — the superloop's TIME discipline: a service table, a per-pass
 * budget watchdog, and the ONE bounded-wait primitive that replaces every
 * hand-rolled `for (i = 0; i < SOMETHING_POLL_BOUND; i++)` in the tree.
 *
 * TWO PROBLEMS THIS FILE EXISTS TO CLOSE
 * --------------------------------------
 * 1. THE SUPERLOOP WAS A LIST OF CALLS. firmware/platform/src/main.c ran nine
 *    _poll() functions in a hand-written sequence with no notion of how long
 *    any of them took. When the board wedged, the diag mailbox could say which
 *    QSPI phase or which ICAP status was last seen, but nothing said WHICH
 *    SERVICE ate the pass -- the one question a superloop wedge actually asks.
 *    A table makes the pass data, so the loop can measure itself: every service
 *    is timed, its worst pass is kept, and a service that keeps blowing its
 *    budget is taken out of the rotation instead of holding the whole shell.
 *
 * 2. THE BOUNDS WERE ITERATION COUNTS. Five files each spelled
 *        #define <THING>_POLL_BOUND 100000
 *    and spun that many times waiting on a register bit. An iteration count is
 *    not a duration: it is (loop body cycles / clock) seconds, so the SAME
 *    constant means ~7 ms in one build and something else entirely the moment
 *    the AXI clock, the compiler's optimisation level or the loop body changes.
 *    Nothing in the tree could state what any of those five waits was actually
 *    worth in microseconds -- the comments guessed ("a few ms worst case").
 *    mps3_spin_until() takes MICROSECONDS off the same free-running AXI timer
 *    the superloop is measured with, so a wait means the same thing in every
 *    build and can be asserted in a host test.
 *
 * PORTABILITY: this file touches no register and includes no platform header.
 * The only thing it needs is mps3_sys_now_us(), which the platform provides on
 * target (main.c, off the free-running AXI timer) and firmware/test/mock_regs.c
 * provides on the host. So it links into the host-gcc harness unchanged, which
 * is what makes the budget/skip policy testable at all.
 */
#ifndef MPS3_SERVICE_H
#define MPS3_SERVICE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------------------
 * The microsecond timebase.
 *
 * common/timebase.h owns the MILLISECOND clock (mps3_sys_now_ms). Budgets and
 * bounded waits both need better than millisecond resolution -- a service that
 * must not exceed 5 ms cannot be measured by a clock whose tick is 1 ms -- so
 * the microsecond view is declared here, next to its only two consumers.
 *
 * Same backing counter as mps3_sys_now_ms() on target (the free-running AXI
 * timer) and the same fake clock on the host, so the two never disagree.
 *
 * WRAP: 32 bits of microseconds wraps every ~71.6 minutes. NEVER compare two
 * timestamps with `<`. Subtract and compare the SIGNED difference against zero
 * -- which is what everything in this file does.
 * -------------------------------------------------------------------------- */
uint32_t mps3_sys_now_us(void);

/* ---------------------------------------------------------------------------
 * THE BOUNDED WAIT.
 *
 * Spin until `pred(ctx)` returns non-zero or `timeout_us` microseconds have
 * elapsed, whichever comes first. Returns 1 if the predicate was satisfied,
 * 0 on timeout.
 *
 * The predicate is evaluated at least ONCE before the deadline is consulted,
 * so a timeout_us of 0 still means "check once" and never "skip the check" --
 * an already-true condition is never reported as a timeout.
 *
 * This replaces the *_POLL_BOUND family. The call sites read the same,
 *     if (!mps3_spin_until(is_clear, &c, 2000u)) return ERR_TIMEOUT;
 * but 2000 is two milliseconds in every build, on every clock, forever.
 *
 * COST NOTE: each iteration reads the AXI timer, which on target is one AXI
 * transaction on top of the register read the predicate already does. That is
 * deliberate -- the previous scheme's "cost" was that nobody could say how long
 * it waited -- but it does mean a spin's iteration rate is roughly halved
 * versus the old raw loop. Every one of these waits is an error/backpressure
 * path, never a throughput path.
 *
 * WRITE THE PREDICATE TO STOP ON EVERY OUTCOME, NOT JUST THE GOOD ONE. Return
 * non-zero for anything that ends the wait -- success AND any hard error -- and
 * carry WHICH out in the ctx. A predicate that keeps waiting on a hard error is
 * not merely slow: on target it burns the caller's whole budget for a condition
 * that will never change, and it can do so WITHOUT PERFORMING A REGISTER READ
 * (smsc911x_tx_frame() rejects a zero-length frame before it touches the bus).
 * On the host that used to be an outright HANG rather than a test failure,
 * because the fake clock only moved on register reads; firmware/test/
 * mock_regs.c now charges the CLOCK read as well -- faithfully, since on target
 * mps3_sys_now_us() IS a register read -- so such a bug now surfaces as a clean
 * timeout. See net_if_lwip.c's tx_frame_settled() for the shape to copy.
 * -------------------------------------------------------------------------- */
typedef int (*mps3_spin_pred_fn)(void *ctx);
int mps3_spin_until(mps3_spin_pred_fn pred, void *ctx, uint32_t timeout_us);

/* ---------------------------------------------------------------------------
 * THE SERVICE TABLE
 * -------------------------------------------------------------------------- */
typedef void (*mps3_service_fn)(void);

typedef struct {
    const char      *name;      /* short, stable; index is the wire identity  */
    mps3_service_fn  poll;      /* bounded, non-blocking, called once a pass  */
    uint32_t         budget_us; /* 0 = UNBUDGETED: timed, never made sick     */
} mps3_service_t;

/* Table capacity. The superloop registers 13 (main.c; harnessd the same 13):
 * row 12 is "usd", the user-microSD driver + overlay store (D13, diag v9). The
 * packed per-service telemetry words in the diag mailbox are sized off this, so
 * raising it costs mailbox words -- see MPS3_SVC_PACK_WORDS. */
#define MPS3_SVC_MAX 13u

/* ---- THE SICK-SERVICE POLICY, WRITTEN DOWN ---------------------------------
 * K: a service whose measured duration exceeds its budget on
 * MPS3_SVC_SICK_K CONSECUTIVE passes is marked SICK and SKIPPED. One or two
 * overruns are a burst (a big ICAP chunk, a panel refresh landing on the same
 * pass as a TCP retransmit); three in a row is a service that is no longer
 * bounded, and holding the whole shell for it costs every other service --
 * including the network, which is how the board is recovered.
 *
 * A single overrun does NOT arm anything: the counter is CONSECUTIVE and is
 * reset to zero by the first pass that comes in under budget.
 *
 * UN-SKIPPING (this is a throttle, not an execution): a skipped service is
 * retried once every MPS3_SVC_COOLDOWN_US. If that probe pass comes in under
 * budget the service is healthy again immediately (consecutive count cleared,
 * skip bit cleared). If it overruns again it goes straight back to skipped for
 * another cooldown. So a service that is transiently pathological costs the
 * loop one probe per cooldown, and a service that is permanently pathological
 * costs one probe per cooldown FOREVER rather than being lost until reboot.
 *
 * WHY NOT PERMANENT: every service in this shell is load-bearing. Permanently
 * skipping the network RX drain because of one bad minute would strand the
 * board with no way in but JTAG -- trading a slow shell for a dark one. The
 * cooldown probe keeps the pathology visible (svc_skipped stays set, the
 * overrun counter keeps climbing) while never removing the recovery path.
 *
 * ESCAPE HATCH: budget_us == 0 means the service is timed and reported but
 * never marked sick. Use it for a service whose worst case genuinely is not
 * yet measured on silicon rather than inventing a number.
 * -------------------------------------------------------------------------- */
#define MPS3_SVC_SICK_K       3u
#define MPS3_SVC_COOLDOWN_US  100000u  /* 100 ms between probes of a sick service */

/* Install the table. `tbl` must outlive every run_pass() call (a static array
 * in practice). Entries beyond MPS3_SVC_MAX are REFUSED -- the return value is
 * the number actually installed, so a caller that grows the table past the
 * telemetry's capacity finds out here rather than silently losing services.
 * Resets all statistics. */
unsigned mps3_service_install(const mps3_service_t *tbl, unsigned n);

/* One superloop pass: walk the table, timing every service, applying the
 * budget/sick policy above -- and, at the END of the pass, kicking the hardware
 * watchdog if the pass actually did its job (see THE KICK below). */
void mps3_service_run_pass(void);

/* ---- THE HARDWARE WATCHDOG, AND WHERE THE KICK GOES ------------------------
 * (docs/planning/SERVICES_PARTITION.md §5; WDOG @0x44B4_0000,
 *  xilinx.com:ip:axi_timebase_wdt:3.0 in fpga/shell/bd/shell_bd.tcl)
 *
 * Until 2026-09-14 there was no hardware watchdog at all. If the superloop
 * STOPPED -- not "ran slowly", stopped -- nothing on the board noticed, and the
 * only ingress is the network that superloop serves. Recovery was a physical
 * power-cycle of the chassis.
 *
 * THE KICK IS NOT AT THE TOP OF THE LOOP, and that is the whole design. A kick
 * at the top proves the loop SPINS. This file already measures whether it
 * SERVED, so the kick goes at the END of a pass and only when no VITAL service
 * was skipped:
 *
 *     if ((mps3_service_skipped_mask() & vital_mask) == 0) mps3_wdt_kick();
 *
 * A shell whose network RX has been marked SICK and throttled to one probe per
 * 100 ms therefore STOPS KICKING and is reset -- which is correct, because a
 * board that cannot be reached is a dark board however healthily the CLCD
 * service is running. A shell that is merely slow keeps kicking, because the
 * sick policy is a throttle and not an execution.
 *
 * A pass with NO TABLE INSTALLED does not kick either: run_pass() returns early
 * and never reaches the kick. A shell with no services is not alive.
 *
 * THE MASK DEFAULTS TO ALL-ONES, i.e. ANY sick service withholds the kick.
 * That is the strict reading, and it is the safe DEFAULT rather than the
 * intended CONFIGURATION: the platform is expected to narrow it to the
 * genuinely vital services (§5.2 names net_rx, net_tmr and ctrl) so that a sick
 * CLCD cannot reset a board whose network is fine. Narrowing is a deliberate
 * act with a stated list; widening by forgetting is not possible.
 *
 * Timing margin, from the service table at firmware/platform/src/main.c: the
 * thirteen budgets sum to 270 ms (the twelve sum to 268; "usd" is 1.5), and WDOG's first stage is 2^27/100 MHz = 1.34 s
 * with the reset at ~2.7 s. A pass in which EVERY service ran to its full
 * budget still has 5x margin, and 1.34 s is >10x the 100 ms sick cooldown.
 * -------------------------------------------------------------------------- */

/* Every service vital: any sick service withholds the kick. The default. */
#define MPS3_SVC_VITAL_ALL 0xFFFFFFFFu

/* Kick the hardware watchdog. THE SEAM: service.c carries a WEAK no-op (same
 * mechanism as coordinator.c's weak mps3_shell_static_id(), which
 * generated/greybox_blob.c overrides), so every host-gcc binary in
 * firmware/test/ links unchanged and a shell whose BD has no WDOG block is
 * simply not kicking anything. The platform provides the strong definition that
 * W1Cs WDOG.TWCSR0.WDS. */
void mps3_wdt_kick(void);

/* Which services must be healthy for the pass to earn its kick. Bit i =
 * service i. Defaults to MPS3_SVC_VITAL_ALL; NOT touched by
 * mps3_service_reset_stats() or mps3_service_install(), because it is
 * configuration, not a statistic -- a caller that sets it before installing the
 * table must not have it silently reverted. */
void     mps3_service_set_vital_mask(uint32_t mask);
uint32_t mps3_service_vital_mask(void);

/* ---- CONTROL-CHANNEL LIVENESS: the INTERLEAVE (net-protocol v0.11, A4) ------
 * A pass is a walk of the table, so the control channel (service `ctrl`) was
 * served ONCE per pass: whatever the heavy services cost in between was 6900's
 * latency. On silicon (2026-09-22) a held touch made one clcd call cost 65-150
 * ms, every 6900 connection was RST for the duration, and nothing tripped,
 * because a slow pass is not a sick one.
 *
 * The interleave is a function run AFTER every service whose bit is set in
 * `after_mask` (and which actually ran this pass -- a throttled sick service
 * earns no interleave). The platform installs one that drains a few frames into
 * lwIP and polls the 6900 listener, so the longest the control channel can go
 * unserved is the longest SINGLE service call between two interleave points,
 * not the sum of the pass. That bound is what firmware/test/test_service.c
 * asserts with a synthetic slow service.
 *
 * It is NOT a table row, on purpose: the table's indices are mailbox contract
 * and every row costs diag words and three JTAG readers (the thirteenth, "usd",
 * paid for itself in diag v9). The interleave's own time is still inside the measured
 * PASS (pass_max_us) but is not charged to the service it follows, so a busy
 * control channel cannot make the CLCD look sick. */
void mps3_service_set_interleave(mps3_service_fn fn, uint32_t after_mask);
uint32_t mps3_service_interleave_runs(void);   /* interleave calls since boot  */
uint32_t mps3_service_interleave_max_us(void); /* worst single interleave call */

/* WINDOWED telemetry for the `stats` verb: the worst PASS and the number of
 * healthy->sick edges SINCE THE PREVIOUS take, then restart the window. The
 * since-boot figures (pass_max_us, skip_events) stay untouched for diag and the
 * mailbox; a poller that wants "is it starving NOW" needs the window instead,
 * because a since-boot maximum only ever goes up. */
void mps3_service_window_take(uint32_t *pass_max_us, uint32_t *skip_events);

/* Stop kicking the watchdog for the rest of this boot (the `reboot` verb). The
 * kick in run_pass() is conditional on this as well as on the vital mask, so a
 * future strong mps3_wdt_kick() cannot silently defeat a requested reboot. Not
 * reversible: a reboot that was asked for is not cancelled. */
void mps3_service_inhibit_kick(void);
int  mps3_service_kick_inhibited(void);

/* Zero every statistic (keeps the installed table). Tests use it; the target
 * never calls it -- the counters are free-running-since-boot by design, same
 * as every other mailbox counter. */
void mps3_service_reset_stats(void);

/* ---- telemetry (all cheap reads of statics; safe to call every pass) ------ */
unsigned    mps3_service_count(void);
const char *mps3_service_name(unsigned ix);        /* NULL if out of range     */

uint32_t mps3_service_pass_max_us(void);           /* worst FULL pass          */
uint32_t mps3_service_worst_us(void);              /* worst SINGLE service     */
uint32_t mps3_service_worst_index(void);           /* which service that was   */
uint32_t mps3_service_overrun_events(void);        /* budget overruns, total   */
uint32_t mps3_service_skip_events(void);           /* healthy -> sick edges    */
uint32_t mps3_service_skipped_mask(void);          /* bit i = service i is sick */
uint32_t mps3_service_kicks(void);                 /* passes that earned a kick */
uint32_t mps3_service_max_us(unsigned ix);         /* per-service worst, full u32 */

/* Per-service worst case, PACKED for the mailbox: two services per word, each
 * a 16-bit SATURATING microsecond count (0xFFFF means ">= 65.535 ms", which is
 * past every budget in the table -- the exact value stopped mattering long
 * before that). Word p carries service 2p in bits [15:0] and service 2p+1 in
 * bits [31:16]. Out-of-range services read 0. */
#define MPS3_SVC_PACK_WORDS ((MPS3_SVC_MAX + 1u) / 2u)   /* 7 for MPS3_SVC_MAX=13 */
uint32_t mps3_service_max_us_pack(unsigned pair);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_SERVICE_H */
