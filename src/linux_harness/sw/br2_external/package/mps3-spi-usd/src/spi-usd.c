// SPDX-License-Identifier: GPL-2.0
/*
 * spi-usd.c — SPI controller driver for the SoCLabs MPS3 shell's "usd_spi"
 * block: the USER microSD slot in SPI mode (D13, HANDOVER_USD_OVERLAY_STORE.md
 * §4.1; RTL fpga/shell/ip/usd_spi/usd_spi.sv). One 64 KiB AXI4-Lite page at
 * 0x44A4_0000, no interrupt line.
 *
 * THE USER CARD, NOT THE MCC CARD. The MCC's config card (V2M_MPS3, written
 * by sd_install) is on another controller that nothing here can reach.
 *
 * What it is to Linux:
 *   - an spi_controller (one chip select) that the stock mmc_spi slot driver
 *     sits on ("mmc-spi-slot" child node) -> /dev/mmcblk0;
 *   - a one-line gpio_chip whose line 0 is STATUS.CD_PRESENT (debounced in
 *     hardware, polarity applied). The slot uses it as its card detect
 *     (mmc-spi-slot "gpios" index 0), so with no card the MMC core polls one
 *     register a second and never clocks CMD0 into an empty socket: no card =>
 *     no SPI traffic, no delay, no error (D13 hard rule 1);
 *   - sysfs "card_present" / "cd_raw" / "errors" for scripts
 *     (S12mps3persist decides whether to wait for mmcblk0 off card_present).
 *
 * Register contract (usd_spi.sv header; checked against gen_regmap's derived
 * offsets by tools/dts_gates.py G7 — keep the USD_REG_* spellings):
 *   0x00 ID      RO  0x55534431 "USD1" — probe refuses anything else
 *   0x04 CTRL    RW  [0] EN [1] CS (1 = assert, DAT3 low) [2] WIDE [3] CD_POL
 *                    [4] CD_IGNORE. Pads drive only when EN && (CD_PRESENT ||
 *                    CD_IGNORE) — a hardware gate, not ours.
 *   0x08 CLKDIV  RW  SCK = aclk / (2 * (DIV + 1)); reset 124 = 400 kHz
 *   0x0C DATA    W: start a mode-0 MSB-first shift of [7:0] (or [31:0] with
 *                    WIDE; bit 31 first, first received byte lands in
 *                    [31:24]); R: last received word
 *   0x10 STATUS  [0] BUSY [1] CD_PRESENT [2] CD_RAW, W1C: [3] CD_CHANGED
 *                    [4] OVR (DATA written while BUSY) [5] ABORT (pads gated
 *                    mid-shift: card pulled)
 *
 * CD_POL / CD_IGNORE are PRESERVED on every CTRL write (read-modify-write):
 * D13 board step B0 may set them over JTAG and nothing here undoes that.
 *
 * Polled by design: the block has no IRQ. One shift is at most 80 us (32 bits
 * at 400 kHz); a 512-byte block at 12.5 MHz is ~128 WIDE shifts. The busy
 * wait is readl_poll_timeout_atomic(), which never reads rdtime (patch 0007:
 * the MBV's time CSR freezes in a busy spin).
 */

#include <linux/bits.h>
#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/gpio/driver.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/kernel.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/sched.h>
#include <linux/spi/spi.h>
#include <linux/unaligned.h>

#define DRV_NAME		"spi-usd"

#define USD_REG_ID		0x00
#define USD_REG_CTRL		0x04
#define USD_REG_CLKDIV		0x08
#define USD_REG_DATA		0x0C
#define USD_REG_STATUS		0x10

#define USD_ID_VALUE		0x55534431	/* "USD1" */

#define USD_CTRL_EN		BIT(0)
#define USD_CTRL_CS		BIT(1)
#define USD_CTRL_WIDE		BIT(2)
#define USD_CTRL_CD_POL		BIT(3)
#define USD_CTRL_CD_IGNORE	BIT(4)

#define USD_ST_BUSY		BIT(0)
#define USD_ST_CD_PRESENT	BIT(1)
#define USD_ST_CD_RAW		BIT(2)
#define USD_ST_CD_CHANGED	BIT(3)
#define USD_ST_OVR		BIT(4)
#define USD_ST_ABORT		BIT(5)

#define USD_CLKDIV_MAX		0xFFFFu
/* DIV 0 = aclk/2 = 50 MHz: legal in the RTL, outside SD SPI-mode timing. */
#define USD_CLKDIV_MIN		1u
#define USD_DEFAULT_ACLK_HZ	100000000ul

struct usd_spi {
	void __iomem *base;
	struct device *dev;
	struct gpio_chip gc;
	unsigned long aclk_hz;
	u32 div;		/* last CLKDIV written */
	bool wide;		/* last CTRL.WIDE written */
	unsigned long n_abort, n_ovr, n_timeout;
};

static inline u32 usd_rd(struct usd_spi *u, u32 off)
{
	return readl(u->base + off);
}

static inline void usd_wr(struct usd_spi *u, u32 off, u32 v)
{
	writel(v, u->base + off);
}

/* CTRL read-modify-write: CD_POL/CD_IGNORE survive whatever we do. */
static void usd_ctrl_update(struct usd_spi *u, u32 clr, u32 set)
{
	u32 v = usd_rd(u, USD_REG_CTRL);

	usd_wr(u, USD_REG_CTRL, (v & ~clr) | set);
}

static u32 usd_div_for(struct usd_spi *u, u32 hz)
{
	u32 div;

	if (!hz)
		hz = 400000;
	div = DIV_ROUND_UP(u->aclk_hz, 2ul * hz) - 1;
	return clamp_t(u32, div, USD_CLKDIV_MIN, USD_CLKDIV_MAX);
}

/* Upper bound for one shift of @bits at the current divider, plus slack. */
static unsigned long usd_shift_timeout_us(struct usd_spi *u, unsigned int bits)
{
	/* div_u64: a plain 64-bit '/' is __udivdi3 on rv32, which modules lack */
	u64 ns = div_u64((u64)bits * 2 * (u->div + 1) * NSEC_PER_SEC, u->aclk_hz);

	return (unsigned long)div_u64(ns, NSEC_PER_USEC) * 4 + 100;
}

/*
 * ONE shift. The fence between the DATA store and the first STATUS load keeps
 * the device-side order write-then-read on a hart whose uncached stores may be
 * posted; D13's bare-metal driver relies on the same in-order property.
 */
static int usd_shift(struct usd_spi *u, u32 out, u32 *in, bool wide)
{
	u32 st;
	int ret;

	if (wide != u->wide) {
		usd_ctrl_update(u, USD_CTRL_WIDE, wide ? USD_CTRL_WIDE : 0);
		u->wide = wide;
	}
	usd_wr(u, USD_REG_DATA, out);
	mb();
	ret = readl_poll_timeout_atomic(u->base + USD_REG_STATUS, st,
					!(st & USD_ST_BUSY), 0,
					usd_shift_timeout_us(u, wide ? 32 : 8));
	if (ret) {
		u->n_timeout++;
		return -ETIMEDOUT;
	}
	if (st & (USD_ST_ABORT | USD_ST_OVR)) {
		/* W1C only the two error bits; CD_CHANGED belongs to nobody here */
		usd_wr(u, USD_REG_STATUS, st & (USD_ST_ABORT | USD_ST_OVR));
		if (st & USD_ST_ABORT)
			u->n_abort++;
		if (st & USD_ST_OVR)
			u->n_ovr++;
		return -EIO;
	}
	if (in)
		*in = usd_rd(u, USD_REG_DATA);
	return 0;
}

static void usd_set_speed(struct usd_spi *u, u32 hz)
{
	u32 div = usd_div_for(u, hz);

	/* only between shifts: every shift above waits for BUSY to clear */
	if (div != u->div) {
		usd_wr(u, USD_REG_CLKDIV, div);
		u->div = div;
	}
}

/*
 * @level is the chip-select LINE level the SPI core wants (it has already
 * folded SPI_CS_HIGH in, which is how mmc_spi clocks its 74+ init cycles
 * with CS high). CTRL.CS = 1 drives DAT3 LOW.
 */
static void usd_set_cs(struct spi_device *spi, bool level)
{
	struct usd_spi *u = spi_controller_get_devdata(spi->controller);

	usd_ctrl_update(u, USD_CTRL_CS, level ? 0 : USD_CTRL_CS);
}

static int usd_transfer_one(struct spi_controller *ctlr, struct spi_device *spi,
			    struct spi_transfer *t)
{
	struct usd_spi *u = spi_controller_get_devdata(ctlr);
	const u8 *tx = t->tx_buf;
	u8 *rx = t->rx_buf;
	unsigned int i = 0, len = t->len;
	u32 in;
	int ret;

	usd_set_speed(u, t->speed_hz ? t->speed_hz : spi->max_speed_hz);

	while (len - i >= 4) {
		/* no tx buffer: MOSI high, which is what an SD card expects */
		ret = usd_shift(u, tx ? get_unaligned_be32(tx + i) : 0xFFFFFFFFu,
				rx ? &in : NULL, true);
		if (ret)
			return ret;
		if (rx)
			put_unaligned_be32(in, rx + i);
		i += 4;
		if (!(i & 0x1FF))
			cond_resched();
	}
	while (i < len) {
		ret = usd_shift(u, tx ? tx[i] : 0xFF, rx ? &in : NULL, false);
		if (ret)
			return ret;
		if (rx)
			rx[i] = in & 0xFF;
		i++;
	}
	return 0;
}

/* ---- card detect as a one-line GPIO chip -------------------------------- */
static int usd_gpio_get(struct gpio_chip *gc, unsigned int off)
{
	struct usd_spi *u = gpiochip_get_data(gc);

	return !!(usd_rd(u, USD_REG_STATUS) & USD_ST_CD_PRESENT);
}

static int usd_gpio_get_direction(struct gpio_chip *gc, unsigned int off)
{
	return GPIO_LINE_DIRECTION_IN;
}

static int usd_gpio_direction_input(struct gpio_chip *gc, unsigned int off)
{
	return 0;
}

/* ---- sysfs --------------------------------------------------------------- */
static ssize_t card_present_show(struct device *dev, struct device_attribute *a,
				 char *buf)
{
	struct usd_spi *u = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%d\n", !!(usd_rd(u, USD_REG_STATUS) & USD_ST_CD_PRESENT));
}
static DEVICE_ATTR_RO(card_present);

static ssize_t cd_raw_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct usd_spi *u = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%d\n", !!(usd_rd(u, USD_REG_STATUS) & USD_ST_CD_RAW));
}
static DEVICE_ATTR_RO(cd_raw);

static ssize_t errors_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct usd_spi *u = dev_get_drvdata(dev);

	return sysfs_emit(buf, "abort=%lu ovr=%lu timeout=%lu\n",
			  u->n_abort, u->n_ovr, u->n_timeout);
}
static DEVICE_ATTR_RO(errors);

static struct attribute *usd_attrs[] = {
	&dev_attr_card_present.attr,
	&dev_attr_cd_raw.attr,
	&dev_attr_errors.attr,
	NULL,
};
ATTRIBUTE_GROUPS(usd);

/* ---- probe ---------------------------------------------------------------- */
static void usd_disable(void *data)
{
	struct usd_spi *u = data;

	/* float the pads: EN, CS, WIDE off; CD_POL/CD_IGNORE kept */
	usd_ctrl_update(u, USD_CTRL_EN | USD_CTRL_CS | USD_CTRL_WIDE, 0);
}

static int usd_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct spi_controller *ctlr;
	struct usd_spi *u;
	struct clk *clk;
	u32 id, freq;
	int ret;

	ctlr = devm_spi_alloc_host(dev, sizeof(*u));
	if (!ctlr)
		return -ENOMEM;
	u = spi_controller_get_devdata(ctlr);
	u->dev = dev;

	u->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(u->base))
		return PTR_ERR(u->base);

	/*
	 * Presence first, and read-only: a shell without the block (an older
	 * static with the pad-less axi_quad_spi_0 on this page) must never be
	 * written. One info line, no error: no block is not a fault.
	 */
	id = usd_rd(u, USD_REG_ID);
	if (id != USD_ID_VALUE) {
		dev_info(dev, "no usd_spi block (ID 0x%08x, want 0x%08x); user microSD unavailable\n",
			 id, USD_ID_VALUE);
		return -ENODEV;
	}

	clk = devm_clk_get_optional_enabled(dev, NULL);
	if (IS_ERR(clk))
		return dev_err_probe(dev, PTR_ERR(clk), "clock\n");
	u->aclk_hz = clk ? clk_get_rate(clk) : 0;
	if (!u->aclk_hz && !of_property_read_u32(dev->of_node, "clock-frequency", &freq))
		u->aclk_hz = freq;
	if (!u->aclk_hz)
		u->aclk_hz = USD_DEFAULT_ACLK_HZ;

	/* a known state: identification clock, CS released, byte shifts, EN on */
	u->div = usd_div_for(u, 400000);
	usd_wr(u, USD_REG_CLKDIV, u->div);
	u->wide = false;
	usd_ctrl_update(u, USD_CTRL_CS | USD_CTRL_WIDE, USD_CTRL_EN);
	usd_wr(u, USD_REG_STATUS, USD_ST_CD_CHANGED | USD_ST_OVR | USD_ST_ABORT);

	platform_set_drvdata(pdev, u);
	/* devres unwinds LIFO: registered here, this runs AFTER the controller
	 * and the gpio chip are gone, so it never floats the pads under I/O */
	ret = devm_add_action_or_reset(dev, usd_disable, u);
	if (ret)
		return ret;

	u->gc.label = dev_name(dev);
	u->gc.parent = dev;
	u->gc.owner = THIS_MODULE;
	u->gc.base = -1;
	u->gc.ngpio = 1;
	u->gc.can_sleep = false;
	u->gc.get = usd_gpio_get;
	u->gc.get_direction = usd_gpio_get_direction;
	u->gc.direction_input = usd_gpio_direction_input;
	/* before the controller: registering it creates the slot, which looks
	 * the CD line up at once */
	ret = devm_gpiochip_add_data(dev, &u->gc, u);
	if (ret)
		return dev_err_probe(dev, ret, "card-detect gpio chip\n");

	ctlr->dev.of_node = dev->of_node;
	ctlr->bus_num = -1;
	ctlr->num_chipselect = 1;
	ctlr->mode_bits = SPI_CS_HIGH;		/* mode 0 only; CS_HIGH for mmc_spi */
	ctlr->bits_per_word_mask = SPI_BPW_MASK(8);
	ctlr->min_speed_hz = DIV_ROUND_UP(u->aclk_hz, 2ul * (USD_CLKDIV_MAX + 1));
	ctlr->max_speed_hz = u->aclk_hz / (2ul * (USD_CLKDIV_MIN + 1));
	ctlr->set_cs = usd_set_cs;
	ctlr->transfer_one = usd_transfer_one;

	ret = devm_spi_register_controller(dev, ctlr);
	if (ret)
		return dev_err_probe(dev, ret, "spi controller\n");

	dev_info(dev, "user microSD controller, aclk %lu Hz, SCK %u..%u Hz, card %s\n",
		 u->aclk_hz, ctlr->min_speed_hz, ctlr->max_speed_hz,
		 (usd_rd(u, USD_REG_STATUS) & USD_ST_CD_PRESENT) ? "present" : "absent");
	return 0;
}

static const struct of_device_id usd_of_match[] = {
	{ .compatible = "soclabs,usd-spi-1.0" },
	{ }
};
MODULE_DEVICE_TABLE(of, usd_of_match);

static struct platform_driver usd_driver = {
	.probe = usd_probe,
	.driver = {
		.name = DRV_NAME,
		.of_match_table = usd_of_match,
		.dev_groups = usd_groups,
	},
};
module_platform_driver(usd_driver);

MODULE_DESCRIPTION("SoCLabs MPS3 usd_spi user-microSD SPI controller");
MODULE_LICENSE("GPL");
