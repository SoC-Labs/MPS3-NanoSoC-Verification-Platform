/*
 * lcdmirror.h -- the pixel-exact LCD mirror, harnessd side: ONE layout for both
 * sources, the GRAM model, the tile codec and the 6940 wire constants.
 *
 * Contract: docs/planning/linux_lanes/LCD_MIRROR_FPGA.md (§2 model, §4 register
 * map, §6 Linux side, §6.2 wire + its six amendments, §7 interim mode) and
 * docs/contracts/net-protocol.md "LCD mirror (TCP 6940)" (the byte layout).
 *
 * THE BACKEND SWAP. The mint-4 snooper exposes a 256 KiB aperture (LCDMIR @
 * 0x44B8_0000). The interim software mode writes the SAME aperture layout into
 * a shared-memory file (/dev/shm/mps3-lcdmirror): harnessd's register tap feeds
 * every CLCD byte harnessd itself issues into lcdm_model_*(), which keeps a
 * 320x240 RGB565 frame in viewer order, the 16x16-tile DIRTY/VALID maps and the
 * raw 256-register file at the hardware offsets. mps3-lcdmirror (the niced
 * child that serves 6940) reads either source through lcdm_src_t, so going to
 * hardware is a backend swap, not a protocol change.
 *
 * WHAT ONLY SOFTWARE HAS: the page at LCDM_SW_* (0x1000..0x1FFF, unused in the
 * hardware map). The hardware's SNAP is one CTRL write that copies the live
 * dirty map into DIRTY and clears it in the same cycle; two processes cannot do
 * that with plain stores, so the model ORs its dirty bits into LCDM_SW_LIVE with
 * a release RMW and the child's SNAP exchanges them out with an acquire RMW.
 * A tile written after the exchange is marked again and resent on the next pass:
 * the mirror converges, exactly as §2.7 says of the hardware.
 */
#ifndef HARNESSD_LCDMIRROR_H
#define HARNESSD_LCDMIRROR_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- geometry ------------------------------------------------------------- */
#define LCDM_W          320u
#define LCDM_H          240u
#define LCDM_TILE       16u
#define LCDM_TX         20u                  /* tiles across                  */
#define LCDM_TY         15u                  /* tiles down                    */
#define LCDM_NTILES     300u
#define LCDM_MAP_WORDS  10u                  /* ceil(300 / 32)                */
#define LCDM_MAP_BYTES  38u                  /* ceil(300 / 8): the wire map   */
#define LCDM_TILE_PX    256u
#define LCDM_NPX        (LCDM_W * LCDM_H)    /* 76,800                        */

/* ---- the aperture (LCD_MIRROR_FPGA.md §4), byte offsets ------------------ */
#define LCDM_ID          0x000u   /* RO 0x4C43444D "LCDM"                     */
#define LCDM_VERSION     0x004u   /* RO [31:24] 1 [23:16] 0 [15:8] 16 [7:0] 1  */
#define LCDM_GEOM        0x008u   /* RO [31:16] H=240 [15:0] W=320            */
#define LCDM_CTRL        0x00Cu
#define LCDM_STATUS      0x010u
#define LCDM_SEQ         0x014u   /* pixels written (wraps)                   */
#define LCDM_FRAMES      0x018u   /* window completions                       */
#define LCDM_RAMWR       0x01Cu   /* 0x22 index writes                        */
#define LCDM_RESETS      0x020u   /* panel RST assertions                     */
#define LCDM_BYTES       0x024u   /* 8080 write cycles decoded                */
#define LCDM_VIOL        0x028u
#define LCDM_OOB         0x02Cu
#define LCDM_RDS         0x030u
#define LCDM_TMIN        0x034u
#define LCDM_WIN_X       0x040u   /* {EC[24:16], SC[8:0]}                     */
#define LCDM_WIN_Y       0x044u   /* {EP[24:16], SP[8:0]}                     */
#define LCDM_AC          0x048u   /* {y[24:16], x[8:0]}                        */
#define LCDM_MODE        0x04Cu   /* R16 | R17<<8 | R36<<16 | R01<<24         */
#define LCDM_SNAP_SEQ    0x050u
#define LCDM_SNAP_BBOX_X 0x054u   /* {max[24:16], min[8:0]}, empty: min > max  */
#define LCDM_SNAP_BBOX_Y 0x058u
#define LCDM_DIRTY       0x080u   /* [10] snapshot dirty tiles                 */
#define LCDM_VALID       0x0C0u   /* [10] live: written since the last RST     */
#define LCDM_REGS        0x100u   /* 256 bytes: byte i = last datum to index i  */
#define LCDM_FB          0x10000u /* 38,400 words: px[vy*320+vx], LE, even x low */
#define LCDM_APERTURE    0x40000u /* 256 KiB                                   */

#define LCDM_ID_VALUE      0x4C43444Du
#define LCDM_VERSION_VALUE 0x01001001u
#define LCDM_GEOM_VALUE    ((LCDM_H << 16) | LCDM_W)

/* CTRL */
#define LCDM_CTRL_AC_LOAD     (1u << 0)   /* 0 = on start-register write, 1 = on 0x22 */
#define LCDM_CTRL_FLIP_CONV   (1u << 1)
#define LCDM_CTRL_SNAP        (1u << 8)   /* W1P */
#define LCDM_CTRL_CLR_STICKY  (1u << 9)   /* W1P */
#define LCDM_CTRL_CLR_COUNTS  (1u << 10)  /* W1P */

/* STATUS */
#define LCDM_ST_RST_N       (1u << 0)   /* live                               */
#define LCDM_ST_BL          (1u << 1)   /* live                               */
#define LCDM_ST_OWNER       (1u << 2)   /* 0 harness, 1 DUT                   */
#define LCDM_ST_DISPLAY_ON  (1u << 3)   /* R28 GON.DTE.D = 11                  */
#define LCDM_ST_STANDBY     (1u << 4)   /* R1F STB                            */
#define LCDM_ST_IN_GRAM     (1u << 5)   /* idx = 0x22                         */
#define LCDM_ST_FMT_OK      (1u << 6)
#define LCDM_ST_APPROX      (1u << 7)
#define LCDM_ST_VIOL        (1u << 8)   /* sticky                             */
#define LCDM_ST_OOB         (1u << 9)   /* sticky                             */
#define LCDM_ST_RD_SEEN     (1u << 10)  /* sticky                             */
#define LCDM_ST_STICKY      (LCDM_ST_VIOL | LCDM_ST_OOB | LCDM_ST_RD_SEEN)

/* ---- the software-only page (0x1000..0x1FFF; unused in the hardware map) --- */
#define LCDM_SW_MAGIC      0x1000u  /* "LMSW" once harnessd initialised the file */
#define LCDM_SW_VERSION    0x1004u
#define LCDM_SW_WRITER     0x1008u  /* harnessd's pid                            */
#define LCDM_SW_GEN        0x100Cu  /* bumped on every harnessd start            */
#define LCDM_SW_STATIC_ID  0x1010u  /* for HELLO                                  */
#define LCDM_SW_MODE       0x1014u  /* 1 = sw (this file); 0 = hw (future)         */
#define LCDM_SW_PUBSEQ     0x1018u  /* header seqlock: odd while a publish runs   */
#define LCDM_SW_PUBS       0x101Cu  /* publishes that changed something           */
#define LCDM_SW_PUB_MAX_NS 0x1020u  /* worst publish, ns                          */
#define LCDM_SW_PASS_BYTES 0x1024u  /* most CLCD bytes the tap saw in one pass    */
#define LCDM_SW_TAP_BYTES  0x1028u  /* CLCD bytes the tap fed the model, ever      */
#define LCDM_SW_BLIND      0x102Cu  /* 1 while the DUT owns the panel (sw mode)   */
#define LCDM_SW_MODEL_PS   0x1030u  /* the model's measured cost, ps per CLCD byte */
#define LCDM_SW_LIVE       0x1040u  /* [10] live dirty map: model ORs, SNAP takes  */
#define LCDM_SW_MAGIC_VALUE 0x57534D4Cu   /* "LMSW" little-endian */
#define LCDM_SW_VERSION_VALUE 1u

/* The child's status page, read by harnessd for `stats.lcd_mirror`. Written
 * only by mps3-lcdmirror, under the LCDM_SV_SEQ seqlock. */
#define LCDM_SV_MAGIC      0x1100u  /* "LMSV"                                    */
#define LCDM_SV_SEQ        0x1104u  /* odd while the child writes                 */
#define LCDM_SV_PID        0x1108u
#define LCDM_SV_BEAT_MS    0x110Cu  /* CLOCK_MONOTONIC ms of the last write       */
#define LCDM_SV_CLIENTS    0x1110u
#define LCDM_SV_SINCE_MS   0x1114u  /* CLOCK_MONOTONIC ms the oldest client joined */
#define LCDM_SV_FPS_X10    0x1118u
#define LCDM_SV_BYTES      0x111Cu  /* bytes sent to the oldest client            */
#define LCDM_SV_REFUSED    0x1120u  /* refusal lines sent, ever                   */
#define LCDM_SV_UPDATES    0x1124u  /* UPDATEs sent, ever (all clients)           */
#define LCDM_SV_KEYS       0x1128u  /* keyframes started, ever                    */
#define LCDM_SV_PEER       0x1140u  /* 32 bytes, NUL-terminated "a.b.c.d:port"    */
#define LCDM_SV_PEER_LEN   32u
#define LCDM_SV_MAGIC_VALUE 0x56534D4Cu   /* "LMSV" */

/* ---- the GRAM model (lcdmirror_model.c): a C port of §2.3-2.7 ------------- */
typedef struct {
    volatile uint32_t *ap;          /* the aperture                               */
    volatile uint16_t *fb;          /* ap + LCDM_FB                               */
    uint32_t ctrl;                  /* LCDM_CTRL_AC_LOAD | LCDM_CTRL_FLIP_CONV     */
    uint32_t live;                  /* ST_RST_N | ST_BL | ST_OWNER, set from outside */
    uint32_t sticky;
    uint32_t seq, frames, ramwr, resets, bytes, oob;
    uint16_t sc, ec, sp, ep;        /* the window, 9 bits each                     */
    uint16_t acx, acy;              /* the address counter                         */
    uint8_t  idx;
    uint8_t  nb;                    /* pixel bytes pending                         */
    uint8_t  pb[3];
    uint8_t  mv, flip_g, flip_s;    /* MADCTL geometry, precomputed               */
    uint8_t  in_reset;              /* the panel is held in reset: bytes ignored   */
    uint8_t  regs_dirty;
    uint8_t  changed;               /* something to publish                        */
    uint8_t  r01, r16, r17, r1f, r28, r36;   /* the decoded/recorded registers  */
    uint8_t  regs[256];             /* the RAW log: never reset (golden model)     */
    uint32_t pend[LCDM_MAP_WORDS];  /* tiles written since the last publish        */
    uint32_t valid[LCDM_MAP_WORDS];
} lcdm_model_t;

/* Bind the model to an aperture and put it in the power-on state (the decoded
 * fields at their O3 defaults, the raw REGS log zero). keep_fb = 1
 * leaves the frame buffer's pixels alone (a harnessd respawn: the glass still
 * shows them), 0 clears them. VALID starts all 0 either way. */
void lcdm_model_init(lcdm_model_t *m, volatile uint32_t *ap, int keep_fb);
/* The panel's RST pad: held = 1 asserts it (the decoded state returns to the
 * datasheet defaults, VALID is cleared, RESETS counts the edge; bytes are
 * ignored until it is released). Frame-buffer pixels are kept (§2.5). */
void lcdm_model_set_reset(lcdm_model_t *m, int held);
/* A whole reset sequence the tap only saw complete (the KVM's handover pulse). */
void lcdm_model_pulse_reset(lcdm_model_t *m);
/* One 8080 write cycle: rs = 0 index, 1 data. */
void lcdm_model_byte(lcdm_model_t *m, int rs, uint8_t b);
/* n of them, in order (rs[i] 0 index, 1 data): EXACTLY lcdm_model_byte() called
 * for each, bit for bit, with a GRAM-data run at 16 bpp decoded without the
 * per-byte call (the CLCD bulk tap, hal.h; lane CLCD-SPEED). */
void lcdm_model_bytes(lcdm_model_t *m, const uint8_t *rs, const uint8_t *b, uint32_t n);
/* The live STATUS inputs the tap knows (ST_RST_N / ST_BL / ST_OWNER). */
void lcdm_model_set_live(lcdm_model_t *m, uint32_t mask, uint32_t bits);
/* Blind (sw mode, the DUT owns the panel): every VALID bit to 0. */
void lcdm_model_invalidate(lcdm_model_t *m);
void lcdm_model_set_ctrl(lcdm_model_t *m, uint32_t ctrl);
uint32_t lcdm_model_status(const lcdm_model_t *m);
/* Write the counters/STATUS/window/REGS/VALID into the aperture and OR the
 * pending dirty tiles into LCDM_SW_LIVE (release). Cheap when nothing changed. */
void lcdm_model_publish(lcdm_model_t *m);

/* ---- the tile codec + the wire builders (lcdmirror_enc.c) -------------------- */
enum {
    LCDM_ENC_FILL  = 0,   /* 2 B:  one colour                                        */
    LCDM_ENC_PAL1  = 1,   /* 36 B: 2 colours, then 16 u16 rows, bit x -> colour 1     */
    LCDM_ENC_PAL2  = 2,   /* 72 B: 4 colours, then 16 u32 rows, pixel x = bits 2x+1:2x */
    LCDM_ENC_RLE16 = 3,   /* PackBits over u16: 0x80|(n-1) + one u16 = a run of n;
                           * n-1 + n u16 = literals; n <= 128                          */
    LCDM_ENC_RAW   = 4,   /* 512 B                                                    */
};
#define LCDM_TILE_MAX_PAYLOAD 512u
#define LCDM_REC_MAX          (5u + LCDM_TILE_MAX_PAYLOAD)
/* Encode one tile (256 px, row-major, x fastest). Returns the payload length;
 * *enc gets the code. FILL for one colour; else RLE16 if shorter than 512 (else
 * RAW); PAL1 (2 colours) / PAL2 (3-4) only if STRICTLY shorter than that -- the
 * same choice as Harness Manager's encoder, byte for byte. Pixels little-endian. */
unsigned lcdm_encode_tile(const uint16_t px[LCDM_TILE_PX], uint8_t *out, uint8_t *enc);
/* Decode one payload; returns 0 on success, -1 if it is malformed. */
int lcdm_decode_tile(uint8_t enc, const uint8_t *in, unsigned len, uint16_t px[LCDM_TILE_PX]);
/* Copy tile t out of a viewer-order frame (320 px stride). */
void lcdm_tile_get(const volatile uint16_t *frame, unsigned t, uint16_t px[LCDM_TILE_PX]);
/* Test-only encoder mutations (host test builds; the product never sets them). */
enum { LCDM_MUT_NONE = 0, LCDM_MUT_TILE_IDX, LCDM_MUT_PAL_BITS, LCDM_MUT_RLE_LEN, LCDM_MUT_PX_ENDIAN };
extern int lcdm_mutation;

/* ---- the 6940 wire (net-protocol.md "LCD mirror (TCP 6940)"; LCD_MIRROR_FPGA.md
 * §6.2 + the six amendments + Harness Manager's H1/H3) ---------------------------
 * Every message: 'L' 'M' u8 type, u8 rsvd (0), u32 len (LE), then len bytes.
 * max_msg (HELLO) bounds the WHOLE message, this 8-byte header included. A
 * refusal is instead ONE JSON line (first byte '{'), then close. */
#define LCDM_PORT          6940u
#define LCDM_PROTO         1u
#define LCDM_MAX_CLIENTS   2u
#define LCDM_HDR           8u
#define LCDM_UPD_FIXED     59u       /* seq t_ms frames resets status owner valid[38] */
#define LCDM_MAX_MSG       65536u
#define LCDM_MIN_MSG       4096u
#define LCDM_WINDOW        2u        /* UPDATEs a client may leave unACKed (H1)     */
#define LCDM_RATE_DEFAULT  5u
#define LCDM_RATE_MAX      30u       /* RATE 0 = pause                              */
#define LCDM_CLIENT_MSG_MAX 64u      /* a client body longer than this = close       */

#define LCDM_MSG_HELLO   0x01u       /* board: JSON                                 */
#define LCDM_MSG_UPDATE  0x02u       /* board                                       */
#define LCDM_MSG_KEY     0x10u       /* client                                      */
#define LCDM_MSG_RATE    0x11u       /* client u8 hz; the board answers u8 clamped  */
#define LCDM_MSG_PING    0x12u       /* client u32                                  */
#define LCDM_MSG_PONG    0x13u       /* board u32 (the PING's)                      */
#define LCDM_MSG_ACK     0x14u       /* client u32 seq                              */

/* UPDATE.status: [10:0] the CSR STATUS, then the wire's own bits */
#define LCDM_S_EXACT      (1u << 16) /* hw && !viol && fmt_ok && !approx            */
#define LCDM_S_TEXT_ONLY  (1u << 17) /* sw: exact only for clcd.c's text cells      */
#define LCDM_S_BLIND      (1u << 18) /* sw while the DUT owns the panel             */
#define LCDM_S_KEY        (1u << 24) /* a part of a keyframe                        */
#define LCDM_S_KEY_FIRST  (1u << 25) /* its first part: REGS[256] instead of MODE   */
#define LCDM_S_KEY_LAST   (1u << 26)
#define LCDM_S_SNAP_LAST  (1u << 27) /* the last UPDATE of one SNAP                 */
#define LCDM_OWNER_HARNESS 0u
#define LCDM_OWNER_DUT     1u
#define LCDM_OWNER_UNKNOWN 3u

#define LCDM_MODE_HW 0u
#define LCDM_MODE_SW 1u

/* One SNAP's header, shared by all its UPDATE parts (H1: one t_ms / frames). */
typedef struct {
    uint32_t t_ms, frames, resets, status;   /* status: CSR bits + EXACT/TEXT_ONLY/BLIND */
    uint8_t  owner;
    uint8_t  valid[LCDM_MAP_BYTES];
    uint8_t  regs[256];
    uint32_t mode;
} lcdm_snaphdr_t;

/* The 8-byte header. */
void lcdm_put_hdr(uint8_t *out, uint8_t type, uint32_t len);
/* Records {u16 idx, u8 enc, u16 len, payload} for every tile set in tiles[],
 * in index order, from a viewer-order frame. Returns bytes; *ntiles the count. */
size_t lcdm_encode_records(const uint16_t *frame, const uint32_t tiles[LCDM_MAP_WORDS],
                           uint8_t *out, unsigned *ntiles);
/* Record bytes one part of a snap may carry: max_msg less the header, the fixed
 * UPDATE fields, REGS (key snaps, every part: the conservative bound) or MODE,
 * and ntiles. */
size_t lcdm_part_budget(unsigned max_msg, int key);
/* The end of the next part starting at `start` (at least one record); *n its count. */
size_t lcdm_next_part(const uint8_t *recs, size_t len, size_t start, size_t budget, unsigned *n);
/* One whole UPDATE message into out; returns its size. `flags` are the KEY /
 * KEY_FIRST / KEY_LAST / SNAP_LAST bits of this part (REGS ride on KEY_FIRST). */
size_t lcdm_build_update(uint8_t *out, uint32_t seq, const lcdm_snaphdr_t *h, uint32_t flags,
                         const uint8_t *recs, size_t rlen, unsigned ntiles);
/* The refusal (amendment 3): ONE line, then the board closes. Returns its length. */
#define LCDM_REFUSE_NOT_LOOPBACK "not loopback (use an ssh port forward)"
#define LCDM_REFUSE_BUSY         "busy (2 clients)"
size_t lcdm_refusal_line(char *out, size_t cap, const char *why);
/* The HELLO message (header + JSON); returns its size. */
size_t lcdm_build_hello(uint8_t *out, size_t cap, uint32_t static_id, unsigned mode,
                        unsigned max_msg, const char *boot_id, unsigned rate);

/* ---- harnessd hooks (lcdmirror_tap.c) -------------------------------------- */
/* Called by main_linux.c: after clcd_init() (tap + shm + first spawn), from the
 * clcd service slot every pass (publish + supervise), and on a clean stop. */
void harnessd_lcdmirror_init(void);
void harnessd_lcdmirror_poll(void);
void harnessd_lcdmirror_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* HARNESSD_LCDMIRROR_H */
