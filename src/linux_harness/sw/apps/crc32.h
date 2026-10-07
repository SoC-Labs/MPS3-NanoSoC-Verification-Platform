/*
 * crc32.h — IEEE/zlib CRC-32 (reflected 0xEDB88320, init/xorout 0xFFFFFFFF).
 * The overlay store's slot CRCs and the 6910 push-header CRC are zlib crc32
 * (SERVICE_DISPOSITION §2 "crc32 (zlib/IEEE, payload only)"); this must match
 * bit-for-bit. Verified against Python binascii.crc32 in the test suite.
 */
#ifndef MPS3_APPS_CRC32_H
#define MPS3_APPS_CRC32_H

#include <stddef.h>
#include <stdint.h>

uint32_t mps3_crc32(uint32_t crc, const void *buf, size_t len); /* crc=0 to start */

#endif
