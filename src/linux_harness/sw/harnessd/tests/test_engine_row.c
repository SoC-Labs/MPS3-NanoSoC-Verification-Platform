/*
 * test_engine_row.c — harnessd's CLCD ENGINE ROW provider (tofu_linux.c's
 * mps3_clcd_engine(), firmware/clcd/clcd.h "THE ENGINE ROW"): what the panel's
 * row 12 is told. The ROW itself (text, 40 columns, yielding to the banner, the
 * bare-metal blank) is firmware/test's test_clcd / test_clcd_engine.
 *
 * Here: unclaimed -> claimed with the claimed key's fingerprint prefix (the SAME
 * vector test_sshfp.c holds against ssh-keygen); a changed claim is re-hashed,
 * but only after the 1 s re-stat period (the row costs one stat a second); an
 * unclaim clears it; impl is version.impl's.
 *
 * And identify's ssh.key_sha256 (2026-09-26, HM_ANSWERS C1: tofu_linux.c's
 * mps3_identify_ssh()): the claim's FIRST key, whole, from the same cache -- ""
 * while unclaimed.
 */
#define _GNU_SOURCE
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../../../../../firmware/clcd/clcd.h"
#include "../../../../../firmware/identify/identify.h"
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
const char *mps3_proto_impl(void) { return "linux"; }
static uint64_t s_now = 5000000u;
uint64_t harnessd_now_us64(void) { return s_now; }

static const char KEY1[] =
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J me\n";
/* the same fingerprint `ssh-keygen -lf` printed for KEY1 (test_sshfp.c) */
static const char KEY1_FP8[] = "s6n1vtZI";
static const char KEY1_FP[] = "SHA256:s6n1vtZIZ6d1cBnloLKFQw3sQPxxRouFIgZLEdo3gT0";
static const char KEY2[] =
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQDM4aUuQyzZyb3LsIxbnQwvwZjB7lyApYp3fQyvNjs8 second\n";

int main(void)
{
    char ak[600];
    const char *tmp = getenv("TMPDIR");
    snprintf(ak, sizeof(ak), "%s/test_engine_row.%d.ak", tmp ? tmp : "/tmp", (int)getpid());
    unlink(ak);
    g_hd.authorized_keys = ak;

    mps3_clcd_engine_t e;
    memset(&e, 0, sizeof(e));
    CHECK(mps3_clcd_engine(&e) == 1);            /* the Linux engine always speaks */
    CHECK(e.impl && strcmp(e.impl, "linux") == 0);
    CHECK(e.claimed == 0 && e.fpr[0] == '\0');

    FILE *f = fopen(ak, "w");
    fputs(KEY1, f);
    fclose(f);
    CHECK(mps3_clcd_engine(&e) == 1 && e.claimed == 0);   /* cached: < 1 s since the stat */
    s_now += 1000000u;
    CHECK(mps3_clcd_engine(&e) == 1 && e.claimed == 1);
    CHECK(strcmp(e.fpr, KEY1_FP8) == 0);

    /* identify's ssh.key_sha256: the claim's first key, whole; a second line
     * (a claim may hold up to 64) does not change it */
    mps3_identify_ssh_t id;
    memset(&id, 0, sizeof(id));
    CHECK(mps3_identify_ssh(&id) == 1 && id.claimed == 1);
    CHECK(strcmp(id.key_sha256, KEY1_FP) == 0);
    f = fopen(ak, "a");
    fputs("# a comment\n", f);
    fputs(KEY2, f);
    fclose(f);
    CHECK(mps3_identify_ssh(&id) == 1 && strcmp(id.key_sha256, KEY1_FP) == 0);
    f = fopen(ak, "w");
    fputs(KEY1, f);
    fclose(f);

    /* negative control: another key must change the prefix (once re-stat'ed) */
    f = fopen(ak, "w");
    fputs("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGVub3RoZXIga2V5IHRoYXQgbmV2ZXIgd2lucyEhISE= x\n", f);
    fclose(f);
    s_now += 1000000u;
    CHECK(mps3_clcd_engine(&e) == 1 && e.claimed == 1);
    CHECK(strlen(e.fpr) == 8 && strcmp(e.fpr, KEY1_FP8) != 0);

    /* an unclaim (mps3-unclaim removes the file) */
    unlink(ak);
    s_now += 1000000u;
    CHECK(mps3_clcd_engine(&e) == 1 && e.claimed == 0 && e.fpr[0] == '\0');
    memset(&id, 0x55, sizeof(id));
    CHECK(mps3_identify_ssh(&id) == 1 && id.claimed == 0 && id.key_sha256[0] == '\0');

    if (s_fail) {
        fprintf(stderr, "test_engine_row: %d FAILED\n", s_fail);
        return 1;
    }
    printf("test_engine_row: ALL PASS\n");
    return 0;
}
