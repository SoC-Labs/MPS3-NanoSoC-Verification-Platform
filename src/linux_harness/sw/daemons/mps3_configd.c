/*
 * mps3_configd — the config_agent bitstream receiver on UDP :69 (TFTP WRQ).
 *
 * This is the TFTP front of the FROZEN config_agent contract
 * (SERVICE_DISPOSITION.md §3.3, the §2 port table, and the "Bitstream push
 * header" section). It is the twin of mps3-pushd: pushd owns the TCP :6910
 * receive step, configd owns the UDP :69 (TFTP WRQ) receive step, and BOTH
 * feed the IDENTICAL spool (spool.h) that mps3-ctrld's swap worker consumes.
 * Nothing about the swap/ICAP path changes — the receiver transport is the
 * only difference. Every ordering invariant, the validation ORDER, the
 * single-session discipline, and the 30 s RX-idle reap are reproduced from
 * pushd verbatim so that a bitstream pushed by TFTP is indistinguishable,
 * downstream, from one pushed on 6910.
 *
 * The Linux decomposition (why two daemons, not one):
 *   The bare-metal config_agent multiplexed TFTP/69 AND TCP/6910 through one
 *   session state and one abort path (config_agent_abort_session()). Under
 *   Linux the two transports are split into sibling single-session daemons
 *   because a UDP TFTP state machine and a TCP byte-stream state machine
 *   share no socket discipline — but they DO share the durable spool, which
 *   IS the frozen session state (rx.tmp + the published .bin/.meta pair).
 *   The two never bind the same port, so they coexist; in practice the host
 *   uses one transport per deploy (pyverify pushes on 6910; a stock TFTP
 *   client / U-Boot-style pusher uses :69).
 *
 * Wire contract that this daemon must honour byte-for-byte:
 *
 *  - The FILE written by TFTP is exactly the 6910 frame: 24-byte big-endian
 *    header ">4sHBBIIII" (magic "MPS3", ver=1, kind 0=clearing/1=partial,
 *    rm_slot, static_id, rm_id, len_words, crc32) followed by the raw
 *    payload. Validation ORDER is contractual: header
 *    (magic/ver/static_id/kind/ordering/size) THEN full-payload CRC, all
 *    before the stage is published (the stand-in for "before any ICAP
 *    write" — the swap worker is what actually reaches the ICAP).
 *  - Pair ordering: a partial (kind=1) is only accepted after a clearing
 *    (kind=0) has been staged; a new clearing drops any stale staged
 *    partial. The spool .meta is the truth (a swap CONSUMES the staged pair
 *    from another process, so an in-memory flag alone goes stale).
 *  - ONE receive session at a time (shared with :6910 only through the
 *    spool, not in-process): a second WRQ while a transfer is active is
 *    refused with a TFTP ERROR, the active transfer is untouched.
 *  - 30 s RX-idle reap, re-armed on every block of progress (bounds
 *    silence, never a slow transfer) — the silicon-observed 2026-07-09
 *    hang, kept identical to pushd.
 *
 * TFTP (RFC 1350) specifics implemented here:
 *  - WRQ + "octet" mode only (RRQ / "netascii" / "mail" -> ERROR).
 *  - 512-byte DATA blocks; a per-transfer TID socket (ephemeral port,
 *    connect()ed to the client TID) carries the transfer, exactly as the
 *    RFC requires — the initial ACK(0) and all subsequent ACKs come from
 *    the TID socket, never from :69.
 *  - Lock-step ACK per block; a duplicate DATA (block == last-acked) re-ACKs
 *    (the client's previous ACK/DATA crossed); a lost DATA is recovered by
 *    an ACK-retransmit timer (RETX_TIMEOUT_MS, MAX_RETX attempts).
 *  - End of transfer = a DATA block shorter than 512 B (incl. a trailing
 *    0-byte block when the file is an exact multiple of 512). The FINAL ACK
 *    is the "consumed/staged" signal — the TFTP analogue of pushd's
 *    server-close-equals-consumed (§2A.3): it is sent only after the CRC
 *    gate passes and the stage is published. A CRC/header/framing failure
 *    sends a TFTP ERROR instead and stages nothing.
 *  - Short post-completion dally: a retransmitted terminating DATA is
 *    answered with a repeat of the final ACK so the client cannot hang if
 *    that ACK was lost.
 *
 * Backend seam (-m): configd, like pushd, has NO direct MMIO — its sink is
 * the hardware-free spool, and the ICAP/DFX hardware lives DOWNSTREAM in the
 * DFX driver reached by mps3-ctrld's swap worker (SERVICE_DISPOSITION §3.3,
 * §4). So -m does not switch a register backend the way the aux daemons'
 * -m does; it marks a host/QEMU conformance run (privileged :69 usually
 * paired with -p on a high port) and is reflected in the startup banner so
 * the operator can see which mode is live. The spool path is identical in
 * both modes by design — that is the whole point of the frozen contract.
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "proto.h"
#include "spool.h"

#define TFTP_PORT_DEFAULT  69
#define TFTP_BLOCK_SIZE    512
#define RX_IDLE_TIMEOUT_MS 30000
#define RETX_TIMEOUT_MS    1000
#define MAX_RETX           5
#define DALLY_MS           1000
/* Size sanity bound: largest real partial is ~1.65 MB; 16 MiB is a
 * generous ceiling that still rejects absurd headers before streaming
 * (kept identical to mps3-pushd). */
#define MAX_PAYLOAD_BYTES  (16u * 1024u * 1024u)

/* TFTP opcodes (RFC 1350). */
#define TFTP_OP_RRQ   1
#define TFTP_OP_WRQ   2
#define TFTP_OP_DATA  3
#define TFTP_OP_ACK   4
#define TFTP_OP_ERROR 5

/* TFTP error codes (RFC 1350). */
#define TFTP_ERR_UNDEF     0
#define TFTP_ERR_ILLEGAL   4  /* illegal TFTP operation */
#define TFTP_ERR_UNKNOWN_TID 5

static uint32_t g_static_id = 0x00000000u;
static int      g_port      = TFTP_PORT_DEFAULT;
static const char *g_spool  = "/run/mps3";
static int      g_mock      = 0;

/* Staged-pair state (persists across sessions, like pushd's; the spool
 * files are the durable truth — flags are re-derived at startup). */
static int      g_clearing_staged;
static int      g_partial_staged;
static uint32_t g_staged_rm_id;

/* ---- active transfer ---------------------------------------------------- */
typedef enum { TS_HDR, TS_PAYLOAD, TS_DONE } xfer_state_t;

static int          g_tid_fd = -1;        /* per-transfer TID socket        */
static struct sockaddr_in g_peer;         /* client TID (for WRQ-dup match) */
static xfer_state_t g_state;
static uint8_t      g_hdr[MPS3_BITSTREAM_HDR_WIRE_SIZE];
static uint32_t     g_hdr_got;
static mps3_bitstream_hdr_t g_h;
static uint32_t     g_payload_left;
static uint32_t     g_crc;
static long long    g_last_rx_ms;         /* re-armed on progress           */
static long long    g_last_tx_ms;         /* last ACK send, for retx timer  */
static int          g_retx;               /* consecutive ACK retransmits    */
static uint16_t     g_expected_block;     /* next DATA block we want        */
static uint16_t     g_last_ack_block;     /* last block we ACKed            */
static int          g_completed;          /* final ACK sent, dallying       */
static int          g_sink_fd = -1;       /* <spool>/rx.tmp while receiving */

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* ---- TFTP packet helpers ------------------------------------------------ */

/* Send an ACK for block n on the TID socket (connect()ed to the client). */
static void tftp_send_ack(uint16_t block)
{
    uint8_t pkt[4];
    pkt[0] = 0;
    pkt[1] = TFTP_OP_ACK;
    pkt[2] = (uint8_t)(block >> 8);
    pkt[3] = (uint8_t)(block & 0xff);
    (void)send(g_tid_fd, pkt, sizeof(pkt), 0);
    g_last_ack_block = block;
    g_last_tx_ms = now_ms();
}

/* Send a TFTP ERROR. fd may be the TID socket (connect()ed -> use send) or
 * the listen socket for a busy/illegal refusal to a not-yet-adopted peer
 * (-> sendto). If to == NULL, send() on a connected fd; else sendto(). */
static void tftp_send_error(int fd, const struct sockaddr_in *to,
                            uint16_t code, const char *msg)
{
    uint8_t pkt[4 + 128];
    size_t mlen = strlen(msg);
    if (mlen > sizeof(pkt) - 5)
        mlen = sizeof(pkt) - 5;
    pkt[0] = 0;
    pkt[1] = TFTP_OP_ERROR;
    pkt[2] = (uint8_t)(code >> 8);
    pkt[3] = (uint8_t)(code & 0xff);
    memcpy(pkt + 4, msg, mlen);
    pkt[4 + mlen] = 0;
    size_t len = 4 + mlen + 1;
    if (to)
        (void)sendto(fd, pkt, len, 0, (const struct sockaddr *)to, sizeof(*to));
    else
        (void)send(fd, pkt, len, 0);
}

/* ---- spool sink (identical semantics to mps3-pushd) --------------------- */

static void sink_discard(void)
{
    char p[512];
    if (g_sink_fd >= 0) {
        close(g_sink_fd);
        g_sink_fd = -1;
    }
    (void)unlink(spool_path(g_spool, SPOOL_RX_TMP, NULL, p, sizeof(p)));
}

static void xfer_close(const char *why)
{
    if (g_tid_fd >= 0) {
        fprintf(stderr, "mps3-configd: transfer close (%s)\n", why);
        close(g_tid_fd);
        g_tid_fd = -1;
    }
    sink_discard();   /* no-op after a successful publish */
    g_completed = 0;
}

/* Abort with a TFTP ERROR to the client, then tear the transfer down. This
 * is the TFTP analogue of pushd's early close: the client is told the
 * transfer failed (TFTP has an ERROR packet, so — unlike the 6910 byte
 * stream — we can be explicit) and nothing is staged. */
static void xfer_abort(uint16_t code, const char *msg)
{
    if (g_tid_fd >= 0)
        tftp_send_error(g_tid_fd, NULL, code, msg);
    xfer_close(msg);
}

/* Header validation, in the contractual order:
 * magic (checked by unpack) / ver / static_id / kind / ordering / size.
 * Byte-identical to mps3-pushd's validate_header(). */
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

/* Full payload received; CRC gate LAST (after header validation, before
 * staging) — validation order is contractual. Returns 0 if published, -1 if
 * rejected. On -1 the caller sends the TFTP ERROR (no stage). Publish is the
 * byte-identical bin-then-meta atomic sequence from pushd's finish_payload(),
 * so the swap worker sees the same spool it does for a 6910 push. */
static int publish_payload(void)
{
    if (g_crc != g_h.crc32)
        return -1;

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
    if (ok && g_h.kind == MPS3_BITSTREAM_KIND_CLEARING)
        spool_unlink_pair(g_spool, SPOOL_PARTIAL);
    if (!ok || rename(tmp, bin) != 0 ||
        spool_meta_write(g_spool, base, &m) != 0)
        return -1;

    if (g_h.kind == MPS3_BITSTREAM_KIND_CLEARING) {
        g_clearing_staged = 1;
        g_partial_staged  = 0;   /* a new clearing drops a stale partial */
        fprintf(stderr, "mps3-configd: clearing staged (%u words)\n",
                (unsigned)g_h.len_words);
    } else {
        g_partial_staged = 1;
        g_staged_rm_id   = g_h.rm_id;
        fprintf(stderr, "mps3-configd: partial staged (rm_id=0x%08x, %u words)\n",
                g_h.rm_id, (unsigned)g_h.len_words);
    }
    return 0;
}

/* Feed a run of file bytes (a DATA block's payload, in order) through the
 * header/payload/CRC state machine — the streaming half of pushd's
 * sess_readable(), minus the TCP framing. Returns 0 on success, -1 if the
 * transfer must be aborted (the caller has already sent/​will send ERROR). */
static int feed_bytes(const uint8_t *buf, uint32_t r)
{
    uint32_t off = 0;
    while (off < r) {
        switch (g_state) {
        case TS_HDR: {
            uint32_t need = MPS3_BITSTREAM_HDR_WIRE_SIZE - g_hdr_got;
            uint32_t take = (r - off) < need ? (r - off) : need;
            memcpy(g_hdr + g_hdr_got, buf + off, take);
            g_hdr_got += take;
            off += take;
            if (g_hdr_got < MPS3_BITSTREAM_HDR_WIRE_SIZE)
                break;
            if (mps3_bitstream_hdr_unpack(g_hdr, g_hdr_got, &g_h) != 0 ||
                validate_header() != 0)
                return -1;
            {
                char p[512];
                g_sink_fd = open(spool_path(g_spool, SPOOL_RX_TMP, NULL,
                                            p, sizeof(p)),
                                 O_WRONLY | O_CREAT | O_TRUNC, 0644);
                if (g_sink_fd < 0) {
                    fprintf(stderr, "mps3-configd: spool open: %s\n",
                            strerror(errno));
                    return -1;
                }
            }
            g_payload_left = g_h.len_words * 4u;
            g_crc = mps3_crc32_init();
            g_state = TS_PAYLOAD;
            break;
        }
        case TS_PAYLOAD: {
            uint32_t take = (r - off) < g_payload_left
                          ? (r - off) : g_payload_left;
            g_crc = mps3_crc32_update(g_crc, buf + off, take);
            uint32_t done = 0;
            while (done < take) {
                ssize_t w = write(g_sink_fd, buf + off + done, take - done);
                if (w < 0) {
                    if (errno == EINTR)
                        continue;
                    fprintf(stderr, "mps3-configd: spool write: %s\n",
                            strerror(errno));
                    return -1;
                }
                done += (uint32_t)w;
            }
            g_payload_left -= take;
            off += take;
            if (g_payload_left == 0)
                g_state = TS_DONE;
            break;
        }
        case TS_DONE:
            /* Bytes beyond the declared frame: not well-framed, reject
             * (pushd's "bytes beyond frame"). */
            return -1;
        }
    }
    return 0;
}

/* Terminating short/zero DATA block seen: the whole file is in. Enforce a
 * complete, well-framed transfer, then run the CRC gate + publish. On
 * success the caller sends the FINAL ACK (the consumed signal). */
static int finish_transfer(void)
{
    if (g_state != TS_DONE || g_payload_left != 0)
        return -1;   /* torn: header incomplete or payload short */
    return publish_payload();
}

/* ---- transfer setup ----------------------------------------------------- */

/* Adopt a WRQ: parse "filename\0mode\0", require octet, open a TID socket
 * connect()ed to the client, ACK(0). Refuses (ERROR on the listen socket)
 * without disturbing the daemon on any malformed/unsupported request. */
static void wrq_start(int lfd, const struct sockaddr_in *from,
                      const uint8_t *pkt, ssize_t len)
{
    /* pkt[0..1] already known to be WRQ. Fields are NUL-terminated. */
    const char *p = (const char *)pkt + 2;
    const char *end = (const char *)pkt + len;
    const char *fname = p;
    while (p < end && *p)
        p++;
    if (p >= end) {   /* unterminated filename */
        tftp_send_error(lfd, from, TFTP_ERR_ILLEGAL, "malformed WRQ");
        return;
    }
    const char *mode = ++p;
    while (p < end && *p)
        p++;
    if (p >= end) {   /* unterminated mode */
        tftp_send_error(lfd, from, TFTP_ERR_ILLEGAL, "malformed WRQ");
        return;
    }
    if (strcasecmp(mode, "octet") != 0) {
        tftp_send_error(lfd, from, TFTP_ERR_ILLEGAL, "octet mode only");
        return;
    }

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        tftp_send_error(lfd, from, TFTP_ERR_UNDEF, "no socket");
        return;
    }
    /* Ephemeral TID; connect() so recv/send are pinned to this client and
     * stray packets from other peers are dropped by the kernel. */
    struct sockaddr_in local;
    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    local.sin_port = 0;
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0 ||
        connect(fd, (const struct sockaddr *)from, sizeof(*from)) < 0) {
        tftp_send_error(fd, NULL, TFTP_ERR_UNDEF, "tid setup failed");
        close(fd);
        return;
    }

    g_tid_fd = fd;
    g_peer = *from;
    g_state = TS_HDR;
    g_hdr_got = 0;
    g_payload_left = 0;
    g_completed = 0;
    g_retx = 0;
    g_expected_block = 1;
    g_last_ack_block = 0;
    g_last_rx_ms = now_ms();
    fprintf(stderr, "mps3-configd: WRQ '%.*s' octet from %s:%u\n",
            (int)(mode - fname - 1), fname, inet_ntoa(from->sin_addr),
            (unsigned)ntohs(from->sin_port));
    tftp_send_ack(0);   /* the RFC1350 WRQ acknowledgement */
}

/* A DATA packet arrived on the TID socket. */
static void tid_data(const uint8_t *pkt, ssize_t len)
{
    if (len < 4) {
        xfer_abort(TFTP_ERR_ILLEGAL, "short DATA");
        return;
    }
    uint16_t block = (uint16_t)((pkt[2] << 8) | pkt[3]);
    const uint8_t *data = pkt + 4;
    uint32_t dlen = (uint32_t)(len - 4);
    int terminating = (dlen < TFTP_BLOCK_SIZE);

    if (block == g_expected_block) {
        g_last_rx_ms = now_ms();
        g_retx = 0;
        if (feed_bytes(data, dlen) != 0) {
            xfer_abort(TFTP_ERR_UNDEF, "bad bitstream");
            return;
        }
        if (terminating) {
            if (finish_transfer() != 0) {
                xfer_abort(TFTP_ERR_UNDEF, "crc/framing reject");
                return;
            }
            /* FINAL ACK = the consumed/staged signal (TFTP analogue of
             * §2A.3 server-close-equals-consumed). Then dally to survive a
             * lost final ACK. */
            tftp_send_ack(block);
            g_completed = 1;
            fprintf(stderr, "mps3-configd: transfer complete (%u blocks)\n",
                    (unsigned)block);
        } else {
            tftp_send_ack(block);
            g_expected_block++;
        }
    } else if (block == g_last_ack_block) {
        /* Duplicate: our previous ACK (or the WRQ ACK0) was lost, or the
         * client re-sent the terminating block during our dally. Re-ACK,
         * do NOT re-feed. */
        tftp_send_ack(block);
    }
    /* Any other block number is out-of-window: ignore (RFC "sorcerer's
     * apprentice" avoidance — never ACK a block we did not expect). */
}

int main(int argc, char **argv)
{
    const char *id_file = "/etc/mps3/static_id";
    int opt;
    while ((opt = getopt(argc, argv, "p:i:d:mfh")) != -1) {
        switch (opt) {
        case 'p': g_port = atoi(optarg); break;
        case 'i': id_file = optarg; break;
        case 'd': g_spool = optarg; break;
        case 'm': g_mock = 1; break;
        case 'f': break;   /* accepted for parity with pushd (no-op) */
        default:
            fprintf(stderr,
                    "usage: %s [-p port] [-i static_id_file] [-d spool_dir] [-m]\n"
                    "  -p port   UDP TFTP listen port (default %d)\n"
                    "  -i file   static_id file (default /etc/mps3/static_id)\n"
                    "  -d dir    spool dir shared with mps3-ctrld (default /run/mps3)\n"
                    "  -m        mock/host mode (host/QEMU conformance; spool\n"
                    "            sink is identical — see file header)\n",
                    argv[0], TFTP_PORT_DEFAULT);
            return 2;
        }
    }
    if (spool_mkdir(g_spool) != 0) {
        fprintf(stderr, "mps3-configd: spool dir %s: %s\n", g_spool,
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

    /* Re-derive staged state from the spool (daemon restart survival), same
     * as pushd — the spool is the shared durable session state. */
    g_clearing_staged = spool_staged(g_spool, SPOOL_CLEARING);
    if (g_partial_staged || spool_staged(g_spool, SPOOL_PARTIAL)) {
        spool_meta_t m;
        g_partial_staged = 1;
        if (spool_meta_read(g_spool, SPOOL_PARTIAL, &m) == 0)
            g_staged_rm_id = m.rm_id;
    }
    if (g_clearing_staged || g_partial_staged)
        fprintf(stderr, "mps3-configd: spool carries staged %s%s%s\n",
                g_clearing_staged ? "clearing" : "",
                (g_clearing_staged && g_partial_staged) ? "+" : "",
                g_partial_staged ? "partial" : "");

    int lfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (lfd < 0) { perror("socket"); return 1; }
    int one = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_ANY);
    sa.sin_port = htons((uint16_t)g_port);
    if (bind(lfd, (struct sockaddr *)&sa, sizeof(sa)) < 0) { perror("bind"); return 1; }

    fprintf(stderr,
            "mps3-configd: TFTP listening on udp:%d static_id=0x%08x spool=%s (%s)\n",
            g_port, g_static_id, g_spool, g_mock ? "mock/host" : "board");

    for (;;) {
        struct pollfd pfds[2];
        int nf = 0;
        pfds[nf].fd = lfd;
        pfds[nf].events = POLLIN;
        nf++;
        if (g_tid_fd >= 0) {
            pfds[nf].fd = g_tid_fd;
            pfds[nf].events = POLLIN;
            nf++;
        }

        /* Timer: whichever of {retx of the last ACK, 30 s idle reap,
         * dally expiry} fires soonest. */
        int timeout = -1;
        if (g_tid_fd >= 0) {
            long long now = now_ms();
            long long idle_rem = (g_last_rx_ms + RX_IDLE_TIMEOUT_MS) - now;
            long long t;
            if (g_completed)
                t = (g_last_tx_ms + DALLY_MS) - now;
            else
                t = (g_last_tx_ms + RETX_TIMEOUT_MS) - now;
            if (idle_rem < t)
                t = idle_rem;
            timeout = t > 0 ? (int)t : 0;
        }

        int pr = poll(pfds, (nfds_t)nf, timeout);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            return 1;
        }

        /* 30 s RX-idle reap (bounds silence; re-armed on every accepted
         * block in tid_data()). */
        if (g_tid_fd >= 0 && now_ms() - g_last_rx_ms >= RX_IDLE_TIMEOUT_MS) {
            xfer_abort(TFTP_ERR_UNDEF, "rx idle timeout");
        }

        /* Service the active transfer before adopting a new WRQ (same
         * active-session-first discipline as pushd). */
        if (nf > 1 && g_tid_fd >= 0 &&
            (pfds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
            uint8_t pkt[4 + TFTP_BLOCK_SIZE];
            ssize_t r = recv(g_tid_fd, pkt, sizeof(pkt), 0);
            if (r >= 2) {
                uint16_t op = (uint16_t)((pkt[0] << 8) | pkt[1]);
                if (op == TFTP_OP_DATA) {
                    tid_data(pkt, r);
                } else if (op == TFTP_OP_ERROR) {
                    xfer_close("client ERROR");
                }
                /* stray opcodes on the TID socket: ignore */
            }
        }

        /* Retransmit / dally-close timing on the TID socket. */
        if (g_tid_fd >= 0) {
            long long now = now_ms();
            if (g_completed) {
                if (now - g_last_tx_ms >= DALLY_MS)
                    xfer_close("dally done");
            } else if (now - g_last_tx_ms >= RETX_TIMEOUT_MS) {
                if (++g_retx > MAX_RETX)
                    xfer_abort(TFTP_ERR_UNDEF, "peer timeout");
                else
                    tftp_send_ack(g_last_ack_block);   /* re-ACK to prod DATA */
            }
        }

        /* New request on :69. */
        if (pfds[0].revents & POLLIN) {
            struct sockaddr_in from;
            socklen_t fl = sizeof(from);
            uint8_t pkt[4 + TFTP_BLOCK_SIZE + 2];
            ssize_t r = recvfrom(lfd, pkt, sizeof(pkt), 0,
                                 (struct sockaddr *)&from, &fl);
            if (r >= 2) {
                uint16_t op = (uint16_t)((pkt[0] << 8) | pkt[1]);
                if (op == TFTP_OP_WRQ) {
                    /* A completed transfer only lingers to re-ACK a lost
                     * final ACK to its OWN client (dally). A fresh WRQ means
                     * a new deploy — end the dally and adopt it rather than
                     * refusing as busy. */
                    if (g_tid_fd >= 0 && g_completed &&
                        !(from.sin_addr.s_addr == g_peer.sin_addr.s_addr &&
                          from.sin_port == g_peer.sin_port))
                        xfer_close("dally preempted by new WRQ");
                    if (g_tid_fd < 0) {
                        wrq_start(lfd, &from, pkt, r);
                    } else if (from.sin_addr.s_addr == g_peer.sin_addr.s_addr &&
                               from.sin_port == g_peer.sin_port) {
                        /* Active client re-sent WRQ (its ACK0 was lost):
                         * re-ACK from the TID socket, do not refuse. */
                        tftp_send_ack(g_last_ack_block);
                    } else {
                        /* busy: refuse a second client, active transfer
                         * untouched (single-session discipline). */
                        tftp_send_error(lfd, &from, TFTP_ERR_UNDEF,
                                        "busy: one transfer at a time");
                    }
                } else if (op == TFTP_OP_RRQ) {
                    tftp_send_error(lfd, &from, TFTP_ERR_ILLEGAL,
                                    "write-only (WRQ)");
                }
                /* DATA/ACK to :69 with no session: ignore (stale peer). */
            }
        }
    }
}
