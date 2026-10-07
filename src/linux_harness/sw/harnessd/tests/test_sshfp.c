/*
 * test_sshfp.c — sshfp.c: SHA-256 (FIPS 180-2 vectors), unpadded base64, and the
 * host-key fingerprint from BOTH a dropbear key file and a .pub line, against a
 * fingerprint `ssh-keygen -lf` printed for the same key (vector below, generated
 * 2026-09-23). The OpenSSH private-key parser is checked against a live
 * ssh-keygen in test_harnessd_e2e.py.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../harnessd.h"

static int s_fail;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); s_fail++; } } while (0)

static const char PUB_LINE[] =
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J test@vec\n";
static const char WANT_FP[] = "SHA256:s6n1vtZIZ6d1cBnloLKFQw3sQPxxRouFIgZLEdo3gT0";
static const uint8_t PUB[32] = {
    0x5a,0x82,0x87,0x93,0xad,0x9b,0xdb,0xb9,0xe8,0x12,0x70,0x8b,0x28,0x7d,0x52,0xe7,
    0x92,0x6c,0xad,0xd8,0xce,0xa1,0xc7,0xb6,0x38,0x56,0xc8,0xe2,0x50,0xf5,0x7e,0x89,
};

static void hex(const uint8_t *h, char *out)
{
    for (int i = 0; i < 32; i++) sprintf(out + 2 * i, "%02x", h[i]);
}

int main(void)
{
    uint8_t h[32];
    char hx[65], b64[64];

    harnessd_sha256("abc", 3, h);
    hex(h, hx);
    CHECK(strcmp(hx, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") == 0);
    harnessd_sha256("", 0, h);
    hex(h, hx);
    CHECK(strcmp(hx, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == 0);
    const char *m448 = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    harnessd_sha256(m448, strlen(m448), h);   /* 56 B: the two-block padding case */
    hex(h, hx);
    CHECK(strcmp(hx, "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1") == 0);

    CHECK(harnessd_b64_encode((const uint8_t *)"ab", 2, b64, sizeof(b64), 1) == 4 &&
          strcmp(b64, "YWI=") == 0);
    CHECK(harnessd_b64_encode((const uint8_t *)"ab", 2, b64, sizeof(b64), 0) == 3 &&
          strcmp(b64, "YWI") == 0);

    const char *tmp = getenv("TMPDIR");
    char dir[256], key[300], pub[320], fp[64];
    snprintf(dir, sizeof(dir), "%s/test_sshfp.%d", tmp ? tmp : "/tmp", (int)getpid());
    snprintf(key, sizeof(key), "%s.key", dir);
    snprintf(pub, sizeof(pub), "%s.key.pub", dir);

    /* dropbear format: string "ssh-ed25519" | string(priv[32] || pub[32]) */
    uint8_t db[4 + 11 + 4 + 64];
    memcpy(db, "\0\0\0\x0bssh-ed25519\0\0\0\x40", 19);
    memset(db + 19, 0x77, 32);
    memcpy(db + 19 + 32, PUB, 32);
    FILE *f = fopen(key, "wb");
    fwrite(db, 1, sizeof(db), f);
    fclose(f);
    unlink(pub);
    CHECK(harnessd_host_key_fingerprint(key, fp, sizeof(fp)) == 0 && strcmp(fp, WANT_FP) == 0);

    /* negative: one flipped public-key bit must change the fingerprint */
    db[19 + 32] ^= 1u;
    f = fopen(key, "wb");
    fwrite(db, 1, sizeof(db), f);
    fclose(f);
    CHECK(harnessd_host_key_fingerprint(key, fp, sizeof(fp)) == 0 && strcmp(fp, WANT_FP) != 0);

    /* the .pub beside it wins */
    f = fopen(pub, "w");
    fputs(PUB_LINE, f);
    fclose(f);
    CHECK(harnessd_host_key_fingerprint(key, fp, sizeof(fp)) == 0 && strcmp(fp, WANT_FP) == 0);

    /* no key at all: -1 and "" */
    unlink(pub);
    unlink(key);
    CHECK(harnessd_host_key_fingerprint(key, fp, sizeof(fp)) == -1 && fp[0] == '\0');

    /* The TOFU claim's key (the CLCD engine row): the FIRST key of an
     * authorized_keys file, past comments, blank lines and an options prefix. */
    char ak[600];
    snprintf(ak, sizeof(ak), "%s.ak", dir);
    f = fopen(ak, "w");
    fputs("# the claim\n\nfrom=\"10.0.0.0/8\",no-pty ", f);
    fputs(PUB_LINE, f);
    fputs("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGVub3RoZXIga2V5IHRoYXQgbmV2ZXIgd2lucyEhISE= second\n", f);
    fclose(f);
    CHECK(harnessd_authorized_key_fingerprint(ak, fp, sizeof(fp)) == 0 && strcmp(fp, WANT_FP) == 0);
    /* negative: a type token whose blob names ANOTHER type is not a key */
    f = fopen(ak, "w");
    fputs("ssh-rsa AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J x\n", f);
    fclose(f);
    CHECK(harnessd_authorized_key_fingerprint(ak, fp, sizeof(fp)) == -1 && fp[0] == '\0');
    f = fopen(ak, "w");
    fputs("not a key at all\n", f);
    fclose(f);
    CHECK(harnessd_authorized_key_fingerprint(ak, fp, sizeof(fp)) == -1);
    unlink(ak);
    CHECK(harnessd_authorized_key_fingerprint(ak, fp, sizeof(fp)) == -1);

    if (s_fail) {
        fprintf(stderr, "test_sshfp: %d FAILED\n", s_fail);
        return 1;
    }
    printf("test_sshfp: ALL PASS\n");
    return 0;
}
