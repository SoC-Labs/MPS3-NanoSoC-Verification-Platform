/*
 * mps3-ovlstore — READ-ONLY overlay-store inspector (M5 app port).
 *
 * D16 EMBARGO: the shared-flash collision hazard is still unowned, so this
 * tool has NO write paths at all — see ovlstore_read.h. It inspects and
 * CRC-verifies; it can never modify a store.
 *
 *   mps3-ovlstore info   <image-or-/dev/mtdN>
 *   mps3-ovlstore verify <image-or-/dev/mtdN> [A|B|active|all]
 *
 * Exit codes: 0 ok, 1 store/verify error, 2 usage.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <string.h>

#include "ovlstore_read.h"

static void print_slot(const char *name, const ovlstore_slot_desc_t *d,
                       int active)
{
    printf("slot %s%s:\n", name, active ? " (ACTIVE)" : "");
    if (!d->valid) {
        printf("  valid     : no\n");
        return;
    }
    printf("  valid     : yes\n");
    printf("  static_id : 0x%08x\n", d->static_id);
    printf("  rm_id     : 0x%08x\n", d->rm_id);
    printf("  clearing  : off 0x%06x  len %u B  crc 0x%08x\n",
           d->clear_off, d->clear_len, d->clear_crc);
    printf("  partial   : off 0x%06x  len %u B  crc 0x%08x\n",
           d->part_off, d->part_len, d->part_crc);
}

static int do_verify(ovlstore_src_t *src, const ovlstore_header_t *hdr,
                     int idx)
{
    uint32_t cc = 0, pc = 0;
    overlay_store_status_t st = ovlstore_verify_slot(src, hdr, idx, &cc, &pc);
    const char *name = idx ? "B" : "A";
    if (st == OVLSTORE_OK) {
        printf("slot %s: VERIFY OK (clearing crc 0x%08x, partial crc 0x%08x)\n",
               name, cc, pc);
        return 0;
    }
    if (st == OVLSTORE_ERR_CRC) {
        printf("slot %s: VERIFY FAILED — %s\n", name, ovlstore_status_str(st));
        printf("  clearing: computed 0x%08x, header 0x%08x%s\n",
               cc, hdr->slot[idx].clear_crc,
               cc != hdr->slot[idx].clear_crc ? "  <-- MISMATCH" : "");
        printf("  partial : computed 0x%08x, header 0x%08x%s\n",
               pc, hdr->slot[idx].part_crc,
               pc != hdr->slot[idx].part_crc ? "  <-- MISMATCH" : "");
        return 1;
    }
    printf("slot %s: %s\n", name, ovlstore_status_str(st));
    return (st == OVLSTORE_ERR_NOT_VALID) ? 0 : 1;   /* empty slot != error */
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr,
            "usage: %s info|verify <image-or-/dev/mtdN> [A|B|active|all]\n"
            "READ-ONLY tool (D16 flash-collision embargo: no write flows "
            "exist here).\n", argv[0]);
        return 2;
    }
    const char *cmd = argv[1];
    const char *path = argv[2];
    const char *which = (argc > 3) ? argv[3] : "all";

    ovlstore_src_t src;
    if (ovlstore_src_open(path, &src) != 0) {
        perror(path);
        return 1;
    }

    ovlstore_header_t hdr;
    overlay_store_status_t st = ovlstore_read_header(&src, &hdr);
    if (st != OVLSTORE_OK) {
        fprintf(stderr, "%s: header: %s\n", path, ovlstore_status_str(st));
        ovlstore_src_close(&src);
        return 1;
    }

    int rc = 0;
    if (!strcmp(cmd, "info")) {
        printf("overlay store @ %s\n", path);
        printf("header    : magic OVLS  ver %u  active slot %s  flags 0x%02x\n",
               hdr.ver, hdr.active_slot ? "B" : "A", hdr.flags);
        print_slot("A", &hdr.slot[0], hdr.active_slot == 0);
        print_slot("B", &hdr.slot[1], hdr.active_slot == 1);
        printf("layout    : A@0x%06x  B@0x%06x  clr-stage@0x%06x  "
               "clr-cache@0x%06x  end@0x%06x\n",
               OVLSTORE_SLOT_A_PAYLOAD_OFFSET, OVLSTORE_SLOT_B_PAYLOAD_OFFSET,
               OVLSTORE_CLEARING_STAGE_OFFSET, OVLSTORE_CLEARING_CACHE_OFFSET,
               OVLSTORE_FLASH_END);
    } else if (!strcmp(cmd, "verify")) {
        int a = 0, b = 0;
        if (!strcmp(which, "A") || !strcmp(which, "a"))       a = 1;
        else if (!strcmp(which, "B") || !strcmp(which, "b"))  b = 1;
        else if (!strcmp(which, "active"))                    { if (hdr.active_slot) b = 1; else a = 1; }
        else if (!strcmp(which, "all"))                       a = b = 1;
        else { fprintf(stderr, "bad slot selector '%s'\n", which); rc = 2; }
        if (rc == 0) {
            if (a) rc |= do_verify(&src, &hdr, 0);
            if (b) rc |= do_verify(&src, &hdr, 1);
        }
    } else {
        fprintf(stderr, "unknown command '%s'\n", cmd);
        rc = 2;
    }

    ovlstore_src_close(&src);
    return rc;
}
