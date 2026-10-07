/*
 * config_agent.c — bitstream receiver + header validation + I2 ordering +
 * two-slot {clearing, partial} pair staging (see config_agent.h's STAGING
 * DECISION block).
 *
 * Deliberately has NO #include of coordinator.h/platform_regs.h: every
 * function here is either pure logic (validate_header, check_payload_crc)
 * or receive-session bookkeeping against the common/net_if.h SEAM — no
 * register access, no lwIP. The running static_id arrives through
 * config_agent_set_running_static_id() (coordinator_init() threads it in),
 * which keeps this file link-independent of the coordinator: the host-gcc
 * harness links it with fake_net_if.c and nothing else hardware-shaped.
 *
 * Transports (net-protocol.md): TFTP WRQ/octet on UDP 69 (RFC1350 lock-step,
 * but SINGLE-PORT: every reply comes FROM :69 -- see tftp_poll() -- which
 * host/pusher's PUT accepts), and
 * the raw-TCP push on 6910 (24-byte header + payload; the server closes
 * when validation finishes, EOF being the client's only sync point — the
 * "no acknowledgement" ambiguity fakeshell.py flags for A6).
 */
#include <string.h>
#include "config_agent.h"
#include "../common/crc32.h"
#include "../common/net_if.h"

/* ---- staging slots (the two-slot pair; see config_agent.h) ---------------- */
/* `data` is a pointer to a per-slot RAM buffer (below), NOT an inline array,
 * so the clearing slot (must hold a full ~48 KiB clearing) and the partial
 * slot (dead in the gate-2 QSPI world — a large partial streams to flash,
 * so its RAM fast-path buffer can be a few KiB on a tight target) can be
 * sized independently (clearing = MPS3_CFG_AGENT_CLEARING_RAM_BYTES, partial =
 * MPS3_CFG_AGENT_PARTIAL_RAM_BYTES). On the host test build both are
 * MPS3_CFG_AGENT_STAGING_BYTES (CLEARING_RAM and PARTIAL_RAM both default to
 * STAGING), so this is byte-for-byte the old two-equal-slots layout. `cap` is
 * that buffer's size
 * — the RAM/QSPI routing threshold for this slot (see begin_payload). */
typedef struct {
    int                  full;
    int                  in_qspi;   /* 1 => large partial staged in QSPI scratch; data[] unused */
    int                  in_icap;   /* 1 => large partial streamed straight to HWICAP (Path 3); data[] unused, already in the ICAP */
    mps3_bitstream_hdr_t hdr;
    uint8_t             *data;      /* -> s_clearing_ram / s_partial_ram */
    uint32_t             cap;       /* sizeof(*data) — RAM staging bound for this slot */
} staging_slot_t;

static uint8_t s_clearing_ram[MPS3_CFG_AGENT_CLEARING_RAM_BYTES];
static uint8_t s_partial_ram[MPS3_CFG_AGENT_PARTIAL_RAM_BYTES];

static staging_slot_t s_stage_clearing = { .data = s_clearing_ram, .cap = sizeof(s_clearing_ram) };
static staging_slot_t s_stage_partial  = { .data = s_partial_ram,  .cap = sizeof(s_partial_ram)  };
static int      s_ordering_seen_clearing;
static uint32_t s_running_static_id;

/* QSPI swap-staging seam (config_agent.h MPS3_CFG_AGENT_QSPI_SINK): registered
 * by coordinator_init(), NULL on builds/tests that don't link overlay_store
 * (large partials then rejected — keeps this module's zero-SPI link
 * independence). */
static const mps3_cfg_agent_qspi_sink_t *s_qspi_sink;          /* partial -> inactive A/B slot */
static const mps3_cfg_agent_qspi_sink_t *s_qspi_clearing_sink; /* clearing -> clearing-STAGE region */
static const mps3_cfg_agent_qspi_sink_t *s_icap_direct_sink;   /* partial -> straight to HWICAP.WF (Path 3) */
static const mps3_cfg_agent_qspi_sink_t *s_active_sink;        /* the sink THIS session staged through (partial or clearing or ICAP-direct) */
static int s_qspi_staging;  /* this receive session's payload streams to a sink (QSPI or ICAP-direct), not RAM */
static int s_stream_is_icap;/* the active streaming sink is the ICAP-direct one (Path 3) — sets slot->in_icap, not in_qspi */
static int s_qspi_active;   /* sink->begin() ran but finish()/abort() hasn't — needs abort on tear */

/* v0.13 COMMIT sink (config_agent.h). s_commit_sink is the registration (read at
 * each push's header); s_commit_act is the sink THIS push began with, so a
 * registration change mid-push cannot strand its abort(). s_commit_active: begin()
 * accepted this push and neither finish() nor abort() has run yet. */
static const mps3_cfg_agent_commit_sink_t *s_commit_sink;
static const mps3_cfg_agent_commit_sink_t *s_commit_act;
static int s_commit_active;

/* THE BACK-PRESSURE CARRY. A commit sink may take fewer bytes than one recv()
 * delivered (config_agent.h write_some). The rest -- at most one recv chunk --
 * waits here, is offered to the sink first on the next poll, and until it has
 * drained NOTHING more is pulled off the connection, so the TCP window (and, with
 * WINDOWED, the grant) stalls the host instead of this module buffering. */
#define CFG_AGENT_CARRY_BYTES 512u
static uint8_t  s_carry[CFG_AGENT_CARRY_BYTES];
static uint32_t s_carry_off;
static uint32_t s_carry_len;

/* ---- one receive session at a time (either transport) --------------------- */
typedef enum {
    RECV_IDLE = 0,
    RECV_HEADER,     /* accumulating the 24 wire header bytes */
    RECV_PAYLOAD,    /* accumulating len_words*4 payload bytes into a slot */
    RECV_AWAIT_EOF,  /* TCP only: payload complete; EOF = well-framed,
                      * any further byte = framing violation (mirrors
                      * fakeshell's expected+1 read) */
    RECV_PENDING_ICAP_BEGIN, /* TCP only: partial header validated + routed to the
                      * ICAP-direct sink, but the sink is not ready yet (FSM not in
                      * SWAP_AWAIT_PARTIAL). The session is HELD here — no payload
                      * pulled, TCP window left closed so the host backpressures —
                      * and tcp_poll re-checks sink->ready() each poll, then arms +
                      * advances to RECV_PAYLOAD. The clearing->partial handoff-race
                      * fix; see the sink struct's ready() note. */
} recv_state_t;

typedef enum { XPORT_NONE = 0, XPORT_TCP, XPORT_TFTP } xport_t;

static recv_state_t s_recv_state;
static xport_t      s_xport;

static uint8_t  s_hdr_raw[MPS3_BITSTREAM_HDR_WIRE_SIZE];
static uint32_t s_hdr_got;
static mps3_bitstream_hdr_t s_hdr;
static staging_slot_t *s_dst;           /* slot the payload accumulates into */
static uint32_t s_payload_expect;       /* bytes */
static uint32_t s_payload_got;
static uint32_t s_crc_run;              /* incremental I13 CRC as bytes land */

/* Clearing->partial handoff-race backpressure (RECV_PENDING_ICAP_BEGIN): the
 * streaming sink whose begin() we deferred, and whether it is the ICAP-direct one
 * (only that sink ever defers). Re-armed from tcp_poll once ready() flips true.
 * s_icap_defer_polls is a free-running diag of ticks spent holding — read over
 * JTAG to prove the fix engaged. See the sink struct's ready() note. */
static const mps3_cfg_agent_qspi_sink_t *s_pending_sink;
static int      s_pending_is_icap;
static uint32_t s_icap_defer_polls;
/* Window-as-grant pacing instrumentation (free-running; read via
 * config_agent_win_diag()). Declared unconditionally so the accessor + diag verb
 * are uniform across builds; only ever advanced by the windowed receive path.
 *   windows_drained  — full CFG_AGENT_ACK_WINDOW_BYTES windows reopened via
 *                      mps3_net_recved() (one per fully-drained window). This is
 *                      the pacing count a HW run reads over JTAG (diag mailbox
 *                      +0x30): for a deadlock-free push it climbs to ~total/window.
 *   grant_send_fails — retained (always 0 now) so the diag mailbox layout / JSON
 *                      schema stay stable; window-as-grant sends no application
 *                      byte, so there is nothing to fail. */
static uint32_t s_win_windows_drained;
static uint32_t s_win_grant_send_fails;
#ifdef MPS3_CFG_AGENT_WINDOWED
static uint32_t s_win_recved_cum;       /* diag only: reopened bytes not yet counted
                                         * as a whole window (s_win_windows_drained) */
static uint32_t s_win_recv_pending;     /* consumed+drained bytes not yet handed to
                                         * mps3_net_recved() — accumulates toward the
                                         * next full window's tcp_recved() reopen */
#endif

/* transport handles */
static mps3_net_listener_t *s_tcp_listener;   /* 6910 */
static mps3_net_conn_t     *s_tcp_conn;
static mps3_net_udp_t      *s_tftp_sock;      /* UDP 69: intake AND the whole transfer */
static mps3_net_addr_t      s_tftp_client;    /* the session's peer (ip, port) while XPORT_TFTP */
static uint16_t             s_tftp_expected_block;

/* TOFU named-file transfer (config_agent.h "the NAMED-FILE sink"). Non-NULL
 * exactly while a claim transfer is open, i.e. begin() succeeded and neither
 * finish() nor abort() has run: session_reset() owes it an abort(). */
static const mps3_cfg_named_sink_t *s_named;

/* The provider hook's WEAK default: no named-file sink for any name, so every
 * claim is refused with TFTP ERROR 2 (config_agent.h). Same mechanism as
 * coordinator.c's weak mps3_shell_static_id(). */
__attribute__((weak)) const mps3_cfg_named_sink_t *mps3_cfg_named_sink(const char *name)
{
    (void)name;
    return 0;
}

/* v0.14 slot-image push (config_agent.h "SLOT-IMAGE pushes"): this receive
 * session streams a stage0 boot image to the engine's slot sink. It rides the
 * streaming-sink machinery (s_active_sink / s_qspi_staging / s_qspi_active, so
 * abort-on-tear is the SAME code path), but it has no staging slot and never
 * touches the clearing->partial ordering flag. */
static int s_slot_push;

/* The lock's WEAK default (config_agent.h "THE LOCK"): refuse nobody -- bare
 * metal's provider declines every slot image anyway. */
__attribute__((weak)) int mps3_cfg_slot_refuse_peer(const mps3_net_addr_t *peer, int known)
{
    (void)peer;
    (void)known;
    return 0;
}

/* The slot provider's WEAK default: no slot sink, so a kind-2 push is refused as
 * an unknown kind always was (config_agent.h). */
__attribute__((weak)) const mps3_cfg_agent_qspi_sink_t *mps3_cfg_slot_sink(const mps3_bitstream_hdr_t *hdr)
{
    (void)hdr;
    return 0;
}

/* Bounded work per poll: at most this many payload bytes over TCP, and at
 * most this many datagrams over UDP, per config_agent_poll() call. */
#define CFG_AGENT_TCP_BYTES_PER_POLL 4096u
#define CFG_AGENT_UDP_DGRAMS_PER_POLL 4

/* When a large payload streams STRAIGHT to a slow sink (ICAP-direct or QSPI)
 * rather than into RAM, cap the bytes pushed to that sink per poll to this much
 * — far below CFG_AGENT_TCP_BYTES_PER_POLL. The shell is single-threaded: an
 * unbounded ICAP/QSPI burst inside one config_agent_poll() keeps the main loop
 * from servicing the LAN9220 RX FIFO (smsc911x_rx_frame) and the lwIP timers,
 * so the MAC RX FIFO overruns and this very transfer starves (HARDWARE
 * EVIDENCE: a MiB partial stalls partway, board goes unreachable). Returning
 * after this many bytes lets the main loop drain RX + pump timers between
 * chunks, capping the worst-case RX-starvation window; the driver's
 * smsc911x_rx_recover() is the belt for any overrun that still slips through.
 * The RAM fast path (small payloads, s_qspi_staging == 0) is unaffected. 1024 B
 * = 256 config words, matching swap_fsm's MPS3_HWICAP_CHUNK_WORDS. */
#define CFG_AGENT_TCP_STREAM_BYTES_PER_POLL 1024u

/* ---- WINDOW-AS-GRANT flow control on 6910 (MPS3_CFG_AGENT_WINDOWED) -----------
 *
 * PRIMARY over-the-wire-reconfig fix (the HW pivot: the stall is in the lwIP/TCP
 * RECEIVE path under a fire-and-hose push — NOT the MAC or the ICAP). The shell
 * paces the host purely via the TCP RECEIVE WINDOW: the 6910 connection runs in
 * manual-window mode (mps3_net_set_manual_window at accept), so mps3_net_recv()
 * consumes bytes off the chain WITHOUT reopening the window; the shell reopens
 * the window by exactly one CFG_AGENT_ACK_WINDOW_BYTES only AFTER a full window's
 * worth of received bytes has DRAINED to the sink (RAM staged / streamed to the
 * ICAP / QSPI). Bounded by TCP flow control — the window is a matched pair with
 * TCP_WND — the host sends exactly the next window and no more, then its send()
 * blocks until the shell reopens. Between windows the receive side goes quiescent.
 *
 * This REPLACES the earlier 1-byte 0x06 application grant. On this board's
 * LAN9220 a lone shell->host 1-byte TCP segment egresses only after a TCP RTO
 * (~1-4 s) while the shell is busy DMAing RX / writing ICAP (create_platform.tcl
 * "tcp_wnd" note), so a per-window grant deadlocked the host's blocking read
 * (deterministically at ~98304 B). Window-update ACKs, by contrast, egress
 * reliably (every data byte arrived on the HW run, which required the shell's
 * window updates to have gone out) — so pacing by the window carries NO extra
 * shell->host application byte and cannot hit that tiny-TX-segment stall.
 *
 * Accounting is on TOTAL received bytes (24-byte header + payload) because every
 * received byte consumes window; the window reopens on TCP-stream boundaries, not
 * payload boundaries. Build-flag gated (default OFF): with the flag off none of
 * this compiles and the fire-and-hose path is byte-identical. When ON, the host
 * MUST use the windowed pusher (pyverify tcp_send_windowed) — a matched pair.
 *
 * CFG_AGENT_ACK_WINDOW_BYTES MUST equal the BSP TCP_WND (16384, see
 * create_platform.tcl / firmware/platform Makefile) — a matched pair: a full app
 * window fits the TCP window exactly, so there is no mid-window reopen. */
#ifndef CFG_AGENT_ACK_WINDOW_BYTES
#define CFG_AGENT_ACK_WINDOW_BYTES 16384u  /* == BSP TCP_WND (matched pair) */
#endif

/* RFC1350 */
#define TFTP_BLOCK_BYTES 512u
enum { TFTP_OP_RRQ = 1, TFTP_OP_WRQ = 2, TFTP_OP_DATA = 3, TFTP_OP_ACK = 4, TFTP_OP_ERROR = 5 };

void config_agent_set_running_static_id(uint32_t static_id)
{
    s_running_static_id = static_id;
}

void config_agent_set_qspi_sink(const mps3_cfg_agent_qspi_sink_t *sink)
{
    s_qspi_sink = sink;
}

void config_agent_set_qspi_clearing_sink(const mps3_cfg_agent_qspi_sink_t *sink)
{
    s_qspi_clearing_sink = sink;
}

void config_agent_set_icap_direct_sink(const mps3_cfg_agent_qspi_sink_t *sink)
{
    s_icap_direct_sink = sink;
}

void config_agent_set_commit_sink(const mps3_cfg_agent_commit_sink_t *sink)
{
    s_commit_sink = sink;
}

/* The commit push died (why = -1 torn, or a CFG_AGENT_ERR_*): tell its sink, at
 * most once. */
static void commit_push_abort(int why)
{
    if (s_commit_active && s_commit_act && s_commit_act->abort) {
        s_commit_act->abort(why);
    }
    s_commit_active = 0;
    s_commit_act = 0;
}

static void session_reset(void)
{
    if (s_qspi_active && s_active_sink && s_active_sink->abort) {
        /* A QSPI staging session (partial OR clearing) was in flight and did
         * NOT finish cleanly (torn transfer / rejection) — re-arm block
         * protection and drop the scratch via whichever sink began it.
         * finish_payload() clears s_qspi_active on the clean path. */
        s_active_sink->abort();
    }
    s_qspi_active = 0;
    s_qspi_staging = 0;
    s_stream_is_icap = 0;
    s_active_sink = 0;
    /* A commit push torn before finish(): its sink is owed an abort. */
    commit_push_abort(-1);
    s_carry_off = 0;
    s_carry_len = 0;
    s_slot_push = 0;
    /* A DEFERRED partial (RECV_PENDING_ICAP_BEGIN) never had its sink begin()
     * called, so nothing is owed an abort — just forget the pending routing. */
    s_pending_sink = 0;
    s_pending_is_icap = 0;
    s_recv_state = RECV_IDLE;
    s_xport = XPORT_NONE;
    s_hdr_got = 0;
    memset(&s_hdr, 0, sizeof(s_hdr));
    s_dst = 0;
    s_payload_expect = 0;
    s_payload_got = 0;
    s_crc_run = MPS3_CRC32_INIT;
#ifdef MPS3_CFG_AGENT_WINDOWED
    s_win_recved_cum = 0;
    s_win_recv_pending = 0;   /* window accounting is per-session; the free-running
                               * s_win_windows_drained diag counter is NOT reset */
#endif
    if (s_tcp_conn) {
        mps3_net_close(s_tcp_conn);
        s_tcp_conn = 0;
    }
    if (s_named) {
        /* A claim torn before finish(): the provider discards what it has. */
        if (s_named->abort) {
            s_named->abort();
        }
        s_named = 0;
    }
}

void config_agent_abort_session(void)
{
    session_reset();   /* closes s_tcp_conn, ends a TFTP session, drops partial staging */
}

void config_agent_init(void)
{
    s_stage_clearing.full = 0;
    s_stage_clearing.in_qspi = 0;
    s_stage_clearing.in_icap = 0;
    s_stage_partial.full = 0;
    s_stage_partial.in_qspi = 0;
    s_stage_partial.in_icap = 0;
    s_ordering_seen_clearing = 0;
    s_icap_defer_polls = 0; /* free-running diag; only ever reset here */
    s_tcp_conn = 0;
    session_reset(); /* leaves s_qspi_sink registration intact (persistent) */
    /* ...but NOT the commit sink: a commit is one parked request, not a seam. */
    s_commit_sink = 0;

    s_tcp_listener = mps3_net_listen(MPS3_PORT_RAW_PUSH);
    s_tftp_sock    = mps3_net_udp_open(MPS3_PORT_TFTP);
}

/* ---- pure validation (unchanged semantics; see config_agent.h) ------------ */

config_agent_status_t config_agent_validate_header_ex(const mps3_bitstream_hdr_t *hdr,
                                                        uint32_t running_static_id,
                                                        int ordering_seen_clearing)
{
    /* magic is already checked by mps3_bitstream_hdr_unpack() before a
     * caller even gets a filled-in mps3_bitstream_hdr_t; re-checking it
     * here too keeps this function safe to call standalone. */
    if (memcmp(hdr->magic, MPS3_BITSTREAM_MAGIC, 4) != 0) {
        return CFG_AGENT_ERR_MAGIC;
    }
    if (hdr->ver != MPS3_BITSTREAM_VER) {
        return CFG_AGENT_ERR_VERSION;
    }
    if (hdr->static_id != running_static_id) {
        /* overlay-manifest.md: "A shell rebuild changes static_id and
         * invalidates every stored partial." Firmware is the last line of
         * defense even if the host-side freshness gate (A4) catches this
         * first. */
        return CFG_AGENT_ERR_STATIC_ID;
    }
    if (hdr->kind != MPS3_BIN_KIND_CLEARING && hdr->kind != MPS3_BIN_KIND_PARTIAL) {
        return CFG_AGENT_ERR_KIND;
    }
    if (hdr->len_words > MPS3_CFG_AGENT_MAX_PAYLOAD_WORDS) {
        return CFG_AGENT_ERR_SIZE; /* absolute cap (also bounds the *4 below) */
    }
    /* Neither a clearing NOR a partial is size-rejected here beyond the
     * absolute MAX cap above. A payload larger than the RAM staging buffer is
     * routed to a QSPI region in begin_payload() — the partial to the inactive
     * A/B slot, the clearing to the dedicated clearing-STAGE region — which
     * rejects it there if no QSPI sink is registered for that kind. This is
     * the decoupling that closes the gate: the moveable clearing/partial size
     * is no longer capped at MPS3_CFG_AGENT_STAGING_BYTES (nanosoc's
     * 117,684-byte clearing used to be rejected right here). validate_header
     * stays a PURE function — it cannot see whether a sink is registered, so
     * the "too big for RAM AND no QSPI backend" rejection lives in
     * begin_payload(), exactly as it already did for the partial. */
    /* I2 ordering: a partial push is only accepted once the pair's
     * clearing has completed (the flag is module state set/cleared at
     * payload completion — see finish_payload()). */
    if (hdr->kind == MPS3_BIN_KIND_PARTIAL && !ordering_seen_clearing) {
        return CFG_AGENT_ERR_ORDER;
    }
    return CFG_AGENT_OK;
}

config_agent_status_t config_agent_validate_header(const mps3_bitstream_hdr_t *hdr,
                                                     uint32_t running_static_id)
{
    return config_agent_validate_header_ex(hdr, running_static_id, s_ordering_seen_clearing);
}

config_agent_status_t config_agent_check_payload_crc(const mps3_bitstream_hdr_t *hdr,
                                                       const void *payload,
                                                       uint32_t payload_len)
{
    uint32_t expected_bytes = mps3_bitstream_payload_bytes(hdr);
    if (payload_len != expected_bytes) {
        return CFG_AGENT_ERR_SIZE;
    }
    if (mps3_crc32(payload, payload_len) != hdr->crc32) {
        return CFG_AGENT_ERR_CRC;
    }
    return CFG_AGENT_OK;
}

/* ---- shared receive machinery (both transports feed through here) --------- */

/* Run a streaming sink's begin() and latch the staging flags. Split out of
 * begin_payload() so the ICAP-direct DEFER path (RECV_PENDING_ICAP_BEGIN) can
 * re-invoke exactly this — the begin() call that was held back — once the sink
 * reports ready, without re-validating the header. Returns CFG_AGENT_OK, or
 * CFG_AGENT_ERR_SIZE if begin() fails (region prep / SPI / — for ICAP-direct —
 * the swap not being armed even now, which is a real fault, not a race). */
static config_agent_status_t arm_streaming_sink(const mps3_cfg_agent_qspi_sink_t *sink,
                                                int is_icap)
{
    if (sink->begin(s_payload_expect) != 0) {
        return CFG_AGENT_ERR_SIZE;
    }
    s_active_sink = sink;
    s_qspi_staging = 1;
    s_stream_is_icap = is_icap;
    s_qspi_active = 1; /* an abort is now owed until finish_payload() runs */
    return CFG_AGENT_OK;
}

/* v0.14: a slot-image header (config_agent.h "SLOT-IMAGE pushes"). Checked
 * here, never by config_agent_validate_header(): that function is the BITSTREAM
 * validator and stays exactly as it was (kind 2 is still ERR_KIND there). */
static config_agent_status_t begin_slot_payload(void)
{
    if (s_hdr.ver != MPS3_BITSTREAM_VER) {
        return CFG_AGENT_ERR_VERSION;
    }
    if (s_hdr.len_words == 0u ||
        s_hdr.len_words > MPS3_SLOT_IMAGE_MAX_BYTES / 4u) {
        return CFG_AGENT_ERR_SIZE;
    }
    /* The lock, before the provider: who is pushing? (config_agent.h) */
    {
        mps3_net_addr_t peer;
        int known = 0;
        memset(&peer, 0, sizeof(peer));
        if (s_xport == XPORT_TCP) {
            known = (mps3_net_conn_peer(s_tcp_conn, &peer) == 0);
        } else if (s_xport == XPORT_TFTP) {
            peer = s_tftp_client;
            known = 1;
        }
        if (mps3_cfg_slot_refuse_peer(&peer, known) != 0) {
            return CFG_AGENT_ERR_ACCESS;
        }
    }
    const mps3_cfg_agent_qspi_sink_t *sink = mps3_cfg_slot_sink(&s_hdr);
    if (!sink || !sink->begin || !sink->write || !sink->finish) {
        return CFG_AGENT_ERR_KIND;   /* no slot sink on this engine, or it refused */
    }
    if (s_hdr.static_id != s_running_static_id) {
        return CFG_AGENT_ERR_STATIC_ID;   /* the belt: begin() never ran, nothing is owed */
    }
    s_payload_expect = mps3_bitstream_payload_bytes(&s_hdr);
    s_dst = 0;                       /* no staging slot: the bytes live on the card */
    if (arm_streaming_sink(sink, 0) != CFG_AGENT_OK) {
        return CFG_AGENT_ERR_SIZE;
    }
    s_slot_push = 1;
    s_payload_got = 0;
    s_crc_run = MPS3_CRC32_INIT;
    s_recv_state = RECV_PAYLOAD;
    return CFG_AGENT_OK;
}

/* Header complete: unpack + validate, pick the destination slot. */
static config_agent_status_t begin_payload(void)
{
    /* A v0.13 commit is armed: this push belongs to it, whatever its size, and
     * the sink hears about a rejected header too (it answers the parked commit). */
    const mps3_cfg_agent_commit_sink_t *commit = s_commit_sink;
    if (mps3_bitstream_hdr_unpack(s_hdr_raw, sizeof(s_hdr_raw), &s_hdr) != 0) {
        if (commit && commit->abort) {
            commit->abort(CFG_AGENT_ERR_MAGIC);
        }
        return CFG_AGENT_ERR_MAGIC;
    }
    if (s_hdr.kind == MPS3_BIN_KIND_SLOT_IMAGE) {
        if (commit) {
            /* A v0.13 commit is parked waiting for ITS pair: a v0.14 slot image
             * is not part of it. Refuse the push and leave the commit armed
             * (untouched: its sink is not told), so the pair can still follow. */
            return CFG_AGENT_ERR_ORDER;
        }
        return begin_slot_payload();
    }
    config_agent_status_t st = config_agent_validate_header(&s_hdr, s_running_static_id);
    if (st == CFG_AGENT_OK && commit && s_xport != XPORT_TCP) {
        st = CFG_AGENT_ERR_ORDER;   /* commit is TCP only: TFTP has no window to hold */
    }
    if (st != CFG_AGENT_OK) {
        if (commit && commit->abort) {
            commit->abort((int)st);
        }
        return st;
    }
    s_payload_expect = mps3_bitstream_payload_bytes(&s_hdr);
    if (commit) {
        /* The commit's own routing: no staging slot is touched (a swap's staged
         * pair must not be disturbed, and nothing here may reach the ICAP). */
        if (!commit->begin || !commit->write_some || !commit->finish ||
            commit->begin(&s_hdr) != 0) {
            return CFG_AGENT_ERR_SIZE;   /* refused: the sink recorded why */
        }
        s_commit_act = commit;
        s_commit_active = 1;
        s_dst = 0;
        s_payload_got = 0;
        s_crc_run = MPS3_CRC32_INIT;
        s_recv_state = RECV_PAYLOAD;
        return CFG_AGENT_OK;
    }
    s_dst = (s_hdr.kind == MPS3_BIN_KIND_CLEARING) ? &s_stage_clearing : &s_stage_partial;
    /* Receiving into a slot invalidates whatever it held — fail-closed: a
     * torn re-push must not leave a stale "validated" payload behind. */
    s_dst->full = 0;
    s_dst->in_qspi = 0;
    s_dst->in_icap = 0;
    s_qspi_staging = 0;
    s_stream_is_icap = 0;

    /* A payload too large for the RAM staging buffer streams into a sink
     * instead of RAM — this is what lets a MiB-scale partial OR a full-size
     * RM's clearing (nanosoc: 117,684 B) move over the net without a matching
     * .bss buffer. Three sink targets, by kind and by which backend is wired:
     *   - PARTIAL, ICAP-direct sink registered (Path 3, the default target
     *     build): stream STRAIGHT to HWICAP.WF as bytes arrive — no RAM, no
     *     QSPI, no size limit. Preferred over QSPI because QSPI need not be
     *     operational for it, and it removes the limit for every RM.
     *   - PARTIAL, only QSPI wired: the inactive A/B slot scratch (s_qspi_sink).
     *   - CLEARING: the dedicated clearing-STAGE region (s_qspi_clearing_sink).
     * For QSPI, the flash-resident copy is CRC-verified at finish_payload()
     * BEFORE the swap FSM streams/promotes it (reject-before-ICAP). Stream-
     * direct cannot buffer-then-verify, so it relaxes to detect-after-ICAP +
     * park-safe: the running transport CRC (s_crc_run, checked in
     * finish_payload) still rejects a corrupt stream (the RP stays parked,
     * never released), and ICAP's own embedded CRC + the post-load RM_ID
     * verify are the belt-and-suspenders. A payload that fits s_dst->cap stays
     * on the RAM fast path. */
    if (s_payload_expect > s_dst->cap) {
        const mps3_cfg_agent_qspi_sink_t *sink;
        int is_icap = 0;
        if (s_hdr.kind == MPS3_BIN_KIND_PARTIAL) {
            if (s_icap_direct_sink) {
                sink = s_icap_direct_sink; /* Path 3: straight to HWICAP */
                is_icap = 1;
            } else {
                sink = s_qspi_sink;        /* fallback: QSPI A/B-slot scratch */
            }
        } else {
            sink = s_qspi_clearing_sink;   /* clearing -> clearing-STAGE region */
        }
        if (!sink || !sink->begin || !sink->write || !sink->finish) {
            return CFG_AGENT_ERR_SIZE; /* no staging backend for this kind */
        }
        /* Clearing->partial handoff race: the ICAP-direct sink's begin() rejects
         * unless the FSM is already in SWAP_AWAIT_PARTIAL, and for a LARGE outgoing
         * clearing (DAP RMs) the FSM is still in SWAP_STREAM_CLEARING when the
         * pusher opens the partial connection. Rather than tear the session (the
         * old hard reject → the partial was dropped → the swap timed out in
         * AWAIT_PARTIAL — the silicon "swap-away from a DAP RM fails" bug), PARK
         * here: hold the header (no payload pulled — tcp_poll caps the recv to the
         * header while RECV_HEADER, so nothing is stranded), leave the TCP window
         * closed so the host backpressures, and let tcp_poll retry begin() once
         * ready() flips true. TCP only — TFTP has no window to hold, so it falls
         * through to the immediate begin() (hard-fails as before). */
        if (is_icap && s_xport == XPORT_TCP && sink->ready && sink->ready() == 0) {
            s_pending_sink = sink;
            s_pending_is_icap = is_icap;
            s_recv_state = RECV_PENDING_ICAP_BEGIN;
            return CFG_AGENT_OK; /* held; armed later from tcp_poll, payload NOT yet consumed */
        }
        st = arm_streaming_sink(sink, is_icap);
        if (st != CFG_AGENT_OK) {
            /* region prep failed (too large / SPI), OR — for the ICAP-direct
             * sink — the swap was NOT armed first (RP not yet decoupled), so
             * begin() fails closed rather than writing frames to a live RP. */
            return st;
        }
    }

    s_payload_got = 0;
    s_crc_run = MPS3_CRC32_INIT;
    s_recv_state = RECV_PAYLOAD;
    return CFG_AGENT_OK;
}

/* Feed `n` transport bytes into the header/payload accumulator. Advances
 * s_recv_state; returns CFG_AGENT_OK or the rejection. *used (may be NULL)
 * receives the bytes consumed: always n, EXCEPT when a commit sink's write_some()
 * took less (back-pressure) -- the caller then keeps the rest. */
static config_agent_status_t session_feed(const uint8_t *p, uint32_t n, uint32_t *used)
{
    const uint32_t n0 = n;
    if (used) {
        *used = n;
    }
    while (n > 0) {
        if (s_recv_state == RECV_HEADER) {
            uint32_t want = MPS3_BITSTREAM_HDR_WIRE_SIZE - s_hdr_got;
            uint32_t take = (n < want) ? n : want;
            memcpy(&s_hdr_raw[s_hdr_got], p, take);
            s_hdr_got += take;
            p += take;
            n -= take;
            if (s_hdr_got == MPS3_BITSTREAM_HDR_WIRE_SIZE) {
                config_agent_status_t st = begin_payload();
                if (st != CFG_AGENT_OK) {
                    return st; /* rejected BEFORE any payload/ICAP handling */
                }
                if (s_payload_expect == 0) {
                    s_recv_state = RECV_AWAIT_EOF; /* nothing to accumulate */
                }
            }
        } else if (s_recv_state == RECV_PAYLOAD) {
            uint32_t want = s_payload_expect - s_payload_got;
            uint32_t take = (n < want) ? n : want;
            if (s_commit_active) {
                /* v0.13 commit: the store takes what fits (config_agent.h). */
                uint32_t took = 0;
                if (s_commit_act->write_some(p, take, &took) != 0 || took > take) {
                    return CFG_AGENT_ERR_SIZE;   /* the store failed: torn */
                }
                s_crc_run = mps3_crc32_update(s_crc_run, p, took);
                s_payload_got += took;
                p += took;
                n -= took;
                if (s_payload_got == s_payload_expect) {
                    s_recv_state = RECV_AWAIT_EOF;
                }
                if (took < take) {
                    if (used) {
                        *used = n0 - n;
                    }
                    return CFG_AGENT_OK;       /* back-pressure: the caller keeps the rest */
                }
                continue;
            }
            if (s_qspi_staging) {
                /* Stream straight into the QSPI region (the active sink
                 * page-programs it); no MiB-scale RAM buffer involved. */
                if (s_active_sink->write(p, take) != 0) {
                    return CFG_AGENT_ERR_SIZE; /* SPI program failure -> reject */
                }
            } else {
                memcpy(&s_dst->data[s_payload_got], p, take);
            }
            s_crc_run = mps3_crc32_update(s_crc_run, p, take);
            s_payload_got += take;
            p += take;
            n -= take;
            if (s_payload_got == s_payload_expect) {
                s_recv_state = RECV_AWAIT_EOF;
            }
        } else {
            /* Bytes beyond the declared length — framing violation
             * (mirrors fakeshell's expected+1 over-read -> ERR_SIZE). */
            return CFG_AGENT_ERR_SIZE;
        }
    }
    return CFG_AGENT_OK;
}

/* Payload byte-complete (transport says the transfer is over): final CRC +
 * staging + ordering-flag bookkeeping (the C mirror of fakeshell.py
 * ConfigAgentModel.finish_payload). */
static config_agent_status_t finish_payload(void)
{
    if (s_hdr_got < MPS3_BITSTREAM_HDR_WIRE_SIZE) {
        return CFG_AGENT_ERR_MAGIC; /* too short to even carry a header */
    }
    if (s_payload_got < s_payload_expect) {
        return CFG_AGENT_ERR_TRUNCATED;
    }
    if (s_commit_active) {
        /* v0.13 commit: the transport CRC first, then the sink's own acceptance.
         * NOTHING is staged -- a commit's bytes live on the card only. */
        const mps3_cfg_agent_commit_sink_t *c = s_commit_act;
        if (s_crc_run != s_hdr.crc32) {
            commit_push_abort(CFG_AGENT_ERR_CRC);
            return CFG_AGENT_ERR_CRC;
        }
        s_commit_active = 0;       /* finish() consumes it: no abort() is owed */
        s_commit_act = 0;
        if (c->finish(&s_hdr) != 0) {
            return CFG_AGENT_ERR_CRC;
        }
        if (s_hdr.kind == MPS3_BIN_KIND_CLEARING) {
            s_stage_partial.full = 0;
            s_ordering_seen_clearing = 1;
        } else {
            s_ordering_seen_clearing = 0;
        }
        return CFG_AGENT_OK;
    }
    if (s_crc_run != s_hdr.crc32) {
        return CFG_AGENT_ERR_CRC; /* transport-side integrity (the received bytes) */
    }
    if (s_qspi_staging) {
        /* QSPI sink: independently CRC-verify the flash-RESIDENT copy (catches
         * a torn flash write / protection fault the transport CRC can't see)
         * BEFORE the swap FSM ever streams it to HWICAP, and re-arm block
         * protection. ICAP-direct sink: the payload is already in the ICAP —
         * finish() just drains the final DONE/EOS (the transport-CRC gate
         * above, s_crc_run vs the header, already rejected a corrupt stream
         * and left the RP parked). Either way, <0 rejects and s_qspi_active
         * stays set so the caller's abort path re-locks/cleans up. */
        if (s_active_sink->finish(s_hdr.crc32) != 0) {
            return CFG_AGENT_ERR_CRC;
        }
        s_qspi_active = 0; /* finished cleanly */
    }
    if (s_slot_push) {
        return CFG_AGENT_OK;   /* v0.14: on the card now; nothing is staged, no pair flag */
    }
    s_dst->hdr = s_hdr;
    s_dst->in_qspi = s_qspi_staging && !s_stream_is_icap;
    s_dst->in_icap = s_stream_is_icap;
    s_dst->full = 1;
    if (s_hdr.kind == MPS3_BIN_KIND_CLEARING) {
        /* A clearing starts a (new) pair; any stale partial is dropped. */
        s_stage_partial.full = 0;
        s_ordering_seen_clearing = 1;
    } else {
        /* Pair complete: the NEXT pair must again start with a clearing. */
        s_ordering_seen_clearing = 0;
    }
    return CFG_AGENT_OK;
}

/* ---- raw-TCP transport (6910) ---------------------------------------------- */

static void tcp_abort(void)
{
    /* Reject/abort: close the socket (the client's EOF-side signal) and
     * drop all session state. Any half-written slot was already marked
     * not-full by begin_payload(). */
    session_reset();
}

#ifdef MPS3_CFG_AGENT_WINDOWED
/* Window-as-grant pacing (see CFG_AGENT_ACK_WINDOW_BYTES): reopen the TCP receive
 * window by one full window each time a window's worth of received bytes has
 * DRAINED to the sink. Call after every session_feed() that returned OK — by
 * which point those bytes are safely landed:
 *
 * LOAD-BEARING CORRECTNESS POINT — pace on SINK-DRAIN, not on bare recv:
 * s_win_recv_pending is only incremented by the caller AFTER session_feed()
 * returns OK, and for the streaming sinks session_feed advances only PAST the
 * sink->write() (ICAP-direct HWICAP.WF write / QSPI page-program: write() must
 * return 0 before the byte counts as consumed). So the window reopen structurally
 * trails the sink write for those bytes; reopening on bare recv would reintroduce
 * the consumer-rate race this mechanism exists to kill.
 *
 * EVERY drained byte reopens, not only whole windows. Reopening whole windows
 * alone strands a sub-window residual: TCP segmentation — and the 24-byte header
 * consuming window ahead of the payload — means the drained total is not a
 * multiple of the window mid-stream, and lwIP will not advertise a small window
 * grow (silly-window avoidance). The host, whose window is already exhausted,
 * then blocks forever waiting for an update that never comes while we wait for
 * bytes it cannot send. Observed on HW: hard deadlock at 176,684 B == 10.78
 * windows, host stuck in sendall(), shell main-loop idle.
 *
 * Pacing is unaffected: the window still only reopens for bytes that have
 * REACHED THE SINK (see above), so the host cannot outrun the ICAP — that is the
 * whole invariant. Window granularity was only ever an ack-count optimisation.
 * s_win_windows_drained still counts WHOLE windows, for the JTAG diag. */
static void win_reopen_drained_windows(void)
{
    if (!s_tcp_conn || s_win_recv_pending == 0) {
        return;
    }
    uint32_t n = s_win_recv_pending;
    s_win_recv_pending = 0;
    mps3_net_recved(s_tcp_conn, n);   /* reopen for EVERY drained byte */

    /* Diag only: still report progress in whole-window units. */
    s_win_recved_cum += n;
    while (s_win_recved_cum >= CFG_AGENT_ACK_WINDOW_BYTES) {
        s_win_recved_cum -= CFG_AGENT_ACK_WINDOW_BYTES;
        s_win_windows_drained++;
    }
}
#endif /* MPS3_CFG_AGENT_WINDOWED */

static void tcp_poll(void)
{
    /* New connections: adopt one when idle, refuse extras (one transfer at
     * a time — matches the single receive session + swap_fsm's
     * single-swap-in-flight). */
    mps3_net_conn_t *incoming = mps3_net_accept(s_tcp_listener);
    if (incoming) {
        if (s_recv_state == RECV_IDLE && s_xport == XPORT_NONE) {
            s_tcp_conn = incoming;
            s_xport = XPORT_TCP;
            s_recv_state = RECV_HEADER;
            s_hdr_got = 0;
#ifdef MPS3_CFG_AGENT_WINDOWED
            /* Window-as-grant: consume WITHOUT auto-reopening the window; this
             * poll reopens it one window at a time as bytes drain to the sink. */
            mps3_net_set_manual_window(s_tcp_conn, 1);
            s_win_recv_pending = 0;
#endif
        } else {
            mps3_net_close(incoming); /* busy: refuse */
        }
    }
    if (s_xport != XPORT_TCP || !s_tcp_conn) {
        return;
    }

#ifdef MPS3_CFG_AGENT_WINDOWED
    /* Re-drive tcp_output on the 6910 pcb every poll so any window-update ACK that
     * lwIP deferred still egresses (window-as-grant sends NO application byte — the
     * only shell->host output is the receive-window update). No-op when nothing is
     * pending. */
    mps3_net_flush(s_tcp_conn);
#endif

    /* Held partial (clearing->partial handoff race): the ICAP-direct sink was not
     * ready (FSM not yet in SWAP_AWAIT_PARTIAL) when the partial header landed, so
     * begin_payload() PARKED the session here instead of tearing it. Retry arming
     * each poll. While not ready we return WITHOUT pulling any payload — the TCP
     * window stays closed (only the header was consumed), so the host pusher blocks
     * in send() and nothing streams into a not-yet-clear ICAP. Once ready, arm and
     * fall through to recv the payload the host has buffered against the window. */
    if (s_recv_state == RECV_PENDING_ICAP_BEGIN) {
        if (s_pending_sink && s_pending_sink->ready && s_pending_sink->ready() == 0) {
            s_icap_defer_polls++;   /* diag: ticks held (proves the fix engaged over JTAG) */
            return;                 /* still not ready — hold (backpressure via closed window) */
        }
        if (arm_streaming_sink(s_pending_sink, s_pending_is_icap) != CFG_AGENT_OK) {
            /* begin() failed for real now (not a race) — reject, RP left parked. */
            s_pending_sink = 0;
            tcp_abort();
            return;
        }
        s_pending_sink = 0;
        s_payload_got = 0;
        s_crc_run = MPS3_CRC32_INIT;
        s_recv_state = RECV_PAYLOAD; /* a partial always has payload (len_words*4 > cap) */
    }

    uint32_t budget = CFG_AGENT_TCP_BYTES_PER_POLL;
    uint8_t chunk[512];

    /* A commit's back-pressure carry goes FIRST. While any of it remains, nothing
     * more is pulled off the connection (config_agent.h write_some). */
    if (s_carry_len > 0) {
        uint32_t took = 0;
        if (session_feed(&s_carry[s_carry_off], s_carry_len, &took) != CFG_AGENT_OK) {
            tcp_abort();
            return;
        }
        s_carry_off += took;
        s_carry_len -= took;
#ifdef MPS3_CFG_AGENT_WINDOWED
        s_win_recv_pending += took;   /* these bytes have now DRAINED to the sink */
        win_reopen_drained_windows();
#endif
        if (s_carry_len > 0) {
            return;                   /* the store is still busy: hold */
        }
        s_carry_off = 0;
        if (took > budget) {
            took = budget;
        }
        budget -= took;
    }
    while (budget > 0) {
        uint32_t cap = (budget < sizeof(chunk)) ? budget : (uint32_t)sizeof(chunk);
        /* While accumulating the header, read ONLY the header bytes: if
         * begin_payload() then DEFERS a partial (RECV_PENDING_ICAP_BEGIN), no
         * payload bytes have been pulled off the connection, so none are stranded
         * in this stack buffer — the host's already-sent payload waits in lwIP
         * against the (still-closed) window until we arm. Costs one extra recv per
         * transfer (the 24-byte header alone); negligible, and only on TCP. */
        if (s_recv_state == RECV_HEADER) {
            uint32_t hdr_left = MPS3_BITSTREAM_HDR_WIRE_SIZE - s_hdr_got;
            if (cap > hdr_left) {
                cap = hdr_left;
            }
        }
        int n = mps3_net_recv(s_tcp_conn, chunk, cap);
        if (n == 0) {
            break; /* nothing more right now — fall through to grant service */
        }
        if (n == MPS3_NET_CLOSED) {
            /* EOF: the only well-framed end for this transport. Under window-as-grant
             * the host half-closes after its last window, so finish runs here just
             * like the fire-hose path — no early finish, no application ack to trail. */
            if (s_recv_state == RECV_AWAIT_EOF) {
                (void)finish_payload(); /* stage on OK; on CRC fail just drop */
            }
            /* else: torn mid-header/mid-payload (ERR_TRUNCATED shape) */
            tcp_abort();
            return;
        }
        if (n < 0) {
            tcp_abort();
            return;
        }
        uint32_t took = 0;
        if (session_feed(chunk, (uint32_t)n, &took) != CFG_AGENT_OK) {
            /* Bad header / oversize / bytes beyond the frame: close
             * without staging — the client sees the early EOF. */
            tcp_abort();
            return;
        }
        budget -= (uint32_t)n;
#ifdef MPS3_CFG_AGENT_WINDOWED
        /* These bytes (header and/or payload) are now DRAINED (session_feed OK):
         * account them toward the receive window and reopen it one full window at a
         * time as it fills — the entire host pacing, no application ack. Bytes a
         * commit sink did not take yet are NOT drained: they are counted when the
         * carry empties. */
        s_win_recv_pending += took;
        win_reopen_drained_windows();
#endif
        if (took < (uint32_t)n) {
            /* Commit back-pressure: keep the rest, pull nothing more this poll. */
            memcpy(s_carry, &chunk[took], (uint32_t)n - took);
            s_carry_off = 0;
            s_carry_len = (uint32_t)n - took;
            break;
        }
        /* begin_payload() just DEFERRED a partial (ICAP-direct sink not ready): the
         * header is consumed but no payload must be pulled until we arm — leave the
         * host's payload waiting in lwIP against the closed window. Stop this poll;
         * the retry block at the top of tcp_poll re-checks readiness next poll. */
        if (s_recv_state == RECV_PENDING_ICAP_BEGIN) {
            break;
        }
        /* Streaming straight to a slow sink (ICAP-direct / QSPI): yield once this
         * poll has pushed CFG_AGENT_TCP_STREAM_BYTES_PER_POLL so the main loop
         * services the LAN9220 RX FIFO + lwIP timers before the next chunk (see
         * that constant). s_qspi_staging flips on inside begin_payload() during
         * the header chunk, so this bounds the very first payload burst too.
         * A v0.14 slot image is exempt: its sink is the Linux page cache (the
         * card write-back happens in the kernel, off this loop), so it keeps the
         * ordinary per-poll byte budget -- a 22 MB image at 1 KiB a pass would
         * cost 4x the passes for no protection. */
        if (s_qspi_staging && !s_slot_push &&
            (CFG_AGENT_TCP_BYTES_PER_POLL - budget) >= CFG_AGENT_TCP_STREAM_BYTES_PER_POLL) {
            break;
        }
    }
}

/* ---- TFTP transport (UDP 69, RFC1350 WRQ/octet, SINGLE-PORT) ----------------
 *
 * Every datagram this module sends leaves FROM :69 -- ACK 0, every DATA ACK,
 * every ERROR -- never from a fresh transfer ID. A host behind a stateful
 * firewall accepts only replies from the (ip, port) it sent to; the hub (a RHEL
 * host) drops fresh-TID replies (B1 2026-09-24: stage0's rescue TFTP, fixed the
 * same way in 33d0cbd). One session at a time, so the peer (ip, port) alone
 * identifies it: WRQ/RRQ from anyone are requests, and DATA/ERROR count only
 * from the session's peer. There is one socket, so "from :69" holds by
 * construction. Clients that follow RFC1350 (pyverify's tftp_put, stage0_push.py)
 * lock onto the address the ACK 0 came from -- here, :69. */

static void tftp_send_error(const mps3_net_addr_t *to, uint16_t code, const char *msg)
{
    uint8_t pkt[128];
    uint32_t mlen = (uint32_t)strlen(msg);
    if (mlen > sizeof(pkt) - 5) {
        mlen = sizeof(pkt) - 5;
    }
    pkt[0] = 0; pkt[1] = TFTP_OP_ERROR;
    pkt[2] = (uint8_t)(code >> 8); pkt[3] = (uint8_t)code;
    memcpy(&pkt[4], msg, mlen);
    pkt[4 + mlen] = 0;
    (void)mps3_net_udp_sendto(s_tftp_sock, pkt, 5 + mlen, to);
}

static void tftp_send_ack(uint16_t block)
{
    uint8_t pkt[4] = { 0, TFTP_OP_ACK, (uint8_t)(block >> 8), (uint8_t)block };
    (void)mps3_net_udp_sendto(s_tftp_sock, pkt, sizeof(pkt), &s_tftp_client);
}

/* Is `a` this TFTP session's peer? */
static int tftp_is_peer(const mps3_net_addr_t *a)
{
    return s_xport == XPORT_TFTP && a->ip == s_tftp_client.ip && a->port == s_tftp_client.port;
}

static void tftp_abort(void)
{
    session_reset();
}

/* A WRQ arrived on :69. */
static void tftp_handle_wrq(const uint8_t *pkt, int len, const mps3_net_addr_t *from)
{
    if (s_recv_state != RECV_IDLE || s_xport != XPORT_NONE) {
        if (tftp_is_peer(from) && s_tftp_expected_block == 1) {
            tftp_send_ack(0);   /* our ACK 0 was lost: the client repeated its WRQ */
            return;
        }
        tftp_send_error(from, 0, "transfer already in progress");
        return;
    }
    /* filename\0mode\0 — filename is advisory (the header identifies the
     * payload); mode must be octet (netascii would corrupt a .bin). */
    const uint8_t *end = pkt + len;
    const uint8_t *fn = pkt + 2;
    const uint8_t *p = fn;
    while (p < end && *p) p++;
    if (p >= end) {
        tftp_send_error(from, 4, "malformed WRQ");
        return;
    }
    const uint8_t *mode = p + 1;
    p = mode;
    while (p < end && *p) p++;
    if (p >= end) {
        tftp_send_error(from, 4, "malformed WRQ");
        return;
    }
    /* case-insensitive "octet" */
    static const char octet[] = "octet";
    int ok = 1;
    for (uint32_t i = 0; i < sizeof(octet); i++) {
        char c = (char)mode[i];
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
        if (c != octet[i]) { ok = 0; break; }
    }
    if (!ok) {
        tftp_send_error(from, 0, "only octet mode is supported");
        return;
    }

    /* TOFU first-key claim (config_agent.h): EXACTLY this one filename leaves the
     * bitstream path for the platform's named-file sink. Every other filename
     * falls through to the code below unchanged. The provider decides; with no
     * provider (the weak default) or a refusing begin() it is ERROR 2. */
    const mps3_cfg_named_sink_t *named = 0;
    if (strcmp((const char *)fn, MPS3_CFG_TOFU_FILENAME) == 0) {
        named = mps3_cfg_named_sink((const char *)fn);
        if (!named || !named->begin || !named->write || !named->finish ||
            named->begin((const char *)fn) != 0) {
            tftp_send_error(from, 2, "access violation");
            return;
        }
    }

    /* SINGLE-PORT: the transfer stays on :69 (not RFC1350's fresh TID -- the
     * block comment above); from here on this peer's DATA/ERROR are the session. */
    s_tftp_client = *from;
    s_tftp_expected_block = 1;
    s_xport = XPORT_TFTP;
    /* A claim has no MPS3 header: it is payload from its first byte. RECV_PAYLOAD
     * (not IDLE) keeps the one-session rule -- a 6910 push or a second WRQ during
     * a claim is refused exactly as during a bitstream transfer. */
    s_recv_state = named ? RECV_PAYLOAD : RECV_HEADER;
    s_named = named;
    s_hdr_got = 0;
    tftp_send_ack(0);
}

/* One DATA block of a TOFU claim transfer (s_named != NULL). Same block-number
 * discipline as the bitstream path in tftp_handle_session_packet(); the bytes go to
 * the named-file sink instead of session_feed(). */
static void tftp_named_data(uint16_t block, const uint8_t *data, uint32_t dlen)
{
    if (s_named->write(data, dlen) != 0) {
        tftp_send_error(&s_tftp_client, 3, "disk full or allocation exceeded");
        tftp_abort();   /* session_reset() -> the provider's abort() */
        return;
    }
    if (dlen < TFTP_BLOCK_BYTES) {
        const mps3_cfg_named_sink_t *ns = s_named;
        s_named = 0;    /* finish() consumes it: no abort() is owed any more */
        if (ns->finish() != 0) {
            tftp_send_error(&s_tftp_client, 2, "access violation");
        } else {
            tftp_send_ack(block);
        }
        tftp_abort();   /* session done (abort == reset; nothing is staged) */
        return;
    }
    tftp_send_ack(block);
    s_tftp_expected_block++;
}

/* A non-request datagram from the session's peer (tftp_poll() checked the peer). */
static void tftp_handle_session_packet(const uint8_t *pkt, int len)
{
    if (len < 4) {
        return;
    }
    uint16_t opcode = (uint16_t)((pkt[0] << 8) | pkt[1]);
    if (opcode == TFTP_OP_ERROR) {
        tftp_abort(); /* client gave up — torn transfer, nothing staged */
        return;
    }
    if (opcode != TFTP_OP_DATA) {
        tftp_send_error(&s_tftp_client, 4, "expected DATA");
        tftp_abort();
        return;
    }
    uint16_t block = (uint16_t)((pkt[2] << 8) | pkt[3]);
    if (block == (uint16_t)(s_tftp_expected_block - 1)) {
        tftp_send_ack(block); /* our ACK was lost: re-ACK, drop the dup */
        return;
    }
    if (block != s_tftp_expected_block) {
        tftp_send_error(&s_tftp_client, 4, "unexpected block");
        tftp_abort();
        return;
    }

    const uint8_t *data = pkt + 4;
    uint32_t dlen = (uint32_t)(len - 4);
    if (s_named) {
        tftp_named_data(block, data, dlen);   /* TOFU claim, not a bitstream */
        return;
    }
    uint32_t took = 0;
    config_agent_status_t st = session_feed(data, dlen, &took);
    if (st == CFG_AGENT_OK && took != dlen) {
        st = CFG_AGENT_ERR_SIZE;   /* unreachable: a commit never takes a TFTP push */
    }
    if (st != CFG_AGENT_OK) {
        /* Bad header/oversize/over-length — ERROR aborts the transfer
         * before any (modelled) ICAP involvement. A slot image this peer may
         * not push (v0.14 lock) is an access violation, as a refused claim is. */
        if (st == CFG_AGENT_ERR_ACCESS) {
            tftp_send_error(&s_tftp_client, 2, "access violation");
        } else {
            tftp_send_error(&s_tftp_client, 0, "rejected");
        }
        tftp_abort();
        return;
    }

    int final = (dlen < TFTP_BLOCK_BYTES);
    if (final) {
        st = finish_payload();
        if (st != CFG_AGENT_OK) {
            /* ERROR instead of the final ACK: the client learns the push
             * was refused (torn/CRC/short). */
            tftp_send_error(&s_tftp_client, 0, "rejected");
            tftp_abort();
            return;
        }
        tftp_send_ack(block);
        tftp_abort(); /* session done (abort == reset; the payload is staged) */
        return;
    }
    tftp_send_ack(block);
    s_tftp_expected_block++;
}

static void tftp_poll(void)
{
    uint8_t pkt[600]; /* > 4 + 512 (TFTP DATA max) */
    mps3_net_addr_t from;

    /* :69 carries everything (single-port): WRQ/RRQ from anyone, the session's
     * DATA/ERROR from its peer. Bounded per poll -- the old intake + TID budgets. */
    for (int i = 0; i < 2 * CFG_AGENT_UDP_DGRAMS_PER_POLL; i++) {
        int n = mps3_net_udp_recvfrom(s_tftp_sock, pkt, sizeof(pkt), &from);
        if (n <= 0) {
            break;
        }
        if (n < 2) {
            continue;
        }
        uint16_t opcode = (uint16_t)((pkt[0] << 8) | pkt[1]);
        if (opcode == TFTP_OP_WRQ) {
            tftp_handle_wrq(pkt, n, &from);
        } else if (opcode == TFTP_OP_RRQ) {
            tftp_send_error(&from, 4, "reads not supported (push-only server)");
        } else if (tftp_is_peer(&from)) {
            tftp_handle_session_packet(pkt, n);
        }
        /* anything else: not this session's (a stale or foreign peer, or no
         * session at all) -- ignore, never answer (RFC1350 §4) */
    }
}

void config_agent_poll(void)
{
    tcp_poll();
    tftp_poll();
}

/* ---- hand-off to swap_fsm ---------------------------------------------------- */

static int take_slot(staging_slot_t *slot, config_agent_bitstream_info_t *info_out)
{
    if (!slot->full) {
        return -1;
    }
    info_out->rm_id     = slot->hdr.rm_id;
    info_out->static_id = slot->hdr.static_id;
    info_out->len_words = slot->hdr.len_words;
    info_out->crc32     = slot->hdr.crc32;
    info_out->in_qspi   = slot->in_qspi;
    info_out->in_icap   = slot->in_icap;
    /* QSPI-staged partial: bytes live in the QSPI scratch, streamed to HWICAP
     * from flash by swap_fsm/overlay_store. ICAP-direct partial (Path 3): bytes
     * are ALREADY in the ICAP (streamed as they arrived) — nothing left for
     * swap_fsm to push. Neither is available in `data`. */
    info_out->data      = (slot->in_qspi || slot->in_icap) ? 0 : slot->data;
    slot->full = 0; /* consumed; `data` stays readable until the next push of this kind */
    return 0;
}

int config_agent_take_validated_partial(config_agent_bitstream_info_t *info_out)
{
    return take_slot(&s_stage_partial, info_out);
}

int config_agent_take_validated_clearing(config_agent_bitstream_info_t *info_out)
{
    return take_slot(&s_stage_clearing, info_out);
}

int config_agent_pair_ready(void)
{
    return s_stage_clearing.full && s_stage_partial.full;
}

void config_agent_rx_progress(uint32_t *got, uint32_t *expect)
{
    if (got)    *got    = s_payload_got;
    if (expect) *expect = s_payload_expect;
}

struct mps3_net_conn *config_agent_active_tcp_conn(void)
{
    return s_tcp_conn; /* the in-flight 6910 push connection, or NULL when idle */
}

void config_agent_win_diag(uint32_t *windows_drained, uint32_t *grant_send_fails)
{
    if (windows_drained)  *windows_drained  = s_win_windows_drained;
    if (grant_send_fails) *grant_send_fails = s_win_grant_send_fails;
}

uint32_t config_agent_icap_defer_polls(void)
{
    return s_icap_defer_polls;
}
