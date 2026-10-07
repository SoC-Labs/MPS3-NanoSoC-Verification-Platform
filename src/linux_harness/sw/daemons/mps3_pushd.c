/*
 * mps3_pushd — the :6910 bitstream push daemon.
 *
 * Implements the FROZEN 6910 lifecycle of SERVICE_DISPOSITION.md §2/§2A.3
 * with a REAL sink (M3): the payload streams into the shared spool
 * (<spool>/rx.tmp) as it arrives and is atomically published as the staged
 * clearing/partial on CRC-gate success (spool.h). mps3-ctrld's swap worker
 * consumes the staged pair; the in-flight rx.tmp growth is its RX-progress
 * signal, so a swap parked on 6900 keeps its 30 s idle timer re-armed for
 * as long as this daemon is actually receiving. The wire behaviour is the
 * contract:
 *
 *  - 24-byte big-endian header ">4sHBBIIII" (magic "MPS3", ver=1,
 *    kind 0=clearing/1=partial, rm_slot, static_id, rm_id, len_words,
 *    crc32) then raw payload. Validation ORDER is contractual: header
 *    (magic/ver/static_id/kind/ordering/size) THEN full-payload CRC,
 *    all before any ICAP write (stub: before "staged").
 *  - The server sends ZERO application bytes, ever. The only
 *    acknowledgement is connection lifecycle (§2A.3):
 *      * client streams header+payload and half-closes (FIN);
 *      * server validates, then closes: server-close after a complete,
 *        well-framed transfer = consumed/staged (the client drains to EOF);
 *      * bad header / oversize / bytes beyond the frame -> server closes
 *        EARLY; rejection is EOF-before-the-client-finished, no error byte.
 *  - ONE push session at a time: a second connection while one is active
 *    gets the same accept-then-close refusal as 6900.
 *  - 30 s RX-idle reap (re-armed on every byte of progress; bounds silence,
 *    never a slow transfer) — the silicon-observed 2026-07-09 hang.
 *  - Pair ordering: a partial (kind=1) is only accepted after a clearing
 *    (kind=0) completed; a new clearing drops any stale staged partial.
 *
 * DIAG COUNTERS (README §6 item 6): this daemon owns the 6910-side push
 * progress/outcome counters that the frozen `diag` verb exposes but that
 * mps3-ctrld (a SEPARATE process) reported 0 for, because it had no channel
 * to them. We publish them to a shared stats file in the spool dir
 * (<spool>/push_stats), atomically (tmp+rename), on every meaningful change —
 * exactly mirroring how the DFX driver publishes icap_bytes to sysfs and
 * mps3-ctrld reads it in handle_diag(). ctrld picks these up with the same
 * read shape (see write_stats() for the field/format contract). Counters are
 * process-lifetime and honest (they reset on daemon restart; nothing is
 * invented).
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "proto.h"
#include "spool.h"

#define RX_IDLE_TIMEOUT_MS 30000
/* Size sanity bound: largest real partial is ~1.65 MB; 16 MiB is a
 * generous ceiling that still rejects absurd headers before streaming. */
#define MAX_PAYLOAD_BYTES  (16u * 1024u * 1024u)

static uint32_t g_static_id = 0x00000000u;
static int      g_port      = MPS3_PORT_PUSH;
static const char *g_spool  = "/run/mps3";

/* Staged-pair state (persists across sessions, like config_agent's; the
 * spool files are the durable truth — flags are re-derived at startup). */
static int      g_clearing_staged;
static int      g_partial_staged;
static uint32_t g_staged_rm_id;

/* ---- diag counters (published to <spool>/push_stats for mps3-ctrld) ------
 * These back the pushd-domain keys of the frozen `diag` verb (proto.h):
 *   got          bytes received of the IN-FLIGHT / most recent push
 *   expect       declared payload bytes of the in-flight / most recent push
 *   grants_sent  pushes accepted + staged (server-close-equals-consumed, the
 *                6910 acknowledgement — §2A.3)
 *   grant_fails  pushes rejected (bad header / CRC mismatch / torn transfer /
 *                spool error)
 *   rx_drops     stale staged partials dropped by a new clearing (pair rule)
 *   rx_recover   RX-idle reaps (the 30 s silence bound firing)
 * All free-running within the process lifetime, like the driver's icap_bytes.*/
static uint32_t g_stat_got;
static uint32_t g_stat_expect;
static uint32_t g_stat_grants;
static uint32_t g_stat_grant_fails;
static uint32_t g_stat_drops;
static uint32_t g_stat_recover;

typedef enum { SS_HDR, SS_PAYLOAD, SS_AWAIT_EOF } sess_state_t;

static int          g_sess_fd = -1;
static sess_state_t g_state;
static uint8_t      g_hdr[MPS3_BITSTREAM_HDR_WIRE_SIZE];
static uint32_t     g_hdr_got;
static mps3_bitstream_hdr_t g_h;
static uint32_t     g_payload_left;
static uint32_t     g_crc;
static long long    g_last_rx_ms;
static int          g_sink_fd = -1;   /* <spool>/rx.tmp while receiving */

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Publish the diag counters to <spool>/push_stats, atomically (tmp+rename)
 * so a concurrent ctrld read never sees a torn line. Best-effort: a failure
 * to write stats must never affect the wire path (leave the previous file). *
 * Format contract (ONE line, space-separated key=value — mirror this in
 * ctrld handle_diag with an fscanf, the same shape as the icap_bytes read):
 *   got=%u expect=%u grants_sent=%u grant_fails=%u rx_drops=%u rx_recover=%u
 */
static void write_stats(void)
{
    char tmp[512], dst[512];
    spool_path(g_spool, "push_stats", ".tmp", tmp, sizeof(tmp));
    spool_path(g_spool, "push_stats", NULL, dst, sizeof(dst));
    FILE *f = fopen(tmp, "w");
    if (!f)
        return;
    int n = fprintf(f,
        "got=%u expect=%u grants_sent=%u grant_fails=%u rx_drops=%u rx_recover=%u\n",
        (unsigned)g_stat_got, (unsigned)g_stat_expect,
        (unsigned)g_stat_grants, (unsigned)g_stat_grant_fails,
        (unsigned)g_stat_drops, (unsigned)g_stat_recover);
    if (n < 0 || fclose(f) != 0) {
        (void)unlink(tmp);
        return;
    }
    (void)rename(tmp, dst);
}

static void sink_discard(void)
{
    char p[512];
    if (g_sink_fd >= 0) {
        close(g_sink_fd);
        g_sink_fd = -1;
    }
    (void)unlink(spool_path(g_spool, SPOOL_RX_TMP, NULL, p, sizeof(p)));
}

static void sess_close(const char *why)
{
    if (g_sess_fd >= 0) {
        fprintf(stderr, "mps3-pushd: session close (%s)\n", why);
        close(g_sess_fd);
        g_sess_fd = -1;
    }
    sink_discard();   /* no-op after a successful publish */
}

/* Header validation, in the contractual order:
 * magic (checked by unpack) / ver / static_id / kind / ordering / size. */
static int validate_header(void)
{
    if (g_h.ver != MPS3_BITSTREAM_VER)
        return -1;
    if (g_h.static_id != g_static_id)
        return -1;
    if (g_h.kind != MPS3_BITSTREAM_KIND_CLEARING &&
        g_h.kind != MPS3_BITSTREAM_KIND_PARTIAL)
        return -1;
    /* Ordering: clearing-then-partial within a pair. The spool file is the
     * truth (a swap CONSUMES the staged pair from another process, so the
     * in-memory flag alone can go stale after each swap). */
    if (g_h.kind == MPS3_BITSTREAM_KIND_PARTIAL &&
        !spool_staged(g_spool, SPOOL_CLEARING))
        return -1;
    if (g_h.len_words == 0 ||
        (uint64_t)g_h.len_words * 4u > MAX_PAYLOAD_BYTES)
        return -1;
    return 0;
}

static void finish_payload(void)
{
    /* Full payload received and client half-closed. CRC check LAST (after
     * header validation, before staging) — validation order is contractual. */
    if (g_crc != g_h.crc32) {
        /* Rejected: close without staging. The client sees close-after-
         * its-own-FIN either way; the swap that consumes the stage is what
         * surfaces the failure (same containment as bare metal). */
        g_stat_grant_fails++;
        write_stats();
        sess_close("crc mismatch");
        return;
    }

    /* Publish: fsync + close rx.tmp, unlink a stale partial if this is a
     * new clearing (frozen pair rule), rename bin, then meta (the marker). */
    char tmp[512];
    spool_path(g_spool, SPOOL_RX_TMP, NULL, tmp, sizeof(tmp));
    const char *base = (g_h.kind == MPS3_BITSTREAM_KIND_CLEARING)
                     ? SPOOL_CLEARING : SPOOL_PARTIAL;
    spool_meta_t m = {
        .kind      = g_h.kind,
        .rm_slot   = g_h.rm_slot,
        .static_id = g_h.static_id,
        .rm_id     = g_h.rm_id,
        .len_words = g_h.len_words,
        .crc32     = g_h.crc32,
    };
    char bin[512];
    spool_path(g_spool, base, ".bin", bin, sizeof(bin));
    int ok = g_sink_fd >= 0 && fsync(g_sink_fd) == 0;
    if (g_sink_fd >= 0) {
        close(g_sink_fd);
        g_sink_fd = -1;
    }
    if (ok && g_h.kind == MPS3_BITSTREAM_KIND_CLEARING) {
        /* Pair rule: a new clearing drops a stale staged partial — count it
         * only when one was actually present to drop. */
        if (g_partial_staged || spool_staged(g_spool, SPOOL_PARTIAL))
            g_stat_drops++;
        spool_unlink_pair(g_spool, SPOOL_PARTIAL);
    }
    if (!ok || rename(tmp, bin) != 0 ||
        spool_meta_write(g_spool, base, &m) != 0) {
        g_stat_grant_fails++;
        write_stats();
        sess_close("spool publish failed");
        return;
    }

    if (g_h.kind == MPS3_BITSTREAM_KIND_CLEARING) {
        g_clearing_staged = 1;
        g_partial_staged  = 0;   /* a new clearing drops a stale partial */
        fprintf(stderr, "mps3-pushd: clearing staged (%u words)\n",
                (unsigned)g_h.len_words);
    } else {
        g_partial_staged = 1;
        g_staged_rm_id   = g_h.rm_id;
        fprintf(stderr, "mps3-pushd: partial staged (rm_id=0x%08x, %u words)\n",
                g_h.rm_id, (unsigned)g_h.len_words);
    }
    /* Server-close-equals-consumed: THE sync point (§2A.3) — the 6910
     * acknowledgement, so this is what "grants_sent" counts. */
    g_stat_grants++;
    write_stats();
    sess_close("consumed");
}

static void sess_readable(void)
{
    uint8_t buf[65536];
    ssize_t r = recv(g_sess_fd, buf, sizeof(buf), 0);
    if (r < 0) {
        if (errno == EINTR || errno == EAGAIN)
            return;
        sess_close("recv error");
        return;
    }
    if (r == 0) {
        /* client FIN */
        switch (g_state) {
        case SS_AWAIT_EOF:
            finish_payload();
            break;
        default:
            /* FIN mid-header or mid-payload: torn transfer, reject. */
            g_stat_grant_fails++;
            write_stats();
            sess_close("early client EOF");
            break;
        }
        return;
    }
    g_last_rx_ms = now_ms();

    ssize_t off = 0;
    while (off < r) {
        switch (g_state) {
        case SS_HDR: {
            uint32_t need = MPS3_BITSTREAM_HDR_WIRE_SIZE - g_hdr_got;
            uint32_t take = (uint32_t)(r - off) < need ? (uint32_t)(r - off) : need;
            memcpy(g_hdr + g_hdr_got, buf + off, take);
            g_hdr_got += take;
            off += take;
            if (g_hdr_got < MPS3_BITSTREAM_HDR_WIRE_SIZE)
                break;
            if (mps3_bitstream_hdr_unpack(g_hdr, g_hdr_got, &g_h) != 0 ||
                validate_header() != 0) {
                /* EARLY close = rejection; no error byte exists (§2A.3). */
                g_stat_grant_fails++;
                write_stats();
                sess_close("bad header");
                return;
            }
            /* SPOOL SINK: stream the payload to <spool>/rx.tmp as it
             * arrives (the swap worker watches its growth as the RX-
             * progress signal). Publish happens only after the CRC gate. */
            {
                char p[512];
                g_sink_fd = open(spool_path(g_spool, SPOOL_RX_TMP, NULL,
                                            p, sizeof(p)),
                                 O_WRONLY | O_CREAT | O_TRUNC, 0644);
                if (g_sink_fd < 0) {
                    fprintf(stderr, "mps3-pushd: spool open: %s\n",
                            strerror(errno));
                    g_stat_grant_fails++;
                    write_stats();
                    sess_close("spool open failed");
                    return;
                }
            }
            g_payload_left = g_h.len_words * 4u;
            g_crc = mps3_crc32_init();
            g_state = SS_PAYLOAD;
            /* New transfer: publish its expected size and reset progress so a
             * mid-transfer `diag` reads got/expect meaningfully. */
            g_stat_expect = g_payload_left;
            g_stat_got    = 0;
            write_stats();
            break;
        }
        case SS_PAYLOAD: {
            uint32_t take = (uint32_t)(r - off) < g_payload_left
                          ? (uint32_t)(r - off) : g_payload_left;
            g_crc = mps3_crc32_update(g_crc, buf + off, take);
            {
                uint32_t done = 0;
                while (done < take) {
                    ssize_t w = write(g_sink_fd, buf + off + done, take - done);
                    if (w < 0) {
                        if (errno == EINTR)
                            continue;
                        fprintf(stderr, "mps3-pushd: spool write: %s\n",
                                strerror(errno));
                        sess_close("spool write failed");
                        return;
                    }
                    done += (uint32_t)w;
                }
            }
            g_payload_left -= take;
            g_stat_got += take;
            off += take;
            if (g_payload_left == 0)
                g_state = SS_AWAIT_EOF;
            break;
        }
        case SS_AWAIT_EOF:
            /* Bytes beyond the declared frame: not well-framed, reject. */
            g_stat_grant_fails++;
            write_stats();
            sess_close("bytes beyond frame");
            return;
        }
    }
    /* Publish RX progress (got) — the swap worker's RX-progress signal is the
     * rx.tmp growth; this file is the human/ctrld-facing progress mirror. */
    write_stats();
}

int main(int argc, char **argv)
{
    const char *id_file = "/etc/mps3/static_id";
    int opt;
    while ((opt = getopt(argc, argv, "p:i:d:fh")) != -1) {
        switch (opt) {
        case 'p': g_port = atoi(optarg); break;
        case 'i': id_file = optarg; break;
        case 'd': g_spool = optarg; break;
        case 'f': break;
        default:
            fprintf(stderr,
                    "usage: %s [-p port] [-i static_id_file] [-d spool_dir]\n",
                    argv[0]);
            return 2;
        }
    }
    if (spool_mkdir(g_spool) != 0) {
        fprintf(stderr, "mps3-pushd: spool dir %s: %s\n", g_spool,
                strerror(errno));
        return 1;
    }
    FILE *f = fopen(id_file, "r");
    if (f) {
        char tmp[64];
        if (fgets(tmp, sizeof(tmp), f))
            g_static_id = (uint32_t)strtoul(tmp, NULL, 0);
        fclose(f);
    }
    signal(SIGPIPE, SIG_IGN);

    /* Re-derive staged state from the spool (daemon restart survival). */
    g_clearing_staged = spool_staged(g_spool, SPOOL_CLEARING);
    if (g_partial_staged || spool_staged(g_spool, SPOOL_PARTIAL)) {
        spool_meta_t m;
        g_partial_staged = 1;
        if (spool_meta_read(g_spool, SPOOL_PARTIAL, &m) == 0)
            g_staged_rm_id = m.rm_id;
    }
    if (g_clearing_staged || g_partial_staged)
        fprintf(stderr, "mps3-pushd: spool carries staged %s%s%s\n",
                g_clearing_staged ? "clearing" : "",
                (g_clearing_staged && g_partial_staged) ? "+" : "",
                g_partial_staged ? "partial" : "");

    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    if (lfd < 0) { perror("socket"); return 1; }
    int one = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_ANY);
    sa.sin_port = htons((uint16_t)g_port);
    if (bind(lfd, (struct sockaddr *)&sa, sizeof(sa)) < 0) { perror("bind"); return 1; }
    if (listen(lfd, 4) < 0) { perror("listen"); return 1; }

    write_stats();   /* publish an initial all-zero push_stats so ctrld's
                      * diag has a file to read from the first boot moment */

    fprintf(stderr, "mps3-pushd: listening on :%d static_id=0x%08x spool=%s\n",
            g_port, g_static_id, g_spool);

    for (;;) {
        struct pollfd pfds[2];
        int nf = 0;
        pfds[nf].fd = lfd;
        pfds[nf].events = POLLIN;
        nf++;
        if (g_sess_fd >= 0) {
            pfds[nf].fd = g_sess_fd;
            pfds[nf].events = POLLIN;
            nf++;
        }

        int timeout = -1;
        if (g_sess_fd >= 0) {
            long long rem = (g_last_rx_ms + RX_IDLE_TIMEOUT_MS) - now_ms();
            timeout = rem > 0 ? (int)rem : 0;
        }

        int pr = poll(pfds, (nfds_t)nf, timeout);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            return 1;
        }

        /* 30 s RX-idle reap (bounds silence, never a slow transfer — the
         * timer re-arms on every byte in sess_readable()). */
        if (g_sess_fd >= 0 &&
            now_ms() - g_last_rx_ms >= RX_IDLE_TIMEOUT_MS) {
            g_stat_recover++;
            write_stats();
            sess_close("rx idle timeout");
        }

        /* Service the active session before accepting extras (same
         * close-then-reconnect ordering discipline as mps3-ctrld). */
        if (nf > 1 && g_sess_fd >= 0 &&
            (pfds[1].revents & (POLLIN | POLLHUP | POLLERR)))
            sess_readable();

        if (pfds[0].revents & POLLIN) {
            struct sockaddr_in ca;
            socklen_t cl = sizeof(ca);
            int cfd = accept(lfd, (struct sockaddr *)&ca, &cl);
            if (cfd >= 0) {
                if (g_sess_fd < 0) {
                    g_sess_fd = cfd;
                    g_state = SS_HDR;
                    g_hdr_got = 0;
                    g_last_rx_ms = now_ms();
                } else {
                    /* busy: refuse — same accept-then-close as 6900. */
                    close(cfd);
                }
            }
        }
    }
}
