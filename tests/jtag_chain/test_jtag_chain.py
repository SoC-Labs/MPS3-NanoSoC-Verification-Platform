"""jtag_chain — TWO TAPs on ONE 4-wire partition wire-set.

The gate for the IICE RM's rebase onto the then-fielded boundary (mint 0xA8C1C535).
The old design gave Identify's soft TAP the four `swd_*` partition pins
outright; that group no longer exists, and the one debug wire-set the boundary
has is already carrying the DUT's CoreSight SoC-400 SWJ-DP. So
fpga/rp/nanosoc_iice/rp_nanosoc_iice_shim.sv puts the two TAPs in an IEEE
1149.1 DAISY CHAIN, and this bench drives that chain through the REAL shim.

WHAT IS REAL HERE AND WHAT IS A MODEL
  REAL   the shim under test (compiled from fpga/rp/, not copied); the Arm
         SoC-400 SWJ-DP + AHB-AP + dbg bridge + Cortex-M0 behind the jtag_*
         legs; the OpenOCD remote_bitbang byte semantics of the driver.
  MODEL  the Identify soft TAP (tests/jtag_chain/iice_soft_tap_model.sv). Its
         IR length (5) and IDCODE (0x1063E4CD) are read off Identify's own
         device table, not invented -- see that file's header.

CHAIN ORDER AND THE DECLARATION CONVENTION -- the thing to get right
  Fabric (shim default, IICE_TAP_NEAREST_TDI=1):

      shell TDI -> [Identify soft TAP] -> [SoC-400 SWJ-DP] -> shell TDO

  Host arithmetic. In a chain the bits shifted onto TDI FIRST travel furthest
  and come to rest in the device nearest TDO; symmetrically the bits read back
  FIRST are that device's capture. So both the TDI stream and the TDO stream
  are ordered "device nearest TDO first, LSB-first within each device".

  That is also exactly OpenOCD's `jtag newtap` declaration order -- first
  declared == nearest TDO -- which is not folklore but code:
    jtag_tap_add() appends to the tap-list tail      core.c:213-224
    the tap list is walked head-first into fields[]  drivers/driver.c:152
    jtag_build_buffer() lays fields in from bit 0    commands.c:215-221
    bitbang.c shifts buffer bit 0 out FIRST          drivers/bitbang.c:215-227
  and stated by worked example in doc/openocd.texi:13861-13895.

  So CHAIN below is [DAP, IICE] -- DAP first because it is nearest TDO -- and
  host/openocd/nanosoc_iice_chain.cfg declares them in that same order.

ACCEPTANCE FLOW
  test_chain_idcodes                  both IDCODEs, in the right positions
  test_chain_ir_length                the classic ones-then-zero probe -> 9
  test_dap_halt_through_chain         M0 DHCSR S_HALT=1 with the IICE in BYPASS
  test_iice_shift_with_dap_in_bypass  an Identify-TAP DR write+readback with the
                                      DAP in BYPASS
  test_control_wrong_chain_order      CONTROL: decode the same scan with the
                                      declaration order swapped -> both IDCODEs
                                      wrong
  test_control_single_tap_arithmetic  CONTROL: the legacy single-TAP scan (no
                                      BYPASS padding) does NOT halt the core
  test_dut_reset_drops_the_debug_powerup
                                      the operational hazard, MEASURED: a DUT
                                      reset does NOT disturb the chain, but it
                                      does clear the DP power-up latch
"""

import cocotb
from cocotb.triggers import RisingEdge, Timer


# ---------------------------------------------------------------------------
# The chain, in OpenOCD declaration order == nearest TDO FIRST.
# ---------------------------------------------------------------------------
class Tap:
    def __init__(self, name, irlen, ir_bypass, idcode):
        self.name = name
        self.irlen = irlen
        self.ir_bypass = ir_bypass
        self.idcode = idcode


# SWJ-DP: IRLEN 4, BYPASS 0xF, TAP IDCODE from cxdapswjdp JTAGDP_DEVICEID_IR4
# (the same constant tests/jtag_dap_bringup asserts).
DAP = Tap("nanosoc.cpu", irlen=4, ir_bypass=0xF, idcode=0x6BA00477)

# Identify soft TAP: IRLEN 5, BYPASS 0b11111, IDCODE from
# .../SFPGA_2022.09-SP2/identify/lib/share/contrib/syn_idcodes.tcl:741-742.
IICE = Tap("nanosoc.iice", irlen=5, ir_bypass=0x1F, idcode=0x1063E4CD)

CHAIN = [DAP, IICE]          # index 0 == nearest TDO == first `jtag newtap`
IDX_DAP, IDX_IICE = 0, 1
CHAIN_IRLEN = sum(t.irlen for t in CHAIN)     # 9

# ---- JTAG-DP IR opcodes (IRLEN=4; cxdapswjdp_jtag_dp_constants.v) ----------
IR_ABORT = 0x8
IR_DPACC = 0xA
IR_APACC = 0xB
IR_IDCODE_DAP = 0xE
IR_BYPASS_DAP = 0xF

# ---- Identify soft-TAP opcodes (identify_debugger_shell symbol strings) ----
IR_IICE_VENDORID = 0x00      # 32-bit IDCODE DR
IR_IICE_HCR = 0x02           # 16-bit control DR
IR_IICE_IDHW = 0x03          # 32-bit data DR
IR_IICE_BYPASS = 0x1F

# ---- DP register A[3:2] selects (JTAG-DP) ---------------------------------
DP_RDBUFF = 0b00
DP_CTRLSTAT = 0b01
DP_SELECT = 0b10

# ---- AHB-AP register word addresses = {APBANKSEL[3:0], A[3:2]} ------------
AP_CSW = 0x00
AP_TAR = 0x01
AP_DRW = 0x03
AP_IDR = 0x3F

ID_REG_AHBAP = 0x84770001

# ---- Cortex-M0 PPB debug registers ----------------------------------------
DHCSR_ADDR = 0xE000EDF0
DBGKEY = 0xA05F0000
C_DEBUGEN = 1 << 0
C_HALT = 1 << 1
S_HALT = 1 << 17
CSW_WORD = 0x00000002

CSYSPWRUPACK = 1 << 31
CSYSPWRUPREQ = 1 << 30
CDBGPWRUPACK = 1 << 29
CDBGPWRUPREQ = 1 << 28
POWERUP_REQ = CSYSPWRUPREQ | CDBGPWRUPREQ

ACK_OK = 0b010
ACK_WAIT = 0b001

TCK_HALF_NS = 50      # 10 MHz TCK, 10x slower than the 100 MHz dut_clk so the
                      # SWJ-DP's own CDC settles. (The FABRIC's ceiling is
                      # lower: Identify auto-constrains its soft TAP to 250 ns /
                      # 4 MHz, which is why host/openocd/nanosoc_iice_chain.cfg
                      # caps `adapter speed`. A model has no such limit.)


def _resolved(sig):
    try:
        return int(sig.value)
    except ValueError:
        return None


class DapError(Exception):
    pass


class ChainJtag:
    """OpenOCD remote_bitbang byte layer + a CHAIN-AWARE 1149.1 scan layer.

    Every TCK edge and TDO sample goes through _emit_* which appends the literal
    remote_bitbang byte to self.byte_log, so byte_stats() reports exactly the
    wire traffic the shell's firmware jtag_server must interpret -- the same
    byte layer tests/jtag_dap_bringup validates, now carrying a two-device chain.

    `chain` is ordered NEAREST TDO FIRST (see the module docstring). Everything
    below is generic in that list: nothing hard-codes "two devices", so the
    swapped-order control is a different list and not different code.
    """

    def __init__(self, dut, chain=None):
        self.dut = dut
        self.chain = list(chain if chain is not None else CHAIN)
        self.byte_log = bytearray()
        self.cur_ir = None          # tuple of per-device IR values, or None
        self.cur_select = None

    # ---- remote_bitbang byte layer ----------------------------------------
    async def _emit_write(self, tck, tms, tdi):
        # OpenOCD bitbang: char '0' + (tck<<2 | tms<<1 | tdi)
        self.byte_log.append(ord('0') + ((tck & 1) << 2 | (tms & 1) << 1 | (tdi & 1)))
        self.dut.jtag_tck.value = tck & 1
        self.dut.jtag_tms.value = tms & 1
        self.dut.jtag_tdi.value = tdi & 1
        await Timer(TCK_HALF_NS, unit="ns")

    async def _emit_read(self):
        self.byte_log.append(ord('R'))
        val = _resolved(self.dut.jtag_tdo)
        return 0 if val is None else (val & 1)

    def byte_stats(self):
        w = sum(1 for b in self.byte_log if ord('0') <= b <= ord('7'))
        r = self.byte_log.count(ord('R'))
        return f"remote_bitbang bytes: writes={w} reads(R)={r} total={len(self.byte_log)}"

    # ---- one JTAG bit: sample TDO while TCK is low, TAP consumes on the rise
    async def _clock_bit(self, tms, tdi, read=False):
        await self._emit_write(0, tms, tdi)
        tdo = await self._emit_read() if read else 0
        await self._emit_write(1, tms, tdi)
        return tdo

    async def run_test_idle(self, n):
        for _ in range(n):
            await self._clock_bit(0, 0)

    # ---- raw scans (chain-agnostic: value is the WHOLE concatenated word) --
    async def _raw_shift_ir(self, value, nbits):
        for tms in (1, 1, 0, 0):            # RTI->SelDR->SelIR->CapIR->ShIR
            await self._clock_bit(tms, 0)
        out = 0
        for i in range(nbits):
            last = i == nbits - 1
            b = await self._clock_bit(1 if last else 0, (value >> i) & 1, read=True)
            out |= (b & 1) << i
        await self._clock_bit(1, 0)         # Exit1-IR -> Update-IR (latches)
        await self._clock_bit(0, 0)         # Update-IR -> Run-Test/Idle
        return out

    async def _raw_shift_dr(self, value, nbits, read=True):
        for tms in (1, 0, 0):               # RTI->SelDR->CapDR->ShDR
            await self._clock_bit(tms, 0)
        out = 0
        for i in range(nbits):
            last = i == nbits - 1
            b = await self._clock_bit(1 if last else 0, (value >> i) & 1, read=read)
            out |= (b & 1) << i
        await self._clock_bit(1, 0)         # Exit1-DR -> Update-DR
        await self._clock_bit(0, 0)         # Update-DR -> Run-Test/Idle
        return out

    # ---- chain layer -------------------------------------------------------
    def _ir_offsets(self):
        off, acc = [], 0
        for t in self.chain:
            off.append(acc)
            acc += t.irlen
        return off, acc

    def pack(self, widths, values):
        """Concatenate per-device values, device[0] (nearest TDO) at bit 0."""
        word, shift = 0, 0
        for w, v in zip(widths, values):
            word |= (v & ((1 << w) - 1)) << shift
            shift += w
        return word, shift

    def unpack(self, widths, word):
        out, shift = [], 0
        for w in widths:
            out.append((word >> shift) & ((1 << w) - 1))
            shift += w
        return out

    async def scan_ir(self, active, ir_value):
        """IR scan with `active` addressed and every other device in BYPASS."""
        widths = [t.irlen for t in self.chain]
        values = [t.ir_bypass for t in self.chain]
        values[active] = ir_value
        if self.cur_ir == tuple(values):
            return
        word, nbits = self.pack(widths, values)
        await self._raw_shift_ir(word, nbits)
        self.cur_ir = tuple(values)

    async def scan_dr(self, active, value, nbits):
        """DR scan with `active` carrying nbits and every other device 1 bypass bit."""
        widths = [1] * len(self.chain)
        widths[active] = nbits
        values = [0] * len(self.chain)
        values[active] = value
        word, total = self.pack(widths, values)
        out = await self._raw_shift_dr(word, total)
        return self.unpack(widths, out)[active]

    async def reset_tap(self):
        """Test-Logic-Reset the WHOLE chain: TMS=1 x 6, then one TMS=0 to RTI.

        There is no TRST anywhere in this chain -- the RM straps dap_ntrst high
        (rp_nanosoc_wrapper.sv:526) and Identify's soft TAP has only four pins --
        so this is the only reset the host has. TLR reloads IDCODE in every
        compliant TAP, which is what makes the auto-detect scan below work.
        """
        self.dut.jtag_tck.value = 0
        self.dut.jtag_tms.value = 1
        self.dut.jtag_tdi.value = 0
        await Timer(TCK_HALF_NS, unit="ns")
        for _ in range(6):
            await self._clock_bit(1, 0)
        await self._clock_bit(0, 0)
        self.cur_ir = None          # TLR reloaded IDCODE everywhere
        self.cur_select = None

    async def read_idcodes(self):
        """One DR scan of 32 bits per device, straight out of Test-Logic-Reset.

        This is what both OpenOCD's jtag_examine_chain() and Identify's
        auto-detect do, and the ONLY reason it works is that TLR selects IDCODE
        in every device at once -- which it can only do because TMS is shared.
        """
        widths = [32] * len(self.chain)
        word, total = self.pack(widths, [0] * len(self.chain))
        out = await self._raw_shift_dr(word, total)
        return self.unpack(widths, out)

    async def _probe_length(self, flush, limit):
        """The classic chain-length probe, run in whichever Shift state the
        caller has already navigated to.

        Fill the whole concatenated shift register with ones, then feed zeros
        and count how many TCK it takes for the first zero to reach TDO. That
        count IS the total register length, whatever the chain is made of --
        which is precisely why it is worth measuring rather than asserting: it
        needs no knowledge of how many devices there are.

        Note the phase. _clock_bit() samples TDO BEFORE its rising edge, so the
        read taken on iteration i is the register's LSB after i shifts; the
        first iteration that reads 0 therefore reports the length directly.
        """
        for _ in range(flush):
            await self._clock_bit(0, 1)       # fill with ones
        for i in range(limit):
            b = await self._clock_bit(0, 0, read=True)   # now feed zeros
            if b == 0:
                return i
        return -1

    async def probe_ir_length(self, flush=64, limit=64):
        """Total IR length across the whole chain.

        Ends in Test-Logic-Reset, NOT in whatever instruction the probe's zeros
        happened to leave behind: shifting zeros means the IR is junk at
        Update-IR (0x0 is an unimplemented opcode in the JTAG-DP and is IDCODE
        in the soft TAP), and leaving that latched would make the next test
        depend on this one. TLR reloads IDCODE everywhere, deterministically.
        """
        for tms in (1, 1, 0, 0):              # RTI->SelDR->SelIR->CapIR->ShIR
            await self._clock_bit(tms, 0)
        n = await self._probe_length(flush, limit)
        await self._clock_bit(1, 0)           # Exit1-IR
        await self._clock_bit(1, 0)           # Update-IR (junk, discarded below)
        await self.reset_tap()                # -> TLR: IDCODE everywhere
        return n

    async def probe_dr_length(self, flush=64, limit=64):
        """Total DR length across the whole chain, for whatever instruction each
        device currently holds. With everything in BYPASS that is one bit per
        device; with a TAP back on its IDCODE register it is 31 bits longer --
        which is exactly how the DUT-reset hazard is measured.

        Leaves the IR alone (a DR scan cannot change it), so the caller's chosen
        instructions survive.
        """
        for tms in (1, 0, 0):                 # RTI->SelDR->CapDR->ShDR
            await self._clock_bit(tms, 0)
        n = await self._probe_length(flush, limit)
        await self._clock_bit(1, 0)           # Exit1-DR
        await self._clock_bit(1, 0)           # Update-DR
        await self._clock_bit(0, 0)           # RTI
        return n

    async def set_all_bypass(self):
        """Put EVERY device in the chain into BYPASS with one IR scan.

        Not the same as the probe above: this latches the real all-ones opcode
        (0xF in the JTAG-DP, 0b11111 in the soft TAP) so the chain's DR is
        exactly one bit per device and stays that way.
        """
        widths = [t.irlen for t in self.chain]
        values = [t.ir_bypass for t in self.chain]
        word, nbits = self.pack(widths, values)
        await self._raw_shift_ir(word, nbits)
        self.cur_ir = tuple(values)

    # ---- ADIv5 JTAG-DP, through the chain ---------------------------------
    async def _xfer(self, ir, a32, rnw, data=0):
        await self.scan_ir(IDX_DAP, ir)
        val = ((data & 0xFFFFFFFF) << 3) | ((a32 & 3) << 1) | (rnw & 1)
        out = await self.scan_dr(IDX_DAP, val, 35)
        await self.run_test_idle(2)
        return (out & 0x7), ((out >> 3) & 0xFFFFFFFF)

    async def _xfer_retry(self, ir, a32, rnw, data=0, tries=256):
        for _ in range(tries):
            ack, rdata = await self._xfer(ir, a32, rnw, data)
            if ack == ACK_OK:
                return rdata
            if ack == ACK_WAIT:
                await self.run_test_idle(6)
                continue
            raise DapError(
                f"unexpected JTAG-DP ACK={ack:#05b} (ir={ir:#x} a32={a32:#x} rnw={rnw})")
        raise DapError(f"JTAG-DP stuck in WAIT (ir={ir:#x} a32={a32:#x} rnw={rnw})")

    async def power_up(self):
        await self._xfer_retry(IR_DPACC, DP_CTRLSTAT, rnw=0, data=POWERUP_REQ)

    async def read_ctrlstat(self):
        await self._xfer_retry(IR_DPACC, DP_CTRLSTAT, rnw=1)
        return await self._xfer_retry(IR_DPACC, DP_CTRLSTAT, rnw=1)

    async def _select(self, apbank, apsel=0):
        sel = ((apsel & 0xFF) << 24) | ((apbank & 0xF) << 4)
        if sel != self.cur_select:
            await self._xfer_retry(IR_DPACC, DP_SELECT, rnw=0, data=sel)
            self.cur_select = sel

    async def ap_write(self, apreg6, value):
        await self._select((apreg6 >> 2) & 0xF)
        await self._xfer_retry(IR_APACC, apreg6 & 3, rnw=0, data=value)

    async def ap_read(self, apreg6):
        await self._select((apreg6 >> 2) & 0xF)
        await self._xfer_retry(IR_APACC, apreg6 & 3, rnw=1)
        return await self._xfer_retry(IR_DPACC, DP_RDBUFF, rnw=1)

    async def mem_write(self, addr, value):
        await self.ap_write(AP_CSW, CSW_WORD)
        await self.ap_write(AP_TAR, addr)
        await self.ap_write(AP_DRW, value)

    async def mem_read(self, addr):
        await self.ap_write(AP_CSW, CSW_WORD)
        await self.ap_write(AP_TAR, addr)
        return await self.ap_read(AP_DRW)

    # ---- the Identify side -------------------------------------------------
    async def iice_write_idhw(self, value):
        """Address the Identify TAP's 32-bit IDHW_CHAIN data register with the
        DAP sitting in BYPASS, and leave the written value latched at Update-DR.
        """
        await self.scan_ir(IDX_IICE, IR_IICE_IDHW)
        return await self.scan_dr(IDX_IICE, value, 32)


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------
async def _wait_clock(dut):
    for _ in range(4):
        await RisingEdge(dut.cpu0_sys_hclk)


async def _boot_core(dut):
    """Pulse the three contract resets so the RM re-boots into the spin loop.

    NOTE this also pulses dap_npotrst (the RM ties it to the ANDed system
    reset), so it resets the SWJ-DP's TAP -- which is why every test re-runs
    reset_tap() and power_up() afterwards, and why the hazard test exists.
    """
    dut.dut_resetn.value = 0
    dut.rp_resetn.value = 0
    dut.dbg_resetn.value = 0
    for _ in range(10):
        await RisingEdge(dut.cpu0_sys_hclk)
    dut.dut_resetn.value = 1
    dut.rp_resetn.value = 1
    dut.dbg_resetn.value = 1
    for _ in range(400):
        await RisingEdge(dut.cpu0_sys_hclk)
        if _resolved(dut.cpu0_sys_hresetn) == 1:
            break
    else:
        raise AssertionError("cpu0_sys_hresetn never deasserted")
    for _ in range(60):
        await RisingEdge(dut.cpu0_sys_hclk)


async def _fresh(dut, powered=True, boot=True, chain=None):
    await _wait_clock(dut)
    if boot:
        await _boot_core(dut)
    drv = ChainJtag(dut, chain=chain)
    await drv.reset_tap()
    if powered:
        await drv.power_up()
    return drv


def _assert_shipped_order(dut):
    """Every positive test below assumes the SHIPPED fabric order. `make
    control-order` rebuilds with it flipped; refuse to silently reinterpret."""
    assert _resolved(dut.chain_iice_nearest_tdi) == 1, (
        "this simv was built with CHAIN_IICE_NEAREST_TDI=0 (the fabric-side "
        "control). The positive tests assume the shipped order.")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_chain_idcodes(dut):
    """Both TAPs answer, in the positions the host config declares.

    THE headline: one Test-Logic-Reset and one 64-bit DR scan pull two IDCODEs
    off a wire-set that used to carry one. Position is the whole point -- the
    first 32 bits belong to the device nearest TDO, which is the first
    `jtag newtap` in host/openocd/nanosoc_iice_chain.cfg.
    """
    drv = await _fresh(dut, powered=False, boot=True)
    ids = await drv.read_idcodes()
    dut._log.info("chain IDCODEs (nearest TDO first): %s  (%s)",
                  [f"0x{v:08X}" for v in ids], drv.byte_stats())
    assert ids[IDX_DAP] == DAP.idcode, (
        f"position 0 (nearest TDO, `{DAP.name}`) read 0x{ids[IDX_DAP]:08X}, "
        f"expected the SWJ-DP's 0x{DAP.idcode:08X}")
    assert ids[IDX_IICE] == IICE.idcode, (
        f"position 1 (nearest TDI, `{IICE.name}`) read 0x{ids[IDX_IICE]:08X}, "
        f"expected the Identify soft TAP's 0x{IICE.idcode:08X}")


@cocotb.test()
async def test_chain_ir_length(dut):
    """Total IR length is 9 = the SWJ-DP's 4 plus the soft TAP's 5.

    This is the measurement OpenOCD makes implicitly on every IR scan and the
    one Identify reports as "The actual IR chain size differs from the defined
    size" when a cfg is wrong. Proving it here means the two `-irlen` values in
    the shipped config are not a guess.
    """
    _assert_shipped_order(dut)
    drv = await _fresh(dut, powered=False, boot=True)
    n = await drv.probe_ir_length()
    dut._log.info("total chain IR length = %d (DAP %d + IICE %d)",
                  n, DAP.irlen, IICE.irlen)
    assert n == CHAIN_IRLEN, (
        f"IR-length probe returned {n}, expected {CHAIN_IRLEN}. A wrong total "
        f"means at least one -irlen in nanosoc_iice_chain.cfg is wrong and "
        f"every IR scan is misaligned.")


@cocotb.test()
async def test_dap_halt_through_chain(dut):
    """THE gate: halt the real Cortex-M0 with a second TAP in the serial path.

    Same DHCSR write tests/jtag_dap_bringup proves on a single-TAP wire-set, now
    with 5 extra IR bits and 1 extra DR bit of Identify BYPASS in front of every
    scan. If the chain arithmetic is wrong by one bit anywhere, this cannot pass.
    """
    _assert_shipped_order(dut)
    drv = await _fresh(dut, powered=True, boot=True)

    idr = await drv.ap_read(AP_IDR)
    assert idr == ID_REG_AHBAP, (
        f"AHB-AP IDR 0x{idr:08X} != 0x{ID_REG_AHBAP:08X} through the chain")

    pre = await drv.mem_read(DHCSR_ADDR)
    dut._log.info("DHCSR pre-halt = 0x%08X (S_HALT=%d)", pre, (pre >> 17) & 1)

    await drv.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN | C_HALT)
    for _ in range(20):
        await RisingEdge(dut.cpu0_sys_hclk)

    dhcsr = await drv.mem_read(DHCSR_ADDR)
    dut._log.info("DHCSR post-halt = 0x%08X (S_HALT=%d)  %s",
                  dhcsr, (dhcsr >> 17) & 1, drv.byte_stats())
    assert dhcsr & S_HALT, (
        f"core did not halt through the chain: DHCSR=0x{dhcsr:08X}")


@cocotb.test()
async def test_iice_shift_with_dap_in_bypass(dut):
    """The other direction: an Identify-TAP DR shift with the DAP in BYPASS.

    Writes a pattern into the soft TAP's 32-bit IDHW_CHAIN register and scans it
    back, then cross-checks the TAP's own latched value. Two independent
    confirmations, because a readback alone could be the same bits going round
    the loop rather than through a register.
    """
    _assert_shipped_order(dut)
    drv = await _fresh(dut, powered=False, boot=True)

    pattern = 0xA5C3_5A3C
    await drv.iice_write_idhw(pattern)          # Update-DR latches it
    await Timer(TCK_HALF_NS, unit="ns")

    latched = _resolved(dut.iice_idhw)
    ir_now = _resolved(dut.iice_ir)
    dut._log.info("IICE IR=0x%02X IDHW latched = 0x%08X", ir_now or 0, latched or 0)
    assert ir_now == IR_IICE_IDHW, (
        f"soft TAP IR is 0x{ir_now:02X}, expected IDHW_CHAIN 0x{IR_IICE_IDHW:02X} "
        f"-- the 9-bit IR scan did not land the right 5 bits in the right device")
    assert latched == pattern, (
        f"soft TAP IDHW latched 0x{latched:08X}, wrote 0x{pattern:08X}")

    readback = await drv.iice_write_idhw(0x0000_0000)
    dut._log.info("IICE IDHW scan-back = 0x%08X  (%s)", readback, drv.byte_stats())
    assert readback == pattern, (
        f"soft TAP IDHW read back 0x{readback:08X}, expected 0x{pattern:08X} "
        f"-- the DAP's single BYPASS bit is not being accounted for")


@cocotb.test()
async def test_control_wrong_chain_order(dut):
    """CONTROL: swap the declaration order in the CONFIG and the IDCODEs break.

    This is the failure an operator produces by writing the two `jtag newtap`
    lines the wrong way round -- OpenOCD's first-declared TAP is the one nearest
    TDO, and getting that backwards is the single most likely cfg mistake here.
    The scan is byte-identical; only the host's interpretation changes. If this
    test could not fail, test_chain_idcodes would not be reading
    position-dependent data and would prove nothing.
    """
    _assert_shipped_order(dut)
    swapped = [IICE, DAP]                       # deliberately wrong order
    drv = await _fresh(dut, powered=False, boot=True, chain=swapped)
    ids = await drv.read_idcodes()
    dut._log.info("swapped-order decode: %s", [f"0x{v:08X}" for v in ids])

    # Position 0 is declared as the Identify TAP but physically holds the DAP.
    assert ids[0] != IICE.idcode, (
        "the swapped declaration read the Identify IDCODE at position 0 -- "
        "the chain is NOT position-sensitive and every positive result here is "
        "an artefact")
    assert ids[1] != DAP.idcode, (
        "the swapped declaration read the SWJ-DP IDCODE at position 1")
    assert ids[0] == DAP.idcode and ids[1] == IICE.idcode, (
        f"swapped decode gave 0x{ids[0]:08X}/0x{ids[1]:08X}; the physical order "
        f"is fixed, so it must simply be the shipped pair read into the wrong "
        f"slots")


@cocotb.test()
async def test_control_single_tap_arithmetic(dut):
    """CONTROL: the LEGACY single-TAP scan does not halt the core.

    host/openocd/nanosoc_mps3_jtag.cfg declares one TAP. Pointed at a chained
    RM it emits 4-bit IR scans and 35-bit DR scans with no BYPASS padding, so
    every scan is short by the Identify TAP's 5 IR bits and 1 DR bit. The core
    must NOT halt -- which is both the control for the positive test above and
    the documented reason the chained RM needs its own config file.
    """
    _assert_shipped_order(dut)
    drv = await _fresh(dut, powered=False, boot=True)

    # A one-device "chain": exactly what the legacy cfg believes.
    solo = ChainJtag(dut, chain=[DAP])
    solo.byte_log = drv.byte_log
    await solo.reset_tap()

    halted = False
    try:
        await solo.power_up()
        await solo.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN | C_HALT)
        for _ in range(20):
            await RisingEdge(dut.cpu0_sys_hclk)
        # Read it back with the CORRECT chain arithmetic, so the verdict cannot
        # itself be a victim of the broken one.
        good = await _fresh(dut, powered=True, boot=False)
        val = await good.mem_read(DHCSR_ADDR)
        halted = bool(val & S_HALT)
        dut._log.info("DHCSR after single-TAP 'halt' = 0x%08X (S_HALT=%d)",
                      val, (val >> 17) & 1)
    except DapError as e:
        dut._log.info("single-TAP arithmetic correctly failed on the chain: %s", e)

    assert not halted, (
        "the core halted from single-TAP scans on a two-TAP chain -- the "
        "BYPASS padding is not load-bearing, so the positive halt result "
        "would be untrustworthy")


@cocotb.test()
async def test_dut_reset_drops_the_debug_powerup(dut):
    """THE OPERATIONAL HAZARD, measured -- and it is NOT the one you expect.

    rp_nanosoc_wrapper.sv:527 ties dap_npotrst to the DUT's system reset, so
    pulsing dut_resetn resets the SWJ-DP. The obvious worry is that this returns
    the TAP to Test-Logic-Reset, where 1149.1 reloads IDCODE and the DAP's DR
    width would jump 1 -> 32 under the host's feet, silently misaligning any
    Identify scan in flight by 31 bits.

    MEASURED, AND THAT IS NOT WHAT HAPPENS. This test first asserted exactly
    that and failed: the DR length is unchanged across the pulse. Reading the
    Arm RTL says why -- in cxdapswjdp's JTAG protocol block the TAP controller
    state and the IR sit in the THREE `negedge ntrst` blocks, while `npotrst`
    resets the other seventeen, which are the DP's own registers. `ntrst` is
    strapped high in the RM, so a DUT reset never touches the TAP.

    What a DUT reset DOES destroy is the debug POWER-UP handshake: CTRL/STAT's
    CDBGPWRUPREQ/CSYSPWRUPREQ are among the flops npotrst clears. Every AP
    access after the pulse is therefore gated off, while the chain still scans
    perfectly and every IDCODE still reads correctly -- a fault that looks like
    "the DAP went deaf" rather than like a reset.

    So the operational rule is NOT "re-scan the chain". It is: after any
    dut_resetn pulse, re-run the DP power-up before touching the AP. Both halves
    are asserted below, including that the chain shape is untouched, so if a
    future DAP DOES reset its TAP this test fails and says so.
    """
    _assert_shipped_order(dut)
    drv = await _fresh(dut, powered=True, boot=True)

    stat_before = await drv.read_ctrlstat()
    dut._log.info("CTRL/STAT before the pulse = 0x%08X", stat_before)
    assert stat_before & CDBGPWRUPACK, (
        f"debug power-up did not come up at all: 0x{stat_before:08X}")

    await drv.set_all_bypass()
    len_before = await drv.probe_dr_length()
    dut._log.info("chain DR length with both TAPs in BYPASS = %d", len_before)
    assert len_before == len(CHAIN), (
        f"DR-length probe returned {len_before} with both TAPs in BYPASS, "
        f"expected {len(CHAIN)} -- one bit per device")

    # Pulse ONLY dut_resetn. The host has not touched TCK or TMS.
    dut.dut_resetn.value = 0
    for _ in range(10):
        await RisingEdge(dut.cpu0_sys_hclk)
    dut.dut_resetn.value = 1
    for _ in range(200):
        await RisingEdge(dut.cpu0_sys_hclk)

    len_after = await drv.probe_dr_length()
    dut._log.info("chain DR length after the dut_resetn pulse = %d", len_after)
    assert len_after == len_before, (
        f"the chain's DR length changed from {len_before} to {len_after} across "
        f"a dut_resetn pulse. This bench and "
        f"docs/planning/IICE_JTAG_CHAIN.md both state that it does NOT -- if "
        f"that has changed, the host must re-scan the chain after every DUT "
        f"reset, and the doc needs rewriting, not this assertion.")

    stat_after = await drv.read_ctrlstat()
    dut._log.info("CTRL/STAT after the pulse = 0x%08X  (%s)",
                  stat_after, drv.byte_stats())
    assert (stat_after & CDBGPWRUPREQ) == 0, (
        f"CDBGPWRUPREQ survived a dut_resetn pulse (0x{stat_after:08X}). If the "
        f"power-up latch is NOT cleared, the 're-run power-up after a DUT "
        f"reset' rule is unnecessary -- but so is this whole test.")

    await drv.power_up()
    stat_re = await drv.read_ctrlstat()
    dut._log.info("CTRL/STAT after re-powering = 0x%08X", stat_re)
    assert stat_re & CDBGPWRUPACK, (
        f"re-running the power-up did not restore debug power: 0x{stat_re:08X} "
        f"-- the documented recovery does not work")
