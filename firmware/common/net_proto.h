/*
 * net_proto.h — wire contract for docs/contracts/net-protocol.md v0.
 *
 * The MicroBlaze/lwIP shell firmware (A3) is the SERVER for every port and
 * verb declared here; host tender + pyverify (A4) are the clients. Keep this
 * header in lockstep with net-protocol.md — it is the second half of the
 * "shared contract header" pair alongside platform_regs.h.
 */
#ifndef MPS3_NET_PROTO_H
#define MPS3_NET_PROTO_H

#include <stdint.h>

#include "diag.h"   /* mps3_diag_t + MPS3_DIAG_FIELDS — the diag verb's payload */

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------
 * Port map (net-protocol.md "TCP/UDP port map")
 * ------------------------------------------------------------------------ */
#define MPS3_PORT_CONTROL    6900u /* TCP  control/status JSON-lines   -> coordinator   */
#define MPS3_PORT_TFTP         69u /* UDP  TFTP clearing/partial push  -> config_agent  */
#define MPS3_PORT_RAW_PUSH   6910u /* TCP  raw partial push (alt TFTP) -> config_agent  */
#define MPS3_PORT_XVC        2542u /* TCP  XVC -> Debug Bridge         -> xvc_server    */
#define MPS3_PORT_SWD        6920u /* TCP  RETIRED: swd_server is built only with LEGACY_SWD=1 */
#define MPS3_PORT_JTAG       6921u /* TCP  OpenOCD remote_bitbang JTAG -> jtag_server   */
#define MPS3_PORT_UART0      6930u /* TCP  UART0 (boot monitor)        -> uart_over_eth */
#define MPS3_PORT_UART1      6931u /* TCP  UART1: INERT, the BD ties the seam off  */
#define MPS3_PORT_SWO        6932u /* TCP  SWO/ITM trace               -> uart_over_eth */

/* Default static IP (net-protocol.md: "matches the existing fpgahub MPS3
 * board block"). DHCP is D8/site-dependent — see firmware/README.md. */
#define MPS3_DEFAULT_IP_A 192
#define MPS3_DEFAULT_IP_B 168
#define MPS3_DEFAULT_IP_C 10
#define MPS3_DEFAULT_IP_D 101

/* ==========================================================================
 * Control channel (TCP 6900) — one JSON object per line, request/response.
 * ==========================================================================
 * The contract only fixes the *wire shape* (newline-terminated JSON), not a
 * parser. The tokenizer below (mps3_json_*) is a hand-rolled, bounded,
 * fail-closed parser for the contract's actual subset: ONE flat JSON object
 * per line. Nesting and arrays are deliberately REJECTED — no verb in
 * net-protocol.md v0 uses them (asserted here as a contract fact, not a
 * shortcut); if the verb set ever grows structured payloads, a small
 * permissively-licensed tokenizer (e.g. jsmn, MIT) is the v2 upgrade —
 * reference only, do not vendor.
 *
 * Hard guarantees (see firmware/test/test_net_proto_json.c):
 *   - No allocation, no recursion; all state is caller-provided or on-stack.
 *   - Every scan is bounded by the caller's `len` — the line buffer need
 *     not be NUL-terminated (it arrives off the wire) and is never read
 *     past `len`.
 *   - Malformed input (truncated line, missing quote/colon/comma, unknown
 *     escape, raw control char, nesting, duplicate key, int overflow,
 *     trailing garbage) is REJECTED, never best-effort-parsed.
 *   - Escape policy: the six JSON single-char escapes plus \" \\ are
 *     UNESCAPED on extraction; \uXXXX (and anything else) is REJECTED —
 *     every contract value is a plain-ASCII identifier, so \u would only
 *     ever appear in hostile/corrupt input (fail closed).
 *   - A string value that does not fit the caller's buffer is an ERROR
 *     (MPS3_JSON_ETOOLONG), never a silent truncation — a truncated rm
 *     name must not be able to target the wrong overlay.
 */
typedef enum {
    MPS3_OP_UNKNOWN = 0,
    MPS3_OP_PING,        /* {"op":"ping"} */
    MPS3_OP_RESET,       /* {"op":"reset","target":"dut"} */
    MPS3_OP_SET_CLK,     /* {"op":"set_clk","preset":"25mhz"} */
    MPS3_OP_SWAP,        /* {"op":"swap","rm":"nanosoc","src":"tftp"} */
    MPS3_OP_LINK,        /* {"op":"link","event":"down"} */
    MPS3_OP_COMMIT,      /* {"op":"commit","rm":..,"src":"tcp","rm_id":..,"static_id":..,
                          *  "clear_len":N,"clear_crc":..,"part_len":N,"part_crc":..} (v0.13) */
    MPS3_OP_TELEMETRY,   /* {"op":"telemetry"} */
    MPS3_OP_MACGEN,      /* {"op":"macgen","gen":true,"chk":true,"inject":"bad_fcs"} */
    MPS3_OP_DIAG,        /* {"op":"diag"} — RX-recovery / ICAP / receive counters */
    MPS3_OP_DISPLAY,     /* {"op":"display","owner":"dut|harness|toggle|query"} — CLCD KVM flip */
    MPS3_OP_VERSION,     /* {"op":"version"} — what image IS this? (v0.8) */
    MPS3_OP_DUTRX,       /* {"op":"dutrx"} — one chunk of a DUT-transmitted frame (v0.10) */
    /* v0.11 — APPENDED, so every existing enumerator keeps its value. */
    MPS3_OP_STATS,       /* {"op":"stats"} — one-line board state (fpgahub's shape) */
    MPS3_OP_LOG,         /* {"op":"log","off":N} — one chunk of the console log ring */
    MPS3_OP_TOUCH_CAL,   /* {"op":"touch_cal","act":"get|set|raw|default",...} */
    MPS3_OP_REBOOT,      /* {"op":"reboot"} — warm restart via the shell watchdog */
    /* v0.13 — APPENDED. */
    MPS3_OP_USD,         /* {"op":"usd"[,"action":"format|clear|rescan"][,"confirm":".."]} */
    /* v0.14 — APPENDED (Linux harness, plan §10a S10). Enumerator values are not
     * wire contract (ops travel as names), so a merge that appends another op
     * (D13's MPS3_OP_USD) beside this one only has to keep both. */
    MPS3_OP_SLOT,        /* {"op":"slot","act":"status|commit|rollback|verify"[,"slot":"A|B"]} */
    /* v0.16 -- APPENDED (Linux harness, lane IDENT): the board identity. ENGINE
     * verbs: the engine's provider parses the arguments from the raw line
     * (mps3_ctrl_request_t.line) and renders the reply body; bare metal declines. */
    MPS3_OP_IDENTITY,    /* {"op":"identity"} */
    MPS3_OP_IDENTITY_SET,/* {"op":"identity_set"[,"label":..][,"hostname":..][,"ip":..][,"mac":..]}
                          * | {"op":"identity_set","clear":true} */
    MPS3_OP_LOCATE,      /* {"op":"locate","s":0..30[,"who":".."]} -- blink + identify banner */
    /* v0.17 -- APPENDED (Linux harness, lane PANEL-PROTO; the Harness Manager's R1/R2):
     * presence and the front panel. ENGINE verbs like v0.16's. `hello` is the ONE
     * request whose values nest (lease/job, one level): the decoder lets exactly
     * that through (mps3_json_parse_ex); every other nested line is still bad json. */
    MPS3_OP_HELLO,       /* {"op":"hello","v":1,"sid":..,"who":..,"app":..[,"name":..],"role":..
                          *  [,"lease":{by,left,q,req,rl}][,"job":{k,p}],"ttl":30..300} */
    MPS3_OP_PANEL,       /* {"op":"panel"} | {"op":"panel","frame":"a"|"b"}
                          * | {"op":"panel","page":"status"|"apps"} (claim-locked) */
} mps3_ctrl_op_t;

/* ------------------------------------------------------------------------
 * `dutrx` CHUNK SIZE (net-protocol.md v0.10 "DUT egress").
 * ------------------------------------------------------------------------
 * One `dutrx` reply carries at most this many bytes of the head frame, hex-
 * encoded (2 chars per byte). An Ethernet frame is up to MAX_FRAME = 1536 B in
 * DUTEGR, so a frame takes ceil(len/256) replies; `more` says when to ask
 * again.
 *
 * 256 IS CHOSEN SO THAT `diag` STAYS THE BINDING CASE FOR MPS3_CTRL_RESP_MAX.
 * The worst-case dutrx line is
 *      {"ok":true                                                10 B
 *      ,"len":65535 ,"off":65535 ,"n":256                        13+13+9
 *      ,"more":true ,"last":true                                 12+12
 *      ,"frames":65535                                           16
 *      ,"rx":4294967295 ,"drop_full":… ,"drop_giant":…           17+24+25
 *      ,"ovf":true ,"desync":true                                11+14
 *      ,"data":"<512 hex chars>"                                 9+512
 *      }\n + NUL                                                 3
 *   = 694 B with the NUL (693 on the wire -- MEASURED through the real encoder,
 * not counted by hand), against `diag`'s measured 1040. Doubling the chunk to
 * 512 would take it to 1206 and make THIS verb the thing the bound is sized
 * for -- and then every future diag counter would be arguing with a frame chunk
 * for the same buffer. Measured, not argued: firmware/test/test_net_proto_json.c
 * encodes a full-width chunk through the real encoder and asserts both that it
 * fits and that it stays under the diag worst case.
 *
 * It costs stack: mps3_ctrl_response_t carries the chunk as raw bytes (the
 * codec owns the hex rendering), so the struct grows by this many bytes:
 * sizeof(mps3_ctrl_response_t) is 772 B (from 480). One of those lives in
 * coordinator_dispatch_line()'s frame, beneath coordinator_net_poll()'s two
 * MPS3_CTRL_RESP_MAX line buffers -- ~3.2 KiB worst case against the 8 KiB
 * _STACK_SIZE, whose arithmetic is in firmware/platform/lscript.ld.in. */
#define MPS3_DUTRX_CHUNK_MAX 256

/* ------------------------------------------------------------------------
 * `version` FEATURE BITS (net-protocol.md v0.8 "version"; v0.11 appends).
 * ------------------------------------------------------------------------
 * One bit per COMPILE-TIME flag in firmware/platform/Makefile. The wire form
 * is a JSON array of names, emitted in the FIXED order below (lowest bit
 * first) with absent features simply omitted — so a client may compare the
 * array for equality, not just membership, and two images with the same
 * feature set always render the same bytes.
 *
 * WHY a bitmask in the struct rather than a pre-built string: it keeps the
 * codec (net_proto.c) free of #ifdefs — the handler that KNOWS which flags
 * were set (coordinator_handle_version) sets bits, and the codec owns the
 * names and their order. It also makes the host tests able to exercise every
 * combination without recompiling the firmware under a different flag set.
 *
 * Bits 0..4 are the five build flags that made up PRODUCT=1 at v0.8. v0.11
 * APPENDS bits 5..12 -- never reorders: a client compares the array for
 * equality, so a bit's position is wire contract. Bits 5..12 are not all build
 * FLAGS: several are services a client must know are compiled in (a verb it may
 * send, a port it may open) even though no knob turns them off today. The rule
 * for adding one is unchanged: a bit here, a name in net_proto.c's
 * s_feature_names (SAME order), a line in version_features() (coordinator.c),
 * and a row in net-protocol.md -- appended, always. */
enum {
    MPS3_FEATURE_CLCD        = 1u << 0, /* -DMPS3_HAS_CLCD          (CLCD=1)        */
    MPS3_FEATURE_CLCD_KVM    = 1u << 1, /* -DMPS3_HAS_CLCD_KVM      (CLCD_KVM=1)    */
    MPS3_FEATURE_TOUCH       = 1u << 2, /* -DMPS3_HAS_TOUCH         (TOUCH=1)       */
    MPS3_FEATURE_HWICAP_FIFO = 1u << 3, /* -DMPS3_HWICAP_FIFO=1     (HWICAP_FIFO=1) */
    MPS3_FEATURE_WINDOWED    = 1u << 4, /* -DMPS3_CFG_AGENT_WINDOWED (WINDOWED=1)   */
    /* ---- v0.11, appended ---------------------------------------------- */
    MPS3_FEATURE_DUT_EGRESS  = 1u << 5, /* -DMPS3_HAS_DUT_EGRESS    (DUT_EGRESS=1): `dutrx` answers */
    MPS3_FEATURE_JTAG_SERVER = 1u << 6, /* jtag_server (TCP 6921 remote_bitbang) is linked + polled */
    MPS3_FEATURE_XVC_DBGBR   = 1u << 7, /* XVC 2542 drives the Debug Bridge (XVC_TARGET empty)       */
    MPS3_FEATURE_XVC_JTAGBB  = 1u << 8, /* XVC 2542 drives jtag_bb @0x44A7 (XVC_TARGET=jtagbb|swdbb) */
    MPS3_FEATURE_STATS       = 1u << 9, /* the `stats` verb                                           */
    MPS3_FEATURE_LOG         = 1u << 10,/* the `log` verb + the console log ring                      */
    MPS3_FEATURE_REBOOT      = 1u << 11,/* the `reboot` verb (shell watchdog warm restart)            */
    MPS3_FEATURE_TOUCH_CAL   = 1u << 12,/* the `touch_cal` verb (needs TOUCH=1)                       */
    /* ---- v0.13, appended (D13). Clients test it by NAME ("usd"): if another
     * lane appends a bit first, renumber at merge -- the name does not move. */
    MPS3_FEATURE_USD         = 1u << 13,/* the `usd` verb + the v0.13 `commit` (user-microSD store)   */
    /* ---- v0.14 amendments (2026-09-26, HM_ANSWERS), appended in landing order.
     * They follow the LINKED PROVIDER (a weak seam), not a -D flag: bare metal
     * never sets them. Clients test them by NAME. */
    MPS3_FEATURE_SLOT        = 1u << 14,/* the `slot` verb + the kind-2 push are served (mps3_slot_supported) */
    MPS3_FEATURE_XVC_LOCK    = 1u << 15,/* 2542/6921 refuse a non-local peer on a claimed board (mps3_debug_lock_supported) */
};

/* Number of names in net_proto.c's s_feature_names[] — the encoder walks bits
 * 0..MPS3_FEATURE_COUNT-1, so a bit added above without a name is simply not
 * emitted (fail quiet on the wire, loud in review). */
#define MPS3_FEATURE_COUNT 16

/* Parsed request — decode fills whichever field(s) the op uses; unused
 * fields are left zeroed/empty. Strings are NUL-terminated, sized generously
 * for rm names / preset ids (contract does not bound these; 32 is a
 * firmware-side choice, not a protocol limit). */
#define MPS3_CTRL_STR_MAX 32

typedef struct {
    mps3_ctrl_op_t op;
    char target[MPS3_CTRL_STR_MAX];  /* reset.target            */
    char preset[MPS3_CTRL_STR_MAX];  /* set_clk.preset           */
    char rm[MPS3_CTRL_STR_MAX];      /* swap.rm / commit.rm      */
    char src[MPS3_CTRL_STR_MAX];     /* swap.src ("tftp"/"tcp")  */
    char event[MPS3_CTRL_STR_MAX];   /* link.event ("down"/"up") */
    int  gen;                        /* macgen.gen  (GENCHK.CTRL.gen_en) */
    int  chk;                        /* macgen.chk  (GENCHK.CTRL.chk_en) */
    char inject[MPS3_CTRL_STR_MAX];  /* macgen.inject (INJECT fault name) */
    char owner[MPS3_CTRL_STR_MAX];   /* display.owner ("dut"/"harness"/"toggle"/"query") */
    /* v0.11 */
    int32_t off;                     /* log.off (OPTIONAL; 0 when absent)          */
    char act[MPS3_CTRL_STR_MAX];     /* touch_cal.act ("get"/"set"/"raw"/"default");
                                      * v0.14 slot.act ("status"/"commit"/"rollback"/"verify") */
    int32_t cal[7];                  /* touch_cal set: ax,bx,cx,ay,by,cy,shift      */
    /* v0.13 (D13): `usd` actions and the re-push `commit`. `action`/`confirm`
     * are OPTIONAL strings ("" when absent); the commit fields are ALL required
     * (the v0.11 `{"op":"commit","rm":..}` form decodes as bad args). The ids and
     * CRCs arrive as "0x" + 1..8 hex digits, the lengths as JSON integers. */
    char action[MPS3_CTRL_STR_MAX];  /* usd.action ("format"/"clear"/"rescan")   */
    char confirm[MPS3_CTRL_STR_MAX]; /* usd.confirm ("erase"/"erase-all")        */
    uint32_t rm_id;                  /* commit.rm_id                               */
    uint32_t static_id;              /* commit.static_id                           */
    uint32_t clear_len;              /* commit.clear_len (bytes)                   */
    uint32_t clear_crc;              /* commit.clear_crc (zlib CRC-32)             */
    uint32_t part_len;               /* commit.part_len (bytes)                    */
    uint32_t part_crc;               /* commit.part_crc                            */
    /* v0.14 */
    char slot_sel[8];                /* slot.slot (OPTIONAL: "A"/"B"; "" when absent) */
    /* v0.16: the ENGINE verbs (identity, identity_set) parse their own arguments:
     * the decoder hands their provider the raw line it decoded (valid for the
     * dispatch only), so no engine-only field grows this struct. NULL otherwise. */
    const char *line;
    int         line_len;
} mps3_ctrl_request_t;

/* touch_cal coefficient order, shared by the request's cal[] and the reply. */
enum {
    MPS3_CAL_AX = 0, MPS3_CAL_BX, MPS3_CAL_CX,
    MPS3_CAL_AY, MPS3_CAL_BY, MPS3_CAL_CY,
    MPS3_CAL_SHIFT,
    MPS3_CAL_N
};

/* touch_cal reply shapes (mps3_ctrl_response_t.tc_kind). */
enum {
    MPS3_TC_COEFFS = 0,  /* get / set / default: the seven coefficients */
    MPS3_TC_RAW    = 1,  /* raw: the last raw ADC sample + its mapping  */
};

/* `stats` (v0.11) -- fpgahub's _from_stats_verb key shape, plus additive
 * extras. Every field is a plain value the handler fills; the codec owns the
 * key names, their order and the rendering. See coordinator_handle_stats() for
 * which register/state each one comes from. NO power fields, by design: there
 * is no power sensor on this platform (see the TELEMETRY note below), and
 * fpgahub treats their absence as deliberate. */
typedef struct {
    uint32_t up_ms;       /* mps3_sys_now_ms() -- the reboot witness          */
    uint32_t sid;         /* g_shell_state.static_id                          */
    uint32_t rm;          /* g_shell_state.current_rm_id                      */
    int      rm_ok;       /* DFXCTL.RM_STATUS.rm_id_valid                     */
    int      lock;        /* DFXCTL.RM_STATUS.dut_lockup (raw pin)            */
    uint32_t clk_sel;     /* CLKRST.DUT_CLK_SEL[7:0] (last preset id written) */
    int      mmcm;        /* CLKRST.STATUS.mmcm_locked                        */
    int      clk_alive;   /* CLKRST.STATUS.dut_clk_alive                      */
    int      dut_rst;     /* CLKRST.RESET_CTRL.dut_resetn (1 = RELEASED)      */
    int      rp_rst;      /* !DFXCTL.STATUS.rp_in_reset   (1 = RELEASED)      */
    int      decpl;       /* DFXCTL.STATUS.decoupled                          */
    int      link;        /* LAN9220 PHY BMSR link                            */
    uint32_t spd;         /* 10 / 100 (0 = link down or unknown)              */
    int      fdx;         /* full duplex                                      */
    uint8_t  mac[6];      /* the shell's own MAC                              */
    char     swap[20];    /* swap FSM state name, "idle" at rest              */
    int      swap_ok;     /* last completed swap succeeded                    */
    uint32_t swap_n;      /* swaps COMPLETED (done + failed) since boot       */
    uint32_t icap;        /* swap_fsm_icap_bytes()                            */
    uint32_t rxdrop;      /* diag rx_drops                                    */
    uint32_t txerr;       /* diag tx_errors                                   */
    /* additive extras (v0.11) */
    char     swap_err[20];/* state the last FAILED swap failed in; "" if none */
    int      clr_ok;      /* the outgoing (resident) clearing ref is valid    */
    uint32_t dut_mhz;     /* DUT clock the firmware last programmed (MHz)     */
    uint32_t svc_max_us;  /* worst superloop PASS since the previous `stats`  */
    uint32_t svc_skipped; /* healthy->sick service edges since previous stats */
    /* additive (2026-09-24, D1): touch liveness, emitted after svc_skipped and
     * before os_up_ms ONLY when touch_present -- a TOUCH=1 build of either
     * engine. A build without the driver omits all three rather than report a
     * plausible "touch_ok":false about hardware it never drives. */
    int      touch_present;    /* the handler filled the three below         */
    int      touch_ok;         /* touch_health().ok: sampling, not latched   */
    uint32_t touch_bus_lost;   /* bus-loss latch events since the image began */
    uint32_t touch_recoveries; /* latches cleared by the periodic re-init     */
} mps3_stats_t;

/* `usd` (v0.13) -- the user-microSD overlay store, as the verb reports it. The
 * handler (coordinator.c) fills this from the store glue; the codec owns the key
 * names, their order and which keys are omitted. */
#define MPS3_USD_TEXT_MAX 16          /* = the CLCD row-4 field after "USD : " */
typedef struct {
    int      action_reply;       /* 1: an ACTION's reply -> {"ok":true,"state":..} only */
    int      present;            /* a card is in the slot                              */
    char     state[16];          /* none|no_hw|init|unsupported|error|foreign|empty|
                                  * valid|stale|bad                                    */
    char     text[MPS3_USD_TEXT_MAX + 1]; /* exactly what the CLCD shows after "USD : " */
    int      have_card_mb;       /* emit card_mb (a card is READY)                     */
    uint32_t card_mb;
    int      have_default;       /* emit default{} (state valid or stale)              */
    uint32_t def_rm_id;
    uint32_t def_static_id;
    char     def_slot;           /* 'A' / 'B'                                          */
    char     boot[40];           /* loaded|skipped|none|pending|failed:<name>          */
} mps3_usd_t;
/* ------------------------------------------------------------------------
 * `slot` (v0.14, net-protocol.md "Slot images"): the user-microSD boot slots
 * stage0 loads Linux from (STAGE0_CONTRACT.md §6), as a TOOL sees them.
 * ------------------------------------------------------------------------
 * The handler (coordinator.c) only turns the request into an act + selector and
 * hands both to the ENGINE's provider (mps3_slot_op(), coordinator.h). The
 * provider fills this struct with the state AFTER the act; the codec owns every
 * key name, the order and the rendering, exactly as it does for `stats`. Every
 * act answers with the same shape, so a client parses one reply.
 *
 * WHY THE CODEC OWNS THE NAMES: two engines implement the provider (the
 * bare-metal weak default declines; mps3-harnessd has the card), and the
 * strings are wire contract. Enumerations here, names in net_proto.c's tables,
 * so no provider can invent a spelling. Append only. */
enum {  /* request act */
    MPS3_SLOT_ACT_STATUS = 0,
    MPS3_SLOT_ACT_COMMIT,     /* default := the staged slot (a verified push)       */
    MPS3_SLOT_ACT_ROLLBACK,   /* default := the slot that is not the default        */
    MPS3_SLOT_ACT_VERIFY,     /* start a card read-back of a slot (async; see job)  */
};
enum {  /* a slot id; the SAME numbers as stage0's S0_FROM_A/B and default_slot */
    MPS3_SLOT_NONE = 0,       /* wire null */
    MPS3_SLOT_A    = 1,
    MPS3_SLOT_B    = 2,
};
enum {  /* what this OS booted from (stage0 status block booted_from) */
    MPS3_SLOT_RUN_UNKNOWN = 0,/* no valid stage0 block: nothing can be decided      */
    MPS3_SLOT_RUN_A       = 1,
    MPS3_SLOT_RUN_B       = 2,
    MPS3_SLOT_RUN_RESCUE  = 3,/* a TFTP rescue push: neither slot is running        */
    MPS3_SLOT_RUN_NONE    = 4,/* stage0 ran but handed nothing off (a JTAG load)   */
};
enum {  /* per-slot state, from the MBR entry + the S0LB header/table only */
    MPS3_SLOT_ST_ABSENT = 0,  /* the MBR entry is not a 0x7F slot                   */
    MPS3_SLOT_ST_EMPTY,       /* no S0LB magic at the slot's first byte             */
    MPS3_SLOT_ST_BAD,         /* S0LB magic, but version/table/window is wrong      */
    MPS3_SLOT_ST_VALID,       /* the table is valid (region CRCs: see `verified`)   */
    MPS3_SLOT_ST_IO,          /* the card did not answer the read                   */
};
enum {  /* how (and whether) this boot proved the slot's region CRCs */
    MPS3_SLOT_VER_NO = 0,
    MPS3_SLOT_VER_BOOT,       /* stage0 CRC-checked it at THIS boot's hand-off      */
    MPS3_SLOT_VER_READBACK,   /* a push or a verify read it back off the card       */
};
enum {  /* the one asynchronous card job */
    MPS3_SLOT_JOB_NONE = 0, MPS3_SLOT_JOB_PUSH, MPS3_SLOT_JOB_VERIFY,
};
enum {
    MPS3_SLOT_JS_IDLE = 0,    /* no job this boot                                   */
    MPS3_SLOT_JS_WRITING,     /* a 6910/TFTP push is streaming into the slot         */
    MPS3_SLOT_JS_VERIFYING,   /* the card read-back is running                      */
    MPS3_SLOT_JS_OK,          /* the last job passed                                */
    MPS3_SLOT_JS_FAILED,      /* the last job failed: err says why                  */
};

typedef struct {
    uint8_t  state;           /* MPS3_SLOT_ST_*                                     */
    uint8_t  verified;        /* MPS3_SLOT_VER_*                                    */
    uint8_t  has_sid;         /* a slot record binds THIS image to a static_id      */
    uint32_t hdr_crc;         /* S0LB table CRC (VALID only): the image's identity  */
    uint32_t len;             /* image extent in bytes (VALID only)                 */
    uint32_t sid;             /* the record's static_id (has_sid only)              */
    char     err[24];         /* BAD / IO: why                                      */
} mps3_slot_info_t;

typedef struct {
    uint8_t  card;            /* 0: no card -- only fabric_sid/running/staged/job   */
    uint8_t  running;         /* MPS3_SLOT_RUN_*                                    */
    uint8_t  deflt;           /* MPS3_SLOT_A/B: stage0's pick of the boot-select    */
    uint8_t  target;          /* where a push goes (MPS3_SLOT_NONE = nowhere)       */
    uint8_t  staged;          /* the slot a push verified this boot, or NONE        */
    uint32_t seq;             /* the winning boot-select seq (0 = no valid copy)    */
    uint32_t fabric_sid;      /* stage0's baked static_id: what images must match   */
    mps3_slot_info_t s[2];    /* [0] = A, [1] = B                                   */
    uint8_t  job_act;         /* MPS3_SLOT_JOB_*                                    */
    uint8_t  job_state;       /* MPS3_SLOT_JS_*                                     */
    uint8_t  job_slot;        /* MPS3_SLOT_A/B/NONE                                 */
    uint32_t job_got;         /* WRITING: bytes on the card so far                  */
    uint32_t job_len;         /* WRITING: bytes the push announced                  */
    char     job_err[64];     /* FAILED: why                                        */
    char     job_code[16];    /* FAILED: an optional stable code for job_err (HM_ANSWERS
                               * S3); emitted after `err` inside `job` only when set  */
    /* 2026-09-26 (HM_ANSWERS S2/S5), both reply forms, APPENDED after `job`: */
    uint8_t  claimed;         /* the board's SSH is claimed (== identify ssh.claimed:
                               * a non-local peer's mutations are LOCKED)            */
    uint8_t  confirmed;       /* stage0's att_confirm holds S0_CONFIRM_MAGIC: this
                               * boot was confirmed healthy to stage0                */
} mps3_slot_status_t;

/* Generic response fields — a given op only fills the subset it needs
 * (see net-protocol.md's per-op response shapes). `ok=false` + `err` covers
 * every rejection path (bad JSON, unknown op, swap/commit failure, ...).
 * `op` selects which per-op shape mps3_ctrl_encode_response() emits — the
 * dispatcher copies it from the decoded request (MPS3_OP_UNKNOWN for a
 * request that never decoded). */
typedef struct {
    mps3_ctrl_op_t op;       /* selects the response shape (see encode)  */
    int      ok;
    char     shell_id[16];   /* ping: static_id, hex string             */
    char     rm_id[16];      /* ping/swap: current/new rm_id, hex string */
    int      locked;         /* set_clk: mmcm_locked                    */
    int      verified;       /* swap: RM-load verify passed             */
    char     slot;            /* commit: 'A' or 'B'                      */
    char     owner[16];       /* display: resulting COMMITTED owner
                               * ("harness"/"dut", CLCDKVM.STATUS.owner)  */
    /* telemetry: `lockup` ONLY. There are deliberately no mv/ma fields --
     * this platform has NO reachable power sensor (see the encode note and
     * coordinator_handle_telemetry), so there is nothing for them to hold.
     * They were REMOVED rather than left zeroed: a zeroed field is exactly
     * how the old handler shipped a fake reading. */
    int      lockup;          /* telemetry: raw DFXCTL.RM_STATUS.dut_lockup pin */
    uint32_t tx_cnt;          /* macgen: GENCHK.TX_CNT  -> wire "tx"      */
    uint32_t rx_cnt;          /* macgen: GENCHK.RX_CNT  -> wire "rx"      */
    uint32_t err_cnt;         /* macgen: GENCHK.ERR_CNT -> wire "err"     */
    /* diag: the WHOLE mailbox, not a hand-picked subset. This used to be 14
     * `diag_*` u32 members — while mps3_diag_t declared 25 counters, so the
     * eleven TX-path / ICAP-EOS / ovlstore counters existed, were published
     * every superloop pass, were readable over JTAG, and were invisible to the
     * 6900 verb. Nothing could see the gap: three hand-written lists (here,
     * the encoder, coordinator_handle_diag) all agreed with each other and
     * with nothing else. Now the encoder walks MPS3_DIAG_FIELDS (diag.h) and
     * coordinator_handle_diag snapshots straight into this member, so a new
     * counter reaches the wire by existing. */
    mps3_diag_t diag;         /* diag: mailbox snapshot; keys from MPS3_DIAG_FIELDS */
    /* version (v0.8) — the running image's own identity. Sourced from the
     * mps3_harness_*() seam (firmware/platform/mps3_version.h) and the
     * compile-time flags, NOT from any register: this answers "which build
     * is running", which is orthogonal to static_id's "will this partial
     * fit this fabric" (see mps3_version.h's "This is NOT static_id"). */
    char     harness[16];     /* version: "1.0.0"      -> wire "harness"  */
    char     ver32[16];       /* version: "0x01000001" -> wire "ver32"    */
    char     sha[16];         /* version: "5c09de10"   -> wire "sha"      */
    int      dirty;           /* version: 0/1 dirty tree -> wire "dirty"  */
    uint32_t lmb_kb;          /* version: LMB KiB this image is LINKED for */
    uint32_t features;        /* version: MPS3_FEATURE_* bitmask -> "features" */
    /* version: what the FABRIC says it is, beside `ver32` — what the IMAGE was
     * built as. The same HARNESS_VER32 is stamped into the bitstream's
     * USR_ACCESS (AXSS) by fpga/dfx/build_dfx.tcl and compiled into the image by
     * scripts/gen_version.py, from ONE generator. They can only disagree if the
     * .bit and the image inside it came from different builds — the
     * "flashable base whose updatemem was never re-run" hazard this platform has
     * already been bitten by, and which NOTHING has ever cross-checked on
     * hardware.
     *
     * "" (empty) means the fabric value could NOT be read — which is a third
     * state, not a pass: the codec emits null for both keys rather than a
     * comparison it did not make.
     *
     * MUST be rendered by the SAME formatter as `ver32` (format_id_hex), because
     * the codec derives the verdict by comparing the two STRINGS. One rendering,
     * one comparison, no second numeric field to drift. */
    char     usr_access[16];  /* version: "0x01000001" -> wire "usr_access" */
    /* dutrx (v0.10) — ONE CHUNK of the frame at the head of the DUT-egress
     * capture FIFO (DUTEGR @ 0x44B2_0000, fpga/shell/ip/dut_egress/). The
     * handler pops the bytes out of the destructive DATA port; the codec owns
     * the hex rendering, so what is carried here is the raw frame bytes.
     *
     * The three counters and the two sticky flags ride on EVERY reply, empty
     * ones included, and that is the point: the block never backpressures the
     * bridge (it would park the whole thing), so it DROPS -- and a read path
     * that could report frames without reporting what was lost would be the
     * silent drop the RTL was written to avoid. `rx + drop_full + drop_giant`
     * is the block's invariant against frames presented. */
    uint16_t dutrx_len;        /* head frame's TOTAL length (FRAME_LEN[31:16]) */
    uint16_t dutrx_off;        /* bytes of it delivered BEFORE this chunk      */
    uint16_t dutrx_n;          /* bytes in dutrx_data (0..MPS3_DUTRX_CHUNK_MAX)*/
    uint16_t dutrx_frames;     /* committed frames waiting (LEVEL[31:16])      */
    int      dutrx_more;       /* bytes of THIS frame remain after this chunk  */
    int      dutrx_last;       /* DATA[9] LAST on this chunk's final byte      */
    int      dutrx_ovf;        /* STATUS.OVF    (sticky)                        */
    int      dutrx_desync;     /* STATUS.DESYNC (sticky)                        */
    uint32_t dutrx_rx_frames;  /* DUTEGR.RX_FRAMES  -> wire "rx"                */
    uint32_t dutrx_drop_full;  /* DUTEGR.DROP_FULL  -> wire "drop_full"         */
    uint32_t dutrx_drop_giant; /* DUTEGR.DROP_GIANT -> wire "drop_giant"        */
    uint8_t  dutrx_data[MPS3_DUTRX_CHUNK_MAX]; /* raw bytes; hex on the wire    *
                                                * (ALSO the `log` chunk: the two
                                                * verbs never share a reply, and
                                                * a second 256-byte buffer would
                                                * cost stack in every dispatch) */
    /* log (v0.11) -- one chunk of the console ring; bytes in dutrx_data. */
    uint32_t log_off;          /* stream offset of the first byte in the chunk */
    uint16_t log_n;            /* bytes in the chunk (0..MPS3_DUTRX_CHUNK_MAX) */
    int      log_more;         /* bytes remain after this chunk               */
    uint32_t log_dropped;      /* bytes lost to ring overrun since boot       */
    /* stats (v0.11) */
    mps3_stats_t stats;
    /* touch_cal (v0.11) */
    int      tc_kind;          /* MPS3_TC_COEFFS / MPS3_TC_RAW                */
    int32_t  tc_cal[MPS3_CAL_N];/* coefficients (COEFFS)                      */
    uint32_t tc_raw_x, tc_raw_y, tc_raw_z; /* last raw ADC sample (RAW)      */
    uint32_t tc_seen;          /* raw samples captured since boot (RAW)       */
    uint32_t tc_x, tc_y;       /* that sample through the CURRENT calibration */
    /* reboot (v0.11) */
    uint32_t in_ms;            /* upper bound until the watchdog resets us    */
    /* usd (v0.13) */
    mps3_usd_t usd;
    /* slot (v0.14) -- the state after the act, whatever the act */
    mps3_slot_status_t slot_st;
    char     err[64];         /* !ok: human-readable reason               */
    /* !ok: an OPTIONAL stable machine code beside `err` (2026-09-26, HM_ANSWERS
     * S3/C2), e.g. "locked", "busy", "not_verified". Emitted ONLY when non-empty,
     * so every line that never sets it -- every v0.11 verb, every bare-metal
     * reply but the `slot` decline -- is byte-identical to before. */
    char     code[24];
    /* v0.16 ENGINE verbs (identity, identity_set), ok: the provider's reply BODY
     * -- the keys after "op", no braces -- owned by the provider (static), never
     * escaped here: the provider emits only validated values. */
    const char *body;
} mps3_ctrl_response_t;

/* --------------------------------------------------------------------------
 * Flat-JSON-line tokenizer — see the guarantees block above.
 * mps3_json_parse() validates the WHOLE line once into an on-stack member
 * table (spans into the caller's buffer — no copies, valid only while that
 * buffer is); the typed getters then extract by key, order-independent.
 * -------------------------------------------------------------------------- */
enum {
    MPS3_JSON_OK         = 0,
    MPS3_JSON_EMALFORMED = -1, /* not a valid flat JSON object (see guarantees) */
    MPS3_JSON_EMISSING   = -2, /* key not present */
    MPS3_JSON_ETYPE      = -3, /* key present, value has the wrong JSON type */
    MPS3_JSON_ETOOLONG   = -4, /* string value does not fit the caller's buffer */
};

/* Firmware-side bound, not a protocol limit: the largest contract REQUEST is
 * v0.11's `touch_cal` set (op + act + seven coefficients = 9 members). Exceeding
 * this is rejected as malformed (fail closed). (Responses are never parsed by
 * the firmware -- only encoded -- so their size does not enter into it.) */
#define MPS3_JSON_MAX_MEMBERS 10  /* v0.11: touch_cal set = op+act+7 coefficients = 9 */

typedef enum {
    MPS3_JSON_T_STRING = 0,
    MPS3_JSON_T_INT,
    MPS3_JSON_T_BOOL,
    MPS3_JSON_T_NULL,   /* accepted by the scanner (it is valid JSON); no
                         * typed getter ever returns it — extraction of a
                         * null value fails MPS3_JSON_ETYPE */
    MPS3_JSON_T_OBJECT, /* v0.17, mps3_json_parse_ex(allow_objects) ONLY: a flat
                         * nested object; val/val_len span it, braces included */
} mps3_json_type_t;

typedef struct {
    const char      *key;     /* span into the parsed line (no escapes allowed in keys) */
    int              key_len;
    mps3_json_type_t type;
    const char      *val;     /* T_STRING: raw span between the quotes (may
                               * contain escapes — unescaped by get_string);
                               * T_INT: the number's text span */
    int              val_len;
    int32_t          ival;    /* T_INT: value; T_BOOL: 0/1 */
} mps3_json_member_t;

typedef struct {
    int                n_members;
    mps3_json_member_t members[MPS3_JSON_MAX_MEMBERS];
} mps3_json_obj_t;

/* Parse one complete line (`len` bytes, need not be NUL-terminated; a
 * trailing '\n'/'\r'/whitespace is tolerated). Returns MPS3_JSON_OK or
 * MPS3_JSON_EMALFORMED — on failure *out is not to be used. */
int mps3_json_parse(const char *line, int len, mps3_json_obj_t *out);

/* Typed extraction by key (key order in the line is irrelevant, per JSON).
 * get_string NUL-terminates `out` (and unescapes — see escape policy);
 * on ANY error `out` is set to "" (fail closed, never partial content). */
int mps3_json_get_string(const mps3_json_obj_t *obj, const char *key,
                         char *out, int out_sz);
int mps3_json_get_int(const mps3_json_obj_t *obj, const char *key, int32_t *out);
int mps3_json_get_bool(const mps3_json_obj_t *obj, const char *key, int *out);

/* v0.17 (ADDITIVE; mps3_json_parse() is unchanged and still rejects nesting).
 * mps3_json_parse_ex(): the same tokenizer and guarantees, with two options:
 *   only           NULL-terminated key list, or NULL. With a list, a member whose
 *                  key is not listed is VALIDATED but not stored (so it neither
 *                  counts toward MPS3_JSON_MAX_MEMBERS nor is dup-checked):
 *                  "unknown extra keys are ignored" for a request with more keys
 *                  than the member table. NULL = store every member (the bound
 *                  applies, as in mps3_json_parse()).
 *   allow_objects  1 = a value may be ONE flat object (MPS3_JSON_T_OBJECT, its
 *                  own members scalar only); deeper nesting and arrays are still
 *                  rejected. 0 = reject, as mps3_json_parse().
 * Used by the decoder (op sniff of a nested `hello`) and by the Linux harness's
 * `hello` provider. mps3_json_get_object() re-parses such a member FLAT into
 * *sub (EMISSING / ETYPE otherwise). */
int mps3_json_parse_ex(const char *line, int len, mps3_json_obj_t *out,
                       const char *const *only, int allow_objects);
int mps3_json_get_object(const mps3_json_obj_t *obj, const char *key, mps3_json_obj_t *sub);

/* --------------------------------------------------------------------------
 * Verb-level decode / per-op response encode.
 * -------------------------------------------------------------------------- */
enum {
    MPS3_CTRL_DECODE_OK          = 0,
    MPS3_CTRL_DECODE_EBADJSON    = -1, /* line failed mps3_json_parse() */
    MPS3_CTRL_DECODE_EUNKNOWN_OP = -2, /* "op" missing / not a string / not a known verb */
    MPS3_CTRL_DECODE_EBADARGS    = -3, /* a required argument is missing, wrong-typed, or oversized */
};

/* Recommended (and tested) minimum buffer for one encoded response line.
 * Worst case is STILL the diag shape, and it is the WHOLE mailbox: 44 u32
 * counters at max width (diag v9). Exactly:
 *     {"ok":true                                        10 B
 *     + per counter  ,"<key>":<10 digits>                len(key) + 14 B
 *     + }\n                                              2 B
 *     + the NUL snprintf() writes                         1 B
 *   sum(len(key)) over the 44 keys = 455, so 10 + 455 + 44*14 + 2 + 1 = 1084 B.
 *
 * HISTORY OF THIS BOUND, because each step was a near-miss:
 *     14 counters, sum(len(key))=116 ->  325 B  (bound 384)
 *     25 counters, sum(len(key))=274 ->  637 B  (bound 768, v0.9)
 *     29 counters, sum(len(key))=319 ->  738 B  (v0.9.1 touch: still 768,
 *                                                with 30 B to spare)
 *     42 counters, sum(len(key))=439 -> 1040 B  (v0.9.2 diag v8's thirteen
 *                                                service-telemetry keys)
 *     44 counters, sum(len(key))=455 -> 1084 B  (v0.13 diag v9: svc_us_6 +
 *                                                usd_boot; the `usd` reply's
 *                                                worst case is far below it)
 * At 768 the v0.9.2 verb does NOT truncate -- mps3_ctrl_encode_response()
 * returns -1 and the shell stops answering `diag` AT ALL, which is how this
 * was found rather than argued: firmware/test/test_net_proto_json.c encodes an
 * all-0xFFFFFFFF diag response through the REAL encoder and asserts the length,
 * and it went red at `worst > 0`. 1280 restores the same ~23% head-room 768
 * gave 637. `version` (v0.8) is the second-longest at ~270 bytes worst case
 * (FOUR 15-char identity strings escaped to 30 each -- harness, ver32, sha and
 * usr_access -- a max-width lmb_kb, the "skew" verdict, and all five feature
 * names) and so does not move this bound. The Wave-B usr_access/skew pair adds
 * ~40 bytes to it; 1280 still leaves `diag`, the binding case at 1084 (v9), its
 * head-room.
 *
 * `dutrx` (v0.10) DOES NOT MOVE THIS BOUND EITHER, and that is a design
 * constraint rather than an observation: its 256-byte chunk puts its worst case
 * at 694 B (the arithmetic is at MPS3_DUTRX_CHUNK_MAX). A 512-byte chunk would
 * have reached 1206 and made a frame chunk -- not the counter mailbox -- the
 * thing this buffer is sized for, so the next diag counter would be competing
 * with it. test_net_proto_json.c asserts `dutrx worst < diag worst` directly,
 * so the ordering cannot rot into a comment.
 *
 * IT COSTS STACK: coordinator_net_poll() has two `char line[MPS3_CTRL_RESP_MAX]`
 * scopes. firmware/platform/lscript.ld.in's _STACK_SIZE went 0x1000 -> 0x2000
 * in the same change; the arithmetic is in that file's comment.
 *
 * A smaller buffer makes mps3_ctrl_encode_response() fail rather than emit a
 * truncated (= malformed) JSON line. */
#define MPS3_CTRL_RESP_MAX 1280

/* Decode one request line into *out (zeroed first; unused fields stay "").
 * Required arguments per verb (net-protocol.md's examples are normative):
 * reset->target, set_clk->preset, swap->rm+src, link->event,
 * commit (v0.13) -> rm, src, rm_id, static_id, clear_len, clear_crc, part_len,
 * part_crc (ids/CRCs as "0x"+1..8 hex digits, lengths as non-negative ints; the
 * v0.11 rm-only form is bad args), usd (v0.13) -> OPTIONAL action + confirm
 * strings (their VALUES are the handler's to judge),
 * macgen->gen+chk+inject (gen/chk are JSON bools; inject a fault-name string),
 * display->owner (a string: "dut"/"harness"/"toggle"/"query").
 * ping/telemetry/diag/version/dutrx/stats/reboot take NO arguments.
 * v0.11: log takes an OPTIONAL int `off` (>= 0; absent = 0 = "from the oldest
 * retained byte"); touch_cal REQUIRES a string `act`, and act "set" REQUIRES all
 * seven ints ax,bx,cx,ay,by,cy,shift (range checks are the handler's).
 * v0.14: slot REQUIRES a string `act` and takes an OPTIONAL string `slot`
 * (<= 7 chars; its value, like act's, is the handler's to check).
 * v0.16: identity / identity_set / locate take their arguments through the
 * ENGINE provider (mps3_identity_op() / mps3_locate_op(), coordinator.h): the
 * decoder only sets out->line/line_len; any flat JSON object decodes.
 * v0.17: hello / panel likewise (mps3_hello_op() / mps3_panel_op()); a `hello`
 * line whose values nest one level (lease, job) also decodes -- the ONLY
 * nested request: any other line that fails the flat parse is still EBADJSON.
 * Unknown EXTRA keys are ignored (JSON tolerance); `swap.src` is required
 * because pyverify always sends it — flag for A6 if it should instead
 * default to "tftp" server-side. Returns MPS3_CTRL_DECODE_*. */
int mps3_ctrl_decode_line(const char *line, int len, mps3_ctrl_request_t *out);

/* Encode *resp as the per-op response line net-protocol.md specifies,
 * newline-terminated, into out (always NUL-terminated when out_len > 0):
 *   any op, !ok:  {"ok":false,"err":"<reason>"}
 *   ping:         {"ok":true,"shell_id":"0x…","rm_id":"0x…"}
 *   reset/link:   {"ok":true}
 *   set_clk:      {"ok":true,"locked":true|false}
 *   swap:         {"ok":true,"rm_id":"0x…","verified":true|false}
 *   commit:       {"ok":true,"slot":"A"|"B"}
 *   usd:          status  {"ok":true,"present":B,"state":"..","text":".."
 *                          [,"card_mb":N][,"default":{"rm_id":"0x..","static_id":"0x..",
 *                          "slot":"A"}],"boot":".."}
 *                 action  {"ok":true,"state":".."}   (mps3_usd_t.action_reply)
 *                 -- v0.13. card_mb only while a card is READY, default only in
 *                 state valid/stale. The ONE nested object in a response.
 *   display:      {"ok":true,"owner":"harness"|"dut"}   (CLCDKVM.STATUS.owner)
 *                 -- on a build WITHOUT the CLCD KVM slave (MPS3_HAS_CLCD_KVM
 *                 off) the handler returns the uniform failure line
 *                 {"ok":false,"err":"clcd_kvm not present"} instead (the verb
 *                 decodes fine; there is simply no 0x44AD slave to drive).
 *   telemetry:    {"ok":false,"err":"no power sensor","lockup":true|false}
 *                 -- ALWAYS ok:false. See the TELEMETRY note below.
 *   macgen:       {"ok":true,"tx":<u32>,"rx":<u32>,"err":<u32>}
 *   version:      {"ok":true,"harness":"1.0.0","ver32":"0x01000001","sha":"5c09de10",
 *                  "dirty":0,"lmb_kb":1024,"features":["clcd","clcd_kvm","touch",
 *                  "hwicap_fifo","windowed"],
 *                  "usr_access":"0x01000001","skew":false[,"id_skew":"..."][,"impl":"linux"]}
 *                 -- "impl" (additive, v0.11) is the ENGINE, and is emitted
 *                 ONLY when mps3_proto_impl() returns a non-empty string, as
 *                 the LAST key. The weak default returns NULL, so a bare-metal
 *                 image's line is byte-identical to the pre-`impl` one.
 *                 -- "features" is a JSON ARRAY of names in the FIXED
 *                 MPS3_FEATURE_* bit order, absent features OMITTED (an image
 *                 with no feature flags emits []). It is the ONE response shape
 *                 in this protocol carrying an array; the flat-object REQUEST
 *                 tokenizer is untouched (requests still have no arrays).
 *                 -- "usr_access" is what the FABRIC reports (the bitstream's
 *                 USR_ACCESS/AXSS register), and "skew" is the codec's own
 *                 verdict on usr_access vs ver32. THREE states, not two:
 *                     "skew":false  the image and the bitstream agree
 *                     "skew":true   they do NOT -- the .bit and the image baked
 *                                   into it are from different builds
 *                     "usr_access":null,"skew":null
 *                                   the fabric value could not be read, so NO
 *                                   comparison was made. This is not a pass.
 *                 The verdict is DERIVED here, never carried on the wire from
 *                 somewhere else, so no caller can report "agree" without the
 *                 two values that agree.
 *   diag:         {"ok":true,"rx_recover":<u32>,"rx_dumps":<u32>,"rx_drops":<u32>,
 *                  "icap_bytes":<u32>,"got":<u32>,"expect":<u32>,"rcv_wnd":<u32>,
 *                  "rcv_ann_wnd":<u32>,"rx_queued":<u32>,"pbuf_free":<u32>,
 *                  "grants_sent":<u32>,"grant_fails":<u32>,"sndbuf":<u32>,"snd_wnd":<u32>,
 *                  "tx_frames_sent":<u32>,"tx_status_drained":<u32>,
 *                  "tx_fifo_full_drops":<u32>,"tx_errors":<u32>,
 *                  "tx_space_stalls":<u32>,"tx_iface_errors":<u32>,
 *                  "tx_last_status":<u32>,"icap_sr_last":<u32>,
 *                  "icap_eos_status":<u32>,"ovlstore_phase":<u32>,
 *                  "ovlstore_detail":<u32>}
 *                 -- EVERY COUNT row of MPS3_DIAG_FIELDS (firmware/common/diag.h),
 *                 in mailbox word order; the key spellings come from that list
 *                 (which is why "grants_sent" carries win_windows_drained and
 *                 "grant_fails" carries win_grant_send_fails — frozen keys).
 *                 The first 14 keys and their order are UNCHANGED; the eleven
 *                 JTAG-only counters are APPENDED, so an old client that reads
 *                 by key is unaffected.
 *   dutrx:        {"ok":true,"len":<u16>,"off":<u16>,"n":<u16>,"more":<bool>,
 *                  "last":<bool>,"frames":<u16>,"rx":<u32>,"drop_full":<u32>,
 *                  "drop_giant":<u32>,"ovf":<bool>,"desync":<bool>,
 *                  "data":"<2*n lowercase hex chars>"}
 *                 -- ONE CHUNK (<= MPS3_DUTRX_CHUNK_MAX bytes) of the frame at
 *                 the head of the DUT-egress FIFO. The key set is the SAME when
 *                 there is no frame (len/off/n = 0, data ""), so a client never
 *                 branches on shape -- only on `n`/`more`. Bytes are hex, not
 *                 base64: this protocol is plain-ASCII JSON with a hand-rolled
 *                 firmware tokenizer, and hex costs 512 chars for a full chunk
 *                 inside a budget that has room for it (see
 *                 MPS3_DUTRX_CHUNK_MAX). Frames INCLUDE their 4-byte FCS
 *                 (link_partner_mac's AXIS convention).
 *                 -- on a build WITHOUT the DUTEGR slave (MPS3_HAS_DUT_EGRESS
 *                 unset -- today's fielded bitstream, which was minted before
 *                 the block existed) the handler returns the uniform failure
 *                 line {"ok":false,"err":"dut_egress not present"}, exactly like
 *                 `display` on a KVM-less shell. It must NOT answer ok:true with
 *                 zeroes there: "this fabric has no capture block" and "the DUT
 *                 sent nothing" would be indistinguishable, which is the
 *                 reading-shaped lie v0.6 removed from `telemetry`.
 *   stats:        {"ok":true,"up_ms":N,"sid":"0x…","rm":"0x…","rm_ok":B,"lock":B,
 *                  "clk_sel":N,"mmcm":B,"clk_alive":B,"dut_rst":B,"rp_rst":B,
 *                  "decpl":B,"link":B,"spd":N,"fdx":B,"mac":"<12 hex>",
 *                  "swap":"idle",...,"swap_ok":B,"swap_n":N,"icap":N,"rxdrop":N,
 *                  "txerr":N,"swap_err":"…","clr_ok":B,"dut_mhz":N,
 *                  "svc_max_us":N,"svc_skipped":N}
 *                 -- v0.11. The first 22 keys are fpgahub's _from_stats_verb
 *                 shape EXACTLY, in that order (up_ms first after ok); the last
 *                 five are additive. NO power keys, ever. A TOUCH=1 build then
 *                 appends ,"touch_ok":B,"touch_bus_lost":N,"touch_recoveries":N
 *                 (2026-09-24, D1), still before the Linux os_up_ms.
 *   log:          {"ok":true,"off":N,"n":N,"more":B,"dropped":N,"data":"<hex>"}
 *                 -- v0.11. One chunk (<= 256 bytes) of the console ring from
 *                 stream offset `off`; same chunking + hex as dutrx.
 *   touch_cal:    get/set/default -> {"ok":true,"ax":N,"bx":N,"cx":N,"ay":N,
 *                  "by":N,"cy":N,"shift":N}
 *                 raw -> {"ok":true,"raw_x":N,"raw_y":N,"raw_z":N,"seen":N,
 *                  "x":N,"y":N}
 *                 -- v0.11. On a build without TOUCH=1 the handler declines:
 *                 {"ok":false,"err":"touch not present"}.
 *   reboot:       {"ok":true,"in_ms":N}
 *                 -- v0.11. Sent BEFORE the watchdog is armed; N is an upper
 *                 bound. Refused mid-swap: {"ok":false,"err":"EBUSY"}.
 *   slot:         {"ok":true,"card":true,"fabric_sid":"0x…","running":"A",
 *                  "default":"A","seq":N,"target":"B","staged":null,
 *                  "a":{"state":"valid","hdr_crc":"0x…","len":N,["sid":"0x…",]
 *                       "verified":"boot"},
 *                  "b":{"state":"empty","verified":"no"},
 *                  "job":{"act":"none","slot":null,"state":"idle","got":0,
 *                         "len":0,"err":""},"claimed":false,"confirmed":true}
 *                 -- v0.14, the SAME shape for every act (the state after it).
 *                 With card=0: {"ok":true,"card":false,"fabric_sid":…,
 *                 "running":…,"staged":…,"job":{…},"claimed":…,"confirmed":…}
 *                 (`claimed`/`confirmed` APPENDED 2026-09-26; a failed job adds
 *                 ,"code":"<job code>" after its "err" when the provider sets one;
 *                 absence is not an error
 *                 for `status`; every other act declines "no card"). Per slot,
 *                 `err` appears only for bad/io, `hdr_crc`+`len` only for
 *                 valid, `sid` only when a slot record binds the image. The
 *                 two nested objects are the protocol's first in a 6900 reply
 *                 (identify's are on 6899); requests stay flat.
 *   identity:     {"ok":true,"op":"identity",<body>}
 *   identity_set: {"ok":true,"op":"identity_set",<body>}
 *   locate:       {"ok":true,"op":"locate",<body>}   (body "until_ms":N)
 *   hello:        {"ok":true,"op":"hello",<body>}    (v0.17: sessions, panel, events)
 *   panel:        {"ok":true,"op":"panel",<body>}    (v0.17: the state, a frame
 *                 half, or the page set -- net-protocol.md "Presence and the panel")
 *                 -- v0.16 ENGINE verbs: <body> is resp->body, rendered by the
 *                 engine's provider (net-protocol.md "Identity"); the codec
 *                 adds only the braces, "ok" and "op". A bare-metal image
 *                 declines both: {"ok":false,"err":"identity not supported",
 *                 "code":"not_supported"} ("locate not supported" likewise).
 * NOTE: macgen's success "err" is the GENCHK.ERR_CNT *counter* (an integer);
 * on failure the same "err" key carries the diagnostic *string* (uniform
 * failure shape) — the key is polymorphic by "ok", so clients key on "ok"
 * (net-protocol.md "MAC gen/checker control").
 *
 * TELEMETRY -- the one verb with NO success shape. It is the sole exception
 * to the uniform "{"ok":false,"err":"..."} carries no per-op fields" rule,
 * because its two halves have different fates:
 *
 *   - POWER (mv/ma): there is NO power sensor reachable from this design, by
 *     any path -- the TELEM block's data inputs are tied to GROUND in the
 *     block design (shell_bd.tcl gnd_ina228_32 -> ina228_bus_mv_i /
 *     curr_ua_i / power_mw_i), its INA228 I2C engine was never written
 *     (ip/telem/README.md "SEAMED ... NOT yet written"), the I2C pads are not
 *     even on the top level (shell_top.sv: the INA228 bus is MCC-owned on
 *     MPS3), and the MPS3 MCC console itself refuses `CFG R V <dev>` on every
 *     device. So TELEM.BUS_MV / TELEM.CURR_UA read 0 always and forever. The
 *     old handler shipped those zeroes as {"mv":0,"ma":0} -- a reading-shaped
 *     lie, the worst kind of telemetry. The verb now FAILS LOUDLY: ok:false +
 *     err "no power sensor", and the mv/ma keys are ABSENT, not zeroed.
 *   - LOCKUP: a real partition pin (DFXCTL.RM_STATUS.dut_lockup) with a real
 *     value. It stays on the wire, in the failure line, raw and unfiltered --
 *     the shell reports the pin and does NOT editorialise. Whether the pin
 *     MEANS anything is per-RM (several RM wrappers hard-tie it to 0); that
 *     judgement belongs to the host's RM catalogue, not here. Encoding it
 *     into an ok:false line is deliberate: pyverify reads it with
 *     resp.get("lockup") regardless of "ok", so no lockup consumer is lost.
 *
 * Returns bytes written (excluding NUL), or <0 if the encoded line would
 * not fit / *resp is inconsistent (e.g. ok commit without a valid slot, or
 * an ok=true telemetry -- there is no such thing) — on error out is "" (a
 * truncated JSON line is never emitted). */
int mps3_ctrl_encode_response(const mps3_ctrl_response_t *resp, char *out, int out_len);

/* The `version.impl` seam (net-protocol.md "version", v0.11 additive): which
 * ENGINE is serving this protocol. net_proto.c carries a WEAK default returning
 * NULL, which means "do not emit the key" -- so a bare-metal image's `version`
 * reply is byte-identical to the one before `impl` existed. The MicroBlaze V
 * Linux harness (src/linux_harness/sw/harnessd/version_linux.c) provides the
 * strong override returning "linux". At most 12 characters are emitted.
 * firmware/identify/ reports the same value under the same rule. */
const char *mps3_proto_impl(void);

/* `version.id_skew` (v0.11 additive): why this engine's IDENTITY sources
 * disagree -- the Linux harness reports the fabric-bound static_id (stage0's
 * status block) and refuses swaps when the image's claim, or the fabric's
 * USR_ACCESS vs the image manifest, disagrees; this is the reason string. WEAK
 * default NULL => the key is not emitted. Emitted after "skew", before "impl";
 * at most 60 characters. */
const char *mps3_proto_id_skew(void);

/* `stats` key identities, in EMISSION ORDER, for the stats omit mask below. The
 * ORDER is wire contract (fpgahub's _from_stats_verb shape first); these are
 * bit numbers, not offsets. Append only. */
enum {
    MPS3_STATS_K_UP_MS = 0, MPS3_STATS_K_SID, MPS3_STATS_K_RM, MPS3_STATS_K_RM_OK,
    MPS3_STATS_K_LOCK, MPS3_STATS_K_CLK_SEL, MPS3_STATS_K_MMCM,
    MPS3_STATS_K_CLK_ALIVE, MPS3_STATS_K_DUT_RST, MPS3_STATS_K_RP_RST,
    MPS3_STATS_K_DECPL, MPS3_STATS_K_LINK, MPS3_STATS_K_SPD, MPS3_STATS_K_FDX,
    MPS3_STATS_K_MAC, MPS3_STATS_K_SWAP, MPS3_STATS_K_SWAP_OK,
    MPS3_STATS_K_SWAP_N, MPS3_STATS_K_ICAP, MPS3_STATS_K_RXDROP,
    MPS3_STATS_K_TXERR, MPS3_STATS_K_SWAP_ERR, MPS3_STATS_K_CLR_OK,
    MPS3_STATS_K_DUT_MHZ, MPS3_STATS_K_SVC_MAX_US, MPS3_STATS_K_SVC_SKIPPED,
    MPS3_STATS_K_OS_UP_MS,          /* Linux harness only; see mps3_proto_os_up_ms() */
    /* 2026-09-24 (D1), TOUCH=1 builds only. The BIT numbers are appended (the
     * rule above); on the WIRE the three are emitted after svc_skipped and
     * BEFORE os_up_ms, which stays the last key (net-protocol.md "Stats"). */
    MPS3_STATS_K_TOUCH_OK, MPS3_STATS_K_TOUCH_BUS_LOST, MPS3_STATS_K_TOUCH_RECOVERIES,
    /* v0.15 (Linux harness, LCD mirror): the bit is appended; on the WIRE it is
     * emitted after the touch keys and BEFORE os_up_ms, which stays last. */
    MPS3_STATS_K_LCD_MIRROR,
    MPS3_STATS_K_COUNT
};

/* THE OMIT SEAMS (net-protocol.md "diag" / "stats", v0.11 additive). A key an
 * engine CANNOT fill is left off the line instead of being reported as 0 -- a
 * plausible zero is indistinguishable from a measured one (the same reasoning
 * that took mv/ma out of `telemetry`). Bit i of the diag mask is diag.h's
 * MPS3_DIAG_IX_<name>; bit i of the stats mask is MPS3_STATS_K_*. net_proto.c's
 * WEAK defaults return 0, so a bare-metal image omits nothing and its lines are
 * byte-identical to the pre-mask ones. The Linux harness overrides both
 * (src/linux_harness/sw/harnessd/version_linux.c) and lists what it omits in
 * docs/planning/linux_lanes/HARNESSD_CONTRACT.md. */
uint64_t mps3_proto_diag_omit(void);
uint32_t mps3_proto_stats_omit(void);

/* The OS uptime in ms for `stats.os_up_ms` (appended LAST, after svc_skipped)
 * and `identify.os_up_ms`. Returns 1 and fills *out when there is an OS beneath
 * the shell (Linux: /proc/uptime); the WEAK default returns 0 and the key is not
 * emitted. `up_ms` stays the shell's own uptime on both engines. */
int mps3_proto_os_up_ms(uint32_t *out);

/* ---- v0.15 ENGINE seams (net-protocol.md v0.15: the Linux harness's LCD mirror).
 * All three are WEAK in net_proto.c and return "nothing", so a bare-metal image
 * emits none of these keys or names and its bytes are identical for EVERY
 * response (proven by harnessd/tests/test_codec_identity.py, head == new).
 *
 * mps3_proto_features_extra(): ENGINE feature names appended to
 *   `version.features` AFTER the MPS3_FEATURE_* bit names, in list order. They
 *   spend no bit: a feature only one engine can have (a service the Linux
 *   harness runs beside the firmware modules) does not belong in the shared
 *   compile-time bit table, and appending names cannot collide with another
 *   lane's appended bit. NULL-terminated; a name is emitted only if it is 1..24
 *   chars of [a-z0-9_]; at most 8 are read. Clients test names, never positions.
 * mps3_proto_lcd_mirror_info(): `version.lcd_mirror` =
 *   {"port":N,"mode":"hw"|"sw","proto":N}, emitted after `id_skew` and before
 *   `impl` (which stays last). Return 0 = no key.
 * mps3_proto_lcd_mirror_stats(): `stats.lcd_mirror` =
 *   {"peer":"a.b.c.d:port"|null,"since":<up_ms>,"fps":<d.d>,"bytes":<u32>},
 *   emitted after the touch keys and before `os_up_ms` (which stays last).
 *   peer "" renders null. Return 0 = no key. */
const char *const *mps3_proto_features_extra(void);

typedef struct {
    uint16_t port;
    uint8_t  proto;
    char     mode[4];      /* "hw" | "sw" */
} mps3_lcd_mirror_info_t;
int mps3_proto_lcd_mirror_info(mps3_lcd_mirror_info_t *out);

typedef struct {
    char     peer[32];     /* "" = no client (renders null)                   */
    uint32_t since_ms;     /* stats.up_ms at which that client connected       */
    uint32_t fps_x10;      /* UPDATEs carrying tiles per second, x10           */
    uint32_t bytes;        /* bytes sent to that client                        */
} mps3_lcd_mirror_stats_t;
int mps3_proto_lcd_mirror_stats(mps3_lcd_mirror_stats_t *out);

/* ==========================================================================
 * Bitstream framing (TFTP port 69 / raw push TCP 6910)
 * ==========================================================================
 * net-protocol.md:
 *   magic "MPS3" | u16 ver | u8 kind(0=clearing,1=partial) | u8 rm_slot
 *   u32 static_id | u32 rm_id | u32 len_words | u32 crc32(payload)
 * followed immediately by `len_words` 32-bit ICAP-ordered words of payload.
 * A torn/mismatched transfer must be rejected BEFORE any ICAP write — see
 * config_agent/config_agent.c `config_agent_validate_header()`.
 */
#define MPS3_BITSTREAM_MAGIC "MPS3"   /* 4 raw bytes on the wire, not a C string */
#define MPS3_BITSTREAM_VER   1u

typedef enum {
    MPS3_BIN_KIND_CLEARING = 0,
    MPS3_BIN_KIND_PARTIAL  = 1,
    /* v0.14 (Linux harness, plan §10a S10): the payload is a stage0 S0LB v2 boot
     * image for the INACTIVE user-microSD slot, not a bitstream. It never goes
     * near the staging slots, the sinks above or the ICAP: config_agent routes it
     * to the engine's mps3_cfg_slot_sink() provider (config_agent.h), and an
     * engine without one (every bare-metal image) refuses it exactly as it
     * refused an unknown kind before. `rm_slot` carries the pusher's expected
     * target (0 = whichever is inactive, 1 = A, 2 = B), `rm_id` is 0, and the
     * payload is the image zero-padded to a whole word. */
    MPS3_BIN_KIND_SLOT_IMAGE = 2,
} mps3_bin_kind_t;

typedef struct __attribute__((packed)) {
    char     magic[4];      /* "MPS3", not NUL-terminated */
    uint16_t ver;
    uint8_t  kind;           /* mps3_bin_kind_t */
    uint8_t  rm_slot;        /* overlay-manifest.md A/B slot hint, if pushed pre-targeted */
    uint32_t static_id;      /* must match the running shell's static_id */
    uint32_t rm_id;           /* target RM id (for a partial) / cleared RM id (for clearing) */
    uint32_t len_words;       /* payload length, 32-bit ICAP words */
    uint32_t crc32;           /* crc32(payload), payload = len_words * 4 bytes following this header */
} mps3_bitstream_hdr_t;

/* Wire size of the header above (24 bytes: 4+2+1+1+4+4+4+4) -- fixed
 * regardless of host struct padding, since mps3_bitstream_hdr_pack()/
 * _unpack() below serialize field-by-field rather than relying on
 * sizeof(mps3_bitstream_hdr_t)/memcpy. */
#define MPS3_BITSTREAM_HDR_WIRE_SIZE 24u

/* Pack/unpack the ACTUAL wire bytes, BIG-ENDIAN (matches host/pusher/
 * push.py's real, independently-tested `struct.Struct(">4sHBBIIII")`
 * framing -- keep these two in lockstep; push.py is the reference this
 * mirrors). mps3_bitstream_hdr_t itself is treated as an in-memory,
 * native-layout convenience struct (what config_agent.c stages the
 * already-parsed header fields into); these two functions are the only
 * place that cares about the actual byte order on the wire, so they work
 * correctly regardless of the host's own endianness -- including this
 * repo's host-gcc (little-endian x86) unit tests, see
 * firmware/test/test_bitstream_header.c, as well as the big-endian-default
 * MicroBlaze target.
 *
 * mps3_bitstream_hdr_unpack() validates the magic before filling *out* (a
 * malformed/torn header must be rejected before any ICAP write --
 * net-protocol.md's ordering requirement, see config_agent.c). Returns 0 on
 * a structurally sound header (magic + enough bytes), <0 otherwise; it does
 * NOT check ver/static_id/kind -- that's config_agent_validate_header()'s
 * job, one layer up, so this stays pure wire-format logic only. */
void mps3_bitstream_hdr_pack(const mps3_bitstream_hdr_t *hdr,
                              uint8_t out[MPS3_BITSTREAM_HDR_WIRE_SIZE]);
int mps3_bitstream_hdr_unpack(const uint8_t *in, uint32_t in_len,
                                mps3_bitstream_hdr_t *out);

/* I12 (len units): payload length in bytes implied by a header's
 * `len_words` (net-protocol.md: "len_words here = payload bytes / 4 ...
 * len_words = len/4"). Single source of truth for the *4 so it isn't
 * repeated/mistyped at each call site (config_agent.c, overlay_store.c). */
static inline uint32_t mps3_bitstream_payload_bytes(const mps3_bitstream_hdr_t *hdr)
{
    return hdr->len_words * 4u;
}

/* ==========================================================================
 * SWD channel (TCP 6920) — OpenOCD remote_bitbang byte protocol.
 * ==========================================================================
 * BIT ORDER CONFIRMED (2026-07-09, SWD half of I22) — no longer "assumed".
 * Verified against the authoritative implementation, OpenOCD
 * src/jtag/drivers/remote_bitbang.c:
 *
 *     static int remote_bitbang_swd_write(int swclk, int swdio)
 *     {   char c = 'd' + ((swclk ? 0x2 : 0x0) | (swdio ? 0x1 : 0x0));
 *
 *     static int remote_bitbang_reset(int trst, int srst)
 *     {   char c = 'r' + ((trst ? 0x2 : 0x0) | (srst ? 0x1 : 0x0));
 *
 *     static enum bb_value char_to_int(int c)   // the reply to 'c'
 *     {   case '0': return BB_LOW;  case '1': return BB_HIGH;
 *         default: remote_bitbang_quit(); ... return BB_ERROR;
 *
 * So: CLK is bit1 and DIO is bit0 of (c - 'd'); TRST is bit1 and SRST is bit0
 * of (c - 'r'); and a sample reply MUST be ASCII '0'/'1' (0x30/0x31) — any
 * other byte makes OpenOCD log an error and close the connection.
 *
 * This did NOT need a board: the encoding is fixed by the host driver, not by
 * our wiring. `firmware/test/test_swd_server.c`'s
 * test_openocd_remote_bitbang_conformance() re-derives every byte from the
 * formulas above, so an inverted mapping fails there, not at bring-up.
 * (The RMII RXD/TXD half of I22 remains open — that one IS a wiring question.)
 */
typedef enum {
    MPS3_BITBANG_SWDIO_DRIVE   = 'O',
    MPS3_BITBANG_SWDIO_RELEASE = 'o',
    MPS3_BITBANG_SWDIO_SAMPLE  = 'c',  /* reply: one ASCII '0'/'1' byte */
    MPS3_BITBANG_CLK0_DIO0     = 'd',  /* 'd' + (swclk<<1 | swdio) */
    MPS3_BITBANG_CLK0_DIO1     = 'e',
    MPS3_BITBANG_CLK1_DIO0     = 'f',
    MPS3_BITBANG_CLK1_DIO1     = 'g',
    MPS3_BITBANG_RESET_00      = 'r', /* {trst,srst} = {0,0} — CONFIRMED */
    MPS3_BITBANG_RESET_01      = 's', /* {trst,srst} = {0,1} — CONFIRMED */
    MPS3_BITBANG_RESET_10      = 't', /* {trst,srst} = {1,0} — CONFIRMED */
    MPS3_BITBANG_RESET_11      = 'u', /* {trst,srst} = {1,1} — CONFIRMED */
    MPS3_BITBANG_LED_ON        = 'B',
    MPS3_BITBANG_LED_OFF       = 'b',
    MPS3_BITBANG_QUIT          = 'Q',
} mps3_bitbang_char_t;

#ifdef __cplusplus
}
#endif

#endif /* MPS3_NET_PROTO_H */
