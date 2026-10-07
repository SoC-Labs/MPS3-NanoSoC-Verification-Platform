"""tests/sim_smoke/test_sim_smoke.py

Harness sanity bench (W-SIM, I24): proves cocotb + the simulator + the
license environment end-to-end against the ~10-line counter in this same
directory — no dependency on any fpga/ RTL, no skip-gating (this bench is
ALWAYS ready by construction; tests/common/list_benches.py registers it
with its RTL dir pointing right here).

If this bench fails, fix the environment (repo-root set_env.sh) before
debugging any real per-block bench. Written against cocotb 2.0.1 (the
pinned version — see set_env.sh's decision note).
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    await Timer(35, units="ns")
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_counts_up_when_enabled(dut):
    """Reset -> 0; enable for N cycles -> count == N."""
    await _bring_up(dut)
    assert int(dut.count.value) == 0, "count must reset to 0"

    dut.en.value = 1
    await ClockCycles(dut.clk, 10)
    dut.en.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.count.value) == 10, f"expected 10, got {int(dut.count.value)}"


@cocotb.test()
async def test_holds_when_disabled_and_async_reset_clears(dut):
    """en=0 holds the value; a mid-run rst_n drop clears it asynchronously."""
    await _bring_up(dut)
    dut.en.value = 1
    await ClockCycles(dut.clk, 5)
    dut.en.value = 0
    await ClockCycles(dut.clk, 3)
    assert int(dut.count.value) == 5, "count must hold while en=0"

    dut.rst_n.value = 0
    await Timer(1, units="ns")  # async clear -- no clock edge needed
    assert int(dut.count.value) == 0, "rst_n must clear count asynchronously"
    dut.rst_n.value = 1
