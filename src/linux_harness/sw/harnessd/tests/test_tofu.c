/*
 * test_tofu.c — the TOFU first-key claim through the REAL config_agent.c TFTP
 * path (firmware/test/fake_net_if.c carries the datagrams), built TWICE:
 *
 *   test_tofu            + harnessd's tofu_linux.c (the strong named-file sink):
 *                        an oversize claim is ERROR 3 and leaves nothing; a torn
 *                        claim leaves nothing; an empty claim is refused; a good
 *                        claim lands atomically (0600, dir 0700) and ACKs; a
 *                        SECOND claim is ERROR 2 and the file is untouched.
 *   test_tofu_baremetal  no provider linked (-DTEST_BAREMETAL_PROVIDER): the
 *                        weak default refuses every claim with ERROR 2 at the
 *                        WRQ and opens no transfer.
 *
 * Both: a bitstream WRQ under any other filename still takes the bitstream path
 * (ACK 0, then header validation), i.e. the only thing the hook changed is the
 * one exact filename.
 */
#define _GNU_SOURCE
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../../../../../firmware/config_agent/config_agent.h"
#include "../../../../../firmware/common/net_proto.h"
#include "../../../../../firmware/test/fake_net_if.h"
#include "../harnessd.h"

static int s_fail;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); s_fail++; } } while (0)

harnessd_cfg_t g_hd;
void harnessd_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
}

#define CLIENT 50000u

/* SINGLE-PORT TFTP: every reply -- ACK 0, every DATA ACK, every ERROR -- must
 * come FROM :69. The hub's stateful firewall drops anything from a fresh
 * transfer ID (B1 2026-09-24), so a claim answered from one never lands. */
static int take(uint16_t *from, uint8_t *pkt, int cap)
{
    config_agent_poll();
    uint16_t to = 0;
    int n = fake_net_udp_take_sent(from, &to, pkt, cap);
    if (n >= 0) CHECK(to == CLIENT);
    if (n >= 0) CHECK(*from == MPS3_PORT_TFTP);
    return n;
}

/* A WRQ; returns the port the ACK(0) came from (always :69), 0 if the answer
 * was an ERROR (its code in *err). */
static uint16_t wrq(const char *name, int *err)
{
    uint8_t pkt[600], w[128];
    int n = 0;
    w[n++] = 0; w[n++] = 2;
    n += sprintf((char *)w + n, "%s", name) + 1;
    n += sprintf((char *)w + n, "octet") + 1;
    CHECK(fake_net_udp_inject(MPS3_PORT_TFTP, CLIENT, w, n) == 0);
    uint16_t from = 0;
    int r = take(&from, pkt, sizeof(pkt));
    *err = -1;
    if (r >= 4 && pkt[1] == 4 && pkt[2] == 0 && pkt[3] == 0) return from;   /* ACK 0 */
    if (r >= 4 && pkt[1] == 5) *err = (pkt[2] << 8) | pkt[3];
    return 0;
}

/* One DATA block to the session port (:69); returns the ACKed block, or
 * -(error code). */
static int data(uint16_t tid, uint16_t block, const void *buf, int len)
{
    uint8_t pkt[600], d[600];
    d[0] = 0; d[1] = 3; d[2] = (uint8_t)(block >> 8); d[3] = (uint8_t)block;
    memcpy(d + 4, buf, (size_t)len);
    CHECK(fake_net_udp_inject(tid, CLIENT, d, 4 + len) == 0);
    uint16_t from = 0;
    int r = take(&from, pkt, sizeof(pkt));
    if (r >= 4 && pkt[1] == 4) return (pkt[2] << 8) | pkt[3];
    if (r >= 4 && pkt[1] == 5) return -((pkt[2] << 8) | pkt[3]);
    return -1000;
}

static int exists(const char *p)
{
    struct stat st;
    return stat(p, &st) == 0;
}

int main(void)
{
    char dir[256], ak[300], tmpf[320];
    const char *tmp = getenv("TMPDIR");
    snprintf(dir, sizeof(dir), "%s/test_tofu.%d", tmp ? tmp : "/tmp", (int)getpid());
    snprintf(ak, sizeof(ak), "%s/ssh/authorized_keys", dir);
    snprintf(tmpf, sizeof(tmpf), "%s.claim.tmp", ak);
    mkdir(dir, 0755);
    g_hd.authorized_keys = ak;
    g_hd.host_key = "/nonexistent";

    fake_net_reset();
    config_agent_init();
    config_agent_set_running_static_id(0x12345678u);
    int err = 0;
    uint16_t tid = 0;

#ifdef TEST_BAREMETAL_PROVIDER
    CHECK(wrq(MPS3_CFG_TOFU_FILENAME, &err) == 0 && err == 2);
    CHECK(!exists(ak));
    CHECK(fake_net_udp_sent_count() == 0);
#else
    uint8_t big[512];
    memset(big, 'k', sizeof(big));

    /* oversize: 33 full blocks = 16.5 KiB > 16 KiB -> ERROR 3, nothing left */
    tid = wrq(MPS3_CFG_TOFU_FILENAME, &err);
    CHECK(tid != 0);
    int last = 0;
    for (uint16_t b = 1; b <= 33 && last >= 0; b++) last = data(tid, b, big, sizeof(big));
    CHECK(last == -3);
    CHECK(!exists(ak) && !exists(tmpf));

    /* torn: a full block, then the client gives up (ERROR) -> nothing left */
    tid = wrq(MPS3_CFG_TOFU_FILENAME, &err);
    CHECK(tid != 0 && data(tid, 1, big, sizeof(big)) == 1);
    uint8_t e[5] = { 0, 5, 0, 0, 0 };
    CHECK(fake_net_udp_inject(tid, CLIENT, e, sizeof(e)) == 0);
    config_agent_poll();
    CHECK(!exists(ak) && !exists(tmpf));

    /* empty: refused at finish (ERROR 2) */
    tid = wrq(MPS3_CFG_TOFU_FILENAME, &err);
    CHECK(tid != 0 && data(tid, 1, "", 0) == -2);
    CHECK(!exists(ak));

    /* the claim */
    static const char key[] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J me@pc\n";
    tid = wrq(MPS3_CFG_TOFU_FILENAME, &err);
    CHECK(tid != 0 && data(tid, 1, key, (int)strlen(key)) == 1);
    struct stat st;
    CHECK(stat(ak, &st) == 0 && (st.st_mode & 0777) == 0600 && st.st_size == (off_t)strlen(key));
    char dirp[300];
    snprintf(dirp, sizeof(dirp), "%s/ssh", dir);
    CHECK(stat(dirp, &st) == 0 && (st.st_mode & 0777) == 0700);
    CHECK(harnessd_ssh_claimed() == 1);

    /* a second claim: ERROR 2 at the WRQ, the file untouched */
    CHECK(wrq(MPS3_CFG_TOFU_FILENAME, &err) == 0 && err == 2);
    CHECK(stat(ak, &st) == 0 && st.st_size == (off_t)strlen(key));
#endif

    /* the bitstream path is untouched: any other name is an ordinary WRQ (ACK 0)
     * and a non-MPS3 payload is then rejected by header validation. */
    tid = wrq("bitstream.bin", &err);
    CHECK(tid != 0);
    CHECK(data(tid, 1, "not an MPS3 frame, just bytes.", 30) != 1);   /* ERROR, not ACK 1 */

    char cmd[400];
    snprintf(cmd, sizeof(cmd), "rm -rf %s", dir);
    if (system(cmd) != 0) { /* best effort */ }
    if (s_fail) {
        fprintf(stderr, "test_tofu: %d FAILED\n", s_fail);
        return 1;
    }
#ifdef TEST_BAREMETAL_PROVIDER
    printf("test_tofu_baremetal: ALL PASS\n");
#else
    printf("test_tofu: ALL PASS\n");
#endif
    return 0;
}
