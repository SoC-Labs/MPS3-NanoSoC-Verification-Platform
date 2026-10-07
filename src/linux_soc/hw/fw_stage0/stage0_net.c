/*
 * stage0_net.c -- see stage0_net.h. Every multi-byte wire field is read and
 * written a byte at a time in network order, so nothing here depends on the
 * host's endianness or on the frame buffer's alignment.
 */
#include "stage0_net.h"

static uint8_t           s_mac[6];
static uint32_t          s_ip;
static struct s0_status *s_st;
static uint16_t          s_ip_id;
static uint8_t           s_tx[S0_FRAME_MAX];

/* Neighbour table: stage0 only answers, so every peer has just sent us a frame
 * and its MAC is known. Four entries is one TFTP client, one pinger and spare. */
#define NEIGH_N 4u
static struct { uint32_t ip; uint8_t mac[6]; } s_neigh[NEIGH_N];
static uint32_t s_neigh_next;

static const uint8_t k_bcast[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

static uint16_t get16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
static uint32_t get32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}
static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}
static void cpy(uint8_t *d, const uint8_t *s, uint32_t n)
{
    for (uint32_t i = 0; i < n; ++i)
        d[i] = s[i];
}
static int eq6(const uint8_t *a, const uint8_t *b)
{
    for (uint32_t i = 0; i < 6u; ++i)
        if (a[i] != b[i])
            return 0;
    return 1;
}

uint32_t s0_csum_add(uint32_t sum, const uint8_t *p, uint32_t len)
{
    uint32_t i;
    for (i = 0; i + 1u < len; i += 2u)
        sum += (uint32_t)((p[i] << 8) | p[i + 1u]);
    if (i < len)
        sum += (uint32_t)(p[i] << 8);
    return sum;
}

uint16_t s0_csum_fold(uint32_t sum)
{
    while (sum >> 16)
        sum = (sum & 0xFFFFu) + (sum >> 16);
    return (uint16_t)sum;
}

void s0_net_init(const uint8_t mac[6], uint32_t ip, struct s0_status *st)
{
    cpy(s_mac, mac, 6u);
    s_ip = ip;
    s_st = st;
    s_ip_id = 1u;
    s_neigh_next = 0u;
    for (uint32_t i = 0; i < NEIGH_N; ++i)
        s_neigh[i].ip = 0u;
}

static void learn(uint32_t ip, const uint8_t *mac)
{
    if (ip == 0u || (mac[0] & 1u))       /* unset, or a multicast source */
        return;
    for (uint32_t i = 0; i < NEIGH_N; ++i) {
        if (s_neigh[i].ip == ip) {
            cpy(s_neigh[i].mac, mac, 6u);
            return;
        }
    }
    s_neigh[s_neigh_next].ip = ip;
    cpy(s_neigh[s_neigh_next].mac, mac, 6u);
    s_neigh_next = (s_neigh_next + 1u) % NEIGH_N;
}

static const uint8_t *neigh_mac(uint32_t ip)
{
    for (uint32_t i = 0; i < NEIGH_N; ++i)
        if (s_neigh[i].ip == ip && ip != 0u)
            return s_neigh[i].mac;
    return 0;
}

/* Frames shorter than the 60-byte Ethernet minimum are zero-padded here rather
 * than trusted to the MAC's auto-pad, so what the host tests capture is what
 * goes on the wire. */
static int tx(uint32_t len)
{
    while (len < 60u)
        s_tx[len++] = 0u;
    int rc = s0_eth_tx(s_tx, len);
    if (rc == 0 && s_st)
        s_st->tx_frames += 1u;
    return rc;
}

static void eth_hdr(const uint8_t *dst, uint32_t type)
{
    cpy(s_tx, dst, 6u);
    cpy(s_tx + 6, s_mac, 6u);
    put16(s_tx + 12, type);
}

/* ---- ARP --------------------------------------------------------------------- */

static void arp_send(uint32_t op, const uint8_t *eth_dst, const uint8_t *tha, uint32_t tpa)
{
    static const uint8_t zero6[6] = { 0 };
    uint8_t *a = s_tx + S0_ETH_HDR;
    eth_hdr(eth_dst, S0_ETHERTYPE_ARP);
    put16(a + 0, 1u);                 /* htype Ethernet */
    put16(a + 2, S0_ETHERTYPE_IP);    /* ptype IPv4     */
    a[4] = 6u;
    a[5] = 4u;
    put16(a + 6, op);
    cpy(a + 8, s_mac, 6u);
    put32(a + 14, s_ip);
    cpy(a + 18, tha ? tha : zero6, 6u);
    put32(a + 24, tpa);
    (void)tx(S0_ETH_HDR + 28u);
}

void s0_net_announce(void)
{
    arp_send(1u, k_bcast, 0, s_ip);
}

static void arp_input(const uint8_t *a, uint32_t len)
{
    if (len < 28u || get16(a) != 1u || get16(a + 2) != S0_ETHERTYPE_IP ||
        a[4] != 6u || a[5] != 4u)
        return;
    uint32_t spa = get32(a + 14);
    uint32_t tpa = get32(a + 24);
    learn(spa, a + 8);
    if (get16(a + 6) == 1u && tpa == s_ip && spa != s_ip)
        arp_send(2u, a + 8, a + 8, spa);
}

/* ---- IPv4 -------------------------------------------------------------------- */

static void ip_hdr(uint8_t *ip, uint32_t dst, uint32_t proto, uint32_t payload_len)
{
    ip[0] = 0x45u;
    ip[1] = 0u;
    put16(ip + 2, S0_IP_HDR + payload_len);
    put16(ip + 4, s_ip_id++);
    put16(ip + 6, 0x4000u);           /* DF: stage0 never fragments */
    ip[8] = 64u;
    ip[9] = (uint8_t)proto;
    put16(ip + 10, 0u);
    put32(ip + 12, s_ip);
    put32(ip + 16, dst);
    put16(ip + 10, (uint16_t)~s0_csum_fold(s0_csum_add(0u, ip, S0_IP_HDR)));
}

static uint32_t udp_pseudo(uint32_t src, uint32_t dst, uint32_t ulen)
{
    uint8_t ph[12];
    put32(ph, src);
    put32(ph + 4, dst);
    ph[8] = 0u;
    ph[9] = 17u;
    put16(ph + 10, ulen);
    return s0_csum_add(0u, ph, sizeof ph);
}

int s0_udp_send(uint32_t dst_ip, uint16_t sport, uint16_t dport,
                const void *payload, uint32_t len)
{
    const uint8_t *mac = neigh_mac(dst_ip);
    if (!mac || len > S0_ETH_MTU - S0_IP_HDR - S0_UDP_HDR)
        return -1;
    uint8_t *ip = s_tx + S0_ETH_HDR;
    uint8_t *u = ip + S0_IP_HDR;
    uint32_t ulen = S0_UDP_HDR + len;

    eth_hdr(mac, S0_ETHERTYPE_IP);
    ip_hdr(ip, dst_ip, 17u, ulen);
    put16(u + 0, sport);
    put16(u + 2, dport);
    put16(u + 4, ulen);
    put16(u + 6, 0u);
    cpy(u + 8, (const uint8_t *)payload, len);
    uint16_t c = (uint16_t)~s0_csum_fold(s0_csum_add(udp_pseudo(s_ip, dst_ip, ulen), u, ulen));
    put16(u + 6, c ? c : 0xFFFFu);    /* 0 means "no checksum" on the wire */
    return tx(S0_ETH_HDR + S0_IP_HDR + ulen);
}

static void icmp_input(const uint8_t *eth_src, uint32_t src, const uint8_t *m, uint32_t len)
{
    if (len < 8u || m[0] != 8u || m[1] != 0u || s0_csum_fold(s0_csum_add(0u, m, len)) != 0xFFFFu)
        return;
    if (S0_ETH_HDR + S0_IP_HDR + len > sizeof s_tx)
        return;
    uint8_t *ip = s_tx + S0_ETH_HDR;
    uint8_t *r = ip + S0_IP_HDR;
    eth_hdr(eth_src, S0_ETHERTYPE_IP);
    ip_hdr(ip, src, 1u, len);
    cpy(r, m, len);
    r[0] = 0u;                         /* echo reply */
    put16(r + 2, 0u);
    put16(r + 2, (uint16_t)~s0_csum_fold(s0_csum_add(0u, r, len)));
    if (tx(S0_ETH_HDR + S0_IP_HDR + len) == 0 && s_st)
        s_st->pings += 1u;
}

static void ip_input(const uint8_t *f, uint32_t flen)
{
    const uint8_t *ip = f + S0_ETH_HDR;
    uint32_t avail = flen - S0_ETH_HDR;
    if (avail < S0_IP_HDR || (ip[0] >> 4) != 4u)
        return;
    uint32_t ihl = (uint32_t)(ip[0] & 0xFu) * 4u;
    uint32_t total = get16(ip + 2);
    if (ihl < S0_IP_HDR || total < ihl || total > avail)
        return;
    if (s0_csum_fold(s0_csum_add(0u, ip, ihl)) != 0xFFFFu)
        return;
    if ((get16(ip + 6) & 0x3FFFu) != 0u)   /* MF or a fragment offset */
        return;
    uint32_t src = get32(ip + 12);
    if (get32(ip + 16) != s_ip)
        return;
    learn(src, f + 6);

    const uint8_t *p = ip + ihl;
    uint32_t plen = total - ihl;
    if (ip[9] == 1u) {
        icmp_input(f + 6, src, p, plen);
    } else if (ip[9] == 17u) {
        if (plen < S0_UDP_HDR)
            return;
        uint32_t ulen = get16(p + 4);
        if (ulen < S0_UDP_HDR || ulen > plen)
            return;
        if (get16(p + 6) != 0u &&
            s0_csum_fold(s0_csum_add(udp_pseudo(src, s_ip, ulen), p, ulen)) != 0xFFFFu)
            return;
        s0_udp_input(src, get16(p), get16(p + 2), p + S0_UDP_HDR, ulen - S0_UDP_HDR);
    }
}

void s0_net_input(const uint8_t *frame, uint32_t len)
{
    if (len < S0_ETH_HDR)
        return;
    if (s_st)
        s_st->rx_frames += 1u;
    /* The LAN9220 runs promiscuous (the DUT shares the port): filter here. */
    if (!eq6(frame, s_mac) && !eq6(frame, k_bcast))
        return;
    uint32_t type = get16(frame + 12);
    if (type == S0_ETHERTYPE_ARP)
        arp_input(frame + S0_ETH_HDR, len - S0_ETH_HDR);
    else if (type == S0_ETHERTYPE_IP)
        ip_input(frame, len);
}
