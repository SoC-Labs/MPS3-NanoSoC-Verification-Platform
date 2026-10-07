/*
 * ovlstore_codec.c — QSPI A/B-slot store header pack/unpack. See
 * ovlstore_codec.h for the endianness note (I15: little-endian, per
 * OPEN_ISSUES.md's documented resolution). tests/common/ovlstore_header.py
 * now packs the SAME little-endian layout; the two are pinned byte-for-byte
 * by the cross-language golden test (see that header for the guard).
 */
#include <string.h>
#include "ovlstore_codec.h"

static void put_u16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
}

static void put_u32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static uint16_t get_u16le(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t get_u32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void ovlstore_pack_slot(const ovlstore_slot_desc_t *d,
                         uint8_t out[OVLSTORE_SLOT_PACKED_SIZE])
{
    put_u32le(out + 0,  d->static_id);
    put_u32le(out + 4,  d->rm_id);
    put_u32le(out + 8,  d->clear_off);
    put_u32le(out + 12, d->clear_len);
    put_u32le(out + 16, d->clear_crc);
    put_u32le(out + 20, d->part_off);
    put_u32le(out + 24, d->part_len);
    put_u32le(out + 28, d->part_crc);
    out[32] = d->valid ? 1u : 0u;
}

void ovlstore_unpack_slot(const uint8_t in[OVLSTORE_SLOT_PACKED_SIZE],
                           ovlstore_slot_desc_t *out)
{
    out->static_id = get_u32le(in + 0);
    out->rm_id     = get_u32le(in + 4);
    out->clear_off = get_u32le(in + 8);
    out->clear_len = get_u32le(in + 12);
    out->clear_crc = get_u32le(in + 16);
    out->part_off  = get_u32le(in + 20);
    out->part_len  = get_u32le(in + 24);
    out->part_crc  = get_u32le(in + 28);
    out->valid     = in[32];
}

uint32_t ovlstore_header_pack(const ovlstore_header_t *hdr,
                               uint8_t out[OVLSTORE_HEADER_PACKED_SIZE])
{
    memcpy(out, OVLSTORE_MAGIC, 4);
    put_u16le(out + 4, hdr->ver);
    out[6] = hdr->active_slot;
    out[7] = hdr->flags;
    ovlstore_pack_slot(&hdr->slot[0], out + OVLSTORE_HEADER_FIXED_PACKED_SIZE);
    ovlstore_pack_slot(&hdr->slot[1],
                        out + OVLSTORE_HEADER_FIXED_PACKED_SIZE + OVLSTORE_SLOT_PACKED_SIZE);
    return OVLSTORE_HEADER_PACKED_SIZE;
}

overlay_store_status_t ovlstore_header_unpack(const uint8_t *in, uint32_t in_len,
                                                ovlstore_header_t *out)
{
    if (in_len < OVLSTORE_HEADER_PACKED_SIZE) {
        return OVLSTORE_ERR_SHORT;
    }
    if (memcmp(in, OVLSTORE_MAGIC, 4) != 0) {
        return OVLSTORE_ERR_MAGIC;
    }
    uint16_t ver = get_u16le(in + 4);
    if (ver != OVLSTORE_VER) {
        return OVLSTORE_ERR_VERSION;
    }
    memcpy(out->magic, OVLSTORE_MAGIC, 4);
    out->ver = ver;
    out->active_slot = in[6];
    out->flags = in[7];
    ovlstore_unpack_slot(in + OVLSTORE_HEADER_FIXED_PACKED_SIZE, &out->slot[0]);
    ovlstore_unpack_slot(in + OVLSTORE_HEADER_FIXED_PACKED_SIZE + OVLSTORE_SLOT_PACKED_SIZE,
                          &out->slot[1]);
    return OVLSTORE_OK;
}
