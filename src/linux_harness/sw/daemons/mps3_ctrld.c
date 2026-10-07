/*
 * mps3_ctrld — the :6900 JSON control daemon (Linux harness skeleton).
 *
 * Implements the FROZEN wire contract of SERVICE_DISPOSITION.md §2 + §2A
 * with the hardware operations STUBBED (this is the daemon skeleton for
 * the shell_linux transplant; the DFX/UIO backends land with their own
 * waves). Connection-level obligations implemented here, each traceable:
 *
 *  §2A.1  SINGLE client; every extra accept()ed connection is closed
 *         IMMEDIATELY with ZERO bytes (accept-then-EOF). Never a reply
 *         line to a refused client; never stop listening. fpgahub's
 *         busy/offline/wedged triage depends on this exact TCP shape
 *         (fpgahub/src/fpgahub/shell_client.py docstring + read path).
 *  §2A.2  Unknown verb -> {"ok":false,"err":"unknown op"}\n and the
 *         connection STAYS OPEN (fpgahub probes {"op":"stats"} and
 *         latches on ok:false as "not supported yet").
 *  §2A.4  TCP_NODELAY on every accepted 6900 socket.
 *  §2     One flat JSON object per '\n'-terminated line, request->response,
 *         send-now for every verb EXCEPT swap, whose response is HELD (the
 *         client connection is parked, no interim bytes, no server timeout)
 *         until the swap FSM settles.
 *  §2A.5  Baseline decline shapes (no slave exists on the declared
 *         baseline; a blind poke under the MBV is a real S-mode fault):
 *           link   -> {"ok":false,"err":"vphy not present"}
 *           macgen -> {"ok":false,"err":"genchk not present"}
 *           commit -> {"ok":false,"err":"commit failed"}
 *         telemetry is ALWAYS {"ok":false,"err":"no power sensor",
 *         "lockup":<bool>} — deliberate, do not "fix".
 *
 * Handler-level semantics (arg validation order, error strings) mirror
 * firmware/coordinator/coordinator.c verbatim.
 *
 * Backends (M3): `swap` is REAL — it forks a swap worker (swap_worker.c)
 * that drives /dev/mps3dfx through the frozen ioctl sequence against the
 * staged pair mps3-pushd publishes into the shared spool, then delivers
 * the HELD response from the worker's outcome:
 *     success -> {"ok":true,"rm_id":"0x…","verified":true}
 *     failure -> {"ok":false,"err":"swap failed"}   (uniform bare-metal shape)
 * The fork keeps this loop servicing §2A.1 refusals during the seconds a
 * real swap takes; a swap request while one is in flight answers the
 * bare-metal "swap already in progress" immediately (coordinator.c). A
 * host with no /dev/mps3dfx fails fast in the worker, so the held reply
 * still settles at the stub cadence and the wire suite is unchanged.
 * diag returns the frozen 14 keys; icap_bytes is read honestly from the
 * driver's sysfs (0 when the driver is absent).
 * reset/set_clk/display remain stubs (their UIO/clk backends are separate
 * waves).
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "proto.h"
#include "spool.h"
#include "swap_worker.h"

/* ---- configuration ----------------------------------------------------- */
static uint32_t g_static_id  = 0x00000000u;  /* "not provisioned" weak default */
static uint32_t g_current_rm = 0x00000000u;  /* last VERIFIED rm_id (greybox=0) */
static int      g_port       = MPS3_PORT_CONTROL;
static int      g_clcdkvm_present = 1;       /* declared baseline: clcd_kvm_0 IS
                                              * instantiated (shell_linux_bd) */
static int      g_swap_settle_ms  = 150;     /* minimum held-reply latency: the
                                              * wire suite pins "no bytes in the
                                              * first 50 ms" as the no-interim
                                              * proof, so even an instant worker
                                              * failure settles no earlier */
static const char *g_spool_dir = "/run/mps3";
static const char *g_dev_path  = "/dev/mps3dfx";
static const char *g_sysfs_icap_bytes = "/sys/class/misc/mps3dfx/icap_bytes";
static const char *g_sysfs_rm_id      = "/sys/class/misc/mps3dfx/rm_id";
static uint32_t g_await_ms  = 30000;         /* staged-pair RX-idle bound */
static int      g_mock_mode = 0;             /* -M: configure the driver mock */
static int      g_mock_force = 0;            /* -W: mock presents a forced id */
static uint32_t g_mock_force_rm = 0;

/* ---- stub hardware state ------------------------------------------------ */
static int g_kvm_owner_dut = 0;              /* 0=harness (boot default), 1=dut */
static int g_lockup = 0;                     /* DFXCTL.RM_STATUS.lockup stub    */

/* ---- connection state ---------------------------------------------------- */
static int  g_listen_fd = -1;
static int  g_conn_fd   = -1;
static char g_line[MPS3_CTRL_LINE_MAX];
static int  g_line_len;
static int  g_line_overflow;

static int    g_swap_parked;                 /* holding the swap response */
static struct timespec g_swap_deadline;

/* swap worker (forked child + result pipe) */
static pid_t          g_worker_pid = -1;
static int            g_worker_fd  = -1;     /* read end of the result pipe */
static swap_outcome_t g_outcome;
static size_t         g_outcome_got;
static int            g_outcome_ready;       /* worker settled, result valid */

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* ---- verb handlers (stub backends) -------------------------------------- */

static void set_err(mps3_ctrl_response_t *resp, const char *msg)
{
    resp->ok = 0;
    strncpy(resp->err, msg, sizeof(resp->err) - 1);
    resp->err[sizeof(resp->err) - 1] = '\0';
}

static void handle_ping(mps3_ctrl_response_t *resp)
{
    resp->ok = 1;
    /* rm_id is the last VERIFIED identity, deliberately NOT a live register
     * read (mid-swap the register is transient) — coordinator.c ping doc. */
    mps3_format_id_hex(resp->shell_id, sizeof(resp->shell_id), g_static_id);
    mps3_format_id_hex(resp->rm_id, sizeof(resp->rm_id), g_current_rm);
}

static void handle_reset(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    if (strcmp(req->target, "dut") == 0) {
        /* HW backend: CLKRST.RESET_CTRL dut_resetn pulse via UIO, honouring
         * the 3-FF-synchronizer minimum assert width. Stub: state only. */
        resp->ok = 1;
    } else {
        set_err(resp, "bad target");
    }
}

static void handle_set_clk(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    if (strcmp(req->preset, "25mhz") != 0 &&
        strcmp(req->preset, "50mhz") != 0 &&
        strcmp(req->preset, "100mhz") != 0) {
        set_err(resp, "unknown preset");
        return;
    }
    /* HW backend: clk-xlnx-clock-wizard set_rate + LOCKED poll. Stub: locked. */
    resp->ok = 1;
    resp->locked = 1;
}

static void handle_link(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* Arg validation FIRST (mirrors bare-metal handler order), then the
     * §2A.5 baseline decline: NO slave at 0x44A3 — do not touch the page
     * (under the MBV that poke is a real S-mode access fault). */
    if (strcmp(req->event, "down") != 0 &&
        strcmp(req->event, "up")   != 0 &&
        strcmp(req->event, "pulse") != 0) {
        set_err(resp, "bad event");
        return;
    }
    set_err(resp, "vphy not present");
}

static void handle_commit(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    (void)req;
    /* §2A.5: overlay store disabled on this baseline (QSPI v0.2 boundary,
     * SPI master drives nothing) + the D16 shared-flash hazard. The frozen
     * decline reuses the existing bare-metal failure string. */
    set_err(resp, "commit failed");
}

static void handle_telemetry(mps3_ctrl_response_t *resp)
{
    /* ALWAYS ok:false — there is no power sensor by construction (4
     * independent dead-ends). lockup rides the failure line; it is a
     * DFXCTL.RM_STATUS read on hardware, a stub bool here. Do not "fix". */
    set_err(resp, "no power sensor");
    resp->lockup = g_lockup;
}

static void handle_macgen(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    /* Closed inject set (mirrors coordinator.c macgen_inject_bits), then
     * the §2A.5 decline: NO slave at 0x44A6 on this baseline. */
    if (strcmp(req->inject, "none")    != 0 &&
        strcmp(req->inject, "bad_fcs") != 0 &&
        strcmp(req->inject, "runt")    != 0 &&
        strcmp(req->inject, "giant")   != 0 &&
        strcmp(req->inject, "ifg")     != 0 &&
        strcmp(req->inject, "dribble") != 0) {
        set_err(resp, "bad inject");
        return;
    }
    set_err(resp, "genchk not present");
}

static void handle_diag(mps3_ctrl_response_t *resp)
{
    /* The 14 keys are frozen; the VALUES under Linux are honest substitutes
     * (SERVICE_DISPOSITION §2: "keep the keys and return honest substitutes
     * (or 0) — the schema is what's frozen"). The lwIP internals no longer
     * exist; the push-progress counters belong to mps3-pushd (separate
     * process) and are read from the shared spool stats file it publishes
     * (see below) — 0 only until pushd has run.
     * icap_bytes IS honest: the driver's free-running confirmed-into-ICAP
     * counter via sysfs (observable mid-swap by design — §4 chunking note);
     * 0 when the driver is not loaded. */
    resp->ok = 1;
    FILE *f = fopen(g_sysfs_icap_bytes, "r");
    if (f) {
        unsigned long long v = 0;
        if (fscanf(f, "%llu", &v) == 1)
            resp->diag_icap_bytes = (uint32_t)v;   /* wire key is u32 */
        fclose(f);
    }

    /* pushd push-progress counters: read the shared stats file mps3-pushd
     * publishes into the (shared) spool dir — write_stats() in mps3_pushd.c,
     * format "got=.. expect=.. grants_sent=.. grant_fails=.. rx_drops=..
     * rx_recover=..". Absent/malformed -> fields stay 0 (honest: pushd not
     * yet run). Same g_spool_dir both daemons share via S90mps3d. */
    char pstat[512];
    snprintf(pstat, sizeof(pstat), "%s/push_stats", g_spool_dir);
    FILE *pf = fopen(pstat, "r");
    if (pf) {
        unsigned got = 0, expect = 0, gs = 0, gf = 0, drops = 0, recover = 0;
        if (fscanf(pf,
                   "got=%u expect=%u grants_sent=%u grant_fails=%u rx_drops=%u rx_recover=%u",
                   &got, &expect, &gs, &gf, &drops, &recover) == 6) {
            resp->diag_got         = got;
            resp->diag_expect      = expect;
            resp->diag_grants_sent = gs;
            resp->diag_grant_fails = gf;
            resp->diag_rx_drops    = drops;
            resp->diag_rx_recover  = recover;
        }
        fclose(pf);
    }
}

static void handle_display(const mps3_ctrl_request_t *req, mps3_ctrl_response_t *resp)
{
    if (!g_clcdkvm_present) {
        /* Decode-works/handler-declines: the documented OFF-build behaviour
         * (only applies on a 0xE4B1C44A-baseline fork; the declared
         * shell_linux baseline instantiates clcd_kvm_0). */
        set_err(resp, "clcd_kvm not present");
        return;
    }
    if (strcmp(req->owner, "query") == 0) {
        /* read-only */
    } else if (strcmp(req->owner, "harness") == 0) {
        g_kvm_owner_dut = 0;
    } else if (strcmp(req->owner, "dut") == 0) {
        g_kvm_owner_dut = 1;
    } else if (strcmp(req->owner, "toggle") == 0) {
        g_kvm_owner_dut = !g_kvm_owner_dut;
    } else {
        set_err(resp, "bad owner");
        return;
    }
    /* Hardware reports the COMMITTED owner (handover takes ~7-9 ms, so a
     * flip can still read the outgoing owner); the stub commits instantly. */
    resp->ok = 1;
    strncpy(resp->owner, g_kvm_owner_dut ? "dut" : "harness",
            sizeof(resp->owner) - 1);
}

/* ---- swap worker lifecycle ----------------------------------------------- */

/* Fork the swap worker. Returns 0 on success (response is now HELD). */
static int start_swap_worker(void)
{
    int pfd[2];
    if (pipe(pfd) != 0)
        return -1;
    pid_t pid = fork();
    if (pid < 0) {
        close(pfd[0]);
        close(pfd[1]);
        return -1;
    }
    if (pid == 0) {
        /* child: sockets belong to the parent */
        close(pfd[0]);
        if (g_listen_fd >= 0)
            close(g_listen_fd);
        if (g_conn_fd >= 0)
            close(g_conn_fd);
        alarm(600);   /* belt-and-braces: a wedged worker dies, the parent
                       * sees pipe-EOF and settles the swap as failed */
        swap_worker_cfg_t cfg = {
            .spool_dir     = g_spool_dir,
            .dev_path      = g_dev_path,
            .static_id     = g_static_id,
            .await_ms      = g_await_ms,
            .mock          = g_mock_mode,
            .mock_force    = g_mock_force,
            .mock_force_rm = g_mock_force_rm,
        };
        swap_outcome_t o;
        (void)mps3_swap_worker_run(&cfg, &o);
        ssize_t off = 0;
        while (off < (ssize_t)sizeof(o)) {
            ssize_t w = write(pfd[1], (const char *)&o + off,
                              sizeof(o) - (size_t)off);
            if (w < 0) {
                if (errno == EINTR)
                    continue;
                break;
            }
            off += w;
        }
        _exit(0);
    }
    close(pfd[1]);
    g_worker_pid = pid;
    g_worker_fd = pfd[0];
    g_outcome_got = 0;
    g_outcome_ready = 0;
    memset(&g_outcome, 0, sizeof(g_outcome));
    return 0;
}

/* Drain the result pipe; on EOF reap the child and mark the outcome ready
 * (fail-closed if the worker died without writing a full result). */
static void worker_readable(void)
{
    char *dst = (char *)&g_outcome;
    for (;;) {
        ssize_t r = read(g_worker_fd, dst + g_outcome_got,
                         sizeof(g_outcome) - g_outcome_got);
        if (r > 0) {
            g_outcome_got += (size_t)r;
            if (g_outcome_got < sizeof(g_outcome))
                continue;
            /* full result: keep the fd open until EOF (child exiting) */
            continue;
        }
        if (r < 0 && (errno == EINTR))
            continue;
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            return;
        /* EOF (or error): the worker is done. */
        break;
    }
    close(g_worker_fd);
    g_worker_fd = -1;
    if (g_worker_pid > 0) {
        (void)waitpid(g_worker_pid, NULL, 0);
        g_worker_pid = -1;
    }
    if (g_outcome_got < sizeof(g_outcome)) {
        /* worker crashed mid-swap: uniform failure, driver invariant says
         * the RP is parked (or a follow-up ABORT on next open will park). */
        memset(&g_outcome, 0, sizeof(g_outcome));
        g_outcome.err = -1;
        strncpy(g_outcome.stage, "crash", sizeof(g_outcome.stage) - 1);
        fprintf(stderr, "mps3-ctrld: swap worker died without a result\n");
    }
    g_outcome_ready = 1;
}

/* ---- dispatch ------------------------------------------------------------ */

static void encode_or_fallback(const mps3_ctrl_response_t *resp,
                               char *out, int out_len, int *len)
{
    *len = mps3_ctrl_encode_response(resp, out, out_len);
    if (*len < 0) {
        *len = snprintf(out, (size_t)out_len,
                        "{\"ok\":false,\"err\":\"encode overflow\"}\n");
    }
}

/* Returns 1 = response in out (send now), 0 = swap accepted (response held). */
static int dispatch_line(const char *line, int len, char *out, int out_len,
                         int *out_bytes)
{
    mps3_ctrl_request_t req;
    mps3_ctrl_response_t resp;
    memset(&resp, 0, sizeof(resp));

    int rc = mps3_ctrl_decode_line(line, len, &req);
    if (rc != MPS3_CTRL_DECODE_OK) {
        resp.op = MPS3_OP_UNKNOWN;
        set_err(&resp,
                (rc == MPS3_CTRL_DECODE_EUNKNOWN_OP) ? "unknown op" :
                (rc == MPS3_CTRL_DECODE_EBADARGS)    ? "bad args"   :
                                                       "bad json");
        encode_or_fallback(&resp, out, out_len, out_bytes);
        return 1;
    }
    resp.op = req.op;

    switch (req.op) {
    case MPS3_OP_PING:      handle_ping(&resp);            break;
    case MPS3_OP_RESET:     handle_reset(&req, &resp);     break;
    case MPS3_OP_SET_CLK:   handle_set_clk(&req, &resp);   break;
    case MPS3_OP_SWAP:
        /* A parked client cannot pipeline a second swap (we stop recv()ing
         * while parked), but a NEW client adopted after a mid-swap client
         * death can — the worker is still running: answer the bare-metal
         * arm-refusal immediately (coordinator.c handle_swap, send-now). */
        if (g_worker_pid > 0 || g_worker_fd >= 0) {
            set_err(&resp, "swap already in progress");
            break;
        }
        if (start_swap_worker() != 0) {
            /* fork/pipe failure: settle as a normal failed swap at the
             * minimum cadence — the held-response shape is preserved. */
            memset(&g_outcome, 0, sizeof(g_outcome));
            g_outcome.err = -1;
            strncpy(g_outcome.stage, "fork", sizeof(g_outcome.stage) - 1);
            g_outcome_ready = 1;
        }
        g_swap_parked = 1;
        {
            long long deadline = now_ms() + g_swap_settle_ms;
            g_swap_deadline.tv_sec  = deadline / 1000;
            g_swap_deadline.tv_nsec = (deadline % 1000) * 1000000;
        }
        return 0;
    case MPS3_OP_LINK:      handle_link(&req, &resp);      break;
    case MPS3_OP_COMMIT:    handle_commit(&req, &resp);    break;
    case MPS3_OP_TELEMETRY: handle_telemetry(&resp);       break;
    case MPS3_OP_MACGEN:    handle_macgen(&req, &resp);    break;
    case MPS3_OP_DIAG:      handle_diag(&resp);            break;
    case MPS3_OP_DISPLAY:   handle_display(&req, &resp);   break;
    default:
        set_err(&resp, "unknown op");
        break;
    }
    encode_or_fallback(&resp, out, out_len, out_bytes);
    return 1;
}

/* ---- socket plumbing ------------------------------------------------------ */

static int send_all(int fd, const char *buf, int len)
{
    int off = 0;
    while (off < len) {
        ssize_t w = send(fd, buf + off, (size_t)(len - off), MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        off += (int)w;
    }
    return 0;
}

static void drop_client(void)
{
    if (g_conn_fd >= 0)
        close(g_conn_fd);
    g_conn_fd = -1;
    g_line_len = 0;
    g_line_overflow = 0;
    g_swap_parked = 0;   /* a swap client that dies forfeits its held reply;
                          * the WORKER keeps running — a started swap must
                          * settle (the RP state machine cannot be abandoned
                          * half-way), its result is consumed reply-less */
}

/* The worker settled: deliver the HELD response (if the client survived)
 * and fold the outcome into daemon state. The connection was parked the
 * whole time with zero interim bytes (§2 red-flag 3: pyverify blocks on
 * this). Failure shape is the uniform bare-metal one; rm_id/verified are
 * only reported on success (coordinator_swap_final_response). */
static void swap_settle(void)
{
    mps3_ctrl_response_t resp;
    memset(&resp, 0, sizeof(resp));
    resp.op = MPS3_OP_SWAP;

    if (g_outcome.ok && g_outcome.verified) {
        g_current_rm = g_outcome.rm_id;   /* last VERIFIED identity (ping) */
        resp.ok = 1;
        resp.verified = 1;
        mps3_format_id_hex(resp.rm_id, sizeof(resp.rm_id), g_outcome.rm_id);
    } else {
        set_err(&resp, "swap failed");
    }
    g_outcome_ready = 0;

    if (!g_swap_parked)
        return;   /* client died mid-swap: outcome folded, reply forfeited */

    char out[MPS3_CTRL_RESP_MAX];
    int len;
    encode_or_fallback(&resp, out, sizeof(out), &len);
    g_swap_parked = 0;
    if (g_conn_fd >= 0 && send_all(g_conn_fd, out, len) < 0)
        drop_client();
}

static void client_readable(void)
{
    /* Byte-at-a-time, like coordinator_net.c's line assembly. This is what
     * makes the swap PARK exact: once a swap line is dispatched we stop
     * recv()ing entirely, so pipelined bytes stay in the kernel socket
     * buffer (never dropped) until the held response goes out. */
    char buf[1];
    ssize_t r = recv(g_conn_fd, buf, sizeof(buf), 0);
    if (r == 0 || (r < 0 && errno != EINTR && errno != EAGAIN)) {
        drop_client();
        return;
    }
    if (r < 0)
        return;

    for (ssize_t i = 0; i < r; i++) {
        char c = buf[i];
        if (c != '\n') {
            if (g_line_len < (int)sizeof(g_line))
                g_line[g_line_len++] = c;
            else
                g_line_overflow = 1;
            continue;
        }
        /* full line */
        char out[MPS3_CTRL_RESP_MAX];
        int out_bytes = 0;
        int send_now;
        if (g_line_overflow) {
            /* Oversized line: fail closed through the normal dispatch path
             * so the "bad json" line comes from the one real encoder —
             * mirrors coordinator_net.c's empty-line trick. */
            send_now = dispatch_line("", 0, out, sizeof(out), &out_bytes);
        } else {
            send_now = dispatch_line(g_line, g_line_len, out, sizeof(out),
                                     &out_bytes);
        }
        g_line_len = 0;
        g_line_overflow = 0;
        if (send_now) {
            if (send_all(g_conn_fd, out, out_bytes) < 0) {
                drop_client();
                return;
            }
        } else {
            /* swap accepted: park — stop consuming further bytes until the
             * held response goes out (bare-metal parks the pcb the same
             * way). Any pipelined bytes stay in the kernel socket buffer. */
            return;
        }
    }
}

static void usage(const char *argv0)
{
    fprintf(stderr,
        "usage: %s [-p port] [-i static_id_file] [-d spool] [-D dev] [-t ms]\n"
        "          [-M] [-W rm_id] [-K] [-f]\n"
        "  -p port   listen port (default 6900)\n"
        "  -i file   file containing the shell static_id (hex), default\n"
        "            /etc/mps3/static_id\n"
        "  -d dir    staged-bitstream spool shared with mps3-pushd\n"
        "            (default /run/mps3)\n"
        "  -D dev    DFX swap chardev (default /dev/mps3dfx)\n"
        "  -t ms     staged-pair await idle bound (default 30000 — the\n"
        "            silicon-observed hang bound; re-armed on RX progress)\n"
        "  -M        driver is in mock mode: configure the mock per swap\n"
        "            (TEST RIGS ONLY — never on real hardware)\n"
        "  -W id     with -M: mock presents this (wrong) rm_id — verify-fail\n"
        "            injection for the e2e suite\n"
        "  -K        declare CLCDKVM absent (0xE4B1C44A-baseline fork mode)\n"
        "  -f        stay in foreground (default; flag accepted for symmetry)\n",
        argv0);
}

static void load_static_id(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return;   /* keep the weak default 0 = "not provisioned" */
    char tmp[64];
    if (fgets(tmp, sizeof(tmp), f))
        g_static_id = (uint32_t)strtoul(tmp, NULL, 0);
    fclose(f);
}

int main(int argc, char **argv)
{
    const char *id_file = "/etc/mps3/static_id";
    int opt;
    while ((opt = getopt(argc, argv, "p:i:d:D:t:MW:Kfh")) != -1) {
        switch (opt) {
        case 'p': g_port = atoi(optarg); break;
        case 'i': id_file = optarg; break;
        case 'd': g_spool_dir = optarg; break;
        case 'D': g_dev_path = optarg; break;
        case 't': g_await_ms = (uint32_t)strtoul(optarg, NULL, 0); break;
        case 'M': g_mock_mode = 1; break;
        case 'W':
            g_mock_force = 1;
            g_mock_force_rm = (uint32_t)strtoul(optarg, NULL, 0);
            break;
        case 'K': g_clcdkvm_present = 0; break;
        case 'f': break;
        default: usage(argv[0]); return 2;
        }
    }
    load_static_id(id_file);
    signal(SIGPIPE, SIG_IGN);
    (void)spool_mkdir(g_spool_dir);   /* pushd usually made it; best-effort */

    /* Re-sync the last-VERIFIED rm_id from the driver across daemon
     * restarts. This is NOT a live DFXCTL read (the sysfs attr reports the
     * engine's verified identity, transient-safe); absent driver = greybox
     * 0, same as a bare-metal boot. */
    {
        FILE *f = fopen(g_sysfs_rm_id, "r");
        if (f) {
            char tmp[32];
            if (fgets(tmp, sizeof(tmp), f))
                g_current_rm = (uint32_t)strtoul(tmp, NULL, 0);
            fclose(f);
        }
    }

    g_listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (g_listen_fd < 0) { perror("socket"); return 1; }
    int one = 1;
    setsockopt(g_listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_ANY);
    sa.sin_port = htons((uint16_t)g_port);
    if (bind(g_listen_fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        perror("bind");
        return 1;
    }
    /* Small backlog on purpose; extras are drained + closed promptly below,
     * so no second client can sit parked in the backlog long enough to read
     * as "wedged" (>2 s) to fpgahub — the §2A.1 obligation. */
    if (listen(g_listen_fd, 4) < 0) { perror("listen"); return 1; }

    fprintf(stderr,
            "mps3-ctrld: listening on :%d static_id=0x%08x spool=%s dev=%s%s%s\n",
            g_port, g_static_id, g_spool_dir, g_dev_path,
            g_mock_mode ? " (mock)" : "",
            g_clcdkvm_present ? "" : " (no clcd_kvm)");
    if (g_mock_force)
        fprintf(stderr, "mps3-ctrld: MOCK WRONG-ID INJECTION rm_id=0x%08x\n",
                g_mock_force_rm);

    for (;;) {
        struct pollfd pfds[3];
        int nf = 0, conn_ix = -1, worker_ix = -1;
        pfds[nf].fd = g_listen_fd;
        pfds[nf].events = POLLIN;
        nf++;
        if (g_conn_fd >= 0 && !g_swap_parked) {
            conn_ix = nf;
            pfds[nf].fd = g_conn_fd;
            pfds[nf].events = POLLIN;
            nf++;
        }
        if (g_worker_fd >= 0) {
            worker_ix = nf;
            pfds[nf].fd = g_worker_fd;
            pfds[nf].events = POLLIN;
            nf++;
        }

        /* Only the min-settle cadence needs a timed wake: a RUNNING worker
         * wakes us via its pipe, everything else via the sockets. */
        int timeout = -1;
        if (g_outcome_ready) {
            long long dl = (long long)g_swap_deadline.tv_sec * 1000 +
                           g_swap_deadline.tv_nsec / 1000000;
            long long rem = dl - now_ms();
            timeout = rem > 0 ? (int)rem : 0;
        }

        int pr = poll(pfds, (nfds_t)nf, timeout);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            return 1;
        }

        if (worker_ix >= 0 &&
            (pfds[worker_ix].revents & (POLLIN | POLLHUP | POLLERR)))
            worker_readable();

        if (g_outcome_ready) {
            long long dl = (long long)g_swap_deadline.tv_sec * 1000 +
                           g_swap_deadline.tv_nsec / 1000000;
            if (!g_swap_parked || now_ms() >= dl)
                swap_settle();
        }

        /* Service the ADOPTED client before accepting extras: a client that
         * closed and reconnected inside one poll wake must see its EOF
         * processed first, or the reconnect is refused as a "second
         * client" it no longer is. */
        if (conn_ix >= 0 &&
            (pfds[conn_ix].revents & (POLLIN | POLLHUP | POLLERR)))
            client_readable();

        if (pfds[0].revents & POLLIN) {
            struct sockaddr_in ca;
            socklen_t cl = sizeof(ca);
            int cfd = accept(g_listen_fd, (struct sockaddr *)&ca, &cl);
            if (cfd >= 0) {
                if (g_conn_fd < 0) {
                    g_conn_fd = cfd;
                    g_line_len = 0;
                    g_line_overflow = 0;
                    /* §2A.4: request/response protocol, latency > coalescing */
                    setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
                } else {
                    /* §2A.1 THE refusal: handshake completed, close with
                     * ZERO bytes. Never a reply line (would read "idle"),
                     * never stop listening (would read "offline"). */
                    close(cfd);
                }
            }
        }
    }
}
