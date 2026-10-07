// SPDX-License-Identifier: GPL-2.0
/*
 * mps3_dfx_drv.c — MPS3 shell DFX swap driver (ICAP-SPIKE).
 *
 * /dev/mps3dfx chardev carrying the full swap engine (see mps3_dfx_uapi.h
 * for the session protocol), plus an fpga-manager / fpga-bridge veneer that
 * compiles in when the kernel has CONFIG_FPGA / CONFIG_FPGA_BRIDGE (the
 * QEMU-proven 6.18.7 config does NOT — the veneer is compile-checked via
 * the Makefile's `check-fpga` target and documented untested; the chardev
 * path is the product path either way, because the fpga-region model has
 * no slot for clearing-before-partial or RM_ID-verify-with-re-isolate).
 *
 * Binding: the spike instantiates a single device at module init —
 *   mock=1  : in-kernel behavioural mock (see mps3_icap_mock.h for the
 *             mechanism justification) — the QEMU test target.
 *   mock=0  : ioremap of the frozen shell map (0x44A0/44A1/44A2 —
 *             overridable via hwicap_phys/dfxctl_phys/clkrst_phys params).
 * Production shape (documented, not wired here): platform driver matching
 * "soclabs,mps3-hwicap-fpga-mgr" with dfxctl/clkrst phandles; remove
 * generic-uio from those DTS nodes when this lands (DRIVER_MATRIX §2.8).
 *
 * Concurrency: single-open chardev; ONE mutex serialises engine access.
 * The 30 s RX-idle reap is a delayed work re-armed on every byte of write()
 * progress (bounds SILENCE, not transfer duration — ported invariant).
 */
#include <linux/module.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/slab.h>
#include <linux/mutex.h>
#include <linux/io.h>
#include <linux/workqueue.h>
#include <linux/jiffies.h>
#include <linux/sched.h>
#include <linux/device.h>

#include "mps3_icap_engine.h"
#include "mps3_icap_mock.h"
#include "mps3_crc32.h"
#include "mps3_dfx_uapi.h"

static bool mock = true;
module_param(mock, bool, 0444);
MODULE_PARM_DESC(mock, "bind the in-kernel behavioural mock instead of MMIO");

static int fifo_mode = 1; /* this BD: C_MODE 0 = FIFO, depth 1024 */
module_param(fifo_mode, int, 0444);
static uint done_poll_max = 1000000;
module_param(done_poll_max, uint, 0444);
static uint confirm_poll_max = 100000;
module_param(confirm_poll_max, uint, 0444);
static uint chunk_words = 1024;
module_param(chunk_words, uint, 0444);
static int eos_strict; /* default 0: capture-only (SERVICE_DISPOSITION §4) */
module_param(eos_strict, int, 0444);
/* Best-effort post-DESYNC EOS wait bound — SHORT, deliberately NOT done_poll_max.
 * Whether HWICAP_SR.EOS asserts post-DESYNC on this axi_hwicap build is an open
 * question (see firmware swap_fsm.c icap_direct_finish "best-effort EOS wait");
 * bounding it at done_poll_max spun ~150s on the first real swap before proceeding.
 * Keep it short so a non-asserting EOS fails FAST and diagnostic (pr_warn), matching
 * the firmware's best-effort intent. Override via module param if EOS proves slow. */
static uint eos_poll_max = 50000;
module_param(eos_poll_max, uint, 0444);
static uint idle_ms = 30000; /* the silicon-observed 2026-07-09 hang bound */
module_param(idle_ms, uint, 0444);

static ulong hwicap_phys = MPS3_HWICAP_PHYS;
module_param(hwicap_phys, ulong, 0444);
static ulong dfxctl_phys = MPS3_DFXCTL_PHYS;
module_param(dfxctl_phys, ulong, 0444);
static ulong clkrst_phys = MPS3_CLKRST_PHYS;
module_param(clkrst_phys, ulong, 0444);

/* Largest known partial is 2.9 MB (rm_nanosoc_multicore); cap staging at
 * 8 MiB so a corrupt len_words cannot vmalloc the world. */
#define MPS3_DFX_STAGE_MAX (8u << 20)

enum push_mode { PUSH_NONE = 0, PUSH_STAGED, PUSH_SD };

struct mps3_dfx {
	struct mutex lock;
	struct mps3_icap_engine eng;
	struct mps3_mock *mockdev;               /* NULL on real hardware */
	void __iomem *bases[MPS3_BLK_COUNT];

	unsigned long open_flag;

	enum push_mode push;
	struct mps3_dfx_push meta;
	u8  *stage;
	u32 stage_off;
	u8  *bounce;                              /* PAGE_SIZE, sd copies  */

	unsigned long last_progress;              /* jiffies               */
	struct delayed_work idle_work;

	u32 target_rm_id_attr;                    /* fpga-mgr veneer only  */
	struct miscdevice misc;
};

static struct mps3_dfx *g_dfx;

/* ---- engine ops ---------------------------------------------------------- */

static u32 drv_rd(void *ctx, int blk, u32 off)
{
	struct mps3_dfx *d = ctx;

	if (d->mockdev)
		return mps3_mock_rd(d->mockdev, blk, off);
	return readl(d->bases[blk] + off);
}

static void drv_wr(void *ctx, int blk, u32 off, u32 val)
{
	struct mps3_dfx *d = ctx;

	if (d->mockdev)
		mps3_mock_wr(d->mockdev, blk, off, val);
	else
		writel(val, d->bases[blk] + off);
}

static void drv_relax(void *ctx)
{
	(void)ctx;
	cond_resched();
}

/* hostio4-reset hook carrier (TRANSPLANT_CONTRACT §9.4): no hostio4_target
 * exists in the current static shell, so this is a stub that only proves
 * the CALL SITE (between partial-stream-done and release, RP still parked).
 * When the hostio wave lands a target, the reset poke goes here. */
static int drv_hostio4_reset(void *ctx)
{
	(void)ctx;
	return 0;
}

static const struct mps3_icap_ops drv_ops = {
	.rd = drv_rd,
	.wr = drv_wr,
	.relax = drv_relax,
	.hostio4_reset = drv_hostio4_reset,
};

/* ---- helpers -------------------------------------------------------------- */

static int map_err(int rc)
{
	switch (rc) {
	case MPS3_EOK:        return 0;
	case MPS3_ESTATE:     return -EBUSY;
	case MPS3_EORDER:     return -EINVAL;
	case MPS3_ECONFIRM:   return -ETIMEDOUT;
	case MPS3_ESTREAM:    return -EIO;
	case MPS3_ECRC:       return -EBADMSG;
	case MPS3_EVERIFY:    return -ENXIO;
	case MPS3_EVERIFYTMO: return -ETIMEDOUT;
	case MPS3_EALIGN:     return -EINVAL;
	case MPS3_EEOS:       return -EIO;
	case MPS3_EABORT:     return -ECANCELED;
	default:              return -EIO;
	}
}

static void push_cleanup(struct mps3_dfx *d)
{
	vfree(d->stage);
	d->stage = NULL;
	d->stage_off = 0;
	d->push = PUSH_NONE;
}

static void touch_progress(struct mps3_dfx *d)
{
	d->last_progress = jiffies;
	mod_delayed_work(system_wq, &d->idle_work, msecs_to_jiffies(idle_ms));
}

static void idle_reap_fn(struct work_struct *w)
{
	struct mps3_dfx *d = container_of(to_delayed_work(w),
					  struct mps3_dfx, idle_work);

	mutex_lock(&d->lock);
	if (mps3_engine_active(&d->eng)) {
		unsigned long dl = d->last_progress + msecs_to_jiffies(idle_ms);

		if (time_after_eq(jiffies, dl)) {
			dev_warn(d->misc.this_device,
				 "RX-idle %ums expired mid-swap: reaping to parked-decoupled\n",
				 idle_ms);
			push_cleanup(d);
			mps3_engine_abort_park(&d->eng);
		} else {
			schedule_delayed_work(&d->idle_work, dl - jiffies);
		}
	}
	mutex_unlock(&d->lock);
}

/* ---- fops ------------------------------------------------------------------ */

static int dfx_open(struct inode *ino, struct file *f)
{
	struct mps3_dfx *d = g_dfx;

	if (test_and_set_bit(0, &d->open_flag))
		return -EBUSY; /* single owner: the swap daemon */
	f->private_data = d;
	return 0;
}

static int dfx_release(struct inode *ino, struct file *f)
{
	struct mps3_dfx *d = f->private_data;

	mutex_lock(&d->lock);
	if (mps3_engine_active(&d->eng)) {
		/* daemon died mid-swap: same reap as the idle timeout */
		push_cleanup(d);
		mps3_engine_abort_park(&d->eng);
	}
	mutex_unlock(&d->lock);
	cancel_delayed_work_sync(&d->idle_work);
	clear_bit(0, &d->open_flag);
	return 0;
}

static ssize_t dfx_write(struct file *f, const char __user *ubuf,
			 size_t count, loff_t *ppos)
{
	struct mps3_dfx *d = f->private_data;
	size_t done = 0;
	int err = 0;

	if (count == 0)
		return 0;

	mutex_lock(&d->lock);
	if (d->push == PUSH_NONE) {
		mutex_unlock(&d->lock);
		return -EINVAL;
	}
	touch_progress(d);

	if (d->push == PUSH_STAGED) {
		u32 cap = d->meta.len_words * 4u;

		if (d->stage_off + count > cap) {
			/* bytes beyond the declared frame: reject the push
			 * (mirrors config_agent's early-close rejection) */
			push_cleanup(d);
			mutex_unlock(&d->lock);
			return -EINVAL;
		}
		if (copy_from_user(d->stage + d->stage_off, ubuf, count)) {
			mutex_unlock(&d->lock);
			return -EFAULT;
		}
		d->stage_off += count;
		done = count;
	} else { /* PUSH_SD */
		while (done < count) {
			size_t n = min_t(size_t, count - done, PAGE_SIZE);
			int rc;

			if (copy_from_user(d->bounce, ubuf + done, n)) {
				err = -EFAULT;
				break;
			}
			rc = mps3_engine_sd_write(&d->eng, d->bounce, n);
			if (rc != MPS3_EOK) {
				/* engine has already parked + failed the
				 * session (containment) */
				push_cleanup(d);
				err = map_err(rc);
				break;
			}
			done += n;
		}
	}
	if (done)
		touch_progress(d);
	mutex_unlock(&d->lock);
	return err ? err : (ssize_t)done;
}

static long dfx_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
	struct mps3_dfx *d = f->private_data;
	void __user *uarg = (void __user *)arg;
	long ret = 0;

	mutex_lock(&d->lock);
	switch (cmd) {
	case MPS3_DFX_IOC_SWAP_BEGIN: {
		int rc;

		if (mps3_engine_active(&d->eng)) {
			ret = -EBUSY;
			break;
		}
		rc = mps3_engine_swap_begin(&d->eng);
		ret = map_err(rc);
		if (!ret)
			touch_progress(d);
		break;
	}

	case MPS3_DFX_IOC_PUSH_BEGIN: {
		struct mps3_dfx_push m;

		if (d->push != PUSH_NONE) {
			ret = -EBUSY;
			break;
		}
		if (copy_from_user(&m, uarg, sizeof(m))) {
			ret = -EFAULT;
			break;
		}
		if (m.len_words == 0 ||
		    m.len_words > MPS3_DFX_STAGE_MAX / 4u) {
			ret = -EINVAL;
			break;
		}
		if (m.kind == MPS3_DFX_KIND_CLEARING) {
			if (m.flags & MPS3_DFX_PUSH_F_STREAM_DIRECT) {
				ret = -EINVAL; /* clearing is always staged:
						* CRC strictly before ICAP */
				break;
			}
			if (d->eng.st != SWAP_STREAM_CLEARING) {
				ret = -EBUSY;
				break;
			}
		} else if (m.kind == MPS3_DFX_KIND_PARTIAL) {
			if (d->eng.st != SWAP_AWAIT_PARTIAL) {
				ret = -EBUSY;
				break;
			}
		} else {
			ret = -EINVAL;
			break;
		}

		if (m.flags & MPS3_DFX_PUSH_F_STREAM_DIRECT) {
			int rc = mps3_engine_sd_begin(&d->eng,
						      m.len_words * 4u,
						      m.crc32, m.rm_id);
			ret = map_err(rc);
			if (ret)
				break;
			d->push = PUSH_SD;
		} else {
			d->stage = vmalloc(m.len_words * 4u);
			if (!d->stage) {
				ret = -ENOMEM;
				break;
			}
			d->stage_off = 0;
			d->push = PUSH_STAGED;
		}
		d->meta = m;
		touch_progress(d);
		break;
	}

	case MPS3_DFX_IOC_PUSH_END: {
		int rc;

		if (d->push == PUSH_NONE) {
			ret = -EINVAL;
			break;
		}
		if (d->push == PUSH_SD) {
			rc = mps3_engine_sd_finish(&d->eng);
			d->push = PUSH_NONE;
			ret = map_err(rc);
			break;
		}
		/* staged: full frame present? */
		if (d->stage_off != d->meta.len_words * 4u) {
			push_cleanup(d);
			ret = -EINVAL;
			break;
		}
		/* CRC gate BEFORE any ICAP write (frozen validation order).
		 * A staged CRC failure rejects the PUSH but not the SWAP —
		 * the session stays armed, the host re-pushes (mirrors
		 * config_agent rejection semantics). */
		if (mps3_crc32(d->stage, d->stage_off) != d->meta.crc32) {
			push_cleanup(d);
			ret = -EBADMSG;
			break;
		}
		if (d->meta.kind == MPS3_DFX_KIND_CLEARING)
			rc = mps3_engine_stream_clearing(&d->eng, d->stage,
							 d->stage_off);
		else
			rc = mps3_engine_stream_partial(&d->eng, d->stage,
							d->stage_off,
							d->meta.rm_id);
		push_cleanup(d);
		ret = map_err(rc);
		if (!ret)
			touch_progress(d);
		break;
	}

	case MPS3_DFX_IOC_SWAP_FINISH: {
		struct mps3_dfx_result r;

		if (d->eng.st != SWAP_RELEASE) {
			ret = -EBUSY;
			break;
		}
		(void)mps3_engine_finish(&d->eng);
		r.ok = d->eng.last.ok;
		r.verified = d->eng.last.verified;
		r.rm_id = d->eng.last.rm_id;
		r.err = d->eng.last.err;
		cancel_delayed_work(&d->idle_work);
		if (copy_to_user(uarg, &r, sizeof(r)))
			ret = -EFAULT;
		break;
	}

	case MPS3_DFX_IOC_ABORT:
		push_cleanup(d);
		mps3_engine_abort_park(&d->eng);
		cancel_delayed_work(&d->idle_work);
		break;

	case MPS3_DFX_IOC_GET_STATUS: {
		struct mps3_dfx_status s;

		memset(&s, 0, sizeof(s));
		s.state = d->eng.st;
		s.active = mps3_engine_active(&d->eng);
		s.current_rm_id = d->eng.current_rm_id;
		s.icap_bytes_lo = (u32)d->eng.icap_bytes;
		s.icap_bytes_hi = (u32)(d->eng.icap_bytes >> 32);
		s.sr_last = d->eng.sr_last;
		s.eos_status = d->eng.eos_status;
		s.hostio4_calls = d->eng.hostio4_calls;
		s.last_valid = d->eng.last.valid;
		s.last_ok = d->eng.last.ok;
		s.last_verified = d->eng.last.verified;
		s.last_rm_id = d->eng.last.rm_id;
		s.last_err = d->eng.last.err;
		s.dfxctl_status = drv_rd(d, MPS3_BLK_DFXCTL, DFXCTL_STATUS);
		if (copy_to_user(uarg, &s, sizeof(s)))
			ret = -EFAULT;
		break;
	}

	case MPS3_DFX_IOC_MOCK_SET: {
		struct mps3_dfx_mock_cfg mc;
		struct mps3_mock_cfg c;

		if (!d->mockdev) {
			ret = -ENOTTY;
			break;
		}
		if (mps3_engine_active(&d->eng)) {
			ret = -EBUSY;
			break;
		}
		if (copy_from_user(&mc, uarg, sizeof(mc))) {
			ret = -EFAULT;
			break;
		}
		c.next_rm_id = mc.next_rm_id;
		c.faults = mc.faults;
		c.status_lat = mc.status_lat;
		c.valid_lat = mc.valid_lat;
		c.cr_lat = mc.cr_lat;
		c.fifo_depth = mc.fifo_depth;
		mps3_mock_init(d->mockdev, &c);
		break;
	}

	case MPS3_DFX_IOC_MOCK_GET: {
		struct mps3_dfx_mock_state ms;
		int i;

		if (!d->mockdev) {
			ret = -ENOTTY;
			break;
		}
		memset(&ms, 0, sizeof(ms));
		ms.words_total = d->mockdev->words_total;
		ms.stream_crc = mps3_mock_stream_crc(d->mockdev);
		ms.saw_sync = d->mockdev->saw_sync;
		ms.saw_desync = d->mockdev->saw_desync;
		ms.sync_count = d->mockdev->sync_count;
		ms.fifo_overflow = d->mockdev->fifo_overflow;
		for (i = 0; i < 8; i++)
			ms.first_words[i] = d->mockdev->first_words[i];
		if (copy_to_user(uarg, &ms, sizeof(ms)))
			ret = -EFAULT;
		break;
	}

	case MPS3_DFX_IOC_MOCK_ARM_CAP:
		if (!d->mockdev) {
			ret = -ENOTTY;
			break;
		}
		mps3_mock_arm_capture(d->mockdev);
		break;

	default:
		ret = -ENOTTY;
		break;
	}
	mutex_unlock(&d->lock);
	return ret;
}

static const struct file_operations dfx_fops = {
	.owner = THIS_MODULE,
	.open = dfx_open,
	.release = dfx_release,
	.write = dfx_write,
	.unlocked_ioctl = dfx_ioctl,
	/* .llseek NULL: seeking is meaningless on the push stream (the old
	 * no_llseek helper is gone since v6.12) */
};

/* ---- sysfs: swap-progress observability (SERVICE_DISPOSITION §4: progress
 * must be observable MID-swap — the :6900 swap response is held for seconds
 * and the chardev is single-open, so these are lock-free racy reads by
 * design; consumers treat them as monotonic hints, not synchronised state). */

static ssize_t state_show(struct device *dev, struct device_attribute *a, char *buf)
{
	return sysfs_emit(buf, "%u\n", (unsigned)READ_ONCE(g_dfx->eng.st));
}
static DEVICE_ATTR_RO(state);

static ssize_t icap_bytes_show(struct device *dev, struct device_attribute *a, char *buf)
{
	return sysfs_emit(buf, "%llu\n",
			  (unsigned long long)READ_ONCE(g_dfx->eng.icap_bytes));
}
static DEVICE_ATTR_RO(icap_bytes);

static ssize_t rm_id_show(struct device *dev, struct device_attribute *a, char *buf)
{
	/* the LAST VERIFIED id, never a live DFXCTL read (mid-swap the
	 * register is transient) — ported `ping` semantics */
	return sysfs_emit(buf, "0x%08x\n", READ_ONCE(g_dfx->eng.current_rm_id));
}
static DEVICE_ATTR_RO(rm_id);

static ssize_t eos_status_show(struct device *dev, struct device_attribute *a, char *buf)
{
	return sysfs_emit(buf, "%u 0x%08x\n", READ_ONCE(g_dfx->eng.eos_status),
			  READ_ONCE(g_dfx->eng.sr_last));
}
static DEVICE_ATTR_RO(eos_status);

/* fpga-mgr veneer verify target (see the veneer notes below). */
static ssize_t target_rm_id_show(struct device *dev, struct device_attribute *a, char *buf)
{
	return sysfs_emit(buf, "0x%08x\n", READ_ONCE(g_dfx->target_rm_id_attr));
}
static ssize_t target_rm_id_store(struct device *dev, struct device_attribute *a,
				  const char *buf, size_t n)
{
	u32 v;

	if (kstrtou32(buf, 0, &v))
		return -EINVAL;
	WRITE_ONCE(g_dfx->target_rm_id_attr, v);
	return n;
}
static DEVICE_ATTR_RW(target_rm_id);

static struct attribute *dfx_attrs[] = {
	&dev_attr_state.attr,
	&dev_attr_icap_bytes.attr,
	&dev_attr_rm_id.attr,
	&dev_attr_eos_status.attr,
	&dev_attr_target_rm_id.attr,
	NULL,
};
ATTRIBUTE_GROUPS(dfx);

/* =========================================================================
 * fpga-manager / fpga-bridge veneer
 * =========================================================================
 * Compiled when the kernel has the FPGA subsystem (this baseline's QEMU
 * kernel does NOT: CONFIG_FPGA unset — compile-checked only, see
 * COVERAGE.md). The veneer maps the standard flow onto the SAME engine:
 *   fpga-bridge disable  -> nothing (the swap must already be armed: the
 *                           region flow's bridge-disable happens after
 *                           SWAP_BEGIN parked the RP; disable just verifies)
 *   mgr write_init       -> engine sd_begin UNFRAMED (no wire header on an
 *                           fpga-mgr image), requires armed + clearing
 *                           streamed — the ordering invariants CANNOT be
 *                           expressed in the fpga-region model, so the
 *                           daemon must still drive SWAP_BEGIN + the
 *                           clearing push through the chardev first.
 *   mgr write            -> engine sd_write
 *   mgr write_complete   -> engine sd_finish (EOS capture)
 *   fpga-bridge enable   -> engine finish() using the sysfs target_rm_id
 *                           (hostio4 hook + release + verify + commit /
 *                           re-isolate — the full step 6-9 tail).
 */
#if IS_ENABLED(CONFIG_FPGA) || defined(MPS3_FPGA_FORCE)
#include <linux/fpga/fpga-mgr.h>

static enum fpga_mgr_states dfx_mgr_state(struct fpga_manager *mgr)
{
	struct mps3_dfx *d = mgr->priv;

	return mps3_engine_active(&d->eng) ? FPGA_MGR_STATE_WRITE
					   : FPGA_MGR_STATE_UNKNOWN;
}

static int dfx_mgr_write_init(struct fpga_manager *mgr,
			      struct fpga_image_info *info,
			      const char *buf, size_t count)
{
	struct mps3_dfx *d = mgr->priv;
	int rc;

	if (!(info->flags & FPGA_MGR_PARTIAL_RECONFIG))
		return -EOPNOTSUPP; /* a live shell cannot self-load a full
				     * static via its own ICAP */
	mutex_lock(&d->lock);
	rc = mps3_engine_sd_begin(&d->eng, 0, 0,
				  READ_ONCE(d->target_rm_id_attr));
	mutex_unlock(&d->lock);
	return map_err(rc);
}

static int dfx_mgr_write(struct fpga_manager *mgr, const char *buf,
			 size_t count)
{
	struct mps3_dfx *d = mgr->priv;
	int rc;

	mutex_lock(&d->lock);
	rc = mps3_engine_sd_write(&d->eng, (const u8 *)buf, count);
	touch_progress(d);
	mutex_unlock(&d->lock);
	return map_err(rc);
}

static int dfx_mgr_write_complete(struct fpga_manager *mgr,
				  struct fpga_image_info *info)
{
	struct mps3_dfx *d = mgr->priv;
	int rc;

	mutex_lock(&d->lock);
	rc = mps3_engine_sd_finish(&d->eng);
	mutex_unlock(&d->lock);
	return map_err(rc);
}

static const struct fpga_manager_ops dfx_mgr_ops = {
	.state = dfx_mgr_state,
	.write_init = dfx_mgr_write_init,
	.write = dfx_mgr_write,
	.write_complete = dfx_mgr_write_complete,
};

static struct fpga_manager *dfx_mgr;

static int dfx_fpga_register(struct mps3_dfx *d)
{
	struct fpga_manager_info info = {
		.name = "MPS3 shell AXI HWICAP (soclabs,mps3-hwicap-fpga-mgr)",
		.mops = &dfx_mgr_ops,
		.priv = d,
	};

	dfx_mgr = fpga_mgr_register_full(d->misc.this_device, &info);
	return PTR_ERR_OR_ZERO(dfx_mgr);
}

static void dfx_fpga_unregister(void)
{
	if (dfx_mgr)
		fpga_mgr_unregister(dfx_mgr);
}
#else /* !CONFIG_FPGA */
static int dfx_fpga_register(struct mps3_dfx *d) { return 0; }
static void dfx_fpga_unregister(void) {}
#endif

#if IS_ENABLED(CONFIG_FPGA_BRIDGE) || defined(MPS3_FPGA_FORCE)
#include <linux/fpga/fpga-bridge.h>

static int dfx_br_enable_set(struct fpga_bridge *bridge, bool enable)
{
	struct mps3_dfx *d = bridge->priv;
	int rc = 0;

	mutex_lock(&d->lock);
	if (enable) {
		/* Bridge-enable = the full step 6-9 tail (never a bare
		 * decouple flip: releasing without verify would ship the
		 * exact class of bug the R1 reorder exists to prevent). */
		d->eng.target_rm_id = READ_ONCE(d->target_rm_id_attr);
		if (d->eng.st == SWAP_RELEASE)
			rc = mps3_engine_finish(&d->eng);
		else
			rc = MPS3_ESTATE;
	} else {
		/* Bridge-disable: the swap must already be armed (parked). */
		if (!mps3_engine_active(&d->eng))
			rc = MPS3_ESTATE;
	}
	mutex_unlock(&d->lock);
	return map_err(rc);
}

static int dfx_br_enable_show(struct fpga_bridge *bridge)
{
	struct mps3_dfx *d = bridge->priv;
	u32 st = drv_rd(d, MPS3_BLK_DFXCTL, DFXCTL_STATUS);

	return !(st & DFXCTL_STATUS_DECOUPLED);
}

static const struct fpga_bridge_ops dfx_br_ops = {
	.enable_set = dfx_br_enable_set,
	.enable_show = dfx_br_enable_show,
};

static struct fpga_bridge *dfx_br;

static int dfx_bridge_register(struct mps3_dfx *d)
{
	dfx_br = fpga_bridge_register(d->misc.this_device,
				      "MPS3 shell dfx_ctl (soclabs,dfx-ctl-1.0)",
				      &dfx_br_ops, d);
	return PTR_ERR_OR_ZERO(dfx_br);
}

static void dfx_bridge_unregister(void)
{
	if (dfx_br)
		fpga_bridge_unregister(dfx_br);
}
#else /* !CONFIG_FPGA_BRIDGE */
static int dfx_bridge_register(struct mps3_dfx *d) { return 0; }
static void dfx_bridge_unregister(void) {}
#endif

/* ---- module init/exit ------------------------------------------------------ */

static int __init dfx_init(void)
{
	struct mps3_dfx *d;
	struct mps3_icap_cfg cfg;
	int rc, i;

	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	mutex_init(&d->lock);
	INIT_DELAYED_WORK(&d->idle_work, idle_reap_fn);

	d->bounce = kmalloc(PAGE_SIZE, GFP_KERNEL);
	if (!d->bounce) {
		rc = -ENOMEM;
		goto err_free;
	}

	if (mock) {
		struct mps3_mock_cfg mc = {
			.next_rm_id = 0,
			.faults = 0,
			.status_lat = 2,
			.valid_lat = 3,
			.cr_lat = 1,
			.fifo_depth = fifo_mode ? 1024 : 1,
		};
		d->mockdev = kzalloc(sizeof(*d->mockdev), GFP_KERNEL);
		if (!d->mockdev) {
			rc = -ENOMEM;
			goto err_bounce;
		}
		mps3_mock_init(d->mockdev, &mc);
	} else {
		const ulong phys[MPS3_BLK_COUNT] = {
			[MPS3_BLK_HWICAP] = hwicap_phys,
			[MPS3_BLK_DFXCTL] = dfxctl_phys,
			[MPS3_BLK_CLKRST] = clkrst_phys,
		};
		for (i = 0; i < MPS3_BLK_COUNT; i++) {
			d->bases[i] = ioremap(phys[i], MPS3_BLOCK_SPAN);
			if (!d->bases[i]) {
				rc = -ENOMEM;
				goto err_unmap;
			}
		}
	}

	cfg.fifo_mode = fifo_mode;
	cfg.done_poll_max = done_poll_max;
	cfg.confirm_poll_max = confirm_poll_max;
	cfg.chunk_words = chunk_words;
	cfg.eos_strict = eos_strict;
	cfg.eos_poll_max = eos_poll_max;
	mps3_engine_init(&d->eng, &drv_ops, d, &cfg);

	d->misc.minor = MISC_DYNAMIC_MINOR;
	d->misc.name = "mps3dfx";
	d->misc.fops = &dfx_fops;
	d->misc.groups = dfx_groups;
	rc = misc_register(&d->misc);
	if (rc)
		goto err_unmap;

	g_dfx = d;

	rc = dfx_fpga_register(d);
	if (rc)
		pr_warn("mps3dfx: fpga-mgr registration failed (%d), chardev-only\n", rc);
	rc = dfx_bridge_register(d);
	if (rc)
		pr_warn("mps3dfx: fpga-bridge registration failed (%d)\n", rc);

	pr_info("mps3dfx: ready (%s backend, %s mode, idle_ms=%u)\n",
		mock ? "MOCK" : "MMIO", fifo_mode ? "FIFO" : "LITE", idle_ms);
	return 0;

err_unmap:
	for (i = 0; i < MPS3_BLK_COUNT; i++)
		if (d->bases[i])
			iounmap(d->bases[i]);
	kfree(d->mockdev);
err_bounce:
	kfree(d->bounce);
err_free:
	kfree(d);
	return rc;
}

static void __exit dfx_exit(void)
{
	struct mps3_dfx *d = g_dfx;
	int i;

	dfx_bridge_unregister();
	dfx_fpga_unregister();
	misc_deregister(&d->misc);
	cancel_delayed_work_sync(&d->idle_work);
	if (mps3_engine_active(&d->eng))
		mps3_engine_abort_park(&d->eng);
	vfree(d->stage);
	for (i = 0; i < MPS3_BLK_COUNT; i++)
		if (d->bases[i])
			iounmap(d->bases[i]);
	kfree(d->mockdev);
	kfree(d->bounce);
	kfree(d);
	g_dfx = NULL;
}

module_init(dfx_init);
module_exit(dfx_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("SoCLabs MPS3 harness (ICAP-SPIKE)");
MODULE_DESCRIPTION("MPS3 shell DFX swap driver: AXI HWICAP + dfx_ctl sequencing");
