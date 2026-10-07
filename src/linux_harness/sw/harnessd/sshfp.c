/*
 * sshfp.c — the SSH host-key fingerprint identify reports as
 * ssh.host_key_sha256, in the exact form `ssh` and `ssh-keygen -lf` print:
 *     SHA256:<base64(sha256(public key blob)), no '=' padding>
 *
 * The public key blob is the SSH wire encoding (RFC 4253 §6.6):
 *     string "ssh-ed25519" | string <32-byte public key>
 * and it can be recovered from any of the three forms the image may carry:
 *   1. dropbear's own key file (dropbearkey / dropbear -R):
 *        string "ssh-ed25519" | string (priv[32] || pub[32])
 *   2. an OpenSSH private key ("-----BEGIN OPENSSH PRIVATE KEY-----"): the
 *      base64 body is "openssh-key-v1\0" | string cipher | string kdf |
 *      string kdfopts | uint32 nkeys | string pubkey-blob | ...
 *   3. <key>.pub beside it: "ssh-ed25519 <base64 blob> comment"
 * .pub is tried first (cheapest, and authoritative when present).
 *
 * Why not shell out to dropbearkey -y: its fingerprint line has changed format
 * across releases (md5, then sha1, then sha256), and identify must answer in one
 * poll without forking. The hash is ~120 lines of well-known code.
 */
#define _GNU_SOURCE   /* memmem */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "harnessd.h"

/* ---- SHA-256 (FIPS 180-4) --------------------------------------------------- */
static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(uint32_t h[8], const uint8_t *p)
{
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)p[4*i] << 24) | ((uint32_t)p[4*i+1] << 16) |
               ((uint32_t)p[4*i+2] << 8) | p[4*i+3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i-15], 7) ^ ROR(w[i-15], 18) ^ (w[i-15] >> 3);
        uint32_t s1 = ROR(w[i-2], 17) ^ ROR(w[i-2], 19) ^ (w[i-2] >> 10);
        w[i] = w[i-16] + s0 + w[i-7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = hh + S1 + ch + K[i] + w[i];
        uint32_t S0 = ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22);
        uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + mj;
        hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

void harnessd_sha256(const void *data, size_t len, uint8_t out[32])
{
    uint32_t h[8] = { 0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                      0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19 };
    const uint8_t *p = data;
    size_t left = len;
    while (left >= 64) {
        sha256_block(h, p);
        p += 64;
        left -= 64;
    }
    uint8_t tail[128];
    memset(tail, 0, sizeof(tail));
    memcpy(tail, p, left);
    tail[left] = 0x80;
    size_t tl = (left < 56) ? 64 : 128;
    uint64_t bits = (uint64_t)len * 8u;
    for (int i = 0; i < 8; i++) {
        tail[tl - 1 - i] = (uint8_t)(bits >> (8 * i));
    }
    sha256_block(h, tail);
    if (tl == 128) {
        sha256_block(h, tail + 64);
    }
    for (int i = 0; i < 8; i++) {
        out[4*i]   = (uint8_t)(h[i] >> 24);
        out[4*i+1] = (uint8_t)(h[i] >> 16);
        out[4*i+2] = (uint8_t)(h[i] >> 8);
        out[4*i+3] = (uint8_t)h[i];
    }
}

/* ---- base64 ------------------------------------------------------------------ */
static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int harnessd_b64_encode(const uint8_t *in, size_t n, char *out, size_t cap, int pad)
{
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        size_t k = n - i;
        if (k > 1) v |= (uint32_t)in[i+1] << 8;
        if (k > 2) v |= in[i+2];
        char q[4] = { B64[(v >> 18) & 63], B64[(v >> 12) & 63],
                      k > 1 ? B64[(v >> 6) & 63] : '=', k > 2 ? B64[v & 63] : '=' };
        for (int j = 0; j < 4; j++) {
            if (q[j] == '=' && !pad) continue;
            if (o + 1 >= cap) return -1;
            out[o++] = q[j];
        }
    }
    if (o >= cap) return -1;
    out[o] = '\0';
    return (int)o;
}

static int b64_val(char c)
{
    const char *p = strchr(B64, c);
    return (c && p) ? (int)(p - B64) : -1;
}

/* Decodes, skipping whitespace; stops at '=' or any non-base64 byte. */
static size_t b64_decode(const char *in, uint8_t *out, size_t cap)
{
    uint32_t acc = 0;
    int bits = 0;
    size_t o = 0;
    for (; *in; in++) {
        if (*in == '\n' || *in == '\r' || *in == ' ' || *in == '\t') continue;
        int v = b64_val(*in);
        if (v < 0) break;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= cap) return o;
            out[o++] = (uint8_t)(acc >> bits);
        }
    }
    return o;
}

/* ---- key parsing -------------------------------------------------------------- */
static uint32_t be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

/* An SSH string at *off; returns its length and advances, or -1. */
static long ssh_string(const uint8_t *buf, size_t len, size_t *off, const uint8_t **s)
{
    if (*off + 4 > len) return -1;
    uint32_t n = be32(buf + *off);
    if (n > len - *off - 4) return -1;
    *s = buf + *off + 4;
    *off += 4 + n;
    return (long)n;
}

static int fp_of_blob(const uint8_t *blob, size_t n, char *out, size_t cap)
{
    uint8_t h[32];
    harnessd_sha256(blob, n, h);
    if (cap < 8) return -1;
    memcpy(out, "SHA256:", 7);
    return harnessd_b64_encode(h, 32, out + 7, cap - 7, 0) < 0 ? -1 : 0;
}

static int fp_from_pub(const char *path, char *out, size_t cap)
{
    char line[2048], type[64], b64[1600];
    uint8_t blob[1200];
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char *ok = fgets(line, sizeof(line), f);
    fclose(f);
    if (!ok || sscanf(line, "%63s %1599s", type, b64) != 2) return -1;
    size_t n = b64_decode(b64, blob, sizeof(blob));
    return n ? fp_of_blob(blob, n, out, cap) : -1;
}

static int fp_from_openssh(const uint8_t *file, size_t flen, char *out, size_t cap)
{
    static const char BEGIN[] = "-----BEGIN OPENSSH PRIVATE KEY-----";
    const char *s = memmem(file, flen, BEGIN, sizeof(BEGIN) - 1);
    if (!s) return -1;
    s += sizeof(BEGIN) - 1;
    char body[8192];
    size_t bl = 0;
    const char *end = (const char *)file + flen;
    while (s < end && *s != '-' && bl + 1 < sizeof(body)) body[bl++] = *s++;
    body[bl] = '\0';
    uint8_t raw[6144];
    size_t n = b64_decode(body, raw, sizeof(raw));
    static const char MAGIC[] = "openssh-key-v1";
    if (n < sizeof(MAGIC) || memcmp(raw, MAGIC, sizeof(MAGIC)) != 0) return -1;
    size_t off = sizeof(MAGIC);   /* includes the NUL */
    const uint8_t *str;
    for (int i = 0; i < 3; i++) {   /* ciphername, kdfname, kdfoptions */
        if (ssh_string(raw, n, &off, &str) < 0) return -1;
    }
    if (off + 4 > n || be32(raw + off) < 1) return -1;
    off += 4;
    long pl = ssh_string(raw, n, &off, &str);
    return (pl > 0) ? fp_of_blob(str, (size_t)pl, out, cap) : -1;
}

static int fp_from_dropbear(const uint8_t *file, size_t flen, char *out, size_t cap)
{
    size_t off = 0;
    const uint8_t *type, *key;
    long tl = ssh_string(file, flen, &off, &type);
    if (tl != 11 || memcmp(type, "ssh-ed25519", 11) != 0) return -1;
    long kl = ssh_string(file, flen, &off, &key);
    if (kl != 64) return -1;
    uint8_t blob[4 + 11 + 4 + 32];
    blob[0] = 0; blob[1] = 0; blob[2] = 0; blob[3] = 11;
    memcpy(blob + 4, "ssh-ed25519", 11);
    blob[15] = 0; blob[16] = 0; blob[17] = 0; blob[18] = 32;
    memcpy(blob + 19, key + 32, 32);   /* priv[32] || pub[32] */
    return fp_of_blob(blob, sizeof(blob), out, cap);
}

int harnessd_host_key_fingerprint(const char *key_path, char *out, size_t cap)
{
    char pub[600];
    if (!key_path || !out || cap == 0) return -1;
    out[0] = '\0';
    snprintf(pub, sizeof(pub), "%s.pub", key_path);
    if (fp_from_pub(pub, out, cap) == 0) return 0;

    FILE *f = fopen(key_path, "rb");
    if (!f) return -1;
    uint8_t buf[8192];
    size_t n = fread(buf, 1, sizeof(buf), f);
    fclose(f);
    if (fp_from_openssh(buf, n, out, cap) == 0) return 0;
    if (fp_from_dropbear(buf, n, out, cap) == 0) return 0;
    out[0] = '\0';
    return -1;
}

/* The fingerprint of the FIRST public key in an authorized_keys file (the TOFU
 * claim), OpenSSH style -- what `ssh-keygen -lf <that key>.pub` prints. A line
 * may carry options before the key ("from=... ssh-ed25519 AAAA... c"), so the key
 * is the first token whose NEXT token base64-decodes to a blob whose own leading
 * SSH string names that same type: the self-describing check OpenSSH relies on.
 * 0 = found, -1 = no readable key. */
int harnessd_authorized_key_fingerprint(const char *path, char *out, size_t cap)
{
    char buf[16384 + 1];
    FILE *f = path ? fopen(path, "r") : 0;
    if (!out || cap == 0) return -1;
    out[0] = '\0';
    if (!f) return -1;
    size_t n = fread(buf, 1, sizeof(buf) - 1u, f);
    fclose(f);
    buf[n] = '\0';
    for (char *line = strtok(buf, "\n"); line; line = strtok(0, "\n")) {
        char *tok[16];
        int nt = 0;
        for (char *save = 0, *t = strtok_r(line, " \t\r", &save); t && nt < 16;
             t = strtok_r(0, " \t\r", &save)) {
            tok[nt++] = t;
        }
        if (nt == 0 || tok[0][0] == '#') continue;
        for (int i = 0; i + 1 < nt; i++) {
            uint8_t blob[1200];
            size_t bl = b64_decode(tok[i + 1], blob, sizeof(blob));
            size_t off = 0;
            const uint8_t *type;
            long tl = bl ? ssh_string(blob, bl, &off, &type) : -1;
            if (tl > 0 && (size_t)tl == strlen(tok[i]) && memcmp(type, tok[i], (size_t)tl) == 0) {
                return fp_of_blob(blob, bl, out, cap);
            }
        }
    }
    return -1;
}
