"""tests/usr_access_rd/test_usr_access_rd.py

Verifies `fpga/shell/ip/usr_access_rd/usr_access_rd.sv` — USRACC, the block that
lets the running fabric report its own build identity
(docs/VERSIONING_PLAN.md §3.4).

WHAT THIS PROVES, AND WHY EACH PART MATTERS
-------------------------------------------
1. The AXSS word the bitstream was stamped with reaches AXI-Lite. The value is
   supplied by a PLUSARG to the primitive model, never written in this file, so
   a bench that quietly asserted a constant would fail the `control-value` run.
2. It is readable AT THE ADDRESS THE INTERCONNECT USES — base 0x44B3_0000 plus
   the offset, with the block elaborated at C_S_AXI_ADDR_WIDTH=32 exactly as
   shell_bd.tcl instantiates it. This is bug #1's ground: a decode that compares
   the full system address against 'h0 reads 0 for every register, and for THIS
   block a 0 is not a visibly broken answer — it is a plausible identity. The
   `control-decode` run reproduces that failure on purpose, by mutation.
3. MAGIC distinguishes "no block" from "stamped zero". Every unmapped page in
   this shell's window reads back 0; without MAGIC, firmware could not tell a
   fabric honestly stamped 0x00000000 from a fabric with no USRACC at all, and
   would report a comparison it never made.
4. The identity is READABLE AGAIN after a peripheral reset. The watchdog this
   wave wires up pulses `peripheral_aresetn`; a shell that could not say which
   build it was on the reboot where somebody is trying to find out why it reset
   would be useless at exactly the wrong moment.

Run with:  source set_env.sh; make -C tests/usr_access_rd
Controls:  make -C tests/usr_access_rd control-value    (MUST FAIL)
           make -C tests/usr_access_rd control-decode   (MUST FAIL)
           make -C tests/usr_access_rd control-silicon  (MUST FAIL: the
             pre-2026-10 DATAVALID-gated RTL vs the UG570 Fig 7-9 model --
             reproduces the 2026-09-22 silicon read MAGIC ok / VALUE 0 / VALID 0)
Also:      make -C tests/usr_access_rd USRACC_DV_MODE=level  (the old model;
             the fixed RTL must pass under it too)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from dut_presence import rtl_ready
from regmap import AxiLiteMaster

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                        "fpga", "shell", "ip", "usr_access_rd")
NO_RTL = not rtl_ready(_RTL_DIR, ["usr_access_rd.sv"])

# shell_bd.tcl's assignment for usr_access_rd_0 (see fpga/shell/ip/
# usr_access_rd/README.md for why this page and not 0x44A1_001C inside DFXCTL).
USRACC_BASE = 0x44B30000

USRACC_MAGIC = 0x00      # ro
USRACC_VALUE = 0x04      # ro
USRACC_STATUS = 0x08     # ro, bit0 = valid

USRACC_MAGIC_EXPECT = 0x55535241        # "USRA"
USRACC_STATUS_VALID = 1 << 0

# The value the primitive model was told to present, and the value this bench
# asserts. They are the same plusarg by default; `control-value` sets them apart
# and the run MUST go red, which is what makes assertion 1 above mean anything.
_STAMPED = int(os.environ.get("USRACC_STAMPED", "0x01000001"), 0)
_EXPECT = int(os.environ.get("USRACC_EXPECT", str(_STAMPED)), 0)

# Everything is driven at base+offset, exactly as the interconnect presents it.
# NOTE, because it was measured rather than assumed: driving offset WITHOUT the
# base reads the same registers and is therefore NOT a control. LOCAL_ADDR_W
# caps the decode at this block's own 64 KiB page, so araddr[31:16] is ignored
# BY DESIGN -- selecting the page is the interconnect's job. Reproducing bug #1
# needs a mutation of the decode, which is what `make control-decode` does.
_BASE = USRACC_BASE


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)
    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)
    return axi


async def _settle_for_configuration(dut):
    """Wait out the 2-FF synchroniser + stability compare (a few clocks) with
    plenty of margin. In the default `pulse` model the primitive's DATAVALID
    pulse is already over before reset is released -- as on silicon."""
    await ClockCycles(dut.s_axi_aclk, 40)


@cocotb.test(skip=NO_RTL)
async def test_identity_arrives_without_ever_seeing_datavalid(dut):
    """The whole point: the AXSS word the .bit carries is readable by firmware.

    Until this block existed, `coordinator_handle_version()` had nothing to read
    and the `version` verb's cross-check reported "no comparison was made" on
    every board (ba2f4be). The first version of this block then read
    MAGIC=0x55535241, VALUE=0, VALID=0 on silicon (0x3F1A560F, 2026-09-22):
    it waited for DATAVALID, which UG570 Figure 7-9 shows is a one-CFGCLK pulse
    per AXSS write -- and the bitstream's write happens during configuration,
    before this block is out of reset. The primitive model now fires that pulse
    while s_axi_aresetn is still low, exactly like the part, so this test only
    passes for RTL that does not depend on DATAVALID.
    `make control-silicon` runs the old RTL against this model: MUST FAIL.

    There is deliberately no "VALID must be low before the primitive reports"
    check any more: on a configured part the fabric never runs before AXSS is
    loaded, so that window does not exist in silicon.
    """
    axi = await _bring_up(dut)
    await _settle_for_configuration(dut)

    magic, _ = await axi.read(_BASE + USRACC_MAGIC)
    assert magic == USRACC_MAGIC_EXPECT, (
        f"MAGIC read 0x{magic:08X}, expected 0x{USRACC_MAGIC_EXPECT:08X} "
        "-- without this witness firmware cannot tell an absent block "
        "(unmapped page, reads 0) from a fabric honestly stamped 0"
    )

    status, _ = await axi.read(_BASE + USRACC_STATUS)
    assert status & USRACC_STATUS_VALID, (
        "STATUS.VALID low -- the silicon failure of 2026-09-22 (MAGIC ok, "
        "VALUE 0, VALID 0): the capture is waiting for a DATAVALID that fired "
        "during configuration"
    )

    value, _ = await axi.read(_BASE + USRACC_VALUE)
    assert value == _EXPECT, (
        f"VALUE read 0x{value:08X}, but the fabric was stamped 0x{_EXPECT:08X}"
    )


@cocotb.test(skip=NO_RTL)
async def test_identity_is_readable_again_after_a_peripheral_reset(dut):
    """The watchdog reset must not cost the answer to 'which build am I?'.

    `Timebase_WDT_Reset` -> `proc_sys_reset_shell/aux_reset_in` pulses
    `peripheral_aresetn`, which is this block's `s_axi_aresetn` -- and the
    post-mortem read after a watchdog reboot is exactly when somebody needs the
    fabric's identity. The capture is CLEARED by that reset and RE-DERIVED from
    the primitive's DATA within four clocks (AXSS is configuration state; a
    fabric reset does not disturb it, and no DATAVALID pulse is needed), so the
    block can never report a word it has stopped reading, and the answer is
    back long before firmware has finished booting.
    """
    axi = await _bring_up(dut)
    await _settle_for_configuration(dut)
    before, _ = await axi.read(_BASE + USRACC_VALUE)
    assert before == _EXPECT

    # A watchdog-length peripheral reset pulse.
    dut.s_axi_aresetn.value = 0
    await ClockCycles(dut.s_axi_aclk, 8)
    dut.s_axi_aresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 5)
    axi = AxiLiteMaster.from_dut(dut)
    await RisingEdge(dut.s_axi_aclk)

    status, _ = await axi.read(_BASE + USRACC_STATUS)
    assert status & USRACC_STATUS_VALID, (
        "STATUS.VALID did not come back after a peripheral reset -- the shell "
        "cannot say which build it is on exactly the reboot that needs to"
    )
    after, _ = await axi.read(_BASE + USRACC_VALUE)
    assert after == before, (
        f"VALUE changed across a peripheral reset: 0x{before:08X} -> 0x{after:08X}"
    )
    assert after == _EXPECT


@cocotb.test(skip=NO_RTL)
async def test_unmapped_offsets_read_zero_and_writes_are_inert(dut):
    """Read-only means read-only, and the page does not alias.

    A stray write must be ACCEPTED (BRESP=OKAY) and have no effect: an AXI-Lite
    slave that withheld AWREADY on a write would hang the MicroBlaze, which is a
    worse failure than discarding the write. And every offset outside the three
    must read 0 rather than aliasing onto VALUE -- dfx_ctl's RESOLVED ambiguity
    #4 is the precedent (0x20 used to alias onto DECOUPLE).
    """
    axi = await _bring_up(dut)
    await _settle_for_configuration(dut)

    for off in (0x0C, 0x10, 0x20, 0x100, 0x1000):
        val, _ = await axi.read(_BASE + off)
        assert val == 0, f"offset 0x{off:03X} read 0x{val:08X}, expected 0"

    resp = await axi.write(_BASE + USRACC_VALUE, 0xDEADBEEF)
    assert resp == 0, f"a write to a RO register must answer OKAY, got BRESP={resp}"
    val, _ = await axi.read(_BASE + USRACC_VALUE)
    assert val == _EXPECT, (
        f"a write CHANGED the fabric identity: read back 0x{val:08X}"
    )

    resp = await axi.write(_BASE + USRACC_MAGIC, 0x00000000)
    assert resp == 0
    val, _ = await axi.read(_BASE + USRACC_MAGIC)
    assert val == USRACC_MAGIC_EXPECT
