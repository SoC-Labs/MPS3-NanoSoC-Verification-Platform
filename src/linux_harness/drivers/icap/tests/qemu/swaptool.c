/*
 * swaptool.c — guest-side driver exerciser for /dev/mps3dfx (mock mode).
 *
 * One INVOCATION per scenario (the chardev is single-open and release()
 * deliberately reaps an active session — split invocations would abort the
 * swap they set up). Each verb prints exactly one line
 *     RESULT <name> PASS
 *     RESULT <name> FAIL <detail>
 * so the serial-console harness can grep verdicts.
 *
 * Verbs:
 *   happy   <clr> <part> <rm_id> sd|staged   full swap + capture checks
 *   wrongid <clr> <part> <rm_id>             mock presents DEADBEEF -> reisolate
 *   novalid <clr> <part> <rm_id>             rm_id never valid -> timeout, parked
 *   crstuck <clr>                            CR never self-clears -> ESTREAM parked
 *   wfvstuck <clr>                           WFV stuck 0 -> ESTREAM parked
 *   decouple                                 decouple never confirms -> begin fails
 *   release <clr> <part> <rm_id>             release never confirms -> parked
 *   sdcrc   <clr> <part> <rm_id>             corrupt sd CRC -> EBADMSG-class, parked
 *   badcrc  <clr>                            staged CRC gate rejects PRE-ICAP
 *   order   <clr> <part> <rm_id>             pair-ordering violations refused
 *   second  <clr>                            second open() -> EBUSY mid-swap
 *   idle    <clr> <part> <rm_id> <sleep_ms>  RX-idle reap to parked (short idle_ms)
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/stat.h>

#include "../../mps3_dfx_uapi.h"
#include "../../mps3_icap_mock.h"   /* MPS3_MOCK_FAULT_* */
#include "../../mps3_crc32.h"
#include "../../mps3_icap_engine.h" /* MPS3_E*, states */

#define DEV "/dev/mps3dfx"
static const char *g_name = "?";

static void pass(void) { printf("RESULT %s PASS\n", g_name); exit(0); }
static void fail(const char *fmt, ...)
{
	va_list ap;
	printf("RESULT %s FAIL ", g_name);
	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	printf("\n");
	exit(1);
}

static u8 *load(const char *path, u32 *len)
{
	struct stat st;
	FILE *f = fopen(path, "rb");
	u8 *b;

	if (!f)
		fail("open %s: %s", path, strerror(errno));
	if (fstat(fileno(f), &st))
		fail("stat %s", path);
	b = malloc(st.st_size);
	if (!b || fread(b, 1, st.st_size, f) != (size_t)st.st_size)
		fail("read %s", path);
	fclose(f);
	*len = (u32)st.st_size;
	return b;
}

static int g_fd = -1;

static void mock_cfg(u32 next_rm_id, u32 faults)
{
	struct mps3_dfx_mock_cfg c = {
		.next_rm_id = next_rm_id, .faults = faults,
		.status_lat = 2, .valid_lat = 3, .cr_lat = 1, .fifo_depth = 1024,
	};
	if (ioctl(g_fd, MPS3_DFX_IOC_MOCK_SET, &c))
		fail("MOCK_SET: %s", strerror(errno));
	if (ioctl(g_fd, MPS3_DFX_IOC_MOCK_ARM_CAP))
		fail("MOCK_ARM_CAP: %s", strerror(errno));
}

static void get_status(struct mps3_dfx_status *s)
{
	if (ioctl(g_fd, MPS3_DFX_IOC_GET_STATUS, s))
		fail("GET_STATUS: %s", strerror(errno));
}

static void get_mock(struct mps3_dfx_mock_state *m)
{
	if (ioctl(g_fd, MPS3_DFX_IOC_MOCK_GET, m))
		fail("MOCK_GET: %s", strerror(errno));
}

static int check_parked(const char *tag)
{
	struct mps3_dfx_status s;
	u32 parked = DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET;
	int i;

	/* status settles over a few reads (mock latency) */
	for (i = 0; i < 8; i++)
		get_status(&s);
	if ((s.dfxctl_status & parked) != parked)
		fail("%s: not parked (dfxctl=0x%x)", tag, s.dfxctl_status);
	if (s.state != SWAP_IDLE)
		fail("%s: engine not idle (state=%u)", tag, s.state);
	return 0;
}

static void push(u32 kind, const u8 *buf, u32 len, u32 rm_id, int sd,
		 u32 crc_override, int expect_errno, const char *tag)
{
	struct mps3_dfx_push p;
	int rc;

	memset(&p, 0, sizeof(p));
	p.kind = kind;
	p.rm_id = rm_id;
	p.len_words = len / 4u;
	p.crc32 = crc_override ? crc_override : mps3_crc32(buf, len);
	p.flags = sd ? MPS3_DFX_PUSH_F_STREAM_DIRECT : 0;

	rc = ioctl(g_fd, MPS3_DFX_IOC_PUSH_BEGIN, &p);
	if (rc) {
		if (expect_errno && errno == expect_errno)
			return; /* the expected refusal */
		fail("%s PUSH_BEGIN: %s", tag, strerror(errno));
	}
	if (expect_errno == -1)
		fail("%s PUSH_BEGIN unexpectedly accepted", tag);

	{
		u32 off = 0;
		while (off < len) {
			u32 n = len - off;
			if (n > 65536)
				n = 65536;
			ssize_t w = write(g_fd, buf + off, n);
			if (w < 0) {
				if (expect_errno && errno == expect_errno)
					return;
				fail("%s write@%u: %s", tag, off, strerror(errno));
			}
			off += (u32)w;
		}
	}
	rc = ioctl(g_fd, MPS3_DFX_IOC_PUSH_END);
	if (rc) {
		if (expect_errno && errno == expect_errno)
			return;
		fail("%s PUSH_END: %s", tag, strerror(errno));
	}
	if (expect_errno)
		fail("%s push unexpectedly succeeded", tag);
}

static void begin_ok(void)
{
	if (ioctl(g_fd, MPS3_DFX_IOC_SWAP_BEGIN))
		fail("SWAP_BEGIN: %s", strerror(errno));
}

static struct mps3_dfx_result finish(void)
{
	struct mps3_dfx_result r;

	if (ioctl(g_fd, MPS3_DFX_IOC_SWAP_FINISH, &r))
		fail("SWAP_FINISH ioctl: %s", strerror(errno));
	return r;
}

/* ---- scenarios ------------------------------------------------------------ */

static void sc_happy(const char *clr_p, const char *part_p, u32 rm, int sd)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);
	struct mps3_dfx_result r;
	struct mps3_dfx_mock_state m;
	struct mps3_dfx_status s;
	u32 expect_crc;
	u64 bytes_base;

	mock_cfg(rm, 0);
	/* icap_bytes is FREE-RUNNING across swaps (ported diag semantics —
	 * never reset per swap), so check the per-swap DELTA */
	get_status(&s);
	bytes_base = (u64)s.icap_bytes_hi << 32 | s.icap_bytes_lo;
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	push(MPS3_DFX_KIND_PARTIAL, p, pl, rm, sd, 0, 0, "part");
	r = finish();
	if (!r.ok || !r.verified || r.rm_id != rm || r.err != 0)
		fail("result ok=%u ver=%u rm=0x%08x err=%d",
		     r.ok, r.verified, r.rm_id, r.err);

	get_mock(&m);
	if (!m.saw_sync)
		fail("ICAP never saw AA995566 (byte-order!)");
	if (m.words_total != (cl + pl) / 4)
		fail("words_total %u != %u", m.words_total, (cl + pl) / 4);
	expect_crc = mps3_crc32_final(mps3_crc32_update(
			mps3_crc32_update(MPS3_CRC32_INIT, c, cl), p, pl));
	if (m.stream_crc != expect_crc)
		fail("stream CRC 0x%08x != files 0x%08x (byte-lane drift)",
		     m.stream_crc, expect_crc);
	if (m.fifo_overflow)
		fail("FIFO overflow x%u (pacing bug)", m.fifo_overflow);
	if (!m.saw_desync)
		fail("no DESYNC seen");

	get_status(&s);
	if (s.current_rm_id != rm)
		fail("current_rm_id 0x%08x", s.current_rm_id);
	if (s.eos_status != 1)
		fail("eos_status %u (mock asserts EOS post-DESYNC)", s.eos_status);
	if (s.hostio4_calls < 1)
		fail("hostio4 hook never ran");
	if (((u64)s.icap_bytes_hi << 32 | s.icap_bytes_lo) - bytes_base != cl + pl)
		fail("icap_bytes delta %llu != %u",
		     (unsigned long long)(((u64)s.icap_bytes_hi << 32 |
					   s.icap_bytes_lo) - bytes_base),
		     cl + pl);
	/* released: not decoupled */
	if (s.dfxctl_status & DFXCTL_STATUS_DECOUPLED)
		fail("still decoupled after DONE");
	pass();
}

static void sc_wrongid(const char *clr_p, const char *part_p, u32 rm)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);
	struct mps3_dfx_result r;

	mock_cfg(0xDEADBEEFu, 0); /* RP will present the WRONG id */
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	push(MPS3_DFX_KIND_PARTIAL, p, pl, rm, 1, 0, 0, "part");
	r = finish();
	if (r.ok || r.verified)
		fail("verify passed against the wrong id?!");
	if (r.err != MPS3_EVERIFY)
		fail("err %d != EVERIFY", r.err);
	if (r.rm_id == 0xDEADBEEFu)
		fail("failed swap updated current rm_id");
	check_parked("post-mismatch"); /* RE-ISOLATED */
	pass();
}

static void sc_novalid(const char *clr_p, const char *part_p, u32 rm)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);
	struct mps3_dfx_result r;

	mock_cfg(rm, MPS3_MOCK_FAULT_RMID_NEVER);
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	push(MPS3_DFX_KIND_PARTIAL, p, pl, rm, 1, 0, 0, "part");
	r = finish();
	if (r.ok || r.err != MPS3_EVERIFYTMO)
		fail("ok=%u err=%d (want EVERIFYTMO)", r.ok, r.err);
	check_parked("post-novalid");
	pass();
}

static void sc_stream_fault(const char *clr_p, u32 fault, const char *tag)
{
	u32 cl;
	u8 *c = load(clr_p, &cl);

	mock_cfg(0, fault);
	begin_ok();
	/* staged clearing hits the stuck ICAP mid-stream -> EIO, parked */
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, EIO, tag);
	check_parked(tag);
	pass();
}

static void sc_decouple(void)
{
	mock_cfg(0, MPS3_MOCK_FAULT_DECOUPLE_NEVER);
	if (ioctl(g_fd, MPS3_DFX_IOC_SWAP_BEGIN) == 0)
		fail("begin succeeded with a dead decoupler");
	if (errno != ETIMEDOUT)
		fail("begin errno %s (want ETIMEDOUT)", strerror(errno));
	pass();
}

static void sc_release(const char *clr_p, const char *part_p, u32 rm)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);
	struct mps3_dfx_result r;

	mock_cfg(rm, MPS3_MOCK_FAULT_RELEASE_NEVER);
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	push(MPS3_DFX_KIND_PARTIAL, p, pl, rm, 1, 0, 0, "part");
	r = finish();
	if (r.ok || r.err != MPS3_ECONFIRM)
		fail("ok=%u err=%d (want ECONFIRM)", r.ok, r.err);
	check_parked("post-release-stuck"); /* belt-and-suspenders repark */
	pass();
}

static void sc_sdcrc(const char *clr_p, const char *part_p, u32 rm)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);

	mock_cfg(rm, 0);
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	/* stream-direct with a corrupt declared CRC: bytes flow (contained),
	 * PUSH_END must fail and park */
	push(MPS3_DFX_KIND_PARTIAL, p, pl, rm, 1, 0xBADC0DEu,
	     EBADMSG, "sdcrc");
	check_parked("post-sdcrc");
	pass();
}

static void sc_badcrc_staged(const char *clr_p)
{
	u32 cl;
	u8 *c = load(clr_p, &cl);
	struct mps3_dfx_mock_state m;
	struct mps3_dfx_status s;

	mock_cfg(0, 0);
	begin_ok();
	/* staged push with corrupt CRC: rejected BEFORE any ICAP write, and
	 * the SWAP session survives for a retry (config_agent semantics) */
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0xDEADC0DEu, EBADMSG, "badcrc");
	get_mock(&m);
	if (m.words_total != 0)
		fail("CRC-rejected staged push reached ICAP (%u words)!",
		     m.words_total);
	get_status(&s);
	if (s.state != SWAP_STREAM_CLEARING)
		fail("session did not survive a staged CRC reject (state=%u)",
		     s.state);
	/* retry with the right CRC still works */
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "retry");
	get_mock(&m);
	if (m.words_total != cl / 4)
		fail("retry did not stream");
	pass();
}

static void sc_order(const char *clr_p, const char *part_p, u32 rm)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);
	struct mps3_dfx_push q;
	struct mps3_dfx_mock_state m;

	mock_cfg(rm, 0);

	/* push before arm -> refused */
	memset(&q, 0, sizeof(q));
	q.kind = MPS3_DFX_KIND_CLEARING;
	q.len_words = cl / 4;
	q.crc32 = mps3_crc32(c, cl);
	if (ioctl(g_fd, MPS3_DFX_IOC_PUSH_BEGIN, &q) == 0)
		fail("clearing push accepted with no armed swap");
	if (errno != EBUSY)
		fail("pre-arm push errno %s", strerror(errno));

	begin_ok();
	/* partial before clearing -> refused, nothing reaches ICAP */
	memset(&q, 0, sizeof(q));
	q.kind = MPS3_DFX_KIND_PARTIAL;
	q.rm_id = rm;
	q.len_words = pl / 4;
	q.crc32 = mps3_crc32(p, pl);
	q.flags = MPS3_DFX_PUSH_F_STREAM_DIRECT;
	if (ioctl(g_fd, MPS3_DFX_IOC_PUSH_BEGIN, &q) == 0)
		fail("partial accepted before the clearing");
	if (errno != EBUSY)
		fail("early-partial errno %s", strerror(errno));
	get_mock(&m);
	if (m.words_total != 0)
		fail("ordering violation reached ICAP");

	/* finish before the partial -> refused */
	{
		struct mps3_dfx_result r;
		if (ioctl(g_fd, MPS3_DFX_IOC_SWAP_FINISH, &r) == 0)
			fail("finish accepted mid-arm");
	}
	/* abort leaves it parked + idle */
	if (ioctl(g_fd, MPS3_DFX_IOC_ABORT))
		fail("ABORT: %s", strerror(errno));
	check_parked("post-abort");
	pass();
}

static void sc_second(const char *clr_p)
{
	u32 cl;
	u8 *c = load(clr_p, &cl);
	int fd2;

	mock_cfg(0, 0);
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	fd2 = open(DEV, O_RDWR);
	if (fd2 >= 0)
		fail("second open succeeded mid-swap");
	if (errno != EBUSY)
		fail("second open errno %s", strerror(errno));
	if (ioctl(g_fd, MPS3_DFX_IOC_ABORT))
		fail("cleanup abort");
	pass();
}

static void sc_idle(const char *clr_p, const char *part_p, u32 rm, u32 sleep_ms)
{
	u32 cl, pl;
	u8 *c = load(clr_p, &cl), *p = load(part_p, &pl);
	struct mps3_dfx_push q;
	struct mps3_dfx_status s;

	mock_cfg(rm, 0);
	begin_ok();
	push(MPS3_DFX_KIND_CLEARING, c, cl, 0, 0, 0, 0, "clr");
	/* start a stream-direct partial, send HALF, then go silent */
	memset(&q, 0, sizeof(q));
	q.kind = MPS3_DFX_KIND_PARTIAL;
	q.rm_id = rm;
	q.len_words = pl / 4;
	q.crc32 = mps3_crc32(p, pl);
	q.flags = MPS3_DFX_PUSH_F_STREAM_DIRECT;
	if (ioctl(g_fd, MPS3_DFX_IOC_PUSH_BEGIN, &q))
		fail("PUSH_BEGIN: %s", strerror(errno));
	if (write(g_fd, p, (pl / 2) & ~3u) < 0)
		fail("half write: %s", strerror(errno));

	usleep(sleep_ms * 1000);

	get_status(&s);
	if (s.state != SWAP_IDLE)
		fail("idle reap did not fire (state=%u)", s.state);
	if (s.last_err != MPS3_EABORT)
		fail("last_err %d != EABORT", s.last_err);
	check_parked("post-idle-reap");
	/* the session is recoverable: a fresh swap arms fine */
	begin_ok();
	if (ioctl(g_fd, MPS3_DFX_IOC_ABORT))
		fail("post-reap abort");
	pass();
}

int main(int argc, char **argv)
{
	const char *verb;

	if (argc < 2) {
		fprintf(stderr, "usage: swaptool <verb> ...\n");
		return 2;
	}
	verb = argv[1];
	g_name = verb;

	g_fd = open(DEV, O_RDWR);
	if (g_fd < 0)
		fail("open %s: %s", DEV, strerror(errno));

	if (!strcmp(verb, "happy") && argc == 6)
		sc_happy(argv[2], argv[3], strtoul(argv[4], 0, 0),
			 !strcmp(argv[5], "sd"));
	else if (!strcmp(verb, "wrongid") && argc == 5)
		sc_wrongid(argv[2], argv[3], strtoul(argv[4], 0, 0));
	else if (!strcmp(verb, "novalid") && argc == 5)
		sc_novalid(argv[2], argv[3], strtoul(argv[4], 0, 0));
	else if (!strcmp(verb, "crstuck") && argc == 3)
		sc_stream_fault(argv[2], MPS3_MOCK_FAULT_CR_STUCK, "crstuck");
	else if (!strcmp(verb, "wfvstuck") && argc == 3)
		sc_stream_fault(argv[2], MPS3_MOCK_FAULT_WFV_STUCK0, "wfvstuck");
	else if (!strcmp(verb, "decouple") && argc == 2)
		sc_decouple();
	else if (!strcmp(verb, "release") && argc == 5)
		sc_release(argv[2], argv[3], strtoul(argv[4], 0, 0));
	else if (!strcmp(verb, "sdcrc") && argc == 5)
		sc_sdcrc(argv[2], argv[3], strtoul(argv[4], 0, 0));
	else if (!strcmp(verb, "badcrc") && argc == 3)
		sc_badcrc_staged(argv[2]);
	else if (!strcmp(verb, "order") && argc == 5)
		sc_order(argv[2], argv[3], strtoul(argv[4], 0, 0));
	else if (!strcmp(verb, "second") && argc == 3)
		sc_second(argv[2]);
	else if (!strcmp(verb, "idle") && argc == 6)
		sc_idle(argv[2], argv[3], strtoul(argv[4], 0, 0),
			strtoul(argv[5], 0, 0));
	else
		fail("bad usage");
	return 0;
}
