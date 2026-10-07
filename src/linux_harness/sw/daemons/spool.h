/*
 * spool.h — the staged-bitstream spool shared by mps3-pushd and mps3-ctrld.
 *
 * mps3-pushd (6910 receiver) is the WRITER: it streams a validated push
 * into <dir>/rx.tmp and, only after the full-payload CRC gate passes,
 * atomically publishes it as
 *     <dir>/clearing.bin + <dir>/clearing.meta      (kind=0)
 *     <dir>/partial.bin  + <dir>/partial.meta       (kind=1)
 * Publication order is bin-then-meta, so a reader that sees the .meta can
 * always trust the .bin. A newly staged clearing unlinks any stale partial
 * FIRST (the frozen pair rule: a new clearing drops a stale staged partial).
 *
 * mps3-ctrld's swap worker is the READER/CONSUMER: it takes the staged
 * pair for one swap, and on a VERIFIED swap promotes the incoming clearing
 * to the resident cache
 *     <dir>/resident_clearing.bin + .meta
 * which is the clearing streamed at the START of the NEXT swap
 * (SERVICE_DISPOSITION §4 steps 3+9: the shell always holds the OUTGOING
 * RM's clearing; promotion is how it gets there).
 *
 * The in-flight <dir>/rx.tmp doubles as the swap worker's RX-progress
 * signal: its growth re-arms the 30 s await-idle timer (bounds silence,
 * never a slow transfer — the same contract as the driver's idle reap).
 */
#ifndef MPS3_SPOOL_H
#define MPS3_SPOOL_H

#include <stdint.h>
#include <sys/types.h>

#define SPOOL_RX_TMP        "rx.tmp"
#define SPOOL_CLEARING      "clearing"
#define SPOOL_PARTIAL       "partial"
#define SPOOL_RESIDENT_CLR  "resident_clearing"

typedef struct {
    uint32_t kind;       /* 0=clearing 1=partial (wire header kind)      */
    uint32_t rm_slot;
    uint32_t static_id;
    uint32_t rm_id;
    uint32_t len_words;  /* payload bytes / 4                            */
    uint32_t crc32;      /* zlib/IEEE over payload only                  */
} spool_meta_t;

/* Build "<dir>/<name><suffix>" into out; returns out for convenience. */
const char *spool_path(const char *dir, const char *name, const char *suffix,
                       char *out, size_t out_sz);

/* mkdir -p (single level) the spool dir; 0 on success/EEXIST. */
int spool_mkdir(const char *dir);

/* Write <dir>/<base>.meta atomically (tmp + rename). 0 on success. */
int spool_meta_write(const char *dir, const char *base, const spool_meta_t *m);

/* Read <dir>/<base>.meta. 0 on success, -1 if missing/corrupt. */
int spool_meta_read(const char *dir, const char *base, spool_meta_t *m);

/* Unlink <dir>/<base>.bin and .meta (missing files are not an error). */
void spool_unlink_pair(const char *dir, const char *base);

/* Rename <dir>/<from>.{bin,meta} to <dir>/<to>.{bin,meta} (meta last).
 * 0 on success. */
int spool_rename_pair(const char *dir, const char *from, const char *to);

/* 1 if <dir>/<base>.meta exists (the publication marker), else 0. */
int spool_staged(const char *dir, const char *base);

#endif /* MPS3_SPOOL_H */
