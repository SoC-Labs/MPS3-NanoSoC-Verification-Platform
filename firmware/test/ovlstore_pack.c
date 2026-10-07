/*
 * ovlstore_pack.c — the C half of the OVLSTORE-header cross-language golden
 * test (tests/firmware_logic/test_ovlstore_header_golden.py). NOT a
 * self-checking test binary (it is not in the Makefile's $(TESTS)); it is
 * built by `make tools` / its own recipe, mirroring bin/ctrl_echo.
 *
 * It exists to make the I15 endianness resolution (little-endian, per
 * docs/contracts/OPEN_ISSUES.md) impossible to silently drift between the
 * two independent implementations of the SAME on-flash header:
 *   - firmware/overlay_store/ovlstore_codec.c  (this side, the real codec)
 *   - tests/common/ovlstore_header.py          (the Python model)
 *
 * Two stdin/stdout modes, one raw 74-byte header each way:
 *   argv[1] == "pack"   (default): pack the fixed GOLDEN header via the REAL
 *                        ovlstore_header_pack() and write its raw bytes to
 *                        stdout. The pytest packs the identical field values
 *                        via ovlstore_header.py and asserts byte-equality.
 *   argv[1] == "repack": read exactly OVLSTORE_HEADER_PACKED_SIZE bytes from
 *                        stdin, ovlstore_header_unpack() them, then
 *                        ovlstore_header_pack() the result back out. Feeding
 *                        it the Python-produced bytes proves THIS codec can
 *                        round-trip the other side's bytes (and vice-versa,
 *                        with Python unpacking pack-mode output).
 *
 * The GOLDEN vector below is deliberately asymmetric: every u16/u32 field
 * holds four DISTINCT, non-palindromic bytes, so any byte-swap (an
 * accidental big-endian flip on either side) changes the emitted bytes and
 * fails the golden compare. See the pytest for the mirrored values.
 */
#include <stdio.h>
#include <string.h>
#include "../overlay_store/ovlstore_codec.h"

/* Fixed GOLDEN header — must stay byte-identical to the values the pytest
 * feeds tests/common/ovlstore_header.py. Every multi-byte field has four
 * distinct bytes so an endianness flip is detectable. ver MUST be
 * OVLSTORE_VER (1) or repack-mode's unpack rejects it; ver=1 is still
 * endian-distinguishable (LE 01 00 vs BE 00 01). */
static ovlstore_header_t golden_header(void)
{
    ovlstore_header_t h;
    memset(&h, 0, sizeof(h));
    memcpy(h.magic, OVLSTORE_MAGIC, 4);
    h.ver         = OVLSTORE_VER;   /* 1 */
    h.active_slot = 0;              /* points at the valid slot A */
    h.flags       = 0xA5u;
    h.slot[0] = (ovlstore_slot_desc_t){
        .static_id = 0xA1B2C3D4u, .rm_id     = 0x11223344u,
        .clear_off = 0x0A0B0C0Du, .clear_len = 0x10203040u,
        .clear_crc = 0xDEADBEEFu, .part_off  = 0x01020304u,
        .part_len  = 0x05060708u, .part_crc  = 0xCAFEF00Du,
        .valid     = 1,
    };
    h.slot[1] = (ovlstore_slot_desc_t){
        .static_id = 0x0BADF00Du, .rm_id     = 0x55667788u,
        .clear_off = 0x090A0B0Cu, .clear_len = 0x0D0E0F10u,
        .clear_crc = 0xFEEDFACEu, .part_off  = 0x11121314u,
        .part_len  = 0x15161718u, .part_crc  = 0x19202122u,
        .valid     = 0,
    };
    return h;
}

static int mode_pack(void)
{
    ovlstore_header_t h = golden_header();
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    uint32_t n = ovlstore_header_pack(&h, raw);
    if (fwrite(raw, 1, n, stdout) != n) {
        return 1;
    }
    return 0;
}

static int mode_repack(void)
{
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    size_t got = fread(raw, 1, sizeof(raw), stdin);
    if (got != sizeof(raw)) {
        fprintf(stderr, "ovlstore_pack: short stdin (%zu < %u bytes)\n",
                got, (unsigned)OVLSTORE_HEADER_PACKED_SIZE);
        return 2;
    }
    ovlstore_header_t h;
    overlay_store_status_t st = ovlstore_header_unpack(raw, (uint32_t)got, &h);
    if (st != OVLSTORE_OK) {
        fprintf(stderr, "ovlstore_pack: unpack rejected input (status=%d)\n", (int)st);
        return 3;
    }
    uint8_t out[OVLSTORE_HEADER_PACKED_SIZE];
    uint32_t n = ovlstore_header_pack(&h, out);
    if (fwrite(out, 1, n, stdout) != n) {
        return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *mode = (argc > 1) ? argv[1] : "pack";
    if (strcmp(mode, "pack") == 0) {
        return mode_pack();
    }
    if (strcmp(mode, "repack") == 0) {
        return mode_repack();
    }
    fprintf(stderr, "usage: %s [pack|repack]\n", argv[0]);
    return 64;
}
