"""test_lcd_mirror_e2e.py -- LCDMIR through the REAL panel path
(LCD_MIRROR_FPGA.md §8.3, the "e2e variant"). `make MODE=e2e`.

tb_lcd_mirror_e2e = tests/clcd_kvm_e2e/tb_clcd_kvm_e2e.sv as-is (real
clcd_kvm + tunnel mux + nanosoc_exp_socket + ahb_clcd + clcd_core) with the
snooper tapping the KVM's pad-side outputs, as the mint-4 BD will. The harness
side is kvm_models.HarnessSource (clcd.c's shipped 2/4/4 timing); the DUT side
is the REAL reference accelerator driven over AHB, crossing the real tunnel CDC.

What it proves: the mirror stays pixel-exact across handovers both ways and a
DFX forced revert; every KVM handover's panel reset clears VALID, counts in
RESETS and returns the decoded state to its defaults, while the frame buffer
keeps the previous owner's pixels (O4: HM hatches them via VALID); STATUS.owner
follows owner_o; no guard violation at either source's shipped timing.

The reference is fed what each source PUSHED, with a panel reset at each
handover; the bytes the panel actually saw (tests/clcd/clcd_panel_model.py on
the pads) are checked against that same sequence, so the expectation is not
an assumption about the KVM.
"""
from __future__ import annotations

import pathlib
import sys

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

_HERE = pathlib.Path(__file__).resolve().parent
_TESTS = _HERE.parent
for p in (_TESTS / "common", _TESTS / "clcd", _TESTS / "clcd_kvm", _HERE):
    sys.path.insert(0, str(p))

from regmap import AxiLiteMaster  # noqa: E402
from ahb_lite import AhbLiteMaster  # noqa: E402
from clcd_panel_model import PanelBusModel  # noqa: E402
from kvm_models import HarnessSource  # noqa: E402

import hx8347_gram_model as M  # noqa: E402
import streams  # noqa: E402
from lcdmir_bench import RESETS, STATUS, VALID0, VIOL, Bench  # noqa: E402

# clcd_kvm CSRs (tests/clcd_kvm_e2e/test_clcd_kvm_e2e.py)
K_CTRL, K_PANEL_TMR, K_TIMEOUT, K_DEBOUNCE = 0x00, 0x0C, 0x10, 0x14
K_SRC_SEL, K_TIMEOUT_EN, K_PB_EN, K_SRC_SEL_WE = 1 << 0, 1 << 2, 1 << 3, 1 << 16
K_CTRL_BASE = K_TIMEOUT_EN | K_PB_EN
# ahb_clcd (base 0x6000_0000)
DUT_CTRL, DUT_CMD, DUT_DATA = 0x6000_0000, 0x6000_0004, 0x6000_0008
HARNESS, DUT = 0, 1


class E2EBench(Bench):
    """Bench, re-pointed at the lm_axi_* port; the pads come from the KVM."""

    def __init__(self, dut):
        super().__init__(dut)
        self.owner = HARNESS
        self.expected_pads = []

    async def start(self, clear: bool = False):
        d = self.dut
        d.s_axi_aresetn.value = 0
        d.hresetn.value = 0
        d.rp_resetn.value = 0
        for pre in ("s_axi_", "lm_axi_"):
            for nm in ("awvalid", "wvalid", "bready", "arvalid", "rready",
                       "awaddr", "awprot", "wdata", "wstrb", "araddr", "arprot"):
                getattr(d, f"{pre}{nm}").value = 0
        d.user_npb1.value = 1
        d.decouple_status.value = 0
        d.dump_go.value = 0
        d.dump_base.value = 0
        d.dump_count.value = 0
        self.harness = HarnessSource(d, cs_setup=2, wr_lo=4, wr_hi=4)
        self.harness.idle_now()
        self.ahb = AhbLiteMaster.from_dut(d)
        await ClockCycles(d.s_axi_aclk, 5)
        d.hresetn.value = 1
        await ClockCycles(d.hclk, 4)
        d.s_axi_aresetn.value = 1
        await ClockCycles(d.s_axi_aclk, 4)
        d.rp_resetn.value = 1
        await ClockCycles(d.s_axi_aclk, 4)
        self.kvm = AxiLiteMaster.from_dut(d)
        self.axi = AxiLiteMaster.from_dut(d, prefix="lm_axi_")
        self.panel = PanelBusModel(d)
        cocotb.start_soon(self.panel.run())
        cocotb.start_soon(self.harness.run())
        await self.kvm.write(K_PANEL_TMR, (2 << 16) | 2)   # rst/settle 2 us
        await self.kvm.write(K_TIMEOUT, 500)
        await self.kvm.write(K_DEBOUNCE, 2)
        await self.ahb.write(DUT_CTRL, 1)                  # accelerator enabled
        await ClockCycles(d.s_axi_aclk, 120)
        # seed the retained FB/REGS (see Bench.start)
        words = await self.dump_fb()
        for i, w in enumerate(words):
            self.model.fb[2 * i], self.model.fb[2 * i + 1] = w & 0xFFFF, w >> 16
        regs = await self.dump(0x100, 64)
        for k, w in enumerate(regs):
            for j in range(4):
                self.model.regs[4 * k + j] = (w >> (8 * j)) & 0xFF

    def expected_csrs(self):
        exp = self.model.csrs(viol=0, rds=0, rst_n=1, bl=1, owner=self.owner,
                              viol_sticky=0, rds_sticky=0)
        exp[0x034] = 0x00010202
        return exp

    # ---- sources -----------------------------------------------------------
    async def harness_push(self, pairs):
        self.harness.enqueue(pairs)
        await self.harness.wait_idle(timeout_cycles=20 * len(pairs) + 2000)
        await ClockCycles(self.dut.s_axi_aclk, 20)
        self.model.feed_bytes(pairs)
        self.expected_pads += pairs

    async def dut_push(self, pairs):
        n0 = len(self.panel.strobes)
        for k in range(0, len(pairs), 100):              # ahb_clcd FIFO is 128 deep
            chunk = pairs[k:k + 100]
            for rs, v in chunk:
                await self.ahb.write(DUT_DATA if rs else DUT_CMD, v)
            want = n0 + k + len(chunk)
            for _ in range(200000):
                await RisingEdge(self.dut.s_axi_aclk)
                if len(self.panel.strobes) >= want:
                    break
            else:
                raise AssertionError(f"DUT bytes did not reach the panel ({want})")
        await ClockCycles(self.dut.s_axi_aclk, 60)
        self.model.feed_bytes(pairs)
        self.expected_pads += pairs

    async def flip(self, want):
        await self.kvm.write(K_CTRL, K_CTRL_BASE | (want & 1) | K_SRC_SEL_WE)
        await self.wait_owner(want)

    async def wait_owner(self, want, timeout=40000):
        for _ in range(timeout):
            await RisingEdge(self.dut.s_axi_aclk)
            if int(self.dut.owner_o.value) == want:
                break
        else:
            raise AssertionError(f"owner never became {want}")
        await ClockCycles(self.dut.s_axi_aclk, 20)
        self.owner = want
        self.model.panel_reset()          # every handover hard-resets the panel

    def check_pads(self, ctx):
        got = self.panel.sequence
        assert got == self.expected_pads, (
            f"{ctx}: the bytes the panel saw differ from what the sources pushed "
            f"({len(got)} vs {len(self.expected_pads)})")
        bad = [s for s in self.panel.strobes if not (s.pd_stable and s.cs_low_throughout)]
        assert not bad, f"{ctx}: malformed 8080 cycles at the pads: {bad[:4]}"


def _harness_parts():
    boot = streams.record_harness(streams.work_dir())[0]
    init = streams.init_pairs()
    stream = [(e[1], e[2]) for e in boot.events if e[0] == "B"]
    assert stream[:len(init)] == init, "the recorded boot stream must start with the init table"
    cells = stream[len(init):]
    return init, cells[0:273 * 2], cells[273 * 2:273 * 3]


def _dut_pairs(x0, y0, nx, ny, base):
    pix = [(base + 0x0821 * i) & 0xFFFF for i in range(nx * ny)]
    return ([(0, 0x16), (1, 0x20), (0, 0x17), (1, 0x05)]
            + M.window_pairs(x0, x0 + nx - 1, y0, y0 + ny - 1) + [(0, 0x22)]
            + M.pixel_pairs(pix))


@cocotb.test()
async def test_e2e_handover_both_ways(dut):
    b = E2EBench(dut)
    await b.start()
    init, cells2, cell3 = _harness_parts()

    await b.harness_push(init + cells2)
    got = await b.check_all("e2e: harness owns (init + 2 cells)")
    assert got[STATUS] & 0b111 == 0b011 and got[VIOL] == 0

    await b.flip(DUT)
    got = await b.check_csrs("e2e: just handed to the DUT")
    assert got[RESETS] == 1 and not any(got[VALID0 + 4 * i] for i in range(10))
    assert got[STATUS] & (1 << 2), "STATUS.owner must follow owner_o"

    await b.dut_push(_dut_pairs(200, 100, 8, 4, 0x1234))
    got = await b.check_all("e2e: the DUT's REAL ahb_clcd drew through the tunnel")
    assert got[VIOL] == 0

    await b.flip(HARNESS)
    await b.harness_push(init + cell3)
    got = await b.check_all("e2e: harness regained and redrew")
    assert got[RESETS] == 2 and got[VIOL] == 0
    b.check_pads("e2e handover both ways")
    dut._log.info(f"e2e: {len(b.expected_pads)} bytes across 2 handovers, "
                  f"pixel-exact, RESETS={got[RESETS]}, VIOL=0")


@cocotb.test()
async def test_e2e_forced_revert(dut):
    b = E2EBench(dut)
    await b.start()
    init, cells2, cell3 = _harness_parts()
    await b.flip(DUT)
    await b.dut_push(_dut_pairs(16, 32, 16, 16, 0xF00F))
    await b.check_all("e2e: DUT drew a tile")
    # a partial reconfiguration begins: the KVM force-reverts to the harness
    dut.decouple_status.value = 1
    await b.wait_owner(HARNESS)
    got = await b.check_csrs("e2e: forced revert")
    assert got[RESETS] == 2 and not any(got[VALID0 + 4 * i] for i in range(10))
    assert not got[STATUS] & (1 << 2)
    dut.decouple_status.value = 0
    await ClockCycles(dut.s_axi_aclk, 10)
    await b.harness_push(init + cell3)
    got = await b.check_all("e2e: harness redrew after the forced revert")
    assert got[VIOL] == 0
    b.check_pads("e2e forced revert")
