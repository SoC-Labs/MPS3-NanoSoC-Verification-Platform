/*
 * proto.h — MPS3 harness control-channel wire types (Linux userspace port).
 *
 * Semantics are a faithful port of the FROZEN bare-metal contract:
 *   firmware/common/net_proto.{h,c}      (normative encoder/decoder)
 *   firmware/coordinator/coordinator.c   (dispatch + error strings)
 * as frozen by SERVICE_DISPOSITION.md §2/§2A. Nothing here is invented:
 * every reply shape and error string traces to those files.
 *
 * Parser guarantees (same as bare metal, fail-closed):
 *   - FLAT one-line JSON objects only; string/int/bool/null values only
 *   - no nesting, no arrays, no \uXXXX escapes, no duplicate keys
 *   - max 8 members, bounded string lengths
 *   - anything else = EMALFORMED ("bad json" on the wire)
 */
#ifndef MPS3_PROTO_H
#define MPS3_PROTO_H

#include <stdint.h>
#include <stddef.h>

/* ---- limits (firmware/common/net_proto.h) ---- */
#define MPS3_JSON_MAX_MEMBERS 8
#define MPS3_CTRL_RESP_MAX    384
#define MPS3_CTRL_LINE_MAX    384   /* request line assembly bound; oversize -> "bad json" */

#define MPS3_PORT_CONTROL 6900
#define MPS3_PORT_PUSH    6910

/* ---- flat JSON tokenizer ---- */
typedef enum {
    MPS3_JSON_T_STRING,
    MPS3_JSON_T_INT,
    MPS3_JSON_T_BOOL,
    MPS3_JSON_T_NULL,
} mps3_json_type_t;

typedef struct {
    const char *key;
    int key_len;
    const char *val;   /* raw span for strings */
    int val_len;
    int32_t ival;      /* ints and bools */
    mps3_json_type_t type;
} mps3_json_member_t;

typedef struct {
    mps3_json_member_t members[MPS3_JSON_MAX_MEMBERS];
    int n_members;
} mps3_json_obj_t;

#define MPS3_JSON_OK          0
#define MPS3_JSON_EMALFORMED -1
#define MPS3_JSON_EMISSING   -2
#define MPS3_JSON_ETYPE      -3
#define MPS3_JSON_ETOOLONG   -4

int mps3_json_parse(const char *line, int len, mps3_json_obj_t *out);
int mps3_json_get_string(const mps3_json_obj_t *obj, const char *key,
                         char *out, int out_sz);
int mps3_json_get_int(const mps3_json_obj_t *obj, const char *key, int32_t *out);
int mps3_json_get_bool(const mps3_json_obj_t *obj, const char *key, int *out);

/* ---- verbs ---- */
typedef enum {
    MPS3_OP_UNKNOWN = 0,
    MPS3_OP_PING,
    MPS3_OP_RESET,
    MPS3_OP_SET_CLK,
    MPS3_OP_SWAP,
    MPS3_OP_LINK,
    MPS3_OP_COMMIT,
    MPS3_OP_TELEMETRY,
    MPS3_OP_MACGEN,
    MPS3_OP_DIAG,
    MPS3_OP_DISPLAY,
} mps3_ctrl_op_t;

typedef struct {
    mps3_ctrl_op_t op;
    char target[16];   /* reset  */
    char preset[16];   /* set_clk */
    char rm[32];       /* swap/commit */
    char src[16];      /* swap   */
    char event[16];    /* link   */
    char inject[16];   /* macgen */
    int  gen, chk;     /* macgen */
    char owner[16];    /* display */
} mps3_ctrl_request_t;

#define MPS3_CTRL_DECODE_OK           0
#define MPS3_CTRL_DECODE_EBADJSON    -1
#define MPS3_CTRL_DECODE_EUNKNOWN_OP -2
#define MPS3_CTRL_DECODE_EBADARGS    -3

int mps3_ctrl_decode_line(const char *line, int len, mps3_ctrl_request_t *out);

/* ---- response ---- */
typedef struct {
    mps3_ctrl_op_t op;
    int  ok;
    char err[64];
    char shell_id[16];   /* "0x%08x" */
    char rm_id[16];      /* "0x%08x" */
    int  locked;         /* set_clk */
    int  verified;       /* swap */
    char slot;           /* commit: 'A'/'B' */
    int  lockup;         /* telemetry failure line */
    char owner[16];      /* display */
    uint32_t tx_cnt, rx_cnt, err_cnt;            /* macgen */
    uint32_t diag_rx_recover, diag_rx_dumps, diag_rx_drops,
             diag_icap_bytes, diag_got, diag_expect,
             diag_rcv_wnd, diag_rcv_ann_wnd, diag_rx_queued,
             diag_pbuf_free, diag_grants_sent, diag_grant_fails,
             diag_sndbuf, diag_snd_wnd;          /* diag: 14 frozen keys */
} mps3_ctrl_response_t;

/* Returns bytes written (incl. trailing '\n'), or -1 (caller-bug shapes are
 * unencodable, e.g. ok:true telemetry / ok commit without a slot). */
int mps3_ctrl_encode_response(const mps3_ctrl_response_t *resp,
                              char *out, int out_len);

void mps3_format_id_hex(char *dst, size_t dst_sz, uint32_t id);

/* ---- bitstream push header (TFTP + 6910), big-endian ">4sHBBIIII" ---- */
#define MPS3_BITSTREAM_MAGIC "MPS3"
#define MPS3_BITSTREAM_HDR_WIRE_SIZE 24
#define MPS3_BITSTREAM_VER 1
#define MPS3_BITSTREAM_KIND_CLEARING 0
#define MPS3_BITSTREAM_KIND_PARTIAL  1

typedef struct {
    char     magic[4];
    uint16_t ver;
    uint8_t  kind;
    uint8_t  rm_slot;
    uint32_t static_id;
    uint32_t rm_id;
    uint32_t len_words;   /* payload bytes / 4 */
    uint32_t crc32;       /* zlib/IEEE, payload only */
} mps3_bitstream_hdr_t;

int mps3_bitstream_hdr_unpack(const uint8_t *in, uint32_t in_len,
                              mps3_bitstream_hdr_t *out);

/* ---- CRC32 (zlib/IEEE, reflected 0xEDB88320) ---- */
uint32_t mps3_crc32_update(uint32_t crc, const void *buf, size_t len);
static inline uint32_t mps3_crc32_init(void)  { return 0u; }

#endif /* MPS3_PROTO_H */
