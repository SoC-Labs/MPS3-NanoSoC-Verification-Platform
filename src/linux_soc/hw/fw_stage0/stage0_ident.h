/*
 * stage0_ident.h -- the rescue-mode `identify` responder on UDP 6899
 * (plan §10 "identify", §10a S6).
 *
 * Request (the same one harnessd answers):
 *     {"op":"identify","v":1,"nonce":"<8-32 hex>"}
 * Reply, one datagram to the sender's addr:port, fixed key order:
 *     {"ok":true,"op":"identify","v":1,"nonce":"<echo>","board":"mps3",
 *      "mode":"rescue","shell_id":"0x........","ip":"a.b.c.d",
 *      "mac":"xxxxxxxxxxxx","reason":"<why stage0 is in rescue>",
 *      "ports":{"tftp":69}}
 * `mode:"rescue"` + `ports` without `ctrl` is how socharness tells stage0 from
 * a running harness (which also has 6900). shell_id is the FABRIC static_id
 * compiled into this stage0 (plan §10a S1). mac is 12 lowercase hex digits,
 * the form `stats.mac` already uses on the wire.
 *
 * Malformed requests are ignored silently; replies are rate-limited to
 * S0_IDENT_RATE per second (token bucket, burst S0_IDENT_RATE).
 */
#ifndef STAGE0_IDENT_H
#define STAGE0_IDENT_H

#include <stdint.h>
#include "stage0_status.h"

#define S0_IDENT_PORT   6899u
#define S0_IDENT_RATE   10u
#define S0_IDENT_REPLY_MAX 1200u

struct s0_ident_cfg {
    uint32_t ip;          /* host order */
    uint8_t  mac[6];
    uint32_t shell_id;    /* fabric static_id */
};

/* st may be NULL (then `reason` reads "unknown"). rescue_reason is read live,
 * so the reply always matches the status block; `identifies` counts replies. */
void s0_ident_init(const struct s0_ident_cfg *cfg, struct s0_status *st);
void s0_ident_poll(uint32_t now_ms);
void s0_ident_udp(uint32_t src_ip, uint16_t sport, const uint8_t *p, uint32_t len);

/* The request parser, exposed for the host tests. 0 = a valid identify
 * request (nonce copied, NUL-terminated), -1 = malformed. */
int s0_ident_parse(const uint8_t *p, uint32_t len, char nonce[33]);

#endif /* STAGE0_IDENT_H */
