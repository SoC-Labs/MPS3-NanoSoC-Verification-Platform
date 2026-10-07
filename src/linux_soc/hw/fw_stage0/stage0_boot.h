/*
 * stage0_boot.h -- on-storage boot-table format + portable loader core API for
 * the MicroBlaze-V Linux harness first-stage loader.
 *
 * The format deliberately mirrors the SILICON-PROVEN nanosoc flash-boot table
 * (firmware/micropython/flash_pack.py + firmware/bootloader/boot_table.h): a
 * fixed header, then N region descriptors, then the concatenated payloads. The
 * differences from nanosoc: destinations are DDR physical addresses (not IMEM),
 * there are up to S0_MAX_ENTRIES regions (nanosoc had one HOT image), and the
 * header carries an explicit OpenSBI hand-off (pc/a0/a1) so the same loader
 * boots BOTH the 4-region map (fw_jump + Image + dtb + rootfs) AND a single
 * FW_PAYLOAD blob (num_entries = 1) with no code change.
 *
 * ONE image format everywhere: a user-uSD slot (p1 = slot A, p2 = slot B) holds
 * exactly the bytes stage0_pack.py writes, and the TFTP rescue push carries the
 * same bytes. src_offset is always relative to the start of the image.
 *
 * Shared by the target loader (stage0_core.c, freestanding rv32) and the host
 * unit test + packer -- one definition, no second parser.
 */
#ifndef STAGE0_BOOT_H
#define STAGE0_BOOT_H

#include <stdint.h>

/* "S0LB" as bytes 53 30 4C 42; read as a little-endian u32 that is 0x424C3053.
 * The packer writes struct.pack('<I', 0x424C3053) -> those same four bytes.
 *
 * VERSION 2 (2026-09-23): header_crc32 covers the header AND the whole entry
 * table. In v1 it covered the 32-byte header only, so the entries -- each
 * region's src_offset, dst_addr, len and payload CRC -- were bound by nothing:
 * a flipped dst_addr still loaded, passed its payload CRC (computed at the
 * wrong destination) and was jumped over; and two different images with the
 * same pc/a0/a1 had the SAME header_crc32, so the status block's
 * image_hdr_crc could not say which image booted (IMAGE lane's finding).
 * In v2 the table CRC binds every payload's CRC and length, so it is the
 * image's identity, and a v1 image is refused (S0_EVERSION). */
#define S0_MAGIC        0x424C3053u
#define S0_VERSION      2u
#define S0_MAX_ENTRIES  8u          /* sanity bound; 4-region map needs 4 */

/* Largest image stage0 accepts from ANY source: a uSD slot is exactly this big
 * (stage0_mkcard.py) and the TFTP rescue staging window is exactly this big, so
 * an image that can be pushed can always be carded and vice versa. The 1-region
 * blob is 22.4 MB (mk_1region.sh), so this is ~2.8x headroom. */
#define S0_IMAGE_MAX    (64u * 1024u * 1024u)

/* Header, 32 bytes. header_crc32 is the TABLE CRC: CRC32 of the 32-byte header
 * (with header_crc32 itself taken as zero) followed by all num_entries 16-byte
 * entries -- the convention the packer uses. It therefore covers every
 * region's destination, length and payload CRC. */
struct s0_header {
    uint32_t magic;         /* S0_MAGIC                                   */
    uint32_t version;       /* S0_VERSION                                 */
    uint32_t num_entries;   /* 1..S0_MAX_ENTRIES region descriptors follow */
    uint32_t entry_pc;      /* jump here after loading (e.g. 0x80000000)  */
    uint32_t entry_a0;      /* value for a0 at hand-off (hartid, usually 0)*/
    uint32_t entry_a1;      /* value for a1 (dtb addr; 0 for FW_PAYLOAD)  */
    uint32_t flags;         /* reserved, 0                                */
    uint32_t header_crc32;  /* table CRC: header (this field 0) + entries */
};

/* Region descriptor, 16 bytes. Entries start immediately after the header. */
struct s0_entry {
    uint32_t src_offset;    /* byte offset of this region within the image */
    uint32_t dst_addr;      /* DDR physical destination                   */
    uint32_t len;           /* byte length                                */
    uint32_t crc32;         /* CRC32 of the region's bytes                */
};

#define S0_ENTRIES_OFFSET   ((uint32_t)sizeof(struct s0_header))

/* Loader result: what to do after a successful load. */
struct s0_result {
    uint32_t entry_pc;
    uint32_t entry_a0;
    uint32_t entry_a1;
    uint32_t header_crc32;  /* the table CRC: identifies WHICH image booted */
};

/* Return / error codes. Stable: they are recorded in the stage0 status block
 * (stage0_status.h) and decoded by host tooling -- append only. */
enum {
    S0_OK        = 0,
    S0_EMAGIC    = 1,   /* header magic mismatch (no valid table)         */
    S0_EVERSION  = 2,   /* unsupported version                            */
    S0_EHDRCRC   = 3,   /* table CRC mismatch (header or an entry corrupt) */
    S0_ENENT     = 4,   /* num_entries out of range                       */
    S0_EREAD     = 5,   /* a storage read failed / ran past the source    */
    S0_ECRC      = 6,   /* a region's payload CRC mismatch                */
    S0_ENOSLOT   = 7,   /* no such slot on the card (MBR entry not 0x7F)  */
    S0_ENOTTRIED = 8,   /* never attempted this boot (status block only)  */
    S0_ELIMIT    = 9,   /* skipped: the slot hit its boot-attempt limit   */
    S0_ETOOBIG   = 10,  /* rescue: pushed image larger than S0_IMAGE_MAX  */
    S0_EPROTO    = 11,  /* rescue: TFTP protocol violation, push aborted  */
    S0_ETIMEOUT  = 12,  /* rescue: client went silent, push dropped       */
    S0_EDDR      = 13,  /* the DDR calib bit dropped during the load       */
    S0_ESLOW     = 14,  /* the slot load exceeded S0_SLOT_TIME_MS          */
};

/*
 * Backend the loader core drives. Kept deliberately tiny so a storage backend
 * (microSD, an in-memory TFTP staging buffer, a host test image) implements
 * exactly two operations.
 *
 *   sto_read     -- copy `len` bytes from image offset `src_off` into `dst`. On
 *                   the target `dst` is a real pointer (LMB for metadata, or
 *                   the result of addr_to_ptr() for a DDR region). Returns 0 /
 *                   <0 (the load fails S0_EREAD) / >0 = an S0_* code the load
 *                   fails with instead (a guard: S0_EDDR, S0_ESLOW). A read
 *                   that would run past the end of the source MUST fail (that
 *                   is how a truncated image is caught).
 *   addr_to_ptr  -- turn a DDR physical address into a pointer the loader can
 *                   read/write. On the target this is identity ((void*)a) once
 *                   bounded. The host test redirects it to a simulated map so
 *                   no code in the core ever hard-codes a physical address.
 *                   May return NULL to reject an out-of-range region (treated
 *                   as S0_EREAD).
 */
struct s0_backend {
    int   (*sto_read)(uint32_t src_off, void *dst, uint32_t len, void *ctx);
    void *(*addr_to_ptr)(uint32_t dst_addr, uint32_t len, void *ctx);
    void   *ctx;
};

/* The in-DDR CRC pass runs in chunks of this many bytes; s0_load_guarded()
 * calls its `chunk` hook before each one. */
#define S0_CRC_CHUNK    (64u * 1024u)
typedef int (*s0_chunk_fn)(void *ctx, uint32_t region);

/* Portable, no MMIO, no libc. Checks magic, version and num_entries, reads the
 * entry table, verifies the table CRC over header + entries, then for each
 * entry reads the payload to its destination and verifies its CRC there. On
 * S0_OK, *out is filled with the hand-off. Any failure leaves nothing to jump
 * to. */
int s0_load(const struct s0_backend *be, struct s0_result *out);

/* s0_load() with a hook (lane S0-COLDFIX; NULL = s0_load): chunk(be->ctx,
 * region) runs before every S0_CRC_CHUNK bytes of the in-DDR CRC pass and
 * returns 0 to go on, else an S0_* code the load fails with. It is where the
 * target kicks the watchdog and re-checks the DDR calib bit while it CRCs a
 * 22 MB region. A separate entry point so struct s0_backend (harnessd's
 * slot_card.c builds one too) keeps its three members. */
int s0_load_guarded(const struct s0_backend *be, s0_chunk_fn chunk, struct s0_result *out);

/* CRC32, reflected poly 0xEDB88320, init 0xFFFFFFFF, final XOR -- identical to
 * zlib / Python binascii.crc32, so packer and loader agree by construction.
 * s0_crc32_update() continues a running CRC (pass 0 to start; the pre/post
 * inversion is internal), so s0_crc32(b, n) == s0_crc32_update(0, b, n). */
uint32_t s0_crc32(const void *buf, uint32_t len);
uint32_t s0_crc32_update(uint32_t crc, const void *buf, uint32_t len);

/* In-memory image source (the TFTP staging buffer; host tests). ctx is a
 * struct s0_mem_src. Fails any read that runs past `len` (truncation). */
struct s0_mem_src {
    const uint8_t *img;
    uint32_t       len;
};
int s0_mem_read(uint32_t src_off, void *dst, uint32_t len, void *ctx);

/* Short, fixed text for an S0_* code (console + TFTP ERROR messages). */
const char *s0_strerror(int rc);

/* True iff [addr, addr+len) lies wholly within the memory window
 * [base, base+size). Overflow-safe: rejects len > size and any addr+len wrap.
 * The target's addr_to_ptr and the host test share this ONE predicate so the
 * silicon bounds check is exactly what the test exercises. */
static inline int s0_region_in_bounds(uint32_t base, uint32_t size,
                                      uint32_t addr, uint32_t len)
{
    if (addr < base)
        return 0;
    uint32_t off = addr - base;
    if (len > size || off > size - len)   /* off+len > size, no wrap */
        return 0;
    return 1;
}

/* ---- the user-uSD layout (published in docs/planning/linux_lanes/
 *      STAGE0_CONTRACT.md; stage0_mkcard.py writes it) ------------------------
 *
 * MBR (LBA 0). Stage0 reads ONLY the first two partition entries:
 *   entry 1 (p1) type 0x7F = slot A, entry 2 (p2) type 0x7F = slot B.
 * Entry 3 (p3, 0x83) is /persist and entry 4 (p4, 0xDA) is D13's overlay store;
 * stage0 never reads either. 0x7F is the "reserved for individual use" MBR type,
 * chosen so no OS auto-mounts or claims a slot. A slot is a raw boot image
 * starting at the partition's first sector; its size bounds every read (a
 * region past the partition end is S0_EREAD, i.e. a truncated slot). */
#define S0_BLOCK_BYTES       512u
#define S0_MBR_SIG_OFF       510u
#define S0_MBR_PART_OFF      446u
#define S0_PART_TYPE_SLOT    0x7Fu
#define S0_NUM_SLOTS         2u      /* A = MBR entry 1, B = MBR entry 2 */

struct s0_slot {
    uint32_t first_lba;     /* 0 = slot absent */
    uint32_t nblocks;
};

/* ---- the boot-select sector: which slot is the DEFAULT --------------------
 *
 * LINUX writes it, never stage0 (stage0 is read-only on the card). Two copies,
 * LBA 1 and LBA 2 (the gap before the first partition), so an update is
 * power-safe: the writer rewrites the copy with the LOWER seq, with seq+1, and
 * stage0 uses the valid copy with the HIGHER seq. A torn write leaves a bad
 * CRC in the copy being written and the other copy still valid. With neither
 * copy valid (a card fresh from stage0_mkcard.py without --default, or a blank
 * gap) the default is slot A.
 *
 *   0x000 magic         S0_BOOTCFG_MAGIC ("S0BC")
 *   0x004 version       1
 *   0x008 seq           higher wins; a change resets stage0's attempt counters
 *   0x00C default_slot  1 = A, 2 = B
 *   0x010..0x1FB        0
 *   0x1FC crc32         CRC32 (zlib) of bytes 0x000..0x1FB
 */
#define S0_BOOTCFG_MAGIC     0x43423053u   /* bytes 53 30 42 43 = "S0BC" */
#define S0_BOOTCFG_VERSION   1u
#define S0_BOOTCFG_LBA0      1u
#define S0_BOOTCFG_LBA1      2u
#define S0_BOOTCFG_CRC_OFF   0x1FCu

/* Pick the default slot from the two copies. *seq = the winning copy's seq
 * (0 when neither is valid); returns the default slot, 1 (A) or 2 (B). */
uint32_t s0_bootcfg_pick(const uint8_t c0[S0_BLOCK_BYTES],
                         const uint8_t c1[S0_BLOCK_BYTES], uint32_t *seq);

/* Parse an MBR sector into the two stage0 slots. card_blocks bounds each
 * partition (0 = unknown, no bound). Returns 0 if the sector carries a valid
 * MBR signature (even if neither slot exists), <0 if it is not an MBR. A slot
 * whose entry is not type 0x7F, is empty, or runs past the card is returned
 * with first_lba = 0. Pure function, no I/O. */
int s0_mbr_parse(const uint8_t sector[S0_BLOCK_BYTES], uint32_t card_blocks,
                 struct s0_slot out[S0_NUM_SLOTS]);

#endif /* STAGE0_BOOT_H */
