/* ovlstore_read.c — see ovlstore_read.h. READ-ONLY by construction. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "crc32.h"
#include "ovlstore_read.h"

int ovlstore_src_open(const char *path, ovlstore_src_t *src)
{
    src->fd = open(path, O_RDONLY);   /* READ-ONLY — the whole point */
    if (src->fd < 0)
        return -1;
    struct stat st;
    src->size = 0;
    if (fstat(src->fd, &st) == 0 && st.st_size > 0)
        src->size = (uint64_t)st.st_size;
    return 0;
}

void ovlstore_src_close(ovlstore_src_t *src)
{
    if (src->fd >= 0) {
        close(src->fd);
        src->fd = -1;
    }
}

static int read_at(ovlstore_src_t *src, uint64_t off, void *buf, size_t len)
{
    uint8_t *p = (uint8_t *)buf;
    while (len) {
        ssize_t r = pread(src->fd, p, len, (off_t)off);
        if (r <= 0)
            return -1;
        p += r;
        off += (uint64_t)r;
        len -= (size_t)r;
    }
    return 0;
}

overlay_store_status_t ovlstore_read_header(ovlstore_src_t *src,
                                            ovlstore_header_t *hdr)
{
    uint8_t raw[OVLSTORE_HEADER_PACKED_SIZE];
    if (read_at(src, OVLSTORE_HEADER_OFFSET, raw, sizeof(raw)) != 0)
        return OVLSTORE_ERR_SPI;
    return ovlstore_header_unpack(raw, sizeof(raw), hdr);
}

static overlay_store_status_t crc_region(ovlstore_src_t *src, uint64_t abs_off,
                                         uint32_t len, uint64_t region_end,
                                         uint32_t *crc_out)
{
    if (abs_off + len > region_end)
        return OVLSTORE_ERR_SPI;   /* descriptor points outside its region */

    uint8_t buf[65536];
    uint32_t crc = 0;
    uint64_t off = abs_off;
    uint32_t left = len;
    while (left) {
        size_t n = left > sizeof(buf) ? sizeof(buf) : left;
        if (read_at(src, off, buf, n) != 0)
            return OVLSTORE_ERR_SPI;
        crc = mps3_crc32(crc, buf, n);
        off += n;
        left -= (uint32_t)n;
    }
    *crc_out = crc;
    return OVLSTORE_OK;
}

overlay_store_status_t ovlstore_verify_slot(ovlstore_src_t *src,
                                            const ovlstore_header_t *hdr,
                                            int slot_idx,
                                            uint32_t *clear_crc,
                                            uint32_t *part_crc)
{
    if (slot_idx < 0 || slot_idx > 1)
        return OVLSTORE_ERR_SPI;
    const ovlstore_slot_desc_t *d = &hdr->slot[slot_idx];
    if (!d->valid)
        return OVLSTORE_ERR_NOT_VALID;

    uint64_t base = slot_idx ? OVLSTORE_SLOT_B_PAYLOAD_OFFSET
                             : OVLSTORE_SLOT_A_PAYLOAD_OFFSET;
    uint64_t end  = slot_idx ? OVLSTORE_SLOT_B_PAYLOAD_END
                             : OVLSTORE_SLOT_B_PAYLOAD_OFFSET;

    uint32_t cc = 0, pc = 0;
    overlay_store_status_t st;

    st = crc_region(src, base + d->clear_off, d->clear_len, end, &cc);
    if (st != OVLSTORE_OK)
        return st;
    st = crc_region(src, base + d->part_off, d->part_len, end, &pc);
    if (st != OVLSTORE_OK)
        return st;

    if (clear_crc) *clear_crc = cc;
    if (part_crc)  *part_crc  = pc;

    if (cc != d->clear_crc || pc != d->part_crc)
        return OVLSTORE_ERR_CRC;
    return OVLSTORE_OK;
}

const char *ovlstore_status_str(overlay_store_status_t st)
{
    switch (st) {
    case OVLSTORE_OK:            return "OK";
    case OVLSTORE_ERR_MAGIC:     return "bad magic (not an overlay store)";
    case OVLSTORE_ERR_VERSION:   return "unsupported header version";
    case OVLSTORE_ERR_CRC:       return "payload CRC mismatch";
    case OVLSTORE_ERR_NOT_VALID: return "slot not valid";
    case OVLSTORE_ERR_SPI:       return "read failed / descriptor out of range";
    case OVLSTORE_ERR_SHORT:     return "short header";
    default:                     return "unknown status";
    }
}
