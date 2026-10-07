/*
 * identify.h — the UDP 6899 `identify` responder (net-protocol.md "Identify").
 *
 * WHAT IT ANSWERS: "what is at this address?" — board, MAC, IP, the fabric's
 * static_id, the resident RM, the harness release, which engine is running it,
 * whether its SSH has been claimed yet, and which ports serve what. One datagram
 * in, one datagram out, to the SENDER's addr:port.
 *
 * WHY A SEPARATE PORT AND NOT A 6900 VERB: 6900 is single-client and is PARKED
 * for the whole of a swap (the held reply). A discovery tool — socharness
 * scanning a subnet, a standalone PC looking for its board — must get an answer
 * while somebody else holds 6900 and while a swap is in flight. So nothing here
 * reads or waits on 6900 state: every value is a plain read of shell state the
 * coordinator already keeps (g_shell_state) or of a provider below.
 *
 * WHY A FIRMWARE SERVICE MODULE: both engines answer it. The bare-metal image
 * and the MicroBlaze V Linux harness (src/linux_harness/sw/harnessd) compile
 * this same file against common/net_if.h; what differs between them — MAC/IP/
 * DHCP, the Linux-only `impl`/`os_up_ms`/`ssh` keys, the device-DNA `unit` — comes
 * from the PROVIDER functions declared below, each with a WEAK default in
 * identify.c that states the bare-metal answer (or omits the key).
 *
 * Request  (unicast or broadcast; anything else is ignored SILENTLY):
 *   {"op":"identify","v":1,"nonce":"<8-32 hex digits>"}
 * Reply (one line of JSON, no newline, <= MPS3_IDENTIFY_REPLY_MAX bytes), keys
 * in this fixed order, [bracketed] ones present only when their provider says so:
 *   ok, op, v, nonce, board, mac, ip, dhcp, shell_id, rm_id, harness, proto,
 *   mode, [impl], [unit], up_ms, [os_up_ms], [ssh{claimed,host_key_sha256,
 *   key_sha256}], [label] (v0.16), ports{ctrl,push,tftp,jtag,xvc,uart0,uart1,swo}
 *
 * Rate limit: a token bucket of MPS3_IDENTIFY_RATE_PER_S tokens refilled at
 * that rate, so a broadcast storm costs the shell at most ~10 small sends per
 * second. A request that finds the bucket empty is dropped silently (the client
 * retries).
 *
 * BARE-METAL REGISTRATION is not done here: firmware/platform/src/main.c owns
 * that table (and it is full at MPS3_SVC_MAX). See HARNESSD_CONTRACT.md §9.2 for
 * the one-line hand-off and the lwIP broadcast-receive note.
 */
#ifndef MPS3_IDENTIFY_H
#define MPS3_IDENTIFY_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MPS3_PORT_IDENTIFY        6899u
#define MPS3_IDENTIFY_REPLY_MAX   1200u  /* bytes; one datagram, never fragmented on a 1500 MTU */
#define MPS3_IDENTIFY_RATE_PER_S  10u    /* replies per second, and the burst size */
#define MPS3_IDENTIFY_PROTO       "0.11" /* net-protocol.md version this shell speaks */

/* Open UDP 6899. Idempotent (net_if's udp_open rule). */
void identify_init(void);

/* Bounded: at most MPS3_IDENTIFY_DGRAMS_PER_POLL datagrams per call, never
 * blocks. Registered as a service-table row. */
#define MPS3_IDENTIFY_DGRAMS_PER_POLL 4
void identify_poll(void);

/* THE PURE CORE, exposed for the host tests: build the reply for one request
 * datagram. Returns the reply length (> 0), 0 if the request is malformed (the
 * caller then sends NOTHING), or -1 if the reply would not fit `cap` (a firmware
 * bug: cap is MPS3_IDENTIFY_REPLY_MAX in production). No rate limiting here. */
int identify_build_reply(const char *req, int req_len, char *out, int cap);

/* Replies sent / requests dropped by the rate limit / malformed requests, since
 * identify_init(). For tests and a future diag row. */
uint32_t identify_replies(void);
uint32_t identify_rate_drops(void);
uint32_t identify_malformed(void);

/* ---- providers (WEAK defaults in identify.c) ------------------------------ */

typedef struct {
    uint8_t mac[6];   /* the shell's own MAC                                     */
    char    ip[16];   /* dotted quad of the interface the shell serves on        */
    int     dhcp;     /* 1 = that address came from DHCP                         */
} mps3_identify_net_t;

/* Default: mps3_platform_mac() + the static MPS3_DEFAULT_IP_* address, dhcp 0
 * (the bare-metal image has no DHCP client). */
void mps3_identify_net(mps3_identify_net_t *out);

/* The device-DNA "unit" id as lowercase hex, or NULL (key omitted). Default
 * NULL until the D6 DNA register exists. */
const char *mps3_identify_unit(void);

typedef struct {
    int  claimed;               /* an authorized_keys has been claimed (TOFU) */
    char host_key_sha256[64];   /* "SHA256:<base64, no padding>" or ""        */
    char key_sha256[64];        /* the claim's FIRST key, the same rendering;
                                 * "" when unclaimed (2026-09-26, HM_ANSWERS C1).
                                 * Emitted after host_key_sha256.              */
} mps3_identify_ssh_t;

/* Returns 1 and fills *out when the engine runs an SSH server; 0 = no SSH
 * (key omitted). Default 0: bare metal has no SSH. */
int mps3_identify_ssh(mps3_identify_ssh_t *out);

/* "run" normally. Default "run"; stage0's own rescue responder answers
 * "rescue" (it does not link this file). */
const char *mps3_identify_mode(void);

/* v0.16 (lane IDENT): the board's resolved LABEL (e.g. "MPS3-02"), or NULL (key
 * omitted). Default NULL: bare metal has no identity resolver. mps3-harnessd
 * returns this boot's /run/mps3/identity label (identity_linux.c). */
const char *mps3_identify_label(void);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_IDENTIFY_H */
