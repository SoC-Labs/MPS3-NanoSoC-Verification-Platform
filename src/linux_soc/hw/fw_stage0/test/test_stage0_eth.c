/*
 * test_stage0_eth.c -- the rescue server over the REAL bare-metal LAN9220
 * driver (firmware/smsc911x/smsc911x.c, unmodified) through stage0's glue
 * (stage0_eth.c) and stage0's own mps3_spin_until (stage0_spin.c), against
 * firmware/test/fake_lan9220.c's register-level chip model (-DMPS3_HAL_MOCK).
 * This is the proof that the driver's four-accessor + spin HAL is all stage0
 * had to provide ("portability-only" = zero edits to firmware/smsc911x/).
 *
 *   1 s0_eth_init() brings the fake chip up with the harness MAC, promiscuous
 *   2 the gratuitous ARP leaves through the TX FIFO
 *   3 an ARP request in -> reply out; a ping in -> echo reply out
 *   4 a UDP round trip: TFTP read of stage0.status -> the 256-byte block
 *   5 TX FIFO full: s0_eth_tx gives up after its bound (clock moves per read)
 *   6 bad init (BYTE_TEST) is reported, not hung on
 */
#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_status.h"
#include "../stage0_rescue.h"
#include "../stage0_flow.h"
#include "smsc911x.h"
#include "mock_regs.h"
#include "fake_lan9220.h"

int s0_eth_init(uintptr_t base, const uint8_t mac[6]);
int s0_eth_tx(const void *frame, uint32_t len);

#define LAN_BASE 0xC0000000u
static const uint8_t SMAC[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
static const uint8_t CMAC[6] = { 0x3C, 0x11, 0x22, 0x33, 0x44, 0x55 };
#define SIP 0xC0A80A65u
#define CIP 0xC0A80A01u

static void p16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void p32(uint8_t *p, uint32_t v) { p16(p, v >> 16); p16(p + 2, v); }
static uint32_t g16(const uint8_t *p) { return ((uint32_t)p[0] << 8) | p[1]; }
static uint32_t g32(const uint8_t *p) { return (g16(p) << 16) | g16(p + 2); }
static uint16_t csum(const uint8_t *p, uint32_t n, uint32_t s)
{
    for (uint32_t i = 0; i < n; i++)
        s += (i & 1u) ? p[i] : ((uint32_t)p[i] << 8);
    while (s >> 16)
        s = (s & 0xFFFFu) + (s >> 16);
    return (uint16_t)~s;
}

static int no_verify(const uint8_t *i, uint32_t l, struct s0_result *o, void *c)
{
    (void)i; (void)l; (void)o; (void)c;
    return S0_EMAGIC;
}

int main(void)
{
    static struct s0_status st;
    static uint8_t stage[4096], f[1600], rx[1600];
    struct s0_result res;
    uint32_t now = 10;

    printf("test 1: s0_eth_init over the real smsc911x driver\n");
    mock_regs_reset();
    fake_lan9220_reset(LAN_BASE);
    mock_regs_set_us_per_read(1u);
    CHECK(s0_eth_init(LAN_BASE, SMAC) == 0, "init");
    CHECK(fake_lan9220_mac_csr(SMSC911X_MAC_ADDRL) == 0x4D000002u &&
          fake_lan9220_mac_csr(SMSC911X_MAC_ADDRH) == 0x5350u, "harness MAC programmed");
    CHECK(fake_lan9220_mac_csr(SMSC911X_MAC_CR) & SMSC911X_MAC_CR_PRMS, "promiscuous");

    struct s0_rescue_cfg cfg;
    memset(&cfg, 0, sizeof cfg);
    memcpy(cfg.mac, SMAC, 6);
    cfg.ip = SIP;
    cfg.shell_id = 0x3F1A560Fu;
    cfg.ddr_ok = 1;
    cfg.stage = stage;
    cfg.stage_max = sizeof stage;
    cfg.verify = no_verify;
    const struct s0_build_ids ids = { 1, 0x3F1A560Fu, 2 };
    s0_status_open(&st, &ids, 0, 2);
    s0_rescue_start(&cfg, &st);

    printf("test 2: the gratuitous ARP went out through the LAN9220 TX FIFO\n");
    int n = fake_lan9220_take_tx(rx, sizeof rx);
    CHECK(n >= 60 && g16(rx + 12) == 0x0806 && g32(rx + 28) == SIP, "announce (%d B)", n);

    printf("test 3: ARP request -> reply; ping -> echo reply\n");
    memset(f, 0, sizeof f);
    memset(f, 0xFF, 6); memcpy(f + 6, CMAC, 6); p16(f + 12, 0x0806);
    p16(f + 14, 1); p16(f + 16, 0x0800); f[18] = 6; f[19] = 4; p16(f + 20, 1);
    memcpy(f + 22, CMAC, 6); p32(f + 28, CIP); p32(f + 38, SIP);
    fake_lan9220_inject_rx(f, 64, 0);                    /* 60 + FCS */
    s0_rescue_poll(now++, &res);
    n = fake_lan9220_take_tx(rx, sizeof rx);
    CHECK(n >= 60 && g16(rx + 20) == 2 && memcmp(rx, CMAC, 6) == 0 && memcmp(rx + 22, SMAC, 6) == 0,
          "ARP reply");

    memset(f, 0, sizeof f);
    memcpy(f, SMAC, 6); memcpy(f + 6, CMAC, 6); p16(f + 12, 0x0800);
    uint8_t *ip = f + 14, *m = f + 34;
    ip[0] = 0x45; p16(ip + 2, 20 + 40); ip[8] = 64; ip[9] = 1; p32(ip + 12, CIP); p32(ip + 16, SIP);
    p16(ip + 10, csum(ip, 20, 0));
    m[0] = 8; p16(m + 4, 9); p16(m + 6, 1); memset(m + 8, 0x5A, 32);
    p16(m + 2, csum(m, 40, 0));
    fake_lan9220_inject_rx(f, 74 + 4, 0);
    s0_rescue_poll(now++, &res);
    n = fake_lan9220_take_tx(rx, sizeof rx);
    CHECK(n >= 74 && rx[23] == 1 && rx[34] == 0 && csum(rx + 34, 40, 0) == 0, "echo reply");
    CHECK(st.pings == 1u && st.rx_frames == 2u, "counters %u %u", st.pings, st.rx_frames);

    printf("test 4: UDP both ways: TFTP read of stage0.status\n");
    memset(f, 0, sizeof f);
    memcpy(f, SMAC, 6); memcpy(f + 6, CMAC, 6); p16(f + 12, 0x0800);
    static const char rrq[] = "\0\1stage0.status\0octet";   /* 22 bytes incl. the final NUL */
    uint8_t *u = f + 34;
    ip[0] = 0x45; p16(ip + 2, 20 + 8 + 22); ip[8] = 64; ip[9] = 17; p32(ip + 12, CIP); p32(ip + 16, SIP);
    p16(ip + 10, 0); p16(ip + 10, csum(ip, 20, 0));
    p16(u, 5555); p16(u + 2, 69); p16(u + 4, 8 + 22); p16(u + 6, 0);   /* no UDP checksum */
    memcpy(u + 8, rrq, 22);
    fake_lan9220_inject_rx(f, 64 + 4, 0);
    s0_rescue_poll(now++, &res);
    n = fake_lan9220_take_tx(rx, sizeof rx);
    CHECK(n == 14 + 20 + 8 + 4 + 256 && g16(rx + 34 + 2) == 5555 && g16(rx + 42) == 3 &&
          memcmp(rx + 46, "S0ST", 4) == 0, "status DATA (%d B)", n);

    printf("test 5: TX FIFO full -> s0_eth_tx gives up within its bound\n");
    fake_lan9220_set_tx_free(0);
    uint32_t t0 = mock_time_now_us();
    CHECK(s0_eth_tx(f, 60) == -1, "dropped");
    uint32_t took = mock_time_now_us() - t0;
    CHECK(took >= 5000u && took < 20000u, "bounded wait %u us", took);
    fake_lan9220_set_tx_free(0xFFFF);
    CHECK(s0_eth_tx(f, 60) == 0, "sends again when there is room");

    printf("test 6: a chip that fails BYTE_TEST is reported, not hung on\n");
    mock_regs_reset();
    fake_lan9220_reset(LAN_BASE);
    fake_lan9220_set_byte_test(0x12345678u);
    CHECK(s0_eth_init(LAN_BASE, SMAC) == SMSC911X_ERR_BYTE_TEST, "ERR_BYTE_TEST");

    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 rescue over smsc911x %s\n", g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
