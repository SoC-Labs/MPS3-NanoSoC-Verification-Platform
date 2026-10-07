/*
 * crc32.h — zlib/IEEE 802.3 CRC-32 (poly 0xEDB88320, reflected, init/xorout
 * 0xFFFFFFFF -- the "zlib.crc32-compatible" variant net-protocol.md I13 pins
 * down for every crc32 field on the wire ("all crc32 are zlib/IEEE CRC-32,
 * poly 0xEDB88320, computed over the raw .bin payload bytes") and
 * overlay-manifest.md's manifest.json crc32 fields reuse.
 *
 * Pure logic, zero register/hardware/network dependency by design -- this is
 * one of the units firmware/test/'s host-gcc harness compiles and runs
 * directly (see firmware/test/test_crc32.c), and the same object file is
 * what config_agent.c and overlay_store's codec link against for real.
 */
#ifndef MPS3_CRC32_H
#define MPS3_CRC32_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MPS3_CRC32_INIT 0u

/* Chainable/incremental form, matching zlib's crc32(crc, buf, len) calling
 * convention: pass MPS3_CRC32_INIT for the first chunk, then thread the
 * returned value into the next call for subsequent chunks of the same
 * logical payload (config_agent.c's "track a running crc32 as bytes arrive"
 * TODO becomes a real, O(1)-per-chunk call to this). */
uint32_t mps3_crc32_update(uint32_t crc, const void *buf, uint32_t len);

/* One-shot convenience over a buffer that's already fully in memory
 * (equivalent to mps3_crc32_update(MPS3_CRC32_INIT, buf, len)). */
static inline uint32_t mps3_crc32(const void *buf, uint32_t len)
{
    return mps3_crc32_update(MPS3_CRC32_INIT, buf, len);
}

#ifdef __cplusplus
}
#endif

#endif /* MPS3_CRC32_H */
