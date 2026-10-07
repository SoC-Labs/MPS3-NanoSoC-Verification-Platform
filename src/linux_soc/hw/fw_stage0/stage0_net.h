/*
 * stage0_net.h -- the smallest IPv4 stack the rescue server needs, and nothing
 * more: Ethernet II, ARP (reply + one announcement), IPv4 (no fragments, no
 * options honoured, no routing: every reply goes to the MAC it came from),
 * ICMP echo (so `ping` answers "stage0 is alive") and UDP with checksums.
 * No lwIP, no sockets, no heap, no libc. Portable: the same file runs on the
 * MBV and in the host tests.
 *
 * Seams (link-time):
 *   s0_eth_tx()     provided by the platform: stage0_eth.c (smsc911x) on the
 *                   target and in the fake_lan9220 test; a frame queue in the
 *                   protocol tests.
 *   s0_udp_input()  provided by the one UDP consumer, stage0_tftp.c.
 */
#ifndef STAGE0_NET_H
#define STAGE0_NET_H

#include <stdint.h>
#include "stage0_status.h"

#define S0_ETH_HDR      14u
#define S0_IP_HDR       20u
#define S0_UDP_HDR      8u
#define S0_ETH_MTU      1500u
#define S0_FRAME_MAX    1536u    /* >= 14 + 1500 + FCS, rounded */

#define S0_ETHERTYPE_IP   0x0800u
#define S0_ETHERTYPE_ARP  0x0806u

/* Bring the stack up for one MAC + one host-order IPv4 address. st (may be
 * NULL) receives the rx/tx/ping counters. Clears the neighbour table. */
void s0_net_init(const uint8_t mac[6], uint32_t ip, struct s0_status *st);

/* One received Ethernet frame (the length may include the FCS: IPv4 parsing
 * uses the IP total length, never the frame length). */
void s0_net_input(const uint8_t *frame, uint32_t len);

/* Send a UDP datagram. The destination MAC comes from the neighbour table,
 * which learns (ip -> mac) from every ARP packet and every IPv4 frame
 * addressed to us -- stage0 only ever answers, so the peer is always known.
 * Returns 0, or -1 (peer unknown / too big / TX failed). */
int s0_udp_send(uint32_t dst_ip, uint16_t sport, uint16_t dport,
                const void *payload, uint32_t len);

/* Broadcast one ARP announcement (RFC 5227: request, sender = target = us). */
void s0_net_announce(void);

/* Platform seam: transmit one frame (no FCS). 0 ok, <0 failed. */
int s0_eth_tx(const void *frame, uint32_t len);

/* Upcall: a UDP datagram for our address, checksum already verified. */
void s0_udp_input(uint32_t src_ip, uint16_t sport, uint16_t dport,
                  const uint8_t *payload, uint32_t len);

/* One's-complement Internet checksum (RFC 1071) over a buffer, folded, not
 * inverted, continuing from `sum`. Exposed for the host tests. */
uint32_t s0_csum_add(uint32_t sum, const uint8_t *p, uint32_t len);
uint16_t s0_csum_fold(uint32_t sum);

#endif /* STAGE0_NET_H */
