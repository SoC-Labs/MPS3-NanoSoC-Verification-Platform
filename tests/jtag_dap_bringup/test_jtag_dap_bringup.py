"""P2 SIM GATE — host-JTAG -> SWJ-DP serial-decode -> DAP -> Cortex-M0.

The ONE segment nothing currently proves (SOC400_BASELINE_INTEGRATION.md
"The gate"): the serial line reaching the DP through cxdapswjdp +
nanosoc_swj_dp_gate. Every committed multicore bench either drives the
PARALLEL DP-bus (coresight_soc400_dap_to_core) or ties the SWD/JTAG pins idle
(coresight_soc400_swj_smoke). This bench wiggles the four JTAG wires
(TCK/TMS/TDI/TDO) through the SWJ-DP and halts a real Cortex-M0 to DHCSR
S_HALT.

Driver = OpenOCD `remote_bitbang` byte semantics. The JTAG primitives are
built ON TOP of the exact ASCII byte codes OpenOCD's remote_bitbang adapter
emits ('0'..'7' = {tck,tms,tdi}, 'R' = sample TDO, 'r'..'u' = trst/srst),
and a server-side decoder turns each byte into a pin wiggle. Exercising that
byte layer here also validates the mapping the shell's fw jtag_server must
implement (host/openocd/nanosoc_mps3_jtag.cfg, TRANSPORT_MODE=rbb).

Acceptance flow (the plan's list):
  test_tap_reset_idcode       TAP-reset -> read IDCODE == 0x6BA00477
  test_dpacc_powerup_status   power up (CTRL/STAT) -> DPACC read status,
                              CDBGPWRUPACK|CSYSPWRUPACK asserted (JTAG-DP has
                              no DPIDR register; CTRL/STAT is the DPACC-read
                              identity/health proof)
  test_ahb_ap_idr             select AHB-AP -> read AP IDR == 0x84770001,
                              log ROM BASE
  test_halt_via_dhcsr         write DHCSR = DBGKEY|C_DEBUGEN|C_HALT
                              -> read DHCSR, S_HALT (bit17) == 1     <-- THE gate
  test_halt_resume_rehalt     halt -> resume (S_HALT=0) -> re-halt (S_HALT=1)
  test_cpuid_anti_bleed       CPUID == 0x410CC200 while the core SPINS, NOT
                              the 0xE7FEE7FE fetch word (unconditional-capture
                              bridge readback-regression guard)
  test_negative_wrong_ir      a halt write issued in BYPASS (wrong IR) must
                              NOT halt the core
  test_negative_no_powerup    without the CTRL/STAT power-up handshake the
                              core must NOT halt (CDBGPWRUPREQ reads back 0)
"""

import cocotb
from cocotb.triggers import RisingEdge, Timer


# ── JTAG-DP IR opcodes (IRLEN=4; from cxdapswjdp_jtag_dp_constants.v:
#    JTAGDP_x = {1'b1, 3-bit code}) ──────────────────────────────────────────
IR_ABORT  = 0x8
IR_DPACC  = 0xA
IR_APACC  = 0xB
IR_IDCODE = 0xE
IR_BYPASS = 0xF

# ── DP register A[3:2] selects (JTAG-DP) ────────────────────────────────────
DP_RDBUFF   = 0b00   # read buffer (rdmux RBUF); side-effect free
DP_CTRLSTAT = 0b01   # CTRL/STAT (power-up req/ack, sticky)
DP_SELECT   = 0b10   # SELECT (APSEL / APBANKSEL)

# ── AHB-AP register word addresses = {APBANKSEL[3:0], A[3:2]} (dapcaddr[7:2]).
#    Matches the proven coresight_soc400_dap_to_core parallel bench. ─────────
AP_CSW  = 0x00   # AP 0x00
AP_TAR  = 0x01   # AP 0x04
AP_DRW  = 0x03   # AP 0x0C
AP_BASE = 0x3E   # AP 0xF8 (bank 0xF, A=10)
AP_IDR  = 0x3F   # AP 0xFC (bank 0xF, A=11)

ID_REG_AHBAP  = 0x84770001
# tb_top.sv's AP_ROMBASEADDR override: the CoreSight ROM table base the AHB-AP
# reports in its BASE register. Must equal the tb_top parameter.
EXPECTED_AP_ROMBASE = 0xA0000003
TAP_IDCODE    = 0x6BA00477      # cxdapswjdp JTAGDP_DEVICEID_IR4
EXPECTED_CPUID_M0 = 0x410CC200  # Cortex-M0 r0p0
SPIN_LOOP_FETCH_WORD = 0xE7FEE7FE

# ── Cortex-M0 PPB debug registers (AHB-AP rewrites 0xE0.. -> 0xA0..) ────────
CPUID_ADDR = 0xE000ED00
AIRCR_ADDR = 0xE000ED0C
DHCSR_ADDR = 0xE000EDF0

DBGKEY    = 0xA05F0000
C_DEBUGEN = 1 << 0
C_HALT    = 1 << 1
S_HALT    = 1 << 17

CSW_WORD = 0x00000002   # 32-bit access (proven value from dap_to_core)

# ── CTRL/STAT (JTAG-DP MUXSTAT readback bit positions, verified vs
#    cxdapswjdp_jtag_dp_protocol.v) ─────────────────────────────────────────
CSYSPWRUPACK = 1 << 31
CSYSPWRUPREQ = 1 << 30
CDBGPWRUPACK = 1 << 29
CDBGPWRUPREQ = 1 << 28
POWERUP_REQ  = CSYSPWRUPREQ | CDBGPWRUPREQ   # 0x50000000

# JTAG-DP ACK (assembled LSB-first from the 35-bit scan tail):
ACK_OK   = 0b010   # 2 — OK/FAULT(=OK for JTAG)
ACK_WAIT = 0b001   # 1 — retry

HCLK_PERIOD_NS = 10   # 100 MHz
TCK_HALF_NS    = 50   # 10 MHz TCK — 10x slower than HCLK so the SWJ-DP CDC settles


def _resolved(sig):
    try:
        return int(sig.value)
    except ValueError:
        return None


class DapError(Exception):
    pass


class RemoteBitbangJtag:
    """OpenOCD remote_bitbang byte layer + a server-side pin decoder, with
    JTAG-TAP and ADIv5 JTAG-DP transaction layers on top.

    Every TCK edge/TDO sample goes through _emit_* which appends the literal
    remote_bitbang byte to self.byte_log, so byte_stats() reports exactly the
    wire traffic the shell's jtag_server must interpret.
    """

    def __init__(self, dut):
        self.dut = dut
        self.byte_log = bytearray()
        self.cur_ir = None
        self.cur_select = None

    # ── remote_bitbang byte layer (server side wiggles the DUT pins) ────────
    async def _emit_write(self, tck, tms, tdi):
        # OpenOCD: char '0' + (tck<<2 | tms<<1 | tdi)
        self.byte_log.append(ord('0') + ((tck & 1) << 2 | (tms & 1) << 1 | (tdi & 1)))
        self.dut.jtag_tck.value = tck & 1
        self.dut.jtag_tms.value = tms & 1
        self.dut.jtag_tdi.value = tdi & 1
        await Timer(TCK_HALF_NS, unit="ns")

    async def _emit_read(self):
        # OpenOCD 'R' samples TDO at the current (TCK-low) instant.
        self.byte_log.append(ord('R'))
        val = _resolved(self.dut.jtag_tdo)
        return 0 if val is None else (val & 1)

    async def _emit_reset(self, trst, srst):
        # OpenOCD: char 'r' + (trst<<1 | srst). nTRST is active-low.
        self.byte_log.append(ord('r') + ((trst & 1) << 1 | (srst & 1)))
        self.dut.jtag_ntrst.value = 0 if trst else 1
        await Timer(TCK_HALF_NS, unit="ns")

    def byte_stats(self):
        w = sum(1 for b in self.byte_log if ord('0') <= b <= ord('7'))
        r = self.byte_log.count(ord('R'))
        rs = sum(1 for b in self.byte_log if ord('r') <= b <= ord('u'))
        return f"remote_bitbang bytes: writes={w} reads(R)={r} resets(r..u)={rs} total={len(self.byte_log)}"

    # ── JTAG bit primitive: clock one bit, sample TDO while TCK low ─────────
    async def _clock_bit(self, tms, tdi, read=False):
        await self._emit_write(0, tms, tdi)          # falling edge -> TDO updates
        tdo = await self._emit_read() if read else 0
        await self._emit_write(1, tms, tdi)          # rising edge -> TAP samples TMS/TDI
        return tdo

    async def run_test_idle(self, n):
        for _ in range(n):
            await self._clock_bit(0, 0)

    # ── TAP navigation (paths verified against the cxdapswjdp next-state
    #    table); every scan starts and ends in Run-Test/Idle ────────────────
    async def shift_ir(self, value, nbits=4):
        for tms in (1, 1, 0, 0):                     # RTI->SDR->SIR->CIR->SHIR
            await self._clock_bit(tms, 0)
        for i in range(nbits):
            last = i == nbits - 1
            await self._clock_bit(1 if last else 0, (value >> i) & 1)
        await self._clock_bit(1, 0)                  # E1I->UIR (latches IR)
        await self._clock_bit(0, 0)                  # UIR->RTI

    async def shift_dr(self, value, nbits, read=True):
        for tms in (1, 0, 0):                        # RTI->SDR->CDR->SHD
            await self._clock_bit(tms, 0)
        out = 0
        for i in range(nbits):
            last = i == nbits - 1
            b = await self._clock_bit(1 if last else 0, (value >> i) & 1, read=read)
            out |= (b & 1) << i
        await self._clock_bit(1, 0)                  # E1D->UDR
        await self._clock_bit(0, 0)                  # UDR->RTI
        return out

    async def set_ir(self, ir):
        if self.cur_ir != ir:
            await self.shift_ir(ir, 4)
            self.cur_ir = ir

    # ── power-on / TAP reset ────────────────────────────────────────────────
    async def reset_tap(self):
        """External nPOTRST + nTRST pulse, then TMS=1 x6 to Test-Logic-Reset.

        nPOTRST clears the SWJ-DP serial regs (incl. the power-up latch), so a
        power_up() is required after this. Demonstrates a probe re-attach.
        """
        self.dut.jtag_tck.value = 0
        self.dut.jtag_tms.value = 1
        self.dut.jtag_tdi.value = 0
        self.dut.jtag_npotrst.value = 0
        await self._emit_reset(trst=1, srst=0)       # 'r'+2 -> nTRST asserted
        await Timer(200, unit="ns")
        self.dut.jtag_npotrst.value = 1
        await Timer(50, unit="ns")
        await self._emit_reset(trst=0, srst=0)       # 'r' -> nTRST released
        await Timer(50, unit="ns")
        for _ in range(6):
            await self._clock_bit(1, 0)              # -> Test-Logic-Reset
        await self._clock_bit(0, 0)                  # -> Run-Test/Idle
        self.cur_ir = IR_IDCODE                      # TLR reloads IDCODE
        self.cur_select = None

    # ── IDCODE ──────────────────────────────────────────────────────────────
    async def read_idcode(self):
        await self.set_ir(IR_IDCODE)
        return await self.shift_dr(0, 32) & 0xFFFFFFFF

    # ── raw DP/AP scan with WAIT retry ─────────────────────────────────────
    async def _xfer(self, ir, a32, rnw, data=0):
        await self.set_ir(ir)
        val = ((data & 0xFFFFFFFF) << 3) | ((a32 & 3) << 1) | (rnw & 1)
        out = await self.shift_dr(val, 35)
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
            raise DapError(f"unexpected JTAG-DP ACK={ack:#05b} (ir={ir:#x} a32={a32:#x} rnw={rnw})")
        raise DapError(f"JTAG-DP stuck in WAIT (ir={ir:#x} a32={a32:#x} rnw={rnw})")

    # ── DP-level ────────────────────────────────────────────────────────────
    async def power_up(self):
        await self._xfer_retry(IR_DPACC, DP_CTRLSTAT, rnw=0, data=POWERUP_REQ)

    async def read_ctrlstat(self):
        # DPACC read is posted; first read primes rdmux=STAT, second returns it.
        await self._xfer_retry(IR_DPACC, DP_CTRLSTAT, rnw=1)
        return await self._xfer_retry(IR_DPACC, DP_CTRLSTAT, rnw=1)

    async def _select(self, apbank, apsel=0):
        sel = ((apsel & 0xFF) << 24) | ((apbank & 0xF) << 4)
        if sel != self.cur_select:
            await self._xfer_retry(IR_DPACC, DP_SELECT, rnw=0, data=sel)
            self.cur_select = sel

    # ── AP-level (posted read flushed via DP RDBUFF; transapdp==AP at that
    #    CDR returns busrdata directly) ───────────────────────────────────────
    async def ap_write(self, apreg6, value):
        await self._select((apreg6 >> 2) & 0xF)
        await self._xfer_retry(IR_APACC, apreg6 & 3, rnw=0, data=value)

    async def ap_read(self, apreg6):
        await self._select((apreg6 >> 2) & 0xF)
        await self._xfer_retry(IR_APACC, apreg6 & 3, rnw=1)          # post AP read
        return await self._xfer_retry(IR_DPACC, DP_RDBUFF, rnw=1)    # flush result

    # ── memory / PPB access through the AHB-AP ─────────────────────────────
    async def mem_write(self, addr, value):
        await self.ap_write(AP_CSW, CSW_WORD)
        await self.ap_write(AP_TAR, addr)
        await self.ap_write(AP_DRW, value)

    async def mem_read(self, addr):
        await self.ap_write(AP_CSW, CSW_WORD)
        await self.ap_write(AP_TAR, addr)
        return await self.ap_read(AP_DRW)


# ─────────────────────────────────────────────────────────────────────────────
# Test harness helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _wait_clock(dut):
    # tb_top self-generates sys_fclk (always block); just wait for it to run.
    for _ in range(4):
        await RisingEdge(dut.cpu0_sys_hclk)


async def _boot_core(dut):
    """Pulse sys_sysresetn so the core re-boots into the spin loop; wait for
    HRESETn to come back up via the slcorem0 PRMU. Does NOT touch nPOTRST, so
    a live DP link would survive it (the SYSRESETREQ-survival property)."""
    dut.sys_sysresetn.value = 0
    for _ in range(10):
        await RisingEdge(dut.cpu0_sys_hclk)
    dut.sys_sysresetn.value = 1
    for _ in range(400):
        await RisingEdge(dut.cpu0_sys_hclk)
        if _resolved(dut.cpu0_sys_hresetn) == 1:
            break
    else:
        raise AssertionError("cpu0_sys_hresetn never deasserted")
    for _ in range(60):                              # let the core reach the spin loop
        await RisingEdge(dut.cpu0_sys_hclk)


async def _fresh(dut, powered=True, boot=True):
    await _wait_clock(dut)
    if boot:
        await _boot_core(dut)
    drv = RemoteBitbangJtag(dut)
    await drv.reset_tap()
    if powered:
        await drv.power_up()
    return drv


# ─────────────────────────────────────────────────────────────────────────────
# Tests — the acceptance flow
# ─────────────────────────────────────────────────────────────────────────────
@cocotb.test()
async def test_tap_reset_idcode(dut):
    """TAP-reset over the serial pins, then read the 32-bit TAP IDCODE.

    Proves the JTAG state machine inside cxdapswjdp decodes TCK/TMS and shifts
    TDO correctly — the front half of the serial-decode segment.
    """
    drv = await _fresh(dut, powered=False, boot=True)
    idcode = await drv.read_idcode()
    dut._log.info("TAP IDCODE = 0x%08X (%s)", idcode, drv.byte_stats())
    assert idcode == TAP_IDCODE, (
        f"IDCODE 0x{idcode:08X} != 0x{TAP_IDCODE:08X} — SWJ-DP JTAG decode wrong"
    )


@cocotb.test()
async def test_dpacc_powerup_status(dut):
    """DPACC path: write CTRL/STAT power-up, read status back.

    JTAG-DP has no DPIDR register (that is an SW-DP concept; the TAP IDCODE is
    the JTAG identity). The equivalent DPACC-read proof is CTRL/STAT: after the
    power-up handshake CDBGPWRUPACK|CSYSPWRUPACK must read as asserted, which
    proves a full DPACC write+read round-trips through the SWJ-DP DP bus.
    """
    drv = await _fresh(dut, powered=True, boot=True)
    stat = await drv.read_ctrlstat()
    dut._log.info("CTRL/STAT = 0x%08X", stat)
    assert stat & CDBGPWRUPREQ, f"CDBGPWRUPREQ not latched: 0x{stat:08X}"
    assert stat & CSYSPWRUPREQ, f"CSYSPWRUPREQ not latched: 0x{stat:08X}"
    assert stat & CDBGPWRUPACK, f"CDBGPWRUPACK not asserted: 0x{stat:08X}"
    assert stat & CSYSPWRUPACK, f"CSYSPWRUPACK not asserted: 0x{stat:08X}"


@cocotb.test()
async def test_ahb_ap_idr(dut):
    """Select the AHB-AP and read its IDR (and log ROM BASE).

    IDR is AP-internal (no AHB traffic), so this isolates the DP->AP->DP
    round-trip through the serial front end.
    """
    drv = await _fresh(dut, powered=True, boot=True)
    idr = await drv.ap_read(AP_IDR)
    base = await drv.ap_read(AP_BASE)
    dut._log.info("AHB-AP IDR = 0x%08X  ROM BASE = 0x%08X", idr, base)
    assert idr == ID_REG_AHBAP, f"AP IDR 0x{idr:08X} != 0x{ID_REG_AHBAP:08X}"
    # BASE is the ONLY externally-readable value that is a pure function of a
    # tb_top parameter override, so asserting it is what makes those overrides
    # load-bearing. It was only logged before, and that is exactly how tb_top
    # kept four DEAD parameter names (CPU0_DBG_BASE / CPU1_DBG_BASE /
    # AP0_ROMBASEADDR / AP1_ROMBASEADDR, from the pre-NUM_AP _multi block) after
    # the tech-block move: VCS says "Attempting to override undefined parameter
    # ... will ignore it" as a WARNING, the AP silently took its 0xF000_0003
    # default, and every test still passed. A wrong ROM table base on real
    # silicon is a debugger that cannot enumerate the CoreSight ROM.
    assert base == EXPECTED_AP_ROMBASE, (
        f"AHB-AP BASE 0x{base:08X} != 0x{EXPECTED_AP_ROMBASE:08X} -- the "
        f"AP_ROMBASEADDR override in tb_top.sv did not take (VCS demotes an "
        f"override of a parameter the module does not have to a WARNING)")


@cocotb.test()
async def test_halt_via_dhcsr(dut):
    """THE gate: halt the Cortex-M0 over serial JTAG and read S_HALT=1.

    Full path exercised: TCK/TMS/TDI -> cxdapswjdp -> nanosoc_swj_dp_gate ->
    DP bus -> cxdapahbap -> xlate/arb -> nanosoc_dbg_ahb_bridge (unconditional
    capture) -> slcorem0 DBGAHB -> M0 DHCSR.
    """
    drv = await _fresh(dut, powered=True, boot=True)

    # Sanity: core is running before we halt it.
    pre = await drv.mem_read(DHCSR_ADDR)
    dut._log.info("DHCSR pre-halt = 0x%08X (S_HALT=%d)", pre, (pre >> 17) & 1)

    await drv.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN | C_HALT)
    for _ in range(20):
        await RisingEdge(dut.cpu0_sys_hclk)

    dhcsr = await drv.mem_read(DHCSR_ADDR)
    dut._log.info("DHCSR post-halt = 0x%08X (S_HALT=%d)  %s",
                  dhcsr, (dhcsr >> 17) & 1, drv.byte_stats())
    assert dhcsr & S_HALT, (
        f"core did not halt: DHCSR=0x{dhcsr:08X}, S_HALT not set — "
        f"serial-JTAG halt path broken"
    )


@cocotb.test()
async def test_halt_resume_rehalt(dut):
    """halt -> S_HALT=1 -> resume -> S_HALT=0 -> re-halt -> S_HALT=1.

    Confirms the debug write path is not one-shot (regression for any
    C_HALT-clear / resume->halt loss over the serial link).
    """
    drv = await _fresh(dut, powered=True, boot=True)

    await drv.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN | C_HALT)
    for _ in range(20):
        await RisingEdge(dut.cpu0_sys_hclk)
    d1 = await drv.mem_read(DHCSR_ADDR)
    assert d1 & S_HALT, f"halt failed: DHCSR=0x{d1:08X}"

    await drv.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN)     # resume
    for _ in range(20):
        await RisingEdge(dut.cpu0_sys_hclk)
    d2 = await drv.mem_read(DHCSR_ADDR)
    assert (d2 & S_HALT) == 0, f"still halted after resume: DHCSR=0x{d2:08X}"

    await drv.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN | C_HALT)  # re-halt
    for _ in range(20):
        await RisingEdge(dut.cpu0_sys_hclk)
    d3 = await drv.mem_read(DHCSR_ADDR)
    dut._log.info("halt=0x%08X resume=0x%08X rehalt=0x%08X", d1, d2, d3)
    assert d3 & S_HALT, f"re-halt failed: DHCSR=0x{d3:08X}"


@cocotb.test()
async def test_cpuid_anti_bleed(dut):
    """Readback-regression guard for the unconditional-capture bridge.

    With the core actively fetching 0xE7FEE7FE, read CPUID via the AHB-AP. It
    must return 0x410CC200, NOT the spin-loop fetch word — the 0x..e7fe
    data-bleed the micropython poll-gated bridge reopens (plan §"reject the
    single-AP variant"). Read while RUNNING (not halted), which is exactly the
    window that exposed the bleed on FPGA.
    """
    drv = await _fresh(dut, powered=True, boot=True)
    cpuid = await drv.mem_read(CPUID_ADDR)
    dut._log.info("CPUID (running) = 0x%08X", cpuid)
    assert cpuid != SPIN_LOOP_FETCH_WORD, (
        f"CPUID readback 0x{cpuid:08X} == spin-loop fetch word — bridge "
        f"data-bleed regression (ST_CAPTURE must not poll DBGAHB_SLVREADY)"
    )
    assert cpuid == EXPECTED_CPUID_M0, (
        f"CPUID 0x{cpuid:08X} != 0x{EXPECTED_CPUID_M0:08X}"
    )


@cocotb.test()
async def test_negative_wrong_ir(dut):
    """NEGATIVE CONTROL (wrong-IR): a halt write issued while the IR is BYPASS
    must NOT halt the core.

    Proves the serial IR-decode is load-bearing: the same 35-bit payload that
    halts via APACC does nothing via BYPASS (1-bit DR, no AP access), so a
    passing positive test can't be an artefact of always-halting wiring.
    """
    drv = await _fresh(dut, powered=True, boot=True)

    # Program CSW/TAR legitimately, then push the halt DRW word through BYPASS.
    await drv.ap_write(AP_CSW, CSW_WORD)
    await drv.ap_write(AP_TAR, DHCSR_ADDR)
    await drv.set_ir(IR_BYPASS)
    await drv.shift_dr(DBGKEY | C_DEBUGEN | C_HALT, 35)   # goes into BYPASS, not the AP
    for _ in range(20):
        await RisingEdge(dut.cpu0_sys_hclk)

    dhcsr = await drv.mem_read(DHCSR_ADDR)
    dut._log.info("DHCSR after BYPASS 'halt' = 0x%08X (S_HALT=%d)", dhcsr, (dhcsr >> 17) & 1)
    assert (dhcsr & S_HALT) == 0, (
        f"core halted from a BYPASS-IR write (DHCSR=0x{dhcsr:08X}) — IR decode "
        f"not honoured; positive halt result would be untrustworthy"
    )


@cocotb.test()
async def test_negative_no_powerup(dut):
    """NEGATIVE CONTROL (no power-up): without the CTRL/STAT power-up handshake
    the debug AP is gated off, so the core must NOT halt.

    Deterministic anchor: CDBGPWRUPREQ reads back 0 (we never requested it).
    """
    drv = await _fresh(dut, powered=False, boot=True)      # NOTE: no power_up()

    stat = await drv.read_ctrlstat()
    dut._log.info("CTRL/STAT (no power-up) = 0x%08X", stat)
    assert (stat & CDBGPWRUPREQ) == 0, (
        f"CDBGPWRUPREQ set without a power-up request: 0x{stat:08X}"
    )

    halted = False
    try:
        await drv.mem_write(DHCSR_ADDR, DBGKEY | C_DEBUGEN | C_HALT)
        for _ in range(20):
            await RisingEdge(dut.cpu0_sys_hclk)
        val = await drv.mem_read(DHCSR_ADDR)
        halted = bool(val & S_HALT)
        dut._log.info("DHCSR (no power-up) = 0x%08X", val)
    except DapError as e:
        dut._log.info("AP access correctly failed without power-up: %s", e)

    assert not halted, "core halted without the debug power-up handshake"
