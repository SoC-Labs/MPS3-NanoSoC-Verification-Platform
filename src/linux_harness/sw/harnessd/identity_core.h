/*
 * identity_core.h — THE BOARD IDENTITY (label, hostname, IP, MAC) of one MPS3
 * Linux harness: parse, validate, resolve, render. Lane IDENT, 2026-09-28.
 *
 * WHY. Every image used to carry ONE identity for every board: the DTB's
 * local-mac-address, a hard-coded 192.168.10.101/24 in S41mps3net, "MPS3-01"
 * compiled into the CLCD. A second board came up as the first (2026-09-28: board
 * 2's LCD said MPS3-01 / 192.168.10.101 / 02:00:00:4D:50:53). The identity is
 * now RESOLVED at boot, per field, from three sources in this precedence:
 *
 *   1. the OVERRIDE file /persist/etc/mps3/identity (shell-sourceable KEY=VALUE:
 *      MPS3_LABEL, MPS3_HOSTNAME, MPS3_IP (a.b.c.d/nn), MPS3_MAC) -- written by
 *      `mps3-identity set` on the board or the 6900 verb `identity_set`; only on
 *      a card-backed /persist (netboot and mps3.persist=off have none);
 *   2. the stage0 STATUS BLOCK (stage0_status.h "THE BOARD IDENTITY"): the
 *      per-board S0_IP / S0_MAC / S0_LABEL baked into this bitstream, when the
 *      block is valid (identity.c's rule) and the field is nonzero;
 *   3. the IMAGE DEFAULT: label "MPS3", 192.168.10.101/24, 02:00:00:4d:50:53.
 *
 * The hostname has no stage0 source: it is the override's, else the resolved
 * label lowercased (source "label"). The result, with a per-field source, is
 * written ONCE per boot to /run/mps3/identity (mps3-identity resolve, from
 * S13mps3identity); S41mps3net applies it (MAC before link up, the static IP,
 * the hostname) and harnessd reports it (CLCD, identify, the `identity` verb).
 *
 * This file is pure: no I/O but the two file helpers at the end, no harnessd
 * state. mps3-identity (the boot resolver + the board CLI) and harnessd link it
 * unchanged, so the verb and the CLI accept and refuse exactly the same values.
 */
#ifndef HARNESSD_IDENTITY_CORE_H
#define HARNESSD_IDENTITY_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The label is what the CLCD shows at row 0, cols 0..18 (col 20 is "nanoSoC
 * harness"; col 19 stays a blank separator): 1..19 characters of [A-Z0-9-].
 * stage0 bakes at most 8 (two status words). */
#define MPS3_ID_LABEL_MAX     19
/* RFC 1123 host name, as Linux takes it (HOST_NAME_MAX 64 incl. the NUL). */
#define MPS3_ID_HOSTNAME_MAX  63

#define MPS3_ID_DEFAULT_LABEL   "MPS3"
#define MPS3_ID_DEFAULT_IP      0xC0A80A65u      /* 192.168.10.101 */
#define MPS3_ID_DEFAULT_PREFIX  24u
#define MPS3_ID_DEFAULT_MAC     { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 }
#define MPS3_ID_STAGE0_PREFIX   24u              /* stage0 bakes no prefix: /24 */

#define MPS3_ID_OVERRIDE_PATH   "/persist/etc/mps3/identity"
#define MPS3_ID_RUN_PATH        "/run/mps3/identity"
#define MPS3_ID_FILE_MAX        4096u            /* a bigger override is not read */

/* the four fields, in wire order (label, hostname, ip, mac) */
enum { MPS3_ID_LABEL = 0, MPS3_ID_HOSTNAME, MPS3_ID_IP, MPS3_ID_MAC, MPS3_ID_NFIELDS };
#define MPS3_ID_F(i) (1u << (i))

/* where a resolved field came from */
enum {
    MPS3_ID_SRC_NONE = 0,
    MPS3_ID_SRC_OVERRIDE,   /* "override" */
    MPS3_ID_SRC_STAGE0,     /* "stage0"   */
    MPS3_ID_SRC_DEFAULT,    /* "default"  */
    MPS3_ID_SRC_LABEL,      /* "label": the hostname derived from the label */
};

/* What a source says: any subset of the four (have = MPS3_ID_F bits). */
typedef struct {
    unsigned have;
    char     label[MPS3_ID_LABEL_MAX + 1];
    char     hostname[MPS3_ID_HOSTNAME_MAX + 1];
    uint32_t ip;            /* host order                          */
    unsigned prefix;        /* 8..30                               */
    uint8_t  mac[6];
} mps3_id_fields_t;

/* A resolved identity: every field set, each with its source. */
typedef struct {
    mps3_id_fields_t v;     /* have == all four */
    int      src[MPS3_ID_NFIELDS];
} mps3_identity_t;

/* stage0 status block, as the resolver saw it */
enum {
    MPS3_ID_S0_NOWINDOW = 0,  /* no LMB-tail window (QEMU, --no-hw, a test)  */
    MPS3_ID_S0_INVALID,       /* mapped, but not a valid block               */
    MPS3_ID_S0_VALID,         /* valid (its identity fields may still be 0)  */
};

typedef void (*mps3_id_log_fn)(void *ctx, const char *msg);

const char *mps3_id_field_name(int f);      /* "label" / "hostname" / "ip" / "mac" */
const char *mps3_id_src_name(int src);      /* "override" / "stage0" / "default" / "label" */
const char *mps3_id_s0_state_name(int s);   /* "nowindow" / "invalid" / "valid" */

/* ---- validation: NULL = accepted, else the reason (a static string) -------- */
const char *mps3_id_check_label(const char *s);
const char *mps3_id_check_hostname(const char *s);
/* "a.b.c.d/nn" (nn 8..30), or "a.b.c.d" with need_prefix 0 (-> dflt_prefix):
 * a usable host address -- not 0/8, 127/8, multicast/reserved (>= 224), and not
 * the network or broadcast address of its prefix. */
const char *mps3_id_parse_ip(const char *s, int need_prefix, unsigned dflt_prefix,
                             uint32_t *ip, unsigned *prefix);
/* 12 hex digits, bare or ':'/'-' separated; unicast (byte 0 bit 0 clear), not 0. */
const char *mps3_id_parse_mac(const char *s, uint8_t mac[6]);
/* The same IP rules on a binary value (a stage0 word). */
const char *mps3_id_check_ip(uint32_t ip, unsigned prefix);
const char *mps3_id_check_mac(const uint8_t mac[6]);

/* ---- rendering ---------------------------------------------------------------- */
void mps3_id_fmt_ip(uint32_t ip, char *out, size_t cap);                 /* a.b.c.d    */
void mps3_id_fmt_cidr(uint32_t ip, unsigned prefix, char *out, size_t cap); /* a.b.c.d/nn */
void mps3_id_fmt_mac12(const uint8_t mac[6], char out[13]);              /* 0200004d5053 */
void mps3_id_fmt_mac_colon(const uint8_t mac[6], char out[18]);          /* 02:00:00:4d:50:53 */

/* ---- the sources ---------------------------------------------------------------- */
/* The override file's TEXT -> the fields it validly sets. Line by line: blank and
 * '#' lines skipped; MPS3_LABEL / MPS3_HOSTNAME / MPS3_IP / MPS3_MAC =VALUE (value
 * optionally in single or double quotes); a later line for the same key wins, as
 * in the shell. A line that is not KEY=VALUE, names another key, or carries a
 * value the rules refuse is IGNORED and logged (it never takes the whole file
 * down). Returns the number of lines ignored. */
int  mps3_id_parse_override(const char *text, size_t n, mps3_id_fields_t *out,
                            mps3_id_log_fn log, void *ctx);
/* The stage0 status block (64 little-endian words, or NULL) -> its identity
 * fields; returns MPS3_ID_S0_*. A zero field is absent; a nonzero one the rules
 * refuse is absent and logged. */
int  mps3_id_from_stage0(const volatile uint32_t *blk, mps3_id_fields_t *out,
                         mps3_id_log_fn log, void *ctx);
void mps3_id_defaults(mps3_id_fields_t *out);

/* Per field: override, else stage0, else default; hostname: override, else the
 * label lowercased (source "label"; if that is not a valid host name, the
 * default label's, source "default"). */
void mps3_id_resolve(const mps3_id_fields_t *ovr, const mps3_id_fields_t *s0,
                     mps3_identity_t *out);

/* ---- /run/mps3/identity (shell-sourceable; S41mps3net sources it) -------------- */
int  mps3_id_render_run(const mps3_identity_t *id, int s0_state, int persist,
                        char *out, size_t cap);
/* Parse a run file back: 0 = all four fields valid (sources filled; an unknown
 * source name reads as "default"); -1 = anything missing or refused. */
int  mps3_id_parse_run(const char *text, size_t n, mps3_identity_t *out);

/* ---- the override file (what `set` writes) ---------------------------------- */
int  mps3_id_render_override(const mps3_id_fields_t *ovr, char *out, size_t cap);
/* One edit, "key" in label/hostname/ip/mac (the wire names). An EMPTY value
 * removes that key from the override. NULL = applied; else the reason ("unknown
 * key" for any other key) -- the caller names the field ("invalid ip: ..."). An
 * IP without "/nn" takes /24. */
const char *mps3_id_apply_edit(mps3_id_fields_t *ovr, const char *key, const char *val);

/* Fields of `a` that differ from `b` (both fully resolved): an MPS3_ID_F mask. */
unsigned mps3_id_diff(const mps3_identity_t *a, const mps3_identity_t *b);

/* ---- the `identity` reply body ---------------------------------------------------
 * One renderer for the 6900 verb (identity_linux.c) and `mps3-identity get --json`:
 *   "label","hostname","ip":"a.b.c.d/nn","mac":"12hex",
 *   "source":{label,hostname,ip,mac},"stage0":{label,ip,mac}|null,
 *   "override":{...}|null,"pending":{...}|null,"persist":bool
 * (no braces: the caller wraps it). `stage0` is null unless the block is valid;
 * inside it an absent field is null, and its ip carries the implied /24.
 * `override` lists only the keys the file validly sets ({} = a file with none);
 * null = no override in force (no file, or no card-backed /persist). `pending`
 * = the fields `next` (what the next boot would resolve) changes, null if none. */
typedef struct {
    mps3_identity_t  running;     /* this boot's (the run file)                   */
    int              s0_state;    /* MPS3_ID_S0_*                                 */
    mps3_id_fields_t s0;          /* the block's identity fields, as read now     */
    int              ovr_present; /* an override file is in force                 */
    mps3_id_fields_t ovr;
    int              persist;     /* /persist is on the card (writes survive)     */
    mps3_identity_t  next;        /* resolve(ovr, s0) now                         */
} mps3_id_status_t;
int  mps3_id_json_status(const mps3_id_status_t *st, char *out, size_t cap);
/* just the pending object ("null" or {...}) */
int  mps3_id_json_pending(const mps3_identity_t *next, const mps3_identity_t *running,
                          char *out, size_t cap);

/* ---- file helpers (the only I/O here) -------------------------------------------- */
/* Read up to MPS3_ID_FILE_MAX bytes: >= 0 = length (buf NUL-terminated), -1 = no
 * file / unreadable, -2 = too big. */
int  mps3_id_read_file(const char *path, char *buf, size_t cap);
/* tmp + fsync + rename + fsync(dir): a torn write leaves the previous file. The
 * directory is created (0755) if missing. 0 = ok, else -errno. */
int  mps3_id_write_atomic(const char *path, const char *data, size_t n, unsigned mode);
/* unlink + fsync(dir); a missing file is 0. */
int  mps3_id_remove(const char *path);

/* ---- the operations both front ends share ------------------------------------------ */
/* /persist is on the card: the first token of IMAGE's persist.state is
 * "backing=card" (S12mps3persist). A tmpfs /persist (netboot, mps3.persist=off, a
 * blank p3) is NOT persistent: an override written there would vanish, so it is
 * neither read at boot nor written by `set`. */
int  mps3_id_persist_ok(const char *persist_state_path);
/* Read the override in force: 1 = present (fields in *ovr), 0 = none. Only when
 * `persist`; a line it refuses is logged and skipped. */
int  mps3_id_load_override(const char *path, int persist, mps3_id_fields_t *ovr,
                           mps3_id_log_fn log, void *ctx);
/* Fill *st: the running identity is given; the override and the block are read now. */
void mps3_id_gather(const char *override_path, int persist, const volatile uint32_t *s0blk,
                    const mps3_identity_t *running, mps3_id_status_t *st,
                    mps3_id_log_fn log, void *ctx);

/* `set` / `identity_set`: ALL edits validated first (nothing is written if one is
 * refused), then applied onto the current override and written atomically;
 * `clear` removes the file. Returns 0, or:
 *   MPS3_ID_EINVALID  -- *bad_field = the field, *why = the reason
 *   MPS3_ID_ENOPERSIST -- /persist is not on the card
 *   MPS3_ID_EIO       -- the write failed (*why = strerror)                     */
enum { MPS3_ID_OK = 0, MPS3_ID_EINVALID = 1, MPS3_ID_ENOPERSIST = 2, MPS3_ID_EIO = 3 };
int  mps3_id_do_set(const char *override_path, int persist, int clear,
                    const char *const *keys, const char *const *vals, int n,
                    const char **bad_field, const char **why);

#ifdef __cplusplus
}
#endif

#endif /* HARNESSD_IDENTITY_CORE_H */
