/*
 * stage0_tftp.h -- the stage0 RESCUE SERVER: a write-only TFTP server on UDP 69
 * that takes ONE boot image (the same bytes as a uSD slot, stage0_pack.py's
 * format), verifies it in DDR, and hands it to the caller to boot.
 *
 * Protocol: RFC 1350 WRQ, octet mode only, plus RFC 2347 option negotiation
 * with blksize (RFC 2348, clamped to S0_TFTP_BLKSIZE_MAX so a block never
 * fragments on a 1500-byte MTU) and tsize (RFC 2349, rejected up front when
 * the image cannot fit). One session at a time. Lock-step, so no window.
 *
 *   - SINGLE-PORT: every reply (ACK, OACK, DATA, ERROR) comes FROM port 69,
 *     not from a fresh transfer ID, so it matches the flow the client opened
 *     and passes a stateful host firewall (the hub drops fresh-TID replies:
 *     B1 2026-09-24). The session is identified by the peer (ip, port); DATA
 *     from any other peer gets ERROR 5, and ACKs (a status read's) are ignored.
 *   - Retransmission is the server's too: no DATA for S0_TFTP_TIMEOUT_MS ->
 *     resend the last ACK/OACK, up to S0_TFTP_RETRIES, then drop the session.
 *   - A duplicate DATA (its ACK was lost) is re-ACKed, never re-written.
 *   - Block numbers wrap (65535 -> 0), so blksize 512 still carries 64 MiB.
 *   - THE VERDICT IS IN-BAND. The final DATA is ACKed only AFTER the whole
 *     image has been verified (header CRC + every region CRC, s0_load) -- a
 *     bad image gets a TFTP ERROR instead, stage0 stays in rescue and records
 *     why (status block rescue_rejects / rescue_last_rc). A client that sees
 *     the final ACK knows stage0 is booting what it sent.
 *   - After the final ACK the server dallies S0_TFTP_DALLY_MS, re-ACKing a
 *     repeated final DATA (the ACK was lost), then reports ready.
 *   - A read request for "stage0.status" returns the 256-byte status block
 *     (stage0_status.h) -- how stage0_push.py knows it is talking to stage0
 *     and not to the Linux/bare-metal config_agent on the same port 69.
 *
 * Portable: sends through s0_udp_send() (stage0_net.c on target; a POSIX UDP
 * harness in test/ drives the same state machine from stage0_push.py), and is
 * fed by s0_tftp_udp() (stage0_rescue.c's UDP dispatch).
 */
#ifndef STAGE0_TFTP_H
#define STAGE0_TFTP_H

#include <stdint.h>
#include "stage0_boot.h"
#include "stage0_status.h"

#define S0_TFTP_PORT          69u
#define S0_TFTP_BLKSIZE_DEF   512u
#define S0_TFTP_BLKSIZE_MAX   1468u   /* 1500 - 20 IP - 8 UDP - 4 TFTP */
#define S0_TFTP_BLKSIZE_MIN   8u      /* RFC 2348 */
#define S0_TFTP_BLKSIZE_CEIL  65464u  /* RFC 2348 */
#ifndef S0_TFTP_TIMEOUT_MS
#define S0_TFTP_TIMEOUT_MS    1000u
#endif
#ifndef S0_TFTP_RETRIES
#define S0_TFTP_RETRIES       8u
#endif
#ifndef S0_TFTP_DALLY_MS
#define S0_TFTP_DALLY_MS      2000u
#endif
#define S0_TFTP_STATUS_FILE   "stage0.status"

/* TFTP error codes (RFC 1350 / 2347) */
enum {
    S0_TFTP_E_UNDEF   = 0,
    S0_TFTP_E_NOTFOUND = 1,
    S0_TFTP_E_ACCESS  = 2,
    S0_TFTP_E_FULL    = 3,
    S0_TFTP_E_ILLEGAL = 4,
    S0_TFTP_E_TID     = 5,
    S0_TFTP_E_OPTION  = 8,
};

struct s0_tftp_cfg {
    uint8_t  *stage;          /* staging buffer (DDR on target)            */
    uint32_t  stage_max;      /* its size = the largest image accepted     */
    int       ddr_ok;         /* 0: refuse every WRQ (DDR not calibrated)  */
    /* Verify a received image (and place its regions): S0_* result. */
    int     (*verify)(const uint8_t *img, uint32_t len, struct s0_result *out, void *ctx);
    void     *verify_ctx;
    const void *status;       /* RRQ "stage0.status" payload (<= 511 B)    */
    uint32_t  status_len;
    void    (*log)(void *ctx, const char *line);   /* may be NULL */
    void     *log_ctx;
};

/* st may be NULL (the protocol tests); otherwise rescue_* fields are live. */
void s0_tftp_init(const struct s0_tftp_cfg *cfg, struct s0_status *st);

/* A UDP datagram for our address (any port; the server ignores what is not
 * port 69 or its current transfer ID). */
void s0_tftp_udp(uint32_t src_ip, uint16_t sport, uint16_t dport,
                 const uint8_t *payload, uint32_t len);

/* Call every loop pass: timeouts, retransmits, dally. */
void s0_tftp_poll(uint32_t now_ms);

/* 1 once an image was verified, its final ACK sent and the dally is over;
 * *out then holds the hand-off. 0 otherwise. */
int s0_tftp_ready(struct s0_result *out);

/* S0_RS_* (for tests and the console). */
uint32_t s0_tftp_state(void);

#endif /* STAGE0_TFTP_H */
