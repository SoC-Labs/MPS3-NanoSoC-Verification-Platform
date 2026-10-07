/*
 * mps3_crc32.h — zlib/IEEE CRC-32 (the wire-header variant: net_proto.h's
 * crc32 field is "zlib/IEEE, payload only"). Nibble-table implementation so
 * the same header serves the kernel engine, the mock and the host tests
 * without pulling in lib/crc32 vs zlib divergence. Matches zlib's crc32()
 * (init ~0, reflected poly 0xEDB88320, final xor ~).
 */
#ifndef MPS3_CRC32_H
#define MPS3_CRC32_H

#include "mps3_compat.h"

#define MPS3_CRC32_INIT 0xFFFFFFFFu

static const u32 mps3_crc32_nib[16] = {
	0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu,
	0x76DC4190u, 0x6B6B51F4u, 0x4DB26158u, 0x5005713Cu,
	0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
	0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu
};

/* Chained update: crc = mps3_crc32_update(prev, buf, n); seed with
 * MPS3_CRC32_INIT, finish with mps3_crc32_final(). */
static inline u32 mps3_crc32_update(u32 crc, const void *buf, u32 len)
{
	const u8 *p = (const u8 *)buf;
	u32 i;

	for (i = 0; i < len; i++) {
		crc ^= p[i];
		crc = (crc >> 4) ^ mps3_crc32_nib[crc & 0xF];
		crc = (crc >> 4) ^ mps3_crc32_nib[crc & 0xF];
	}
	return crc;
}

static inline u32 mps3_crc32_final(u32 crc)
{
	return crc ^ 0xFFFFFFFFu;
}

static inline u32 mps3_crc32(const void *buf, u32 len)
{
	return mps3_crc32_final(mps3_crc32_update(MPS3_CRC32_INIT, buf, len));
}

#endif /* MPS3_CRC32_H */
