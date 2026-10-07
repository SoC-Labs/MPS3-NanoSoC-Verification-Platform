/*
 * test_pack.c — pins the MSB-first packing + zlib CRC against known
 * vectors, and (when the read-only main repo is visible) against a REAL
 * recorded partial: the first packed word at the sync offset must be
 * 0xAA995566. A re-introduced native-memcpy pack fails HERE, not on a
 * board (the firmware's test_hwicap_byte_lane.c lesson).
 */
#include <stdio.h>
#include <stdlib.h>
#include "../../mps3_icap_regs.h"
#include "../../mps3_crc32.h"

static int fails;
#define CHECK(cond) do { \
	if (!(cond)) { \
		fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
		fails++; \
	} \
} while (0)

/* A real partial from a local mint, if one is present (path relative to this
 * directory, where `make run` executes; override with -DREAL_BIN=\"...\"). */
#ifndef REAL_BIN
#define REAL_BIN \
	"../../../../../../fpga/dfx/build_v2enc/prod/" \
	"config_rm_regdemo_a_pblock_rp_dut_partial.bin"
#endif

int main(void)
{
	/* sync word: file bytes AA 99 55 66 in order -> word 0xAA995566 */
	const u8 sync[4] = { 0xAA, 0x99, 0x55, 0x66 };
	CHECK(mps3_hwicap_pack_word(sync) == 0xAA995566u);

	/* asymmetric vector so any byte-lane permutation is caught */
	{
		const u8 v[4] = { 0x01, 0x02, 0x03, 0x04 };
		CHECK(mps3_hwicap_pack_word(v) == 0x01020304u);
	}

	/* zlib/IEEE CRC-32 known vectors ("123456789" -> 0xCBF43926,
	 * "" -> 0x00000000) */
	CHECK(mps3_crc32((const u8 *)"123456789", 9) == 0xCBF43926u);
	CHECK(mps3_crc32((const u8 *)"", 0) == 0x00000000u);

	/* real recorded partial, if visible: scan the head for the sync
	 * BYTES and confirm the packed word */
	{
		FILE *f = fopen(REAL_BIN, "rb");
		if (f) {
			u8 head[4096];
			size_t n = fread(head, 1, sizeof(head), f);
			size_t i;
			int found = 0;
			fclose(f);
			for (i = 0; i + 4 <= n; i += 4) {
				if (mps3_hwicap_pack_word(&head[i]) == MPS3_ICAP_SYNC_WORD) {
					found = 1;
					break;
				}
			}
			CHECK(found);
			printf("test_pack: real .bin sync found at offset 0x%zx\n", i);
		} else {
			printf("test_pack: NOTE real .bin not visible, "
			       "synthetic vectors only\n");
		}
	}

	if (fails) {
		printf("test_pack: %d FAILURES\n", fails);
		return 1;
	}
	printf("test_pack: PASS\n");
	return 0;
}
