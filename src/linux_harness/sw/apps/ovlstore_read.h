/*
 * ovlstore_read.h — READ-ONLY overlay-store access (M5 app port).
 *
 * ============================  D16 EMBARGO  ============================
 * The D16 flash-collision hazard is STILL UNOWNED (memory: qspi-shared-flash
 * -hazard; WAVE2_STATUS B2). This module and the mps3-ovlstore tool therefore
 * implement ONLY read paths: no erase, no program, no header rewrite, no
 * commit, no A/B staging. There is deliberately NO write code to misuse —
 * the source never opens the store for writing (the test suite greps for
 * that). When D16 is owned, write flows belong to a NEW, separately reviewed
 * module — do not "extend" this one.
 * =======================================================================
 *
 * Sources it reads:
 *   - a flash IMAGE FILE (dump/host copy), any size up to 8 MiB, or
 *   - an MTD char device (/dev/mtd*) once the OVLSTORE QSPI node is enabled
 *     (disabled on the declared shell_linux baseline — QSPI v0.2 removed the
 *     SPI_0 port; the 0xE4B1C44A fork re-enables it, DRIVER_MATRIX §2.6).
 *     Plain pread() works on mtdchar, so both paths are one code path.
 *
 * Layout: the FROZEN on-flash contract from the main repo's
 * firmware/overlay_store/{overlay_store.h,ovlstore_codec.h} (offsets below
 * transcribed; codec copied verbatim — little-endian, I15 RESOLVED).
 */
#ifndef MPS3_APPS_OVLSTORE_READ_H
#define MPS3_APPS_OVLSTORE_READ_H

#include <stdint.h>
#include "ovlstore_codec.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Frozen flash geometry (overlay_store.h:31-66, main repo). */
#define OVLSTORE_HEADER_OFFSET         0x000000u
#define OVLSTORE_SLOT_A_PAYLOAD_OFFSET 0x010000u
#define OVLSTORE_SLOT_B_PAYLOAD_OFFSET 0x400000u
#define OVLSTORE_CLEARING_STAGE_OFFSET 0x780000u
#define OVLSTORE_CLEARING_CACHE_OFFSET 0x7C0000u
#define OVLSTORE_CLEARING_REGION_LEN   0x040000u  /* 256 KiB each */
#define OVLSTORE_FLASH_END             0x800000u
#define OVLSTORE_SLOT_B_PAYLOAD_END    OVLSTORE_CLEARING_STAGE_OFFSET

typedef struct {
    int fd;                  /* O_RDONLY */
    uint64_t size;           /* file/device size if seekable, else 0 */
} ovlstore_src_t;

/* Open image file or mtd chardev READ-ONLY. Returns 0 / -1(errno). */
int ovlstore_src_open(const char *path, ovlstore_src_t *src);
void ovlstore_src_close(ovlstore_src_t *src);

/* Read + structurally validate the 74-byte header at offset 0. */
overlay_store_status_t ovlstore_read_header(ovlstore_src_t *src,
                                            ovlstore_header_t *hdr);

/* CRC-verify one slot's payloads (clearing + partial) against the header
 * descriptors. slot_idx 0=A, 1=B. Reads only. On success fills *clear_crc /
 * *part_crc with the computed values (also on CRC mismatch, for reporting).
 * Returns OVLSTORE_OK, OVLSTORE_ERR_NOT_VALID (slot.valid==0),
 * OVLSTORE_ERR_CRC, or OVLSTORE_ERR_SPI (short read / out-of-range). */
overlay_store_status_t ovlstore_verify_slot(ovlstore_src_t *src,
                                            const ovlstore_header_t *hdr,
                                            int slot_idx,
                                            uint32_t *clear_crc,
                                            uint32_t *part_crc);

const char *ovlstore_status_str(overlay_store_status_t st);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_APPS_OVLSTORE_READ_H */
