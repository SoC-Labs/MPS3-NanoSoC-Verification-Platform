"""clcd_kvm (the CLCD KVM @ 0x44AD_0000) at the width shell_bd.tcl actually
instantiates it with (C_S_AXI_ADDR_WIDTH=32).

Same latent bug as every other shell CSR block: the RTL default is 12 but the BD
instantiates 32, so the interconnect hands the slave the FULL system address
(0x44AD_0000, not 0x0). A decode of addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] compares
the full address against 'h0 and never matches — on silicon every KVM register
would be unreachable (STATUS reads 0, CTRL writes ignored, the button toggle
still works in hardware but firmware can neither see nor steer it). clcd_kvm.sv
decodes its own 64 KiB page (LOCAL_ADDR_W=min(width,16)); this bench elaborates
at 32 and drives base+offset, so it fails on a dead decode and passes on the
64 KiB-page decode.

It also pins the two properties the widened decode must NOT regress:
  * BASE+0x1000 (and in-page unmapped offsets) must NOT alias CTRL @ offset 0 —
    a too-narrow decode there would let a CSR page-dump toggle ownership or arm
    the panel-reset sequencer.
  * NO READ has any side effect, ANYWHERE in the 64 KiB page. The platform dumps
    whole CSR pages over SWD/XVC; a read that armed panel_rst_pulse or cleared an
    EVENT bit would corrupt the screen or eat a handover. This is asserted the
    hard way: EVENT is loaded with a real event, the whole page is read, and
    EVENT + every pad is checked unchanged.

Run with:  make -C tests/csr_decode_width BLOCK=clcd_kvm
"""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from regmap import AxiLiteMaster  # noqa: E402

NO_RTL = os.environ.get("NO_RTL") == "1"

KVM_BASE = 0x44AD0000            # shell_bd.tcl assign_bd_address for clcd_kvm_0

CTRL = 0x00
STATUS = 0x04
EVENT = 0x08
PANEL_TMR = 0x0C
TIMEOUT = 0x10
DEBOUNCE = 0x14
TUNNEL = 0x18
MAPPED = (CTRL, STATUS, EVENT, PANEL_TMR, TIMEOUT, DEBOUNCE, TUNNEL)

C_TIMEOUT_EN = 1 << 2
C_PB_EN = 1 << 3
C_BACKLIGHT = 1 << 5
C_SRC_SEL_WE = 1 << 16
CTRL_RESET = C_TIMEOUT_EN | C_PB_EN                 # 0x0000_000C

S_HARNESS_QUIET = 1 << 8
E_ALL = 0xFF


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())

    dut.s_axi_aresetn.value = 0
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    dut.s_axi_bready.value = 0
    dut.s_axi_arvalid.value = 0
    dut.s_axi_rready.value = 0
    dut.s_axi_wstrb.value = 0xF
    # Idle every non-AXI input so the block is deterministic. The harness source
    # idles quiescent (fifo_empty=1, busy=0, panel lit); the tunnel and the
    # button idle at their unpressed / display-less values.
    dut.h_pd_o.value = 0
    dut.h_pd_oe.value = 1
    dut.h_cs_n.value = 1
    dut.h_wr_n.value = 1
    dut.h_rd_n.value = 1
    dut.h_rs.value = 0
    dut.h_bl.value = 1
    dut.h_rst_n.value = 1
    dut.h_busy.value = 0
    dut.h_fifo_empty.value = 1
    dut.dut_gpio_o_i.value = 0
    dut.dut_gpio_oe_i.value = 0
    dut.decouple_status.value = 0
    dut.rp_resetn.value = 1
    dut.user_npb1.value = 1
    dut.clcd_pd_i.value = 0

    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    # pb_level resets to PRESSED (clcd_kvm README §11 step 5); the released
    # button is accepted on the first 1 µs tick (100 cycles). Wait it out so the
    # sweep test's press is a press, not part of "held through reset".
    await ClockCycles(dut.s_axi_aclk, 110)

    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


@cocotb.test(skip=NO_RTL)
async def test_reads_decode_at_the_real_base_address(dut):
    """A read at BASE+STATUS must return STATUS, not 0.

    STATUS.harness_quiet is 1 at reset (an idle clcd_0), independent of any write
    path — so this isolates 'reads decode at the real base'. A dead decode
    (full-address compare against 'h0) reads 0 for everything, which is how the
    silicon bug hid."""
    axi = await _bring_up(dut)

    at_zero, _ = await axi.read(STATUS)
    at_base, _ = await axi.read(KVM_BASE + STATUS)
    assert at_zero == at_base, (
        "STATUS read 0x%08x at offset-from-zero but 0x%08x at the real base "
        "0x%08x. The decode compares the upper address bits the interconnect "
        "supplies; every KVM register is unreachable on hardware."
        % (at_zero, at_base, KVM_BASE))
    assert at_base & S_HARNESS_QUIET, (
        "STATUS.harness_quiet read back 0 at the real base — a dead decode reads "
        "0 for everything, which is precisely how this bug hid.")


@cocotb.test(skip=NO_RTL)
async def test_ctrl_writes_land_at_the_real_base(dut):
    """A write at BASE+CTRL must stick. CTRL is the write-readback canary; its
    reset value (0x0000_000C: timeout_en + pb_en) is itself a decode witness."""
    axi = await _bring_up(dut)

    ctrl, _ = await axi.read(KVM_BASE + CTRL)
    assert ctrl == CTRL_RESET, (
        "CTRL read 0x%08x at the real base, expected reset 0x%08x. A dead decode "
        "reads 0." % (ctrl, CTRL_RESET))

    await axi.write(KVM_BASE + CTRL, CTRL_RESET | C_BACKLIGHT)
    val, _ = await axi.read(KVM_BASE + CTRL)
    assert val == (CTRL_RESET | C_BACKLIGHT), (
        "wrote CTRL=0x%x at the real base, read back 0x%08x — writes are being "
        "ignored because the decode never matches."
        % (CTRL_RESET | C_BACKLIGHT, val))


@cocotb.test(skip=NO_RTL)
async def test_timers_are_writable_at_the_real_base(dut):
    """PANEL_TMR/TIMEOUT/DEBOUNCE are plain RW registers at non-zero offsets — so
    the proof does not rest on CTRL (offset 0) alone."""
    axi = await _bring_up(dut)

    await axi.write(KVM_BASE + PANEL_TMR, 0x0007_0003)
    v, _ = await axi.read(KVM_BASE + PANEL_TMR)
    assert v == 0x0007_0003, "PANEL_TMR write ignored at the real base: 0x%08x" % v

    await axi.write(KVM_BASE + TIMEOUT, 0x0000_ABCD)
    v, _ = await axi.read(KVM_BASE + TIMEOUT)
    assert v == 0xABCD, "TIMEOUT write ignored at the real base: 0x%08x" % v


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_do_not_alias_and_read_zero(dut):
    """The fix must not regress into a too-narrow decode. CTRL is offset 0; under
    a decode narrow enough to let BASE+0x1000 (or an in-page unmapped offset)
    wrap to offset 0, a CSR sweep would rewrite ownership/backlight."""
    axi = await _bring_up(dut)

    await axi.write(KVM_BASE + CTRL, CTRL_RESET | C_BACKLIGHT)
    known, _ = await axi.read(KVM_BASE + CTRL)

    await axi.write(KVM_BASE + 0x20, 0xFFFFFFFF)        # in-page, unmapped
    await axi.write(KVM_BASE + 0x1000, 0xFFFFFFFF)      # 64 KiB-page width probe
    await ClockCycles(dut.s_axi_aclk, 2)

    ctrl, _ = await axi.read(KVM_BASE + CTRL)
    assert ctrl == known, (
        "a write to an unmapped offset aliased into CTRL (0x%08x -> 0x%08x): a "
        "register sweep would move ownership or the panel pads." % (known, ctrl))
    gap, _ = await axi.read(KVM_BASE + 0x20)
    assert gap == 0, "unmapped in-page offset 0x20 must read 0, got 0x%08x" % gap
    far, _ = await axi.read(KVM_BASE + 0x1000)
    assert far == 0, "BASE+0x1000 is unmapped and must read 0, got 0x%08x" % far


@cocotb.test(skip=NO_RTL)
async def test_full_page_read_sweep_has_no_side_effect(dut):
    """Sweep the WHOLE 64 KiB page at width 32 and assert NO read moves any pad
    or any EVENT bit — the property the block README §5 puts in a 🚨 box, tested
    at the SHIPPED width. EVENT is loaded with a real event first (a button
    press toggles ownership through the hardware path), so a read-to-clear would
    be caught."""
    axi = await _bring_up(dut)

    # Arm EVENT via the hardware button path (no reliance on the ownership FSM's
    # AXI request — the point is to have EVENT non-zero cheaply). Program a short
    # debounce, press, wait.
    await axi.write(KVM_BASE + DEBOUNCE, 2)             # 2 µs
    dut.user_npb1.value = 0
    await ClockCycles(dut.s_axi_aclk, 600)             # > 2 ticks @ TICK_DIV=100
    dut.user_npb1.value = 1
    await ClockCycles(dut.s_axi_aclk, 50)

    ev_before, _ = await axi.read(KVM_BASE + EVENT)
    assert ev_before != 0, "bench premise: a debounced press should set EVENT.pb_toggle"
    pads_before = (int(dut.clcd_bl_o.value), int(dut.clcd_rst_n_o.value),
                   int(dut.clcd_cs_n_o.value), int(dut.owner_o.value))

    # The sweep: every word offset in the 64 KiB page (16384 reads), at the real
    # base, exactly as a host CSR page-dump does.
    for off in range(0, 0x10000, 4):
        await axi.read(KVM_BASE + off)

    ev_after, _ = await axi.read(KVM_BASE + EVENT)
    assert ev_after == ev_before, (
        "the full-page read sweep changed EVENT (0x%08x -> 0x%08x). EVENT is "
        "W1C, NOT read-to-clear — a CSR page dump would eat the handover events."
        % (ev_before, ev_after))
    pads_after = (int(dut.clcd_bl_o.value), int(dut.clcd_rst_n_o.value),
                  int(dut.clcd_cs_n_o.value), int(dut.owner_o.value))
    assert pads_after == pads_before, (
        "the read sweep moved a pad or the owner %s -> %s: a read is arming an "
        "action somewhere in the page (README §5)." % (pads_before, pads_after))

    # And the mapped RO/RW offsets still read sanely after the sweep.
    st, _ = await axi.read(KVM_BASE + STATUS)
    assert st & S_HARNESS_QUIET, "STATUS decode broke after the sweep"
