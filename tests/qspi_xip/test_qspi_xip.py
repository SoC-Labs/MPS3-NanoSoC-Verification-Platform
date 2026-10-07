"""
M2 gate — memory-mapped XiP reads through the SoC's `qspi_flash_ahb` wrapper.

WHAT THIS PROVES
----------------
NanoSoC instantiates the SoC Labs QSPI flash controller as `u_qspi_flash_0`,
module `qspi_flash_ahb` (nanosoc_m0_soc/src/rtl/wrappers/qspi_flash_ahb.v), with

    qspi_mem  @ 0x7000_0000   AHB, read-only, 4 MB — the XiP aperture
    qspi_ctrl @ 0x7400_0000   AHB->APB, 64 KB     — the config registers

The gate is: can a bus master fetch words from the XiP aperture and get back
exactly what is in the flash?  If yes, the CPU can execute code in place from
QSPI flash, which is the premise of the whole boot/interpreter story.

The DUT is the WRAPPER, not `top_ahb_qspi`.  That is deliberate: the wrapper is
what the SoC integrates, and it is where a port-mapping mistake (HSEL vs HSELx,
the PADDR[15:0] slice, an o/i/e swap on the split-tristate pads) would live.
Testing top_ahb_qspi directly would step over exactly the code under suspicion.

The flash is the real Microchip/SST SST26VF064B behavioural model from
ahb_qspi/verif/VIP — the same VIP ahb_qspi's own 55-test suite uses, and the
same part that is on the board.

THE GOLDEN REFERENCE IS INDEPENDENT OF THE DUT
----------------------------------------------
The flash array is loaded by hierarchical deposit into the VIP's own memory
(see qspi_xip_harness.sv, "reason 4"), with a byte pattern derived from the byte
ADDRESS.  Nothing the DUT does can influence it.  Had the bench instead
programmed the flash through the controller's own page-program path — which is
what ahb_qspi's suite does — then a byte-lane swap or endianness error present
in BOTH the program and the read path would cancel out and the test would pass
on broken RTL.  With an address-derived golden, byte order is pinned absolutely:
the byte at flash address A must come back in HRDATA[7:0] of the word at A, which
is what a little-endian Cortex-M0 needs in order to fetch instructions.

REGISTER MAP / BRING-UP SEQUENCE
--------------------------------
Not guessed.  Offsets are from ahb_qspi/sys_desc/register_maps/apb_qspi_regs.yaml
(generated from src/rdl/apb_qspi_regs.rdl); the bring-up ordering — in particular
the CG092 cache-enable read/write handshake, which is load-bearing and
non-obvious — is cribbed from ahb_qspi's own cocotb suite
(verif/cocotb/ahb_qspi_tests.py :: _xip_baseline_minus / _switch_to_plain_spi_xip
and SST26VF064B.py).  Provenance is noted at each helper.
"""

import logging

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer

from cocotbext.ahb import AHBBus, AHBLiteMaster

# -----------------------------------------------------------------------------
# APB register map — apb_qspi_regs.yaml.  These are APB byte offsets; the SoC
# reaches them at 0x7400_0000 + offset through u_qspi_apb_bridge.  Inside the
# wrapper, PADDR[15:12] selects: 0x0 = controller, 0x1 = CG092 cache config.
# -----------------------------------------------------------------------------
QSPI_CTRL       = 0x000
QSPI_STATUS     = 0x004
QSPI_SPI_CMD    = 0x008
QSPI_ADDR       = 0x00C
QSPI_RDATA0     = 0x010
QSPI_AHB_SETUP  = 0x030      # XiP: [15:12] dummy cycles, [7:0] read opcode
QSPI_CLK_DIV    = 0x034

PIDR_BASE       = 0xFD0      # CoreSight-style ID block
CIDR_BASE       = 0xFF0

CACHE_CONFIG    = 0x1000     # CG092 CCR  — bit 0 = EN
CACHE_STATUS    = 0x1004     # CG092 SR   — bits[1:0]: 2 = READY

# QSPI_CTRL bits
CTRL_QIO_MODE   = 1 << 0
CTRL_XIP_ACTIVE = 1 << 8
CTRL_NO_CMD     = 1 << 25

# QSPI_STATUS bits
STAT_BUSY       = 1 << 0
STAT_IRQ_CLR    = 1 << 8

# -----------------------------------------------------------------------------
# XiP aperture.  The SoC decodes qspi_mem at 0x7000_0000, but top_ahb_qspi wires
# only HADDR[21:0] into the cache (`.HADDRS(HADDR[21:0])`), so the region base
# folds away and flash address == HADDR[21:0].  The tests drive the FULL SoC
# address on purpose: if that folding were ever to change, this bench would be
# the thing that notices.
# -----------------------------------------------------------------------------
XIP_BASE = 0x7000_0000

# Must match qspi_xip_harness.sv's PRELOAD_BASE / PRELOAD_LEN.
PRELOAD_BASE = 0x0003_0000
PRELOAD_LEN  = 512

# JEDEC ID the SST26VF064B model reports (ahb_qspi/verif/cocotb/SST26VF064B.py).
SST26VF064B_JEDEC_ID = 0x4326BF00

CLK_NS = 10          # 100 MHz HCLK, as ahb_qspi's bench uses
CLK_DIV = 2          # SCLK = HCLK / (2*CLK_DIV) = 25 MHz. The controller
                     # enforces CLK_DIV >= 2; the VIP's timing checks corrupt
                     # its whole array if driven out of spec.
POLL_CYCLES = 50_000


def flash_byte(addr: int) -> int:
    """The backdoor-preloaded byte at flash byte-address `addr`.

    Mirrors qspi_xip_harness.sv exactly:  ((addr[7:0] + 0x11) & 0xFF) ^ addr[15:8]
    """
    return (((addr & 0xFF) + 0x11) & 0xFF) ^ ((addr >> 8) & 0xFF)


def flash_word(addr: int) -> int:
    """Little-endian 32-bit word at flash byte-address `addr`.

    THE definition of the gate: byte at address A lands in bits [7:0].
    """
    return (
        flash_byte(addr)
        | (flash_byte(addr + 1) << 8)
        | (flash_byte(addr + 2) << 16)
        | (flash_byte(addr + 3) << 24)
    )


def cm0_hprot(addr: int, opcode: bool = False) -> int:
    """The HPROT the Cortex-M0 actually drives for `addr`.

    Not folklore — transcribed from the core RTL in the lab IP library,
    Cortex-M0/AT510-r0p0-03rel2/.../logical/cortexm0/verilog/cm0_matrix.v:

        wire ahb_c = ( ~ahb_addr[31] &  ahb_addr[29] |
                       ~ahb_addr[30] & ~ahb_addr[29] );     // line 249
        wire ahb_b = ahb_addr[30] | ahb_addr[29];           // line 252
        assign hprot_o = { ahb_c, ahb_b, 1'b1, ahb_prot };  // line 340

    i.e. the ARMv6-M default memory map: the M0 derives cacheability from
    address[31:29] alone.  This MATTERS for XiP, because the CG092 only
    allocates when HPROT[3] is set (p_flash_cache_f0_core.v: `non_cache_aphase
    = HWRITE | ~HPROT[3]`).  For the SoC's qspi_mem base of 0x7000_0000 —
    a31=0, a30=1, a29=1 — ahb_c evaluates to 1, so CPU fetches from the XiP
    aperture ARE cacheable.  Had qspi_mem been placed in the peripheral region
    (0x4000_0000) or a device region (0xA/0xC/0xE), ahb_c would be 0 and the
    cache would silently never be used.  See test_xip_aperture_cacheable_for_cm0.
    """
    a31 = (addr >> 31) & 1
    a30 = (addr >> 30) & 1
    a29 = (addr >> 29) & 1
    ahb_c = ((1 - a31) & a29) | ((1 - a30) & (1 - a29))
    ahb_b = a30 | a29
    return (ahb_c << 3) | (ahb_b << 2) | (1 << 1) | (0 if opcode else 1)


# The exact HPROT a Cortex-M0 presents when FETCHING AN INSTRUCTION from the XiP
# aperture. This is what the cache test drives, so the result is a statement
# about the real SoC and not about a convenient bench setting.
CM0_IFETCH_HPROT = cm0_hprot(XIP_BASE, opcode=True)      # 0b1110 for 0x7000_0000


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.log = logging.getLogger("cocotb.tb")
        self.log.setLevel(logging.INFO)

        dut.HRESETn.value = 0
        # Default to the exact HPROT a Cortex-M0 emits when fetching an
        # instruction from the XiP aperture (see cm0_hprot()). The CG092 gates
        # allocation on HPROT[3], so this bit is what decides whether the cache
        # is used at all.
        dut.XIP_HPROT.value = CM0_IFETCH_HPROT

        cocotb.start_soon(Clock(dut.HCLK, CLK_NS, unit="ns").start())

        # cocotbext-ahb masters. `hready` on both busses is the slave's HREADYOUT,
        # fed back to the slave's HREADY input inside the harness — the AHB-Lite
        # single-slave wiring. hready_in is deliberately NOT in the signal map:
        # there is no such port, because the bench must not be able to lie to the
        # slave about readiness.
        #
        # timeout=40000 cycles: unlike ahb_qspi's own TB we do NOT shrink the
        # controller's WATCHDOG_WIDTH (the SoC wrapper doesn't expose it, so the
        # DUT runs the taped-out default of 16 bits = 65536 HCLK). A healthy XiP
        # line fill at CLK_DIV=2 costs ~700 HCLK, so this is ~50x headroom while
        # still failing long before the watchdog would mask a hang.
        self.cfg = AHBLiteMaster(
            AHBBus.from_prefix(
                dut, "config",
                optional_signals={"hsel": "HSEL", "hburst": "HBURST"}),
            dut.HCLK, dut.HRESETn, 40_000)
        self.xip = AHBLiteMaster(
            AHBBus.from_prefix(
                dut, "data",
                optional_signals={"hsel": "HSEL", "hburst": "HBURST"}),
            dut.HCLK, dut.HRESETn, 40_000)

    # --- reset ---------------------------------------------------------------
    async def cycle_reset(self):
        await ClockCycles(self.dut.HCLK, 1)
        await Timer(4.5, unit="ns")
        self.dut.HRESETn.value = 0
        await ClockCycles(self.dut.HCLK, 40)
        await Timer(4.5, unit="ns")
        self.dut.HRESETn.value = 1
        await ClockCycles(self.dut.HCLK, 10)

    # --- APB (via the config AHB port + cmsdk_ahb_to_apb) --------------------
    async def cfg_write(self, addr, data):
        await self.cfg.write(addr, data)

    async def cfg_read(self, addr):
        r = await self.cfg.read(addr, 4)
        return int(r[0].get("data", "0x0"), 16)

    # --- AHB read on the XiP slave port --------------------------------------
    async def xip_read(self, addr):
        r = await self.xip.read(addr, 4)
        return int(r[0].get("data", "0x0"), 16)

    # --- flash-activity counters (RTL-side, see harness) ----------------------
    def ncs_falls(self):
        return int(self.dut.NCS_FALL_COUNT.value)

    def sclk_rises(self):
        return int(self.dut.SCLK_RISE_COUNT.value)

    async def xip_read_counting(self, addr):
        """AHB XiP read, reporting how much flash traffic it caused.

        Returns (data, ncs_assertions, sclk_edges). ncs_assertions == 0 means the
        CG092 served the read from its cache RAM without touching the flash.
        """
        n0, s0 = self.ncs_falls(), self.sclk_rises()
        data = await self.xip_read(addr)
        # Let any trailing flash activity retire before sampling (nCS rises a few
        # HCLK after the AHB data phase completes).
        await ClockCycles(self.dut.HCLK, 40)
        return data, self.ncs_falls() - n0, self.sclk_rises() - s0


# =============================================================================
# Controller command helpers.
#
# Cribbed from ahb_qspi/verif/cocotb/SST26VF064B.py (SPI_RESET, SPI_READ_JEDIC,
# SPI_READ_WORDS) — deliberately NOT re-derived, so the bench cannot be wrong
# about the controller's command encoding in a way that ahb_qspi's own suite
# would not also be wrong about.  Reproduced here rather than imported so this
# bench stays self-contained inside mps3-nanosoc-platform.
#
# QSPI_SPI_CMD (0x08) encoding:
#   [7:0] opcode   [8] ENABLE   [9] READ   [10] WRITE   [11] ADDR_EN
#   [15:12] dummy cycles        [23:16] N_RW_BYTES (bytes-1)
# The controller latches on the 0->1 edge of ENABLE, hence the two writes.
# =============================================================================

async def poll_busy_clear(tb, timeout=POLL_CYCLES):
    for _ in range(timeout):
        if not (await tb.cfg_read(QSPI_STATUS) & STAT_BUSY):
            return
        await ClockCycles(tb.dut.HCLK, 2)
    raise AssertionError("QSPI_BUSY never cleared")


async def wait_for_irq(tb, timeout=POLL_CYCLES):
    for _ in range(timeout):
        if tb.dut.IRQ_QSPI_FINISHED.value == 1:
            break
        await ClockCycles(tb.dut.HCLK, 1)
    else:
        raise AssertionError("IRQ_QSPI_FINISHED never asserted")
    await tb.cfg_write(QSPI_STATUS, STAT_IRQ_CLR)
    await ClockCycles(tb.dut.HCLK, 2)


async def flash_cmd_byte(tb, op):
    """Command-only transaction (no address, no data)."""
    await tb.cfg_write(QSPI_SPI_CMD, op & 0xFF)
    await tb.cfg_write(QSPI_SPI_CMD, 0x100 | (op & 0xFF))
    await wait_for_irq(tb)
    await poll_busy_clear(tb)


async def spi_reset(tb):
    """Return the flash to a known plain-SPI, non-continuous-read state.

    This bench never enters QPI and never sets CONT_READ, so the full hardened
    recovery in ahb_qspi's SST26VF064B.py (which has to unwind a QPI
    continuous-read trap left by a predecessor test) is not needed — plain-SPI
    RSTEN + RST is sufficient and is a subset of it.  The CLK_DIV write first is
    load-bearing for the same reason it is there: at the default CLK_DIV the
    command completes before the CDC-synchronised BUSY flag propagates.
    """
    await tb.cfg_write(QSPI_CLK_DIV, 0x05)
    await tb.cfg_write(QSPI_CTRL, 0x00000000)   # SPI framing, no XIP/NO_CMD/CR
    await flash_cmd_byte(tb, 0x66)              # RSTEN
    await flash_cmd_byte(tb, 0x99)              # RST


async def spi_read_jedec(tb):
    """JEDEC ID (0x9F) in SPI framing. Verbatim from SST26VF064B.py."""
    await tb.cfg_write(QSPI_SPI_CMD, 0x0002_009F)
    await tb.cfg_write(QSPI_SPI_CMD, 0x0002_039F)
    await wait_for_irq(tb)
    await poll_busy_clear(tb)
    return await tb.cfg_read(QSPI_RDATA0)


async def spi_read_word_via_apb(tb, addr):
    """Read ONE 32-bit word from flash over the APB/controller path (fast-read
    0x0B, 8 dummy cycles). This never touches the cache or the AHB XiP port, so
    it is an independent second opinion on what is physically in the flash.

    Encoding from SST26VF064B.py::SPI_READ_WORDS.
    """
    await tb.cfg_write(QSPI_ADDR, addr & 0x3F_FFFF)
    instr = (
        0x0B                    # fast read
        | (1 << 9)              # READ
        | (1 << 11)             # ADDR_EN
        | (8 << 12)             # 8 dummy cycles (SPI fast-read)
        | ((4 - 1) << 16)       # N_RW_BYTES = 4 bytes - 1
    )
    await tb.cfg_write(QSPI_SPI_CMD, instr)
    await tb.cfg_write(QSPI_SPI_CMD, instr ^ (1 << 8))   # ENABLE 0->1
    await wait_for_irq(tb)
    await poll_busy_clear(tb)
    return await tb.cfg_read(QSPI_RDATA0)


# =============================================================================
# XiP bring-up.
#
# Plain SPI, fast-read 0x0B with 8 dummy cycles, no QIO, no NO_CMD, no
# continuous-read: the lowest-common-denominator mode, and the one a bootrom or
# a CPU fetching from reset can actually get into.  ahb_qspi's suite proves this
# combination works (test_xip_plain_spi_fast_read_line_fill).
#
# The CG092 cache-enable handshake below looks redundant and is not: the pre-read
# of SR, the post-read of CCR, and the CCR-read that primes each SR poll are all
# load-bearing.  Omitting any of them leaves STATUS stuck at 0x1 (transitioning).
# ahb_qspi documents this at length above _xip_baseline_minus; the reason the
# priming read matters is that cmsdk_ahb_to_apb holds HREADYOUT=1 with registered
# HRDATA while idle, so an isolated single read returns stale data forever.
# =============================================================================

XIP_OPCODE = 0x0B
XIP_DUMMY  = 8


async def cache_enable(tb):
    await tb.cfg_read(CACHE_STATUS)                # pre-read SR
    await tb.cfg_write(CACHE_CONFIG, 0x1)          # CCR.EN = 1
    await tb.cfg_read(CACHE_CONFIG)                # post-read CCR
    for _ in range(500):
        await tb.cfg_read(CACHE_CONFIG)            # prime (see note above)
        sr = await tb.cfg_read(CACHE_STATUS)
        if (sr & 0x3) == 2:
            return
        await Timer(10, unit="us")
    raise AssertionError(
        f"CG092 cache never reached READY (SR&3 = 0x{sr & 3:x})")


async def cache_disable(tb):
    await tb.cfg_write(CACHE_CONFIG, 0x0)
    for _ in range(100):
        await tb.cfg_read(CACHE_CONFIG)
        if (await tb.cfg_read(CACHE_STATUS) & 0x3) == 0:
            return
        await Timer(10, unit="us")


async def soc_init(tb, hprot=CM0_IFETCH_HPROT):
    """Reset + put the flash in a known plain-SPI state. XIP_ACTIVE stays 0.

    Split out from xip_enable() because of a real constraint on this IP: once
    XIP_ACTIVE is set, `qspi_controller_mux` hands the controller to the AHB
    side and the APB command path is DEAD (APB_QSPI_ENABLE_ACK is forced to 0
    and APB_QSPI_BUSY to 1). Any controller command a test wants to issue —
    including the APB cross-check read in the gate below — must happen while
    XIP_ACTIVE is still 0.  See the README, "Firmware constraint".
    """
    tb.dut.XIP_HPROT.value = hprot
    await tb.cycle_reset()
    await spi_reset(tb)
    await tb.cfg_write(QSPI_CLK_DIV, CLK_DIV)


async def xip_enable(tb):
    """Arm XiP and bring the CG092 cache to READY. After this, no APB commands."""
    await tb.cfg_write(QSPI_AHB_SETUP, (XIP_DUMMY << 12) | XIP_OPCODE)
    await tb.cfg_write(QSPI_CTRL, CTRL_XIP_ACTIVE)   # XIP only: SPI, no NO_CMD
    await cache_enable(tb)
    tb.log.info("XiP up: opcode=0x%02x dummy=%d clk_div=%d hprot=0b%s",
                XIP_OPCODE, XIP_DUMMY, CLK_DIV,
                format(int(tb.dut.XIP_HPROT.value), "04b"))


async def xip_bringup(tb, hprot=CM0_IFETCH_HPROT):
    """Full XiP bring-up from reset. Leaves the cache READY and XIP_ACTIVE set."""
    await soc_init(tb, hprot)
    await xip_enable(tb)


async def xip_teardown(tb):
    """Leave the DUT neutral so the next test in this shared sim starts clean."""
    await tb.cfg_write(QSPI_CTRL, 0x0)
    await cache_disable(tb)
    await tb.cfg_write(QSPI_CLK_DIV, 0x05)


# =============================================================================
# TEST 1 — the APB path into the controller works.
# =============================================================================

@cocotb.test()
async def test_apb_regs_reachable(dut):
    """Read the controller's CoreSight ID block through the APB slave.

    If this fails, nothing else in this file means anything: it would mean the
    wrapper's PADDR[15:0] slice, the cmsdk_apb_slave_mux decode on PADDR[15:12],
    or the SoC's AHB-to-APB bridge is broken, and every later register write is
    going nowhere.
    """
    tb = TB(dut)
    await tb.cycle_reset()

    # apb_qspi_regs.yaml: pidr0..4 = 0x59,0x16,0x15,0x00,0x00 laid out
    # CoreSight-style (PIDR4 first at 0xFD0, PIDR0..3 at 0xFE0..0xFEC).
    expect_pidr = [0x00, 0x00, 0x00, 0x00, 0x59, 0x16, 0x15, 0x00]
    got_pidr = []
    for i in range(8):
        got_pidr.append(await tb.cfg_read(PIDR_BASE + i * 4))
    tb.log.info("PIDR = %s", [f"0x{v:02x}" for v in got_pidr])
    assert got_pidr == expect_pidr, (
        f"PIDR mismatch: got {[hex(v) for v in got_pidr]}, "
        f"expected {[hex(v) for v in expect_pidr]}")

    expect_cidr = [0x50, 0x51, 0x4C, 0x53]
    got_cidr = [await tb.cfg_read(CIDR_BASE + i * 4) for i in range(4)]
    tb.log.info("CIDR = %s", [f"0x{v:02x}" for v in got_cidr])
    assert got_cidr == expect_cidr, (
        f"CIDR mismatch: got {[hex(v) for v in got_cidr]}, "
        f"expected {[hex(v) for v in expect_cidr]}")

    # Reset value + a write/read-back, so we know writes land too (an ID block
    # alone would still read correctly through a bridge that dropped writes).
    assert await tb.cfg_read(QSPI_CTRL) == 0, "QSPI_CTRL reset value is not 0"
    await tb.cfg_write(QSPI_ADDR, 0x0012_3456)
    rb = await tb.cfg_read(QSPI_ADDR)
    assert rb == 0x0012_3456, f"QSPI_ADDR read-back 0x{rb:08x} != 0x00123456"

    # And the CG092 cache config aperture (PADDR[15:12] == 1) is a distinct
    # slave: it must NOT alias onto the controller's registers.
    await cache_disable(tb)
    sr = await tb.cfg_read(CACHE_STATUS)
    tb.log.info("CG092 SR (cache disabled) = 0x%08x", sr)
    assert (sr & 0x3) == 0, f"CG092 SR&3 = 0x{sr & 3:x}, expected 0 (disabled)"

    tb.log.info("APB path into the QSPI controller and the CG092 cache: OK")


# =============================================================================
# TEST 2 — the pads and the flash VIP are wired up.
# =============================================================================

@cocotb.test()
async def test_flash_vip_jedec_id(dut):
    """Read the SST26VF064B's JEDEC ID over the controller's SPI command path.

    Diagnostic, and cheap. It is the first test that leaves the DUT: it drives
    the split-tristate pads, through the harness's per-lane resolution, into the
    VIP, and shifts the answer back in on QSPI_IO_i. If this fails but test 1
    passed, the fault is in the pad wiring (an o/i/e swap, a lane reversal) or
    the clock divider — not in the XiP/cache logic, and test 3's failure would
    tell you nothing new.
    """
    tb = TB(dut)
    await tb.cycle_reset()
    await spi_reset(tb)

    jid = await spi_read_jedec(tb)
    tb.log.info("JEDEC ID = 0x%08x (expect 0x%08x)", jid, SST26VF064B_JEDEC_ID)
    assert jid == SST26VF064B_JEDEC_ID, (
        f"JEDEC ID 0x{jid:08x} != 0x{SST26VF064B_JEDEC_ID:08x} — the QSPI pads "
        f"or the flash VIP are not talking")


# =============================================================================
# TEST 3 — THE GATE.
# =============================================================================

@cocotb.test()
async def test_xip_read_matches_flash(dut):
    """AHB reads on the XiP slave port return exactly the flash contents.

    This is the M2 gate. It proves a bus master — i.e. the CPU — can fetch words
    straight out of QSPI flash through the memory-mapped aperture.

    Three independent things are compared:
      * the backdoor golden      — what the harness deposited into the VIP array
      * the APB/controller read  — what the controller reads back over SPI,
                                   bypassing the cache and the AHB XiP port
      * the AHB XiP read         — the path under test
    All three must agree. The APB leg is what distinguishes "the XiP path is
    broken" from "the flash doesn't contain what we think it does".
    """
    tb = TB(dut)
    n_words = 16          # 64 B = four 16-byte cache lines

    # --- leg 1+2: does the flash physically hold the golden pattern? ----------
    # MUST run before XIP_ACTIVE is set: the controller mux disconnects the APB
    # command path while XiP is armed (see soc_init's docstring).
    await soc_init(tb)
    for i in (0, 1, 5, 15):
        fa = PRELOAD_BASE + i * 4
        want = flash_word(fa)
        got = await spi_read_word_via_apb(tb, fa)
        tb.log.info("APB flash read @0x%06x = 0x%08x (golden 0x%08x)",
                    fa, got, want)
        assert got == want, (
            f"APB/controller read of flash 0x{fa:06x} returned 0x{got:08x}, "
            f"but the VIP was preloaded with 0x{want:08x}. Either the backdoor "
            f"preload did not land or the controller's read path is broken — "
            f"in both cases the XiP result below would be uninterpretable.")

    # --- leg 3: the XiP path -------------------------------------------------
    await xip_enable(tb)

    mismatches = []
    for i in range(n_words):
        fa = PRELOAD_BASE + i * 4
        want = flash_word(fa)
        got = await tb.xip_read(XIP_BASE + fa)
        tb.log.info("AHB XiP read @0x%08x = 0x%08x (golden 0x%08x) %s",
                    XIP_BASE + fa, got, want, "ok" if got == want else "MISMATCH")
        if got != want:
            mismatches.append((fa, got, want))

    await xip_teardown(tb)

    assert not mismatches, (
        f"XiP read mismatch on {len(mismatches)}/{n_words} words: "
        + ", ".join(f"flash 0x{a:06x}: got 0x{g:08x} want 0x{w:08x}"
                    for a, g, w in mismatches)
        + ".  NOTE: the same words read correctly over the APB/controller path, "
          "so the flash contents are right and the fault is in the "
          "cache / AHB-XiP path.")

    tb.log.info("M2 GATE PASSED: %d words fetched from flash via the AHB XiP "
                "aperture, all matching the flash contents byte-for-byte",
                n_words)


# =============================================================================
# TEST 4 — the cache actually caches.
# =============================================================================

@cocotb.test()
async def test_xip_cache_hit(dut):
    """A second read of the same cache line must not go to the flash.

    XiP is only useful if it is fast, and it is only fast because the CG092 holds
    the line. This measures it at the pads: a read that reaches the flash asserts
    QSPI_nCS; a read served from the cache does not.

    Driven with CM0_IFETCH_HPROT — the exact HPROT vector a Cortex-M0 puts on the
    bus when fetching an instruction from 0x7000_0000 (derived from the core's own
    RTL, see cm0_hprot()). So this is a statement about the real SoC, not about a
    bench-convenient HPROT setting.

    The test includes its own control. If the "hit" assertion (zero chip-selects)
    were passing simply because the counter is broken, the next-line read would
    also show zero — so a read of the NEXT line is required to show flash traffic.
    Without that control a dead monitor would look like a perfect cache.
    """
    tb = TB(dut)
    assert CM0_IFETCH_HPROT & 0b1000, (
        f"the XiP aperture 0x{XIP_BASE:08x} is NOT cacheable in the ARMv6-M "
        f"default memory map (CM0 would drive HPROT=0b{CM0_IFETCH_HPROT:04b}), so "
        f"this test cannot demonstrate a cache hit for a CPU fetch. The aperture "
        f"has been moved into a non-cacheable region — see the README.")
    await xip_bringup(tb, hprot=CM0_IFETCH_HPROT)

    line0 = XIP_BASE + PRELOAD_BASE          # 16-byte aligned
    line1 = line0 + 16

    # 1. cold read of line 0 — must fetch from flash
    d0, ncs_miss, sclk_miss = await tb.xip_read_counting(line0)
    tb.log.info("cold  read @0x%08x -> 0x%08x  nCS=%d sclk=%d",
                line0, d0, ncs_miss, sclk_miss)
    assert d0 == flash_word(PRELOAD_BASE), (
        f"cold XiP read returned 0x{d0:08x}, want 0x{flash_word(PRELOAD_BASE):08x}")
    assert ncs_miss >= 1, (
        "a cold read did NOT assert QSPI_nCS — either the cache was already warm "
        "(test isolation broken) or the nCS counter is dead, which would make the "
        "hit measurement below meaningless")

    # 2. same word again — must be served from the cache
    d1, ncs_hit, sclk_hit = await tb.xip_read_counting(line0)
    tb.log.info("re-read    @0x%08x -> 0x%08x  nCS=%d sclk=%d",
                line0, d1, ncs_hit, sclk_hit)
    assert d1 == d0, f"cached re-read returned 0x{d1:08x}, first read gave 0x{d0:08x}"

    # 3. a different word IN THE SAME 16-byte line — also a hit
    d2, ncs_hit2, _ = await tb.xip_read_counting(line0 + 8)
    want2 = flash_word(PRELOAD_BASE + 8)
    tb.log.info("same-line  @0x%08x -> 0x%08x (golden 0x%08x)  nCS=%d",
                line0 + 8, d2, want2, ncs_hit2)
    assert d2 == want2, f"same-line read returned 0x{d2:08x}, want 0x{want2:08x}"

    # 4. CONTROL: the next line has never been fetched — this MUST hit the flash.
    d3, ncs_next, sclk_next = await tb.xip_read_counting(line1)
    want3 = flash_word(PRELOAD_BASE + 16)
    tb.log.info("next-line  @0x%08x -> 0x%08x (golden 0x%08x)  nCS=%d sclk=%d",
                line1, d3, want3, ncs_next, sclk_next)
    assert d3 == want3, f"next-line read returned 0x{d3:08x}, want 0x{want3:08x}"

    await xip_teardown(tb)

    assert ncs_next >= 1, (
        "the control read of an un-cached line caused NO flash traffic — the nCS "
        "counter is not observing the pads, so the cache-hit result is void")

    assert ncs_hit == 0 and ncs_hit2 == 0, (
        f"CACHE IS NOT CACHING: re-reading a line already fetched caused "
        f"{ncs_hit} and {ncs_hit2} new QSPI_nCS assertions (a cold read caused "
        f"{ncs_miss}, a new line caused {ncs_next}). Every CPU fetch would pay "
        f"full flash latency.")

    tb.log.info("CACHE PROVEN: cold read cost %d nCS / %d SCLK; the two cached "
                "reads cost 0; a new line cost %d nCS / %d SCLK",
                ncs_miss, sclk_miss, ncs_next, sclk_next)


# =============================================================================
# TEST 5 — the address-map hazard: cacheability is gated on HPROT[3], and the
#          Cortex-M0 derives HPROT[3] from address[31:29] alone.
# =============================================================================

@cocotb.test()
async def test_xip_noncacheable_hprot_bypasses_cache(dut):
    """With HPROT[3]=0 the CG092 bypasses its cache — every read reaches the flash.

    Why this test exists. Two independent facts combine into a live hazard for
    anyone editing the SoC memory map:

      1. The CG092 only allocates a line when HPROT[3] is set
         (p_flash_cache_f0_core.v: `non_cache_aphase = HWRITE | ~HPROT[3]`).
      2. The Cortex-M0 does not let software choose HPROT[3]. It derives it from
         the ARMv6-M default memory map — address[31:29] alone
         (cm0_matrix.v:249, transcribed in cm0_hprot()).

    qspi_mem sits at 0x7000_0000, which lands in a cacheable ARMv6-M region, so
    today the CPU gets cached XiP (test 4 proves it at the pads). But the moment
    the aperture is moved into the peripheral region (0x4000_0000) or a device
    region (0xA/0xC/0xE_0000_0000), the M0 will drive HPROT[3]=0, the CG092 will
    silently stop caching, and every instruction fetch will pay a full ~168-SCLK
    flash read. Nothing would fail — it would just get ~50x slower, which is
    exactly the kind of regression that never gets caught.

    This test pins that behaviour down so the hazard is a documented, tested
    property rather than a surprise. It asserts the bypass still returns CORRECT
    data (XiP remains functional, just slow) and that it really does re-read the
    flash every time.
    """
    tb = TB(dut)
    noncacheable = cm0_hprot(0x4000_0000, opcode=True)   # peripheral region
    assert not (noncacheable & 0b1000), "cm0_hprot() is not modelling ahb_c"

    await xip_bringup(tb, hprot=noncacheable)

    addr = XIP_BASE + PRELOAD_BASE + 0x40    # a line no earlier test touched
    fa = PRELOAD_BASE + 0x40
    want = flash_word(fa)

    d0, ncs0, _ = await tb.xip_read_counting(addr)
    d1, ncs1, _ = await tb.xip_read_counting(addr)
    tb.log.info("HPROT=0b%s (CM0 @0x40000000)  read1 -> 0x%08x nCS=%d ; "
                "read2 -> 0x%08x nCS=%d  (golden 0x%08x)",
                format(noncacheable, "04b"), d0, ncs0, d1, ncs1, want)

    await xip_teardown(tb)

    assert d0 == want and d1 == want, (
        f"non-cacheable XiP read returned 0x{d0:08x}/0x{d1:08x}, want 0x{want:08x} "
        f"— the CG092 bypass path is returning WRONG DATA. That would break XiP "
        f"outright for any aperture placed in a non-cacheable region.")

    assert ncs0 >= 1 and ncs1 >= 1, (
        f"expected the non-cacheable path to reach the flash on BOTH reads, got "
        f"nCS={ncs0} then nCS={ncs1}. If the second read was served from the "
        f"cache then the CG092 IS allocating on a non-cacheable access, and the "
        f"address-map hazard described above does not exist — good news, but the "
        f"README must be corrected.")

    tb.log.info("ADDRESS-MAP HAZARD CONFIRMED: HPROT[3]=0 -> cache bypassed "
                "(flash re-read on both accesses), data still correct. "
                "qspi_mem MUST stay in a cacheable ARMv6-M region — see README.")
