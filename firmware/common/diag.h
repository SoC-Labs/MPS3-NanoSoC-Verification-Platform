/*
 * diag.h — the shell's always-on diagnostic counter mailbox.
 *
 * A single fixed-layout struct (g_mps3_diag) that the superloop refreshes every
 * pass with the free-running RX-recovery / ICAP / receive-progress counters the
 * individual modules own (smsc911x_get_diag / swap_fsm_icap_bytes /
 * config_agent_rx_progress) plus the lwIP-receive visibility gathered from the
 * net backend (net_if_lwip). It is surfaced TWO ways so a wedging HW run can be
 * diagnosed either before OR after the board goes network-unreachable:
 *   - LIVE, over the wire: the 6900 control-channel `diag` verb encodes these
 *     fields as JSON (coordinator_handle_diag) — poll it while the board is up.
 *     NOTE: 6900 is PARKED during a swap, so this is only pollable when idle.
 *   - POST-MORTEM, over JTAG: on the real target g_mps3_diag is pinned at a FIXED
 *     DMEM address (firmware/platform/lscript.ld.in `.mps3_diag` — the top of the
 *     LMB, 0x3FF00 for the 256 KiB build at v8), so `mrd <addr> 64` over the MDM
 *     reads it after a wedge. The leading `magic` word confirms the reader is
 *     looking at a populated block. This path is readable DURING a swap (the
 *     only one).
 *
 * Portable / zero-HW: diag.c itself does no register or lwIP access (the values
 * are gathered by the platform layer and handed in), so it links into the
 * host-gcc test harness unchanged (the section attribute is inert there).
 */
#ifndef MPS3_DIAG_H
#define MPS3_DIAG_H

#include <stddef.h>   /* offsetof — the layout assertions below */
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MPS3_DIAG_MAGIC   0xD1A6C0DEu  /* "DIAG-CODE"-ish — stamped by mps3_diag_init */
#define MPS3_DIAG_VERSION 9u           /* v9 (D13): TWO formerly-reserved words carry
                                        * the user-microSD store -- svc_max_us_6 (+0xB0,
                                        * the thirteenth service row, "usd") and
                                        * usd_boot (+0xB4, the power-on load latch and
                                        * its decision). APPEND-INTO-RESERVED, the v6/v7
                                        * shape: struct size (0x100), the top anchor and
                                        * every prior offset are UNCHANGED, so the v8
                                        * readers and the Linux mailbox at LMB 0x1FF00
                                        * keep working; reserved[] shrinks 20 -> 18.
                                        * v8: the mailbox GREW 0x80 -> 0x100 (128 -> 256 B)
                                        * and the top-anchored base moved DOWN another
                                        * 0x80 (0x3FF80 -> 0x3FF00 on the 256 KiB build).
                                        * Only the SECOND layout move in the mailbox's
                                        * life -- v5 was the first (0x40 -> 0x80) and its
                                        * checklist is what this one followed. The
                                        * reserved[] pad was down to ONE word, so the
                                        * superloop service telemetry (13 words at
                                        * +0x7C..+0xAC: the per-pass and per-service
                                        * worst-case microseconds, the budget-overrun
                                        * counters and the sick-service skip mask --
                                        * firmware/common/service.h) had nowhere to go.
                                        * EVERY PRIOR OFFSET IS UNCHANGED; what moves is
                                        * the BASE and the total size. Both readers
                                        * therefore scan BOTH anchors (…F00 and …F80),
                                        * ascending, and read 64 words at a 0x100 anchor
                                        * but only 32 at a 0x80 one -- so a fielded v7
                                        * image stays readable by v8 tooling, which is
                                        * the requirement a base move has to meet.
                                        * v7: FOUR more formerly-reserved words carry the
                                        * STMPE811 panel-continuity probe (+0x6C..+0x78:
                                        * touch_probe_regs / _adc_x / _adc_y / _verdict;
                                        * firmware/touch/touch.h states the rule). Same
                                        * APPEND-INTO-RESERVED shape as v6: struct size
                                        * (0x80) and every prior offset UNCHANGED, only
                                        * reserved[] shrinks 5 words -> 1 (+0x7C). An old
                                        * JTAG reader still works; the bump tells a reader
                                        * those four words now mean something.
                                        * v6: two formerly-reserved words now carry the
                                        * QSPI/overlay-store phase hook — ovlstore_phase
                                        * (+0x64) and ovlstore_detail (+0x68). This is an
                                        * APPEND-INTO-RESERVED change: struct size (0x80,
                                        * 32 words) and the top-anchored base are BOTH
                                        * UNCHANGED, so every prior field keeps its offset
                                        * and an old JTAG reader still works — the bump only
                                        * tells a reader those two words now mean something
                                        * (vs. zeroed reserved). v5 was the real layout
                                        * move: struct GROWN 0x40 -> 0x80, base 0x3FFC0 ->
                                        * 0x3FF80. See lscript.ld.in (reservation stays
                                        * 0x80) and scripts/mps3_diag.tcl. */

/* Field offsets are STABLE (append-only) so a JTAG reader can hard-code them.
 * Base is NOT fixed, and TWO things move it. .mps3_diag is TOP-anchored to the
 * LMB end, so LMB_KB moves it -- and the STRUCT SIZE moves it too, because the
 * anchor is (LMB end - sizeof). At v8 (0x100) and at v5..v7 (0x80):
 *              v8 (0x100)      v5..v7 (0x80)
 *     256 KiB  0x0003FF00      0x0003FF80
 *     512 KiB  0x0007FF00      0x0007FF80
 *     1 MiB    0x000FFF00      0x000FFF80   (the current shell; LMB_KB=1024)
 * Worse, the LMB address decode ALIASES: reading 0x0007FF80 on a 256 KiB shell
 * silently returns the 0x0003FF80 mailbox rather than failing, so a hardcoded
 * address cannot even detect that it is wrong. scripts/mps3_diag.tcl therefore
 * SCANS the candidate bases for MPS3_DIAG_MAGIC, ASCENDING, over BOTH anchors,
 * and never trusts a constant. Do the same in any new tooling.
 *
 * HOW MUCH TO READ is decided by WHICH anchor hit, not by this header's
 * version: a 0x100 anchor carries 64 words, a 0x80 anchor 32. Reading 64 at a
 * 0x80 anchor runs off the LMB end (and aliases), so both readers derive the
 * length from `base & 0xFF` and report which layout they found. That is what
 * keeps an already-fielded v7 image readable by v8 tooling.
 *
 * The offsets below are relative to that base. (Historic: 0x3FF80 at v5..v7 on
 * the 256 KiB build; 0x3FFC0 at v4, when the struct was 0x40.) ABSOLUTE
 * addresses shown for the 256 KiB build AT v8:
 *  off   abs     field                     off   abs     field                     off   abs     field
 *  +0x00 0x3FF00 magic                      +0x3C 0x3FF3C tcp_snd_wnd                +0x78 0x3FF78 touch_probe_verdict  (v7)
 *  +0x04 0x3FF04 version                    +0x40 0x3FF40 tx_frames_sent             +0x7C 0x3FF7C svc_count            (v8)
 *  +0x08 0x3FF08 rx_recover_events          +0x44 0x3FF44 tx_status_drained          +0x80 0x3FF80 svc_pass_max_us      (v8)
 *  +0x0C 0x3FF0C rx_recover_dumps           +0x48 0x3FF48 tx_fifo_full_drops         +0x84 0x3FF84 svc_worst_us         (v8)
 *  +0x10 0x3FF10 rx_drop_frames             +0x4C 0x3FF4C tx_errors                  +0x88 0x3FF88 svc_worst_ix         (v8)
 *  +0x14 0x3FF14 icap_bytes                 +0x50 0x3FF50 tx_space_stalls            +0x8C 0x3FF8C svc_overrun_events   (v8)
 *  +0x18 0x3FF18 rx_payload_got             +0x54 0x3FF54 tx_iface_errors            +0x90 0x3FF90 svc_skip_events      (v8)
 *  +0x1C 0x3FF1C rx_payload_expect          +0x58 0x3FF58 tx_last_status             +0x94 0x3FF94 svc_skipped_mask     (v8)
 *  +0x20 0x3FF20 tcp_rcv_wnd                +0x5C 0x3FF5C icap_sr_last               +0x98 0x3FF98 svc_max_us_0         (v8)
 *  +0x24 0x3FF24 tcp_rcv_ann_wnd            +0x60 0x3FF60 icap_eos_status            +0x9C 0x3FF9C svc_max_us_1         (v8)
 *  +0x28 0x3FF28 rx_queued                  +0x64 0x3FF64 ovlstore_phase       (v6)  +0xA0 0x3FFA0 svc_max_us_2         (v8)
 *  +0x2C 0x3FF2C pbuf_free                  +0x68 0x3FF68 ovlstore_detail      (v6)  +0xA4 0x3FFA4 svc_max_us_3         (v8)
 *  +0x30 0x3FF30 win_windows_drained        +0x6C 0x3FF6C touch_probe_regs     (v7)  +0xA8 0x3FFA8 svc_max_us_4         (v8)
 *  +0x34 0x3FF34 win_grant_send_fails       +0x70 0x3FF70 touch_probe_adc_x    (v7)  +0xAC 0x3FFAC svc_max_us_5         (v8)
 *  +0x38 0x3FF38 tcp_sndbuf                 +0x74 0x3FF74 touch_probe_adc_y    (v7)  +0xB0 0x3FFB0 svc_max_us_6         (v9)
 *                                                                                    +0xB4 0x3FFB4 usd_boot             (v9)
 *                                                                                    +0xB8 0x3FFB8 reserved             pad
 *
 * NOTE the coincidence in that table: at v8 the 256 KiB build puts
 * svc_pass_max_us at 0x3FF80, which is where a v5..v7 image's MAGIC lived. It is
 * harmless only because the candidate scan is ASCENDING -- 0x3FF00 is tried
 * first and hits on a v8 image, so 0x3FF80 is never reached there. Do not
 * "optimise" that scan into newest-first; this is the second reason not to,
 * after the LMB aliasing.
 *
 * I18(3): icap_sr_last (+0x5C, abs 0x3FF5C on the 256 KiB build at v8) is the
 * raw last NON-ZERO HWICAP_SR read after DESYNC by icap_direct_finish();
 * icap_eos_status (+0x60, abs 0x3FF60) is 0=none / 1=EOS-seen / 2=EOS-timeout.
 * After a real stream-direct swap, `mrd 0x3FF5C 2` reads both — see the struct
 * field comment.
 */
/* ---------------------------------------------------------------------------
 * THE FIELD LIST — the single source for this mailbox.
 *
 * This layout used to be asserted by hand in FOUR places: the struct below, the
 * 6900 `diag` verb (net_proto.h's diag_* members + net_proto.c's snprintf +
 * coordinator.c's copy), host/socket_harness/xsdb.py's DIAG_FIELDS, and
 * scripts/mps3_diag.tcl's FIELDS. They disagreed — the struct declared 25
 * counters, the JSON verb transported 14 and the two JTAG readers listed 20 —
 * and nothing in the tree could see it. A counter nobody transports reads back
 * its init value forever, which is the same "diagnostic that lied" defect that
 * cost an evening when tx_last_status published as 0x00000000.
 *
 * So there is ONE list. Each row carries:
 *     X(<C field name>, <class>, "<JSON/wire key>")
 * with class
 *     META  — the header words mps3_diag_init() owns (magic, version);
 *     COUNT — a counter: published every superloop pass, read out by BOTH
 *             JTAG readers and (with the net_proto patch) the 6900 verb;
 *     PAD   — reserved[], MPS3_DIAG_RESERVED_WORDS words of growing room.
 * ROW ORDER IS THE WIRE/JTAG LAYOUT: word offset == row index (PAD last), which
 * is why rows are APPEND-ONLY — a JTAG reader hard-codes those offsets.
 *
 * The wire keys are FROZEN where they already shipped, and two of them do not
 * match their field name on purpose: "grants_sent" carries win_windows_drained
 * (the window-as-grant repurpose kept the schema key) and "grant_fails" carries
 * win_grant_send_fails. Do not "tidy" those.
 *
 * The struct below and both JTAG readers are RENDERED from this list by
 * tools/gen_diag.py; the static assertions under the struct tie the two
 * together at COMPILE time, so a row added here without regenerating (or a
 * member added there without a row) fails to build rather than shipping a
 * mailbox two readers disagree about.
 * -------------------------------------------------------------------------- */
#define MPS3_DIAG_RESERVED_WORDS 18u /* reserved[] pad words: +0xB8..+0xFC (v9 took
                                      * +0xB0/+0xB4 for the usd service row and the
                                      * power-on latch). v8 grew
                                      * the struct 128 -> 256 B specifically to
                                      * refill this: the pad was down to ONE word
                                      * (+0x7C) and the next feature that needed a
                                      * counter would have had to move the base
                                      * anyway. Twenty words is deliberate room --
                                      * appending into reserved[] keeps every
                                      * offset AND the base fixed, which is the
                                      * cheap kind of change; moving the base is
                                      * the expensive kind and has now happened
                                      * twice (v5, v8). */

#define MPS3_DIAG_FIELDS(X)                                                    \
    X(magic,   META,  "magic")   /* MPS3_DIAG_MAGIC once mps3_diag_init() ran */ \
    X(version, META,  "version") /* MPS3_DIAG_VERSION */                       \
    X(rx_recover_events,  COUNT, "rx_recover") /* smsc911x_rx_recover() firings */ \
    X(rx_recover_dumps,   COUNT, "rx_dumps")   /* of those, the hard RXE|RWT FIFO-dump path */ \
    X(rx_drop_frames,     COUNT, "rx_drops")   /* MAC frames dropped to overrun (summed RX_DROP) */ \
    X(icap_bytes,         COUNT, "icap_bytes") /* total bytes written to HWICAP.WF */ \
    X(rx_payload_got,     COUNT, "got")        /* config_agent: in-flight partial bytes received */ \
    X(rx_payload_expect,  COUNT, "expect")     /* config_agent: that partial's total bytes */ \
    /* v2 lwIP-receive visibility (the pivot: the stall is in the TCP RX path, \
     * not the MAC/ICAP). All for the 6910 config-agent connection. */         \
    X(tcp_rcv_wnd,        COUNT, "rcv_wnd")     /* 6910 pcb rcv_wnd — receive window available */ \
    X(tcp_rcv_ann_wnd,    COUNT, "rcv_ann_wnd") /* 6910 pcb rcv_ann_wnd — window to announce */ \
    X(rx_queued,          COUNT, "rx_queued")   /* bytes queued in the 6910 conn->rx pbuf chain */ \
    X(pbuf_free,          COUNT, "pbuf_free")   /* free PBUF_POOL bufs (MEMP stats); ~0u = unknown */ \
    /* v4 window-as-grant pacing visibility (the 6910 receive-window pacing that \
     * REPLACED the 1-byte grant). win_windows_drained (+0x30, was win_grants_sent) \
     * counts full CFG_AGENT_ACK_WINDOW_BYTES windows reopened via tcp_recved — for \
     * a healthy push it climbs to ~total/window, so a HW run reads it over JTAG to \
     * confirm pacing. win_grant_send_fails (+0x34) is retained (0 now — no \
     * application grant is sent). tcp_sndbuf/tcp_snd_wnd remain the shell->host \
     * TX visibility (sndbuf==0 => send refused; snd_wnd==0 => peer window shut). */ \
    X(win_windows_drained,  COUNT, "grants_sent") /* full windows reopened via mps3_net_recved() */ \
    X(win_grant_send_fails, COUNT, "grant_fails") /* legacy (0): window-as-grant sends no byte */ \
    X(tcp_sndbuf,           COUNT, "sndbuf")      /* 6910 pcb tcp_sndbuf() */   \
    X(tcp_snd_wnd,          COUNT, "snd_wnd")     /* 6910 pcb snd_wnd (peer's advertised window) */ \
    /* v5 TX-path counters (second 64 B) — the un-drained TX-STATUS-FIFO stall. \
     * The MAC posts one status DWORD per transmitted frame; if the status FIFO is \
     * never popped it fills and (TXSAO clear) the MAC stops STARTING new frames — \
     * TX egress then only lands after a TCP RTO under sustained RX + ICAP load. \
     * On a healthy HW run tx_status_drained tracks tx_frames_sent (TXSUSED never \
     * pinned), tx_fifo_full_drops stays ~0, tx_errors 0. From smsc911x_get_tx_diag \
     * (frames/status/errors) + net_if_lwip linkoutput (full_drops/stalls/iface). */ \
    X(tx_frames_sent,     COUNT, "tx_frames_sent")     /* frames accepted into the TX DATA FIFO */ \
    X(tx_status_drained,  COUNT, "tx_status_drained")  /* TX status words reaped from the TX STATUS FIFO */ \
    X(tx_fifo_full_drops, COUNT, "tx_fifo_full_drops") /* linkoutput ERR_MEM: no TDFREE after the full spin \
                                  * (told lwIP RETRY, never a silent as-sent drop) */ \
    X(tx_errors,          COUNT, "tx_errors")       /* popped TX status words with a transmit-error bit */ \
    X(tx_space_stalls,    COUNT, "tx_space_stalls") /* linkoutput calls that had to spin on TX_SPACE >=1x */ \
    X(tx_iface_errors,    COUNT, "tx_iface_errors") /* linkoutput ERR_IF: oversize / driver hard error */ \
    X(tx_last_status,     COUNT, "tx_last_status")  /* +0x58 raw last TX completion status word (decode aid) */ \
    /* I18 residue (3) — post-DESYNC EOS measurability. swap_fsm.c's stream-direct \
     * partial sink runs a BOUNDED post-DESYNC end-of-sequence wait in            \
     * icap_direct_finish(); that wait is documented best-effort, so a successful  \
     * swap alone does NOT prove HWICAP_SR_EOS ever asserted on this axi_hwicap    \
     * build. These capture the RAW status so the NEXT real stream-direct swap     \
     * answers it by one JTAG read (from swap_fsm_icap_sr_last() /                 \
     * swap_fsm_icap_eos_status()). icap_sr_last is the last NON-ZERO HWICAP_SR seen \
     * at finish (all-zero reads are skipped — the tx_last_status lesson: an empty  \
     * read tells you nothing). icap_eos_status is 0=none (no stream-direct finish  \
     * has run this boot), 1=EOS seen, 2=EOS-wait timed out (swap_fsm.h             \
     * MPS3_ICAP_EOS_*). Both persist across swaps (most recent finish wins). */   \
    X(icap_sr_last,     COUNT, "icap_sr_last")    /* +0x5C raw last NON-ZERO HWICAP_SR at finish */ \
    X(icap_eos_status,  COUNT, "icap_eos_status") /* +0x60 0=none 1=EOS-seen 2=EOS-timeout */ \
    /* v6 QSPI/overlay-store phase hook — locates a QSPI wedge (docs             \
     * QSPI_CLEARING_CACHE_HW_FINDINGS.md defect C: "a hang must be locatable"). \
     * overlay_store.c stamps mps3_ovlstore_phase(phase, detail) immediately BEFORE \
     * every blocking flash step; the platform strong override                    \
     * (platform/src/ovlstore_phase.c) writes them here DIRECTLY at the call, so   \
     * the last-reached phase + flash offset stay JTAG-readable even while the     \
     * superloop is stalled inside the wedged QSPI op (the bottom-of-loop diag     \
     * gather is not running then). phase = OVL_PHASE_* (overlay_store.h);         \
     * detail = the sector/page/region flash offset involved (IDLE => 0). */      \
    X(ovlstore_phase,   COUNT, "ovlstore_phase")  /* +0x64 OVL_PHASE_* of the in-flight QSPI step */ \
    X(ovlstore_detail,  COUNT, "ovlstore_detail") /* +0x68 flash offset that phase is operating on */ \
    /* v7 STMPE811 PANEL-CONTINUITY PROBE (firmware/touch/touch.h states the   \
     * rule and the bit packing). Silicon 2026-09-09 left "chip misconfigured" \
     * and "panel not connected" indistinguishable from every counter we had:  \
     * CHIP_ID 0x0811, 13 init writes ACKed, TSC_CTRL EN=1 read back, 321k     \
     * polls, ZERO I2C errors -- and TSC_STA never asserted under a real press.\
     * touch_init() now reads back the five registers that decide whether a    \
     * touch can be SEEN, and probes the four panel lines for continuity, ONCE,\
     * before the TSC is enabled. These four words are that answer: readable   \
     * over JTAG (mrd) or `pyverify diag` BEFORE anyone opens the case.        \
     * The probe writes them at init only; the superloop just republishes.     \
     * On a build without TOUCH=1 nothing produces them and they read 0 =      \
     * "unknown", which is the truth: no probe ran. */                         \
    X(touch_probe_regs,    COUNT, "touch_regs")    /* +0x6C read-backs: [7:0] GPIO_AF \
                                  * [15:8] SYS_CTRL2 [23:16] TSC_CFG [31:24] ADC_CTRL1 */ \
    X(touch_probe_adc_x,   COUNT, "touch_adc_x")   /* +0x70 [11:0] X+ [23:12] X- \
                                  * [31:24] TSC_I_DRIVE (the 5th read-back rides here) */ \
    X(touch_probe_adc_y,   COUNT, "touch_adc_y")   /* +0x74 [11:0] Y+ [23:12] Y- \
                                  * [31:24] TOUCH_PROBE_ST_* status bits */    \
    X(touch_probe_verdict, COUNT, "touch_verdict") /* +0x78 0=unknown 1=chip-misconfigured \
                                  * 2=panel-open 3=panel-present (touch.h) */  \
    /* v8 SUPERLOOP SERVICE TELEMETRY (firmware/common/service.h). The superloop \
     * is a TABLE now, and the table times itself. Until v8 the mailbox could say \
     * which QSPI phase or which ICAP status was last seen but nothing at all \
     * about WHICH SERVICE ate the pass -- the first question a superloop wedge \
     * asks. These thirteen words are that answer, and they are the reason the \
     * struct grew: reserved[] was down to one word. \
     * \
     * All microsecond figures are off the same free-running AXI timer lwIP's \
     * sys_now() reads (mps3_sys_now_us). All are HIGH-WATER marks since boot, \
     * never last-pass values -- a fast pass after a slow one must not erase the \
     * evidence of the slow one. On a build where the loop has not been installed \
     * they read 0, which is honest: no pass has been measured. */               \
    X(svc_count,          COUNT, "svc_count")   /* +0x7C services in the table   */ \
    X(svc_pass_max_us,    COUNT, "pass_max_us") /* +0x80 worst FULL pass, us     */ \
    X(svc_worst_us,       COUNT, "svc_max_us")  /* +0x84 worst SINGLE service, us */ \
    X(svc_worst_ix,       COUNT, "svc_max_ix")  /* +0x88 which service that was (table index; \
                                  * names are in main.c's table, in this order) */ \
    X(svc_overrun_events, COUNT, "svc_overruns")/* +0x8C budget overruns, total  */ \
    X(svc_skip_events,    COUNT, "svc_skips")   /* +0x90 healthy->sick EDGES (a service that \
                                  * stays sick does NOT keep inflating this)   */ \
    X(svc_skipped_mask,   COUNT, "svc_skipped") /* +0x94 bit i = service i is CURRENTLY sick \
                                  * (skipped, probed once per MPS3_SVC_COOLDOWN_US) */ \
    /* Per-service worst case, PACKED two per word: service 2p in bits [15:0] of \
     * word p, service 2p+1 in [31:16], each a 16-bit SATURATING microsecond \
     * count. 0xFFFF means ">= 65.535 ms", which is past every budget in the \
     * table -- a truncating cast would instead report a 65540 us service as 4 us, \
     * which is the diagnostic-that-lied shape. Seven words cover MPS3_SVC_MAX=13 \
     * (v9: the thirteenth row is "usd"; svc_max_us_6's upper half is service 13, \
     * which does not exist and reads 0). */ \
    X(svc_max_us_0,     COUNT, "svc_us_0")      /* +0x98 services 0,1  */ \
    X(svc_max_us_1,     COUNT, "svc_us_1")      /* +0x9C services 2,3  */ \
    X(svc_max_us_2,     COUNT, "svc_us_2")      /* +0xA0 services 4,5  */ \
    X(svc_max_us_3,     COUNT, "svc_us_3")      /* +0xA4 services 6,7  */ \
    X(svc_max_us_4,     COUNT, "svc_us_4")      /* +0xA8 services 8,9  */ \
    X(svc_max_us_5,     COUNT, "svc_us_5")      /* +0xAC services 10,11 */ \
    X(svc_max_us_6,     COUNT, "svc_us_6")      /* +0xB0 services 12,13 (v9) */ \
    /* v9 (D13) THE POWER-ON LOAD LATCH of the user-microSD overlay store \
     * (firmware/overlay_store/overlay_store.h, "THE BOOT LATCH"): 0 until the \
     * power-on decision is taken in this FPGA configuration, then \
     * [31:16] 0xB007, [15:8] the failure reason, [3:0] the decision (1 pending, \
     * 2 loaded, 3 skipped, 4 none, 5 failed). Under Linux this WORD IS THE \
     * LATCH: it lives in LMB BRAM, which only a reconfiguration clears, so a \
     * harnessd respawn, an OS reboot and a WDOG reset all find it set. */ \
    X(usd_boot,         COUNT, "usd_boot")      /* +0xB4 the power-on latch (v9) */ \
    X(reserved,         PAD,   "")                /* pad to 256 B (+0xB8..+0xFC); zeroed, room to grow */

/* The mailbox itself. RENDERED from MPS3_DIAG_FIELDS above by tools/gen_diag.py
 * — as literal declarations, not a macro expansion, because
 * scripts/harness_gates/check_diag_field_parity.py PARSES this struct to learn
 * what a counter is. Hidden behind `MPS3_DIAG_FIELDS(DECL)` that gate would
 * find no fields, report "covers every counter" over an empty set and return 0
 * — a gate that cannot fail, in the slot where the real check used to be. The
 * _Static_asserts below are what make the two forms provably the same. */
typedef struct {
/* BEGIN GENERATED[diag-struct] — gen_diag.py — DO NOT EDIT BY HAND */
    uint32_t magic;              /* MPS3_DIAG_MAGIC once mps3_diag_init() ran */
    uint32_t version;            /* MPS3_DIAG_VERSION */
    uint32_t rx_recover_events;  /* smsc911x_rx_recover() firings */
    uint32_t rx_recover_dumps;   /* of those, the hard RXE|RWT FIFO-dump path */
    uint32_t rx_drop_frames;     /* MAC frames dropped to overrun (summed RX_DROP) */
    uint32_t icap_bytes;         /* total bytes written to HWICAP.WF */
    uint32_t rx_payload_got;     /* config_agent: in-flight partial bytes received */
    uint32_t rx_payload_expect;  /* config_agent: that partial's total bytes */
    /* v2 lwIP-receive visibility (the pivot: the stall is in the TCP RX path,
     * not the MAC/ICAP). All for the 6910 config-agent connection. */
    uint32_t tcp_rcv_wnd;        /* 6910 pcb rcv_wnd — receive window available */
    uint32_t tcp_rcv_ann_wnd;    /* 6910 pcb rcv_ann_wnd — window to announce */
    uint32_t rx_queued;          /* bytes queued in the 6910 conn->rx pbuf chain */
    uint32_t pbuf_free;          /* free PBUF_POOL bufs (MEMP stats); ~0u = unknown */
    /* v4 window-as-grant pacing visibility (the 6910 receive-window pacing that
     * REPLACED the 1-byte grant). win_windows_drained (+0x30, was win_grants_sent)
     * counts full CFG_AGENT_ACK_WINDOW_BYTES windows reopened via tcp_recved — for
     * a healthy push it climbs to ~total/window, so a HW run reads it over JTAG to
     * confirm pacing. win_grant_send_fails (+0x34) is retained (0 now — no
     * application grant is sent). tcp_sndbuf/tcp_snd_wnd remain the shell->host
     * TX visibility (sndbuf==0 => send refused; snd_wnd==0 => peer window shut). */
    uint32_t win_windows_drained; /* full windows reopened via mps3_net_recved() */
    uint32_t win_grant_send_fails; /* legacy (0): window-as-grant sends no byte */
    uint32_t tcp_sndbuf;         /* 6910 pcb tcp_sndbuf() */
    uint32_t tcp_snd_wnd;        /* 6910 pcb snd_wnd (peer's advertised window) */
    /* v5 TX-path counters (second 64 B) — the un-drained TX-STATUS-FIFO stall.
     * The MAC posts one status DWORD per transmitted frame; if the status FIFO is
     * never popped it fills and (TXSAO clear) the MAC stops STARTING new frames —
     * TX egress then only lands after a TCP RTO under sustained RX + ICAP load.
     * On a healthy HW run tx_status_drained tracks tx_frames_sent (TXSUSED never
     * pinned), tx_fifo_full_drops stays ~0, tx_errors 0. From smsc911x_get_tx_diag
     * (frames/status/errors) + net_if_lwip linkoutput (full_drops/stalls/iface). */
    uint32_t tx_frames_sent;     /* frames accepted into the TX DATA FIFO */
    uint32_t tx_status_drained;  /* TX status words reaped from the TX STATUS FIFO */
    uint32_t tx_fifo_full_drops; /* linkoutput ERR_MEM: no TDFREE after the full spin
                                  * (told lwIP RETRY, never a silent as-sent drop) */
    uint32_t tx_errors;          /* popped TX status words with a transmit-error bit */
    uint32_t tx_space_stalls;    /* linkoutput calls that had to spin on TX_SPACE >=1x */
    uint32_t tx_iface_errors;    /* linkoutput ERR_IF: oversize / driver hard error */
    uint32_t tx_last_status;     /* +0x58 raw last TX completion status word (decode aid) */
    /* I18 residue (3) — post-DESYNC EOS measurability. swap_fsm.c's stream-direct
     * partial sink runs a BOUNDED post-DESYNC end-of-sequence wait in
     * icap_direct_finish(); that wait is documented best-effort, so a successful
     * swap alone does NOT prove HWICAP_SR_EOS ever asserted on this axi_hwicap
     * build. These capture the RAW status so the NEXT real stream-direct swap
     * answers it by one JTAG read (from swap_fsm_icap_sr_last() /
     * swap_fsm_icap_eos_status()). icap_sr_last is the last NON-ZERO HWICAP_SR seen
     * at finish (all-zero reads are skipped — the tx_last_status lesson: an empty
     * read tells you nothing). icap_eos_status is 0=none (no stream-direct finish
     * has run this boot), 1=EOS seen, 2=EOS-wait timed out (swap_fsm.h
     * MPS3_ICAP_EOS_*). Both persist across swaps (most recent finish wins). */
    uint32_t icap_sr_last;       /* +0x5C raw last NON-ZERO HWICAP_SR at finish */
    uint32_t icap_eos_status;    /* +0x60 0=none 1=EOS-seen 2=EOS-timeout */
    /* v6 QSPI/overlay-store phase hook — locates a QSPI wedge (docs
     * QSPI_CLEARING_CACHE_HW_FINDINGS.md defect C: "a hang must be locatable").
     * overlay_store.c stamps mps3_ovlstore_phase(phase, detail) immediately BEFORE
     * every blocking flash step; the platform strong override
     * (platform/src/ovlstore_phase.c) writes them here DIRECTLY at the call, so
     * the last-reached phase + flash offset stay JTAG-readable even while the
     * superloop is stalled inside the wedged QSPI op (the bottom-of-loop diag
     * gather is not running then). phase = OVL_PHASE_* (overlay_store.h);
     * detail = the sector/page/region flash offset involved (IDLE => 0). */
    uint32_t ovlstore_phase;     /* +0x64 OVL_PHASE_* of the in-flight QSPI step */
    uint32_t ovlstore_detail;    /* +0x68 flash offset that phase is operating on */
    /* v7 STMPE811 PANEL-CONTINUITY PROBE (firmware/touch/touch.h states the
     * rule and the bit packing). Silicon 2026-09-09 left "chip misconfigured"
     * and "panel not connected" indistinguishable from every counter we had:
     * CHIP_ID 0x0811, 13 init writes ACKed, TSC_CTRL EN=1 read back, 321k
     * polls, ZERO I2C errors -- and TSC_STA never asserted under a real press.
     * touch_init() now reads back the five registers that decide whether a
     * touch can be SEEN, and probes the four panel lines for continuity, ONCE,
     * before the TSC is enabled. These four words are that answer: readable
     * over JTAG (mrd) or `pyverify diag` BEFORE anyone opens the case.
     * The probe writes them at init only; the superloop just republishes.
     * On a build without TOUCH=1 nothing produces them and they read 0 =
     * "unknown", which is the truth: no probe ran. */
    uint32_t touch_probe_regs;   /* +0x6C read-backs: [7:0] GPIO_AF
                                  * [15:8] SYS_CTRL2 [23:16] TSC_CFG [31:24] ADC_CTRL1 */
    uint32_t touch_probe_adc_x;  /* +0x70 [11:0] X+ [23:12] X-
                                  * [31:24] TSC_I_DRIVE (the 5th read-back rides here) */
    uint32_t touch_probe_adc_y;  /* +0x74 [11:0] Y+ [23:12] Y-
                                  * [31:24] TOUCH_PROBE_ST_* status bits */
    uint32_t touch_probe_verdict; /* +0x78 0=unknown 1=chip-misconfigured
                                   * 2=panel-open 3=panel-present (touch.h) */
    /* v8 SUPERLOOP SERVICE TELEMETRY (firmware/common/service.h). The superloop
     * is a TABLE now, and the table times itself. Until v8 the mailbox could say
     * which QSPI phase or which ICAP status was last seen but nothing at all
     * about WHICH SERVICE ate the pass -- the first question a superloop wedge
     * asks. These thirteen words are that answer, and they are the reason the
     * struct grew: reserved[] was down to one word.
     *
     * All microsecond figures are off the same free-running AXI timer lwIP's
     * sys_now() reads (mps3_sys_now_us). All are HIGH-WATER marks since boot,
     * never last-pass values -- a fast pass after a slow one must not erase the
     * evidence of the slow one. On a build where the loop has not been installed
     * they read 0, which is honest: no pass has been measured. */
    uint32_t svc_count;          /* +0x7C services in the table */
    uint32_t svc_pass_max_us;    /* +0x80 worst FULL pass, us */
    uint32_t svc_worst_us;       /* +0x84 worst SINGLE service, us */
    uint32_t svc_worst_ix;       /* +0x88 which service that was (table index;
                                  * names are in main.c's table, in this order) */
    uint32_t svc_overrun_events; /* +0x8C budget overruns, total */
    uint32_t svc_skip_events;    /* +0x90 healthy->sick EDGES (a service that
                                  * stays sick does NOT keep inflating this) */
    uint32_t svc_skipped_mask;   /* +0x94 bit i = service i is CURRENTLY sick
                                  * (skipped, probed once per MPS3_SVC_COOLDOWN_US) */
    /* Per-service worst case, PACKED two per word: service 2p in bits [15:0] of
     * word p, service 2p+1 in [31:16], each a 16-bit SATURATING microsecond
     * count. 0xFFFF means ">= 65.535 ms", which is past every budget in the
     * table -- a truncating cast would instead report a 65540 us service as 4 us,
     * which is the diagnostic-that-lied shape. Seven words cover MPS3_SVC_MAX=13
     * (v9: the thirteenth row is "usd"; svc_max_us_6's upper half is service 13,
     * which does not exist and reads 0). */
    uint32_t svc_max_us_0;       /* +0x98 services 0,1 */
    uint32_t svc_max_us_1;       /* +0x9C services 2,3 */
    uint32_t svc_max_us_2;       /* +0xA0 services 4,5 */
    uint32_t svc_max_us_3;       /* +0xA4 services 6,7 */
    uint32_t svc_max_us_4;       /* +0xA8 services 8,9 */
    uint32_t svc_max_us_5;       /* +0xAC services 10,11 */
    uint32_t svc_max_us_6;       /* +0xB0 services 12,13 (v9) */
    /* v9 (D13) THE POWER-ON LOAD LATCH of the user-microSD overlay store
     * (firmware/overlay_store/overlay_store.h, "THE BOOT LATCH"): 0 until the
     * power-on decision is taken in this FPGA configuration, then
     * [31:16] 0xB007, [15:8] the failure reason, [3:0] the decision (1 pending,
     * 2 loaded, 3 skipped, 4 none, 5 failed). Under Linux this WORD IS THE
     * LATCH: it lives in LMB BRAM, which only a reconfiguration clears, so a
     * harnessd respawn, an OS reboot and a WDOG reset all find it set. */
    uint32_t usd_boot;           /* +0xB4 the power-on latch (v9) */
    uint32_t reserved[MPS3_DIAG_RESERVED_WORDS]; /* pad to 256 B (+0xB8..+0xFC); zeroed, room to grow */
/* END GENERATED[diag-struct] */
} mps3_diag_t;

/* ---------------------------------------------------------------------------
 * LAYOUT, PROVED AT COMPILE TIME.
 *
 * The struct above is generated text; this block is what makes "generated"
 * mean "cannot have drifted". Every row of MPS3_DIAG_FIELDS is expanded into
 * an offsetof() assertion against its DECLARATION INDEX, and the total is
 * asserted against both the field list and the literal 256 bytes the JTAG
 * readers (and firmware/platform/lscript.ld.in's 0x100 reservation) assume. Add
 * a row without regenerating, regenerate without a row, rename or reorder
 * either one, and the build stops here — not on the bench with a garbage
 * readout from a shifted offset.
 * -------------------------------------------------------------------------- */
#if defined(__STDC_VERSION__) && (__STDC_VERSION__ >= 201112L)
#  define MPS3_DIAG_ASSERT_(tag, cond) _Static_assert(cond, #tag)
#else
/* Pre-C11 (and C++, where __STDC_VERSION__ is absent): the negative-array
 * trick. Same build failure, uglier message — this header is compiled by the
 * host-gcc harness (-std=c11) AND by whatever mb-gcc Vitis ships, so it may
 * not assume either. */
#  define MPS3_DIAG_ASSERT_(tag, cond) \
     typedef char mps3_diag_assert_##tag[(cond) ? 1 : -1]
#endif

/* Words each class of row occupies — the ONLY place a row's size is known. */
#define MPS3_DIAG_WORDS_META   1u
#define MPS3_DIAG_WORDS_COUNT  1u
#define MPS3_DIAG_WORDS_PAD    MPS3_DIAG_RESERVED_WORDS
#define MPS3_DIAG_ROW_WORDS_(name, cls, key) + MPS3_DIAG_WORDS_##cls
/* Total mailbox size in 32-bit words, straight from the list (64 since v8). */
#define MPS3_DIAG_WORDS (0u MPS3_DIAG_FIELDS(MPS3_DIAG_ROW_WORDS_))

/* Row index == word offset (PAD is last and the only multi-word row), so a
 * reader that wants "which word is tx_last_status" can ask the compiler:
 * MPS3_DIAG_IX_tx_last_status. */
#define MPS3_DIAG_ROW_INDEX_(name, cls, key) MPS3_DIAG_IX_##name,
enum { MPS3_DIAG_FIELDS(MPS3_DIAG_ROW_INDEX_) MPS3_DIAG_N_ROWS };

MPS3_DIAG_ASSERT_(mps3_diag_t_is_256_bytes, sizeof(mps3_diag_t) == 256u);
MPS3_DIAG_ASSERT_(mps3_diag_t_matches_the_field_list,
                  sizeof(mps3_diag_t) == 4u * (MPS3_DIAG_WORDS));

/* Every field, at 4 * its row index. */
#define MPS3_DIAG_ROW_OFFSET_(name, cls, key)                                  \
    MPS3_DIAG_ASSERT_(offset_of_##name,                                        \
        offsetof(mps3_diag_t, name) == 4u * (unsigned)MPS3_DIAG_IX_##name);
MPS3_DIAG_FIELDS(MPS3_DIAG_ROW_OFFSET_)

/* The four anchors spelled out in absolute terms as well, because these are the
 * numbers pasted into an `mrd` at 2am (and quoted in the table above). */
MPS3_DIAG_ASSERT_(anchor_magic_at_0x00,
                  offsetof(mps3_diag_t, magic) == 0x00u);
MPS3_DIAG_ASSERT_(anchor_icap_bytes_at_0x14,
                  offsetof(mps3_diag_t, icap_bytes) == 0x14u);
MPS3_DIAG_ASSERT_(anchor_tx_last_status_at_0x58,
                  offsetof(mps3_diag_t, tx_last_status) == 0x58u);
MPS3_DIAG_ASSERT_(anchor_ovlstore_detail_at_0x68,
                  offsetof(mps3_diag_t, ovlstore_detail) == 0x68u);
MPS3_DIAG_ASSERT_(touch_probe_regs_at_0x6C,
                  offsetof(mps3_diag_t, touch_probe_regs) == 0x6Cu);
MPS3_DIAG_ASSERT_(touch_probe_verdict_at_0x78,
                  offsetof(mps3_diag_t, touch_probe_verdict) == 0x78u);
MPS3_DIAG_ASSERT_(svc_count_at_0x7C,
                  offsetof(mps3_diag_t, svc_count) == 0x7Cu);
MPS3_DIAG_ASSERT_(svc_max_us_5_at_0xAC,
                  offsetof(mps3_diag_t, svc_max_us_5) == 0xACu);
MPS3_DIAG_ASSERT_(svc_max_us_6_at_0xB0,
                  offsetof(mps3_diag_t, svc_max_us_6) == 0xB0u);
MPS3_DIAG_ASSERT_(usd_boot_at_0xB4,
                  offsetof(mps3_diag_t, usd_boot) == 0xB4u);
MPS3_DIAG_ASSERT_(pad_starts_at_0xB8,
                  offsetof(mps3_diag_t, reserved) == 0xB8u);

/* The live mailbox. On target it sits at the fixed .mps3_diag address; read it
 * over JTAG-MDM or via the diag verb. */
extern volatile mps3_diag_t g_mps3_diag;

/* Stamp the magic/version and zero the counters. Call once at boot. The counter
 * fields are all re-supplied by the per-superloop gather (main.c), so this may —
 * and now does — run BEFORE the module inits rather than after: the mailbox must
 * be live (magic stamped) before coordinator_init()'s boot-time overlay load
 * touches QSPI, or a wedge in THAT path leaves the mailbox all-zero and the JTAG
 * reader (which scans for the magic) cannot even find it. See main.c. */
void mps3_diag_init(void);

/* Copy the counter fields of *v into the mailbox (magic/version are preserved,
 * set by mps3_diag_init). Called every superloop pass with a freshly-gathered
 * snapshot. */
void mps3_diag_publish(const mps3_diag_t *v);

/* Read a consistent copy of the mailbox (for the diag verb encoder). */
void mps3_diag_snapshot(mps3_diag_t *out);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_DIAG_H */
