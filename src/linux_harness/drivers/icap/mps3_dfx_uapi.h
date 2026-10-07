/*
 * mps3_dfx_uapi.h — userspace ABI of /dev/mps3dfx (the DFX swap chardev).
 *
 * The swap daemon (the Linux successor of coordinator/config_agent) drives
 * one swap as:
 *   open() (single owner)
 *   SWAP_BEGIN                  -> decouple+shutdown+resets, confirm-polled
 *   PUSH_BEGIN{kind=CLEARING}   -> staged: driver vmallocs len_words*4
 *   write() x N                 -> payload bytes (the OUTGOING RM's clearing)
 *   PUSH_END                    -> CRC gate (BEFORE any ICAP write), stream
 *   PUSH_BEGIN{kind=PARTIAL}    -> staged or STREAM_DIRECT (flags bit0)
 *   write() x N                 -> stream-direct packs+pushes as bytes land
 *   PUSH_END                    -> frame CRC gate / EOS capture
 *   SWAP_FINISH                 -> hostio4 hook, release, RM_ID verify,
 *                                  commit or re-isolate; result returned
 * Any failure parks the RP decoupled+in-reset and returns the engine to
 * IDLE; the daemon retries with a fresh SWAP_BEGIN. An armed session with
 * no write() progress for idle_ms (default 30000 — the silicon-observed
 * 2026-07-09 hang bound) is reaped to the same parked state.
 *
 * kind/rm_slot/static_id/rm_id/len_words/crc32 mirror the frozen 6910 wire
 * header (common/net_proto.h `>4sHBBIIII`); static_id/ordering validation
 * beyond the pair rule stays in the daemon (SERVICE_DISPOSITION §3.3).
 */
#ifndef MPS3_DFX_UAPI_H
#define MPS3_DFX_UAPI_H

#include <linux/types.h>
#include <linux/ioctl.h>

#define MPS3_DFX_KIND_CLEARING 0u
#define MPS3_DFX_KIND_PARTIAL  1u

#define MPS3_DFX_PUSH_F_STREAM_DIRECT 0x1u

struct mps3_dfx_push {
	__u32 kind;      /* MPS3_DFX_KIND_*                                */
	__u32 rm_slot;   /* recorded only (daemon concern)                 */
	__u32 static_id; /* recorded only (daemon validates vs shell)      */
	__u32 rm_id;     /* PARTIAL: the verify target (from the wire hdr) */
	__u32 len_words; /* payload bytes / 4                              */
	__u32 crc32;     /* zlib/IEEE over payload only                    */
	__u32 flags;
};

struct mps3_dfx_result {
	__u32 ok;
	__u32 verified;
	__u32 rm_id;     /* confirmed id (success) / still-resident id (fail) */
	__s32 err;       /* MPS3_E* engine code of the failure, 0 on success  */
};

struct mps3_dfx_status {
	__u32 state;           /* mps3_swap_state_t (frozen numeric values) */
	__u32 active;
	__u32 current_rm_id;   /* last VERIFIED rm_id                       */
	__u32 icap_bytes_lo;   /* free-running confirmed-into-ICAP counter  */
	__u32 icap_bytes_hi;
	__u32 sr_last;         /* last non-zero HWICAP_SR at finish         */
	__u32 eos_status;      /* 0 none / 1 seen / 2 timeout               */
	__u32 hostio4_calls;
	__u32 last_valid, last_ok, last_verified, last_rm_id;
	__s32 last_err;
	__u32 dfxctl_status;   /* raw DFXCTL.STATUS                         */
};

/* mock-mode only (module param mock=1): configure faults / read the stream
 * capture. -ENOTTY on a real-hardware bind. */
struct mps3_dfx_mock_cfg {
	__u32 next_rm_id;
	__u32 faults;       /* MPS3_MOCK_FAULT_* */
	__u32 status_lat, valid_lat, cr_lat, fifo_depth;
};

struct mps3_dfx_mock_state {
	__u32 words_total;
	__u32 stream_crc;   /* finalised zlib CRC of the re-serialised stream */
	__u32 saw_sync, saw_desync, sync_count;
	__u32 fifo_overflow;
	__u32 first_words[8];
};

#define MPS3_DFX_IOC_MAGIC 'X'
#define MPS3_DFX_IOC_SWAP_BEGIN   _IO(MPS3_DFX_IOC_MAGIC, 1)
#define MPS3_DFX_IOC_PUSH_BEGIN   _IOW(MPS3_DFX_IOC_MAGIC, 2, struct mps3_dfx_push)
#define MPS3_DFX_IOC_PUSH_END     _IO(MPS3_DFX_IOC_MAGIC, 3)
#define MPS3_DFX_IOC_SWAP_FINISH  _IOR(MPS3_DFX_IOC_MAGIC, 4, struct mps3_dfx_result)
#define MPS3_DFX_IOC_ABORT        _IO(MPS3_DFX_IOC_MAGIC, 5)
#define MPS3_DFX_IOC_GET_STATUS   _IOR(MPS3_DFX_IOC_MAGIC, 6, struct mps3_dfx_status)
#define MPS3_DFX_IOC_MOCK_SET     _IOW(MPS3_DFX_IOC_MAGIC, 0x40, struct mps3_dfx_mock_cfg)
#define MPS3_DFX_IOC_MOCK_GET     _IOR(MPS3_DFX_IOC_MAGIC, 0x41, struct mps3_dfx_mock_state)
#define MPS3_DFX_IOC_MOCK_ARM_CAP _IO(MPS3_DFX_IOC_MAGIC, 0x42)

#endif /* MPS3_DFX_UAPI_H */
