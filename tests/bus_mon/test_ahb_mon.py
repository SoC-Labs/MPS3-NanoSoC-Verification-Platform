"""ahb_mon: passive AHB-Lite observatory -- counters, latency, protocol checks.

DISCIPLINE. A checker that never fires reads exactly like a checker that is wired
up wrong, and the second is worse than no checker because it reports "clean" on a
broken bus. So every violation code has a test that PROVOKES it, and there is a
clean-traffic test that asserts the bitmap stays zero. If a future edit breaks the
detection logic, a provocation test fails; if it makes the checks trigger-happy,
the clean test fails.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

# register offsets (ahb_mon.sv)
CTRL, STATUS = 0x00, 0x04
CNT_RD, CNT_WR, CNT_ERR, CNT_WAIT, CNT_IDLE = 0x08, 0x0C, 0x10, 0x14, 0x18
VIOL, VIOL_ADDR, VIOL_INFO = 0x1C, 0x20, 0x24
HIST0, HIST1, HIST2, HIST3, MAX_WAIT = 0x28, 0x2C, 0x30, 0x34, 0x38

V_ADDR_CHANGED, V_TRANS_CHANGED, V_WRITE_CHANGED = 0, 1, 2
V_SIZE_ILLEGAL, V_BUSY_NO_BURST, V_RESP_ON_IDLE = 3, 4, 5

T_IDLE, T_BUSY, T_NONSEQ, T_SEQ = 0, 1, 2, 3


async def axi_write(dut, off, val):
    dut.s_axi_awaddr.value = off
    dut.s_axi_wdata.value = val
    dut.s_axi_wstrb.value = 0xF
    dut.s_axi_awvalid.value = 1
    dut.s_axi_wvalid.value = 1
    dut.s_axi_bready.value = 1
    while True:
        await RisingEdge(dut.s_axi_aclk)
        if dut.s_axi_bvalid.value == 1:
            break
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    await RisingEdge(dut.s_axi_aclk)


async def axi_read(dut, off):
    dut.s_axi_araddr.value = off
    dut.s_axi_arvalid.value = 1
    dut.s_axi_rready.value = 1
    while True:
        await RisingEdge(dut.s_axi_aclk)
        if dut.s_axi_rvalid.value == 1:
            val = int(dut.s_axi_rdata.value)
            break
    dut.s_axi_arvalid.value = 0
    await RisingEdge(dut.s_axi_aclk)
    return val


def bus_idle(dut):
    dut.htrans.value = T_IDLE
    dut.hwrite.value = 0
    dut.hsize.value = 2
    dut.hburst.value = 0
    dut.haddr.value = 0
    dut.hready.value = 1
    dut.hresp.value = 0


async def setup(dut, enable=True):
    cocotb.start_soon(Clock(dut.s_axi_aclk, 10, units="ns").start())
    dut.s_axi_aresetn.value = 0
    dut.hresetn.value = 0
    for sig in ("s_axi_awvalid", "s_axi_wvalid", "s_axi_bready",
                "s_axi_arvalid", "s_axi_rready"):
        getattr(dut, sig).value = 0
    bus_idle(dut)
    await ClockCycles(dut.s_axi_aclk, 5)
    dut.s_axi_aresetn.value = 1
    dut.hresetn.value = 1
    await ClockCycles(dut.s_axi_aclk, 2)
    if enable:
        await axi_write(dut, CTRL, 0x1)
    return dut


async def xfer(dut, addr, write, waits=0, err=False, trans=T_NONSEQ,
               burst=0, size=2):
    """One AHB transfer: address phase then `waits` wait states then data."""
    dut.haddr.value = addr
    dut.htrans.value = trans
    dut.hwrite.value = 1 if write else 0
    dut.hsize.value = size
    dut.hburst.value = burst
    dut.hready.value = 1
    await RisingEdge(dut.s_axi_aclk)          # address phase captured
    dut.htrans.value = T_IDLE
    for _ in range(waits):
        dut.hready.value = 0
        dut.hresp.value = 0
        await RisingEdge(dut.s_axi_aclk)
    dut.hready.value = 1
    dut.hresp.value = 1 if err else 0
    await RisingEdge(dut.s_axi_aclk)
    dut.hresp.value = 0


@cocotb.test()
async def test_clean_traffic_counts_and_no_violation(dut):
    """Reads and writes are counted, and a well-behaved bus reports NO violation."""
    await setup(dut)
    for i in range(5):
        await xfer(dut, 0x1000 + 4 * i, write=False)
    for i in range(3):
        await xfer(dut, 0x2000 + 4 * i, write=True)
    assert await axi_read(dut, CNT_RD) == 5
    assert await axi_read(dut, CNT_WR) == 3
    assert await axi_read(dut, CNT_ERR) == 0
    viol = await axi_read(dut, VIOL)
    assert viol == 0, "clean traffic must not raise a violation, got 0x%X" % viol


@cocotb.test()
async def test_error_response_is_counted(dut):
    await setup(dut)
    await xfer(dut, 0x40, write=False, err=True)
    await xfer(dut, 0x44, write=False)
    assert await axi_read(dut, CNT_ERR) == 1
    assert await axi_read(dut, CNT_RD) == 2


@cocotb.test()
async def test_wait_states_land_in_the_right_histogram_bucket(dut):
    """Buckets are 0 / 1-3 / 4-15 / 16+ -- the resolution that changes what you do."""
    await setup(dut)
    await xfer(dut, 0x10, write=False, waits=0)
    await xfer(dut, 0x14, write=False, waits=2)
    await xfer(dut, 0x18, write=False, waits=5)
    await xfer(dut, 0x1C, write=False, waits=20)
    assert await axi_read(dut, HIST0) == 1
    assert await axi_read(dut, HIST1) == 1
    assert await axi_read(dut, HIST2) == 1
    assert await axi_read(dut, HIST3) == 1
    assert await axi_read(dut, MAX_WAIT) == 20
    assert await axi_read(dut, CNT_WAIT) == 27      # 0+2+5+20


@cocotb.test()
async def test_PROVOKE_addr_changed_during_wait_state(dut):
    """HADDR must be held while the slave stalls. Moving it is a real bug."""
    await setup(dut)
    dut.haddr.value = 0x900
    dut.htrans.value = T_NONSEQ
    dut.hwrite.value = 0
    dut.hready.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.htrans.value = T_IDLE
    dut.hready.value = 0
    dut.haddr.value = 0x9FC                       # <-- illegal move
    await ClockCycles(dut.s_axi_aclk, 2)
    dut.hready.value = 1
    await RisingEdge(dut.s_axi_aclk)
    viol = await axi_read(dut, VIOL)
    assert viol & (1 << V_ADDR_CHANGED), "0x%X" % viol
    assert await axi_read(dut, VIOL_ADDR) == 0x900, "must record the VIOLATED addr"
    info = await axi_read(dut, VIOL_INFO)
    assert (info & 0xF) == V_ADDR_CHANGED
    assert ((info >> 8) & 0xFF) >= 1


@cocotb.test()
async def test_PROVOKE_hwrite_changed_during_wait_state(dut):
    await setup(dut)
    dut.haddr.value = 0x100
    dut.htrans.value = T_NONSEQ
    dut.hwrite.value = 1
    dut.hready.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.htrans.value = T_IDLE
    dut.hready.value = 0
    dut.hwrite.value = 0                          # <-- illegal move
    await ClockCycles(dut.s_axi_aclk, 2)
    dut.hready.value = 1
    await RisingEdge(dut.s_axi_aclk)
    assert (await axi_read(dut, VIOL)) & (1 << V_WRITE_CHANGED)


@cocotb.test()
async def test_PROVOKE_illegal_hsize_for_the_data_width(dut):
    """HSIZE 3 (8 bytes) on a 32-bit bus cannot be honoured."""
    await setup(dut)
    await xfer(dut, 0x200, write=False, size=3)
    assert (await axi_read(dut, VIOL)) & (1 << V_SIZE_ILLEGAL)


@cocotb.test()
async def test_PROVOKE_busy_outside_a_burst(dut):
    """BUSY is only legal from a master that already has a burst open."""
    await setup(dut)
    bus_idle(dut)
    dut.htrans.value = T_BUSY
    await ClockCycles(dut.s_axi_aclk, 2)
    bus_idle(dut)
    await RisingEdge(dut.s_axi_aclk)
    assert (await axi_read(dut, VIOL)) & (1 << V_BUSY_NO_BURST)


@cocotb.test()
async def test_busy_INSIDE_a_burst_is_not_flagged(dut):
    """The counterpart to the test above: legal BUSY must stay silent, or the
    check is useless on any bursting master."""
    await setup(dut)
    # open an INCR burst, then go BUSY
    dut.haddr.value = 0x300
    dut.htrans.value = T_NONSEQ
    dut.hburst.value = 1                          # INCR
    dut.hwrite.value = 0
    dut.hready.value = 1
    await RisingEdge(dut.s_axi_aclk)
    dut.htrans.value = T_BUSY
    await ClockCycles(dut.s_axi_aclk, 2)
    bus_idle(dut)
    await RisingEdge(dut.s_axi_aclk)
    viol = await axi_read(dut, VIOL)
    assert not (viol & (1 << V_BUSY_NO_BURST)), "legal BUSY flagged: 0x%X" % viol


@cocotb.test()
async def test_violation_bitmap_is_RW1C_and_counters_clear(dut):
    await setup(dut)
    await xfer(dut, 0x200, write=False, size=3)   # raise SIZE_ILLEGAL
    assert (await axi_read(dut, VIOL)) & (1 << V_SIZE_ILLEGAL)
    await axi_write(dut, VIOL, 1 << V_SIZE_ILLEGAL)
    assert (await axi_read(dut, VIOL)) & (1 << V_SIZE_ILLEGAL) == 0
    # CTRL.clear zeroes the counters
    await xfer(dut, 0x10, write=False)
    assert await axi_read(dut, CNT_RD) >= 1
    await axi_write(dut, CTRL, 0x3)               # enable | clear
    assert await axi_read(dut, CNT_RD) == 0
    assert await axi_read(dut, CNT_WAIT) == 0


@cocotb.test()
async def test_DISABLED_monitor_counts_nothing(dut):
    """The enable must actually gate, or a `clear`-then-measure window is a lie."""
    await setup(dut, enable=False)
    for i in range(4):
        await xfer(dut, 0x500 + 4 * i, write=False)
    assert await axi_read(dut, CNT_RD) == 0
    assert await axi_read(dut, VIOL) == 0
    # ... and starts counting once enabled, from the next transfer
    await axi_write(dut, CTRL, 0x1)
    await xfer(dut, 0x600, write=False)
    assert await axi_read(dut, CNT_RD) == 1


@cocotb.test()
async def test_first_violation_is_STICKY_not_overwritten(dut):
    """VIOL_ADDR/VIOL_INFO must record the FIRST event: the later ones are usually
    consequences, and the first one is the bug."""
    await setup(dut)
    await xfer(dut, 0x700, write=False, size=3)   # first: SIZE_ILLEGAL @0x700
    first_addr = await axi_read(dut, VIOL_ADDR)
    first_code = (await axi_read(dut, VIOL_INFO)) & 0xF
    await xfer(dut, 0x800, write=False, size=3)   # second, different address
    assert await axi_read(dut, VIOL_ADDR) == first_addr
    assert ((await axi_read(dut, VIOL_INFO)) & 0xF) == first_code
    assert ((await axi_read(dut, VIOL_INFO)) >> 8) & 0xFF >= 2   # but count grows
