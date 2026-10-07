/*
 * mps3-slot — the Linux side of stage0's user-microSD layout
 * (docs/planning/linux_lanes/STAGE0_CONTRACT.md §6), for admins on ssh. Stage0
 * only READS the card; this writes it.
 *
 * A THIN CLI. Every rule that decides what stage0 will do or what reaches the
 * card -- the MBR slot parse, which boot-select copy stage0 uses and which one a
 * writer may rewrite, the write guard, flush + uncache before every read-back,
 * the image check through stage0's own loader -- is in ONE place,
 * src/linux_harness/sw/harnessd/slot_card.[ch], which mps3-harnessd's `slot`
 * verb links too (so the two writers of this card cannot drift again), over
 * STAGE0's stage0_core.c.
 *
 *   mps3-slot [--disk D] status
 *   mps3-slot [--disk D] default A|B
 *        the boot-select sector: NEVER touch the copy stage0 is using. Rewrite
 *        the other one with max(seq)+1 and read both back off the card. A torn
 *        write leaves the old pick valid. A new seq also resets stage0's attempt
 *        counters (STAGE0_CONTRACT §4).
 *   mps3-slot [--disk D] write A|B IMAGE [--force]
 *        an S0LB boot image (stage0_pack.py output; FLOW's linux_slot.img) into
 *        that slot: stage0's loader accepts it BEFORE a byte is written; the
 *        first sector (the header) goes LAST, so a torn write leaves no valid
 *        table behind; then stage0's loader must accept what is ON THE CARD.
 *        Refuses the current default slot unless --force.
 *
 * D defaults to /dev/mmcblk0. Slots are addressed by the MBR's LBA. Exit 0 ok,
 * 1 refused/failed, 2 usage. The test seam (a caching mock device,
 * br2_external/tests/run.sh) is slot_card.c's.
 */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "slot_card.h"

#define BLK SLOT_CARD_BLK

static const char *disk = "/dev/mmcblk0";

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static int slot_idx(const char *s)
{
    if (!strcmp(s, "A") || !strcmp(s, "a") || !strcmp(s, "1"))
        return 0;
    if (!strcmp(s, "B") || !strcmp(s, "b") || !strcmp(s, "2"))
        return 1;
    return -1;
}

/* ---- commands ----------------------------------------------------------------- */
static int cmd_status(int fd)
{
    slot_card_t c;
    int rc = slot_card_read(fd, &c);
    if (rc == -2) {
        printf("%s: no MBR signature — not a harness card\n", disk);
        return 1;
    }
    if (rc)
        return 1;
    for (int i = 0; i < 2; i++) {
        printf("slot %c: ", 'A' + i);
        if (!c.slot[i].first_lba) {
            printf("absent (MBR entry %d is not type 0x7F)\n", i + 1);
            continue;
        }
        uint8_t h[32];
        printf("LBA %u, %u MiB, ", c.slot[i].first_lba, c.slot[i].nblocks / 2048u);
        if (slot_card_rd(fd, slot_card_off(&c, i), h, sizeof(h)))
            printf("unreadable\n");
        else if (le32(h) != S0_MAGIC)
            printf("empty (no S0LB header)\n");
        else
            printf("S0LB v%u, %u region(s), pc 0x%08x, header_crc 0x%08x\n",
                   le32(h + 4), le32(h + 8), le32(h + 12), le32(h + 28));
    }
    printf("boot-select: LBA1 seq %u%s, LBA2 seq %u%s -> default %c (seq %u, in use: %s)\n",
           c.seqs[0], c.valid[0] ? "" : " (invalid)", c.seqs[1], c.valid[1] ? "" : " (invalid)",
           c.deflt == 2 ? 'B' : 'A', c.seq,
           c.winner == 0 ? "LBA1" : c.winner == 1 ? "LBA2" : "neither, A by rule");
    return 0;
}

static int cmd_default(int fd, int idx)
{
    slot_card_t c;
    if (slot_card_read(fd, &c) || !c.slot[idx].first_lba) {
        fprintf(stderr, "mps3-slot: slot %c is not on this card\n", 'A' + idx);
        return 1;
    }
    unsigned lba = 0;
    uint32_t d = 0, seq = 0;
    int rc = slot_card_bootsel_write(fd, &c, idx, &lba, &d, &seq);
    if (rc == -3) {
        fprintf(stderr, "mps3-slot: boot-select read-back off the card disagrees "
                "(default %c seq %u) — the card did not keep the write\n",
                d == 2 ? 'B' : 'A', seq);
        return 1;
    }
    if (rc) {
        fprintf(stderr, "mps3-slot: write LBA %u: %s\n", lba, strerror(errno));
        return 1;
    }
    printf("mps3-slot: default slot %c (seq %u, LBA %u; LBA %u, stage0's previous pick, untouched)\n",
           'A' + idx, c.seq, lba, lba == S0_BOOTCFG_LBA0 ? S0_BOOTCFG_LBA1 : S0_BOOTCFG_LBA0);
    return 0;
}

static int cmd_write(int fd, int idx, const char *path, int force)
{
    slot_card_t c;
    if (slot_card_read(fd, &c) || !c.slot[idx].first_lba) {
        fprintf(stderr, "mps3-slot: slot %c is not on this card\n", 'A' + idx);
        return 1;
    }
    if (c.deflt == (uint32_t)idx + 1u && !force) {
        fprintf(stderr, "mps3-slot: slot %c is the current default — write the other slot "
                "and switch with `default`, or pass --force\n", 'A' + idx);
        return 1;
    }
    FILE *f = fopen(path, "rb");
    struct stat st;
    if (!f || fstat(fileno(f), &st) || st.st_size <= BLK || (uint64_t)st.st_size > S0_IMAGE_MAX) {
        fprintf(stderr, "mps3-slot: %s: unreadable, too small, or larger than 64 MiB\n", path);
        if (f)
            fclose(f);
        return 1;
    }
    size_t len = (size_t)st.st_size;
    uint64_t base = slot_card_off(&c, idx), size = slot_card_bytes(&c, idx);
    uint8_t *img = malloc(len), *back = malloc(len);
    if (!img || !back || fread(img, 1, len, f) != len) {
        fprintf(stderr, "mps3-slot: cannot read %s\n", path);
        fclose(f);
        return 1;
    }
    fclose(f);
    if (len > size) {
        fprintf(stderr, "mps3-slot: %zu B does not fit slot %c (%llu B)\n", len, 'A' + idx,
                (unsigned long long)size);
        return 1;
    }
    uint32_t want = 0, got = 0;
    slot_card_src_t mem = { -1, 0, len, img };
    const char *why = slot_card_s0_check(&mem, &want);
    if (why) {
        fprintf(stderr, "mps3-slot: %s refused (stage0's loader): %s\n", path, why);
        return 1;
    }
    /* body first, header (first sector) LAST: a torn write leaves no valid table */
    if (slot_card_wr(fd, base + BLK, img + BLK, len - BLK, base, base + size) ||
        slot_card_flush_uncache(fd, base, size) ||
        slot_card_wr(fd, base, img, BLK, base, base + size) ||
        slot_card_flush_uncache(fd, base, size)) {
        fprintf(stderr, "mps3-slot: writing slot %c failed: %s\n", 'A' + idx, strerror(errno));
        return 1;
    }
    slot_card_src_t card = { fd, base, size, NULL };
    if (slot_card_rd(fd, base, back, len) || memcmp(img, back, len) ||
        (why = slot_card_s0_check(&card, &got)) || got != want) {
        fprintf(stderr, "mps3-slot: read-back of slot %c off the card failed (%s) — the card did "
                "not keep the image\n", 'A' + idx, why ? why : "bytes differ");
        return 1;
    }
    printf("mps3-slot: slot %c <- %s (%zu B, table CRC 0x%08x), read back off the card OK\n",
           'A' + idx, path, len, got);
    free(img);
    free(back);
    return 0;
}

static int usage(void)
{
    fprintf(stderr, "usage: mps3-slot [--disk D] status | default A|B | write A|B IMAGE [--force]\n");
    return 2;
}

int main(int argc, char **argv)
{
    int a = 1, force = 0;
    if (a + 1 < argc && !strcmp(argv[a], "--disk")) {
        disk = argv[a + 1];
        a += 2;
    }
    if (a >= argc)
        return usage();
    for (int i = a; i < argc; i++)
        if (!strcmp(argv[i], "--force"))
            force = 1;
    const char *cmd = argv[a];
    int fd = open(disk, strcmp(cmd, "status") ? O_RDWR : O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "mps3-slot: %s: %s\n", disk, strerror(errno));
        return 1;
    }
    int rc;
    if (!strcmp(cmd, "status"))
        rc = cmd_status(fd);
    else if (!strcmp(cmd, "default") && a + 1 < argc && slot_idx(argv[a + 1]) >= 0)
        rc = cmd_default(fd, slot_idx(argv[a + 1]));
    else if (!strcmp(cmd, "write") && a + 2 < argc && slot_idx(argv[a + 1]) >= 0)
        rc = cmd_write(fd, slot_idx(argv[a + 1]), argv[a + 2], force);
    else
        rc = usage();
    close(fd);
    return rc;
}
