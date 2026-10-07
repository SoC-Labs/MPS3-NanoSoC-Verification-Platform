"""tests/eth_ss_bringup/test_eth_ss_bringup.py

NEW leaf bench (A5 verification-confidence pass). Verifies
`fpga/rp/eth_ss/eth_ss_bringup.sv` — the single-master AHB-Lite
constant-programmer FSM that walks a fixed 12-write bring-up sequence to
configure rm_eth_ss (OpenCores ethmac + HA1588 PTP) once after reset, because
the partition-pin boundary carries no shell<->DUT AHB to program it. Real
sequential logic that had ZERO verification before this bench.

This bench plays the AHB-Lite SLAVE: an always-ready responder (HREADY=1,
HRESP=OKAY) that snoops the write stream, plus a variant that injects an
HRESP=ERROR to check the `errored` telemetry flag.

What this proves:
  1. The FSM reaches `done` (bring-up completes and parks the bus IDLE).
  2. It issues EXACTLY the programmed 12 (addr, data) writes, IN ORDER — the
     ROM in eth_ss_bringup.sv's header table (TX_BD_NUM -> ... -> MODER last).
     MODER being written LAST matters: the MAC must wake up fully configured.
  3. `errored` stays 0 across a clean sequence, and LATCHES if any transfer
     returns HRESP=ERROR (v1 makes no recovery attempt — it just reports).
  4. (2026-09-23) After bring-up it does NOT park: it writes TXCTRL =
     TXPAUSERQ | seq straight away and again every REARM_CYCLES, seq = 1, 2,
     3 ... — the transmit beacon. That each write really puts a frame on the
     wire is proven through the real MAC by the ARM=tx arm
     (test_eth_ss_tx.py); this arm proves the bus-level schedule.

Timing note: the Makefile shrinks START_DELAY_CYCLES/GAP_CYCLES; every check
here is timing-independent (it waits for `done`, not a fixed cycle count).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from dut_presence import rtl_ready

_RTL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fpga", "rp", "eth_ss")
NO_RTL = not rtl_ready(_RTL_DIR, ["eth_ss_bringup.sv"])

HTRANS_NONSEQ = 0b10

# Expected write sequence (addr[15:0], data) — mirrors eth_ss_bringup.sv's
# seq_entry() ROM with the RTL's default programmed-value parameters.
_MODER_VAL       = 0x0001_A423
_INT_MASK_VAL    = 0x0000_001F
_MAC_ADDR0_VAL   = 0x454C_5302
_MAC_ADDR1_VAL   = 0x0000_3253
_RX_BUF_PTR      = 0x0000_0000
_RTC_PERIOD_NS   = 40
_RTC_PERIOD_FRAC = 0

EXPECTED_WRITES = [
    (0x0020, 0x0000_0001),      # TX_BD_NUM
    (0x000C, 0x0000_0015),      # IPGT
    (0x0040, _MAC_ADDR0_VAL),   # MAC_ADDR0
    (0x0044, _MAC_ADDR1_VAL),   # MAC_ADDR1
    (0x0008, _INT_MASK_VAL),    # INT_MASK
    (0x040C, _RX_BUF_PTR),      # RX BD1 pointer
    (0x0408, 0x0000_E000),      # RX BD1 E|IRQ|WRAP
    (0x1020, _RTC_PERIOD_NS),   # PTP period ns
    (0x1024, _RTC_PERIOD_FRAC), # PTP period frac
    (0x1000, 0x0000_0004),      # PTP ctrl: period_ld
    (0x1000, 0x0000_0000),      # PTP ctrl: clear
    (0x0000, _MODER_VAL),       # MODER (enable)
    (0x0024, 0x0000_0004),      # CTRLMODER: TXFLOW (arms the PAUSE transmitter)
]

# Transmit beacon (2026-09-23): TXCTRL @0x50 = TXPAUSERQ(bit 16) | seq.
_TXCTRL = 0x0050
_REARM_CYCLES = int(os.environ.get("REARM_CYCLES", "200"))  # Makefile -pvalue


def _beacon(seq):
    return (_TXCTRL, 0x0001_0000 | (seq & 0xFFFF))


async def _bring_up(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.resetn.value = 0
    dut.hrdata.value = 0
    dut.hready.value = 1        # always-ready slave
    dut.hresp.value = 0         # OKAY
    await Timer(50, units="ns")
    dut.resetn.value = 1
    await RisingEdge(dut.clk)


async def _ahb_snoop(dut, writes, err_on_index=None):
    """Snoop the AHB-Lite write stream as an always-ready slave. Records
    (addr, data) pairs: the address is captured in the address phase
    (HTRANS=NONSEQ), the data one cycle later in the data phase. If
    err_on_index is set, HRESP is pulsed ERROR during that write's data
    phase to exercise the `errored` flag."""
    pend_addr = None
    n = 0
    while True:
        await ReadOnly()
        htrans = int(dut.htrans.value)
        haddr = int(dut.haddr.value)
        hwdata = int(dut.hwdata.value)
        await RisingEdge(dut.clk)   # post-edge writable phase
        if pend_addr is not None:
            writes.append((pend_addr & 0xFFFF, hwdata))
            pend_addr = None
            n += 1
            dut.hresp.value = 0     # clear any injected error after its data phase
        if htrans == HTRANS_NONSEQ:
            pend_addr = haddr
            if err_on_index is not None and n == err_on_index:
                dut.hresp.value = 1  # ERROR asserted for this transfer's data phase


async def _wait_done(dut, timeout_cycles=4000):
    for _ in range(timeout_cycles):
        await ReadOnly()
        done = int(dut.done.value)
        await RisingEdge(dut.clk)
        if done:
            return
    raise TimeoutError("eth_ss_bringup never asserted done")


@cocotb.test(skip=NO_RTL)
async def test_bringup_reaches_done_and_issues_programmed_writes(dut):
    """What this proves: the bring-up FSM completes (done=1), issues exactly
    the 12 programmed writes in order (MODER last), and reports no error
    against a clean always-OKAY slave."""
    writes = []
    await _bring_up(dut)
    cocotb.start_soon(_ahb_snoop(dut, writes))

    await _wait_done(dut)
    assert int(dut.errored.value) == 0, "errored must stay 0 against an always-OKAY slave"

    # `done` rises in the last bring-up write's data phase; the snooper logs
    # that write on the following edge.
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    bringup = writes[:len(EXPECTED_WRITES)]
    assert len(bringup) == len(EXPECTED_WRITES), (
        f"expected {len(EXPECTED_WRITES)} bring-up writes, captured {len(writes)}: {writes}")
    for i, (exp, got) in enumerate(zip(EXPECTED_WRITES, bringup)):
        assert got == exp, (
            f"write #{i}: expected addr={exp[0]:#06x} data={exp[1]:#010x}, "
            f"got addr={got[0]:#06x} data={got[1]:#010x}")

    # MODER (the enable) comes after every configuration register; the only
    # write after it is CTRLMODER, which is itself a flow-control enable.
    moder_at = bringup.index((0x0000, _MODER_VAL))
    assert moder_at == len(EXPECTED_WRITES) - 2 and bringup[-1][0] == 0x0024, (
        "MODER must follow all configuration writes; only CTRLMODER may follow it")


@cocotb.test(skip=NO_RTL)
async def test_transmit_beacon_rearms_every_period(dut):
    """What this proves: after bring-up the FSM writes TXCTRL =
    TXPAUSERQ|seq with seq = 1, 2, 3 ..., the first immediately and each next
    one REARM_CYCLES (+ the write's own few cycles) later, with the bus IDLE in
    between. Against the pre-2026-09-23 FSM (which parks after MODER) this
    FAILS: no write ever reaches TXCTRL."""
    writes = []
    await _bring_up(dut)
    cocotb.start_soon(_ahb_snoop(dut, writes))
    await _wait_done(dut)

    n_beacons = 4
    stamps = []
    cycle = 0
    budget = 200 + (n_beacons + 1) * (_REARM_CYCLES + 50)
    while cycle < budget:
        await ReadOnly()
        nonseq = int(dut.htrans.value) == HTRANS_NONSEQ
        await RisingEdge(dut.clk)
        cycle += 1
        if nonseq:
            stamps.append(cycle)
        beacons = writes[len(EXPECTED_WRITES):]
        if len(beacons) >= n_beacons:
            break

    beacons = writes[len(EXPECTED_WRITES):]
    assert len(beacons) >= n_beacons, (
        f"expected >= {n_beacons} TXCTRL beacon writes after bring-up within "
        f"{budget} cycles, saw {len(beacons)}: {[(hex(a), hex(d)) for a, d in beacons]}")
    for i, got in enumerate(beacons[:n_beacons]):
        assert got == _beacon(i + 1), (
            f"beacon #{i}: expected addr={_beacon(i + 1)[0]:#06x} "
            f"data={_beacon(i + 1)[1]:#010x}, got addr={got[0]:#06x} data={got[1]:#010x}")

    # Period: consecutive beacon address phases are REARM_CYCLES + a few apart.
    gaps = [b - a for a, b in zip(stamps, stamps[1:])][-(n_beacons - 1):]
    for g in gaps:
        assert _REARM_CYCLES <= g <= _REARM_CYCLES + 8, (
            f"beacon spacing {g} cycles, expected REARM_CYCLES={_REARM_CYCLES} (+<=8)")
    dut._log.info(f"beacons {[(hex(a), hex(d)) for a, d in beacons[:n_beacons]]}, gaps {gaps}")


@cocotb.test(skip=NO_RTL)
async def test_hresp_error_latches_errored_flag(dut):
    """What this proves: an HRESP=ERROR on any transfer latches the `errored`
    telemetry flag (v1 makes no recovery attempt — it still reaches done, but
    reports the failure), exercising the S_DATA `if (hresp) errored <= 1`
    path that the clean run never touches."""
    writes = []
    await _bring_up(dut)
    cocotb.start_soon(_ahb_snoop(dut, writes, err_on_index=3))  # 4th write

    await _wait_done(dut)
    assert int(dut.errored.value) == 1, (
        "errored must latch after an HRESP=ERROR on one of the transfers")
    # It still completes the whole sequence (no recovery/abort in v1).
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    assert len(writes) >= len(EXPECTED_WRITES), (
        f"FSM must still issue all writes despite the error; got {len(writes)}")
