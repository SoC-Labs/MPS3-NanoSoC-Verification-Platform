"""sd_card_model.py — an SD card in SPI mode, modelled at the pads.

The DUT side of this model is the four pads `fpga/shell/ip/usd_spi/usd_spi.sv`
drives or reads, resolved the way the board resolves them:

    SCK  = usd_clk_o   (edges are ignored while usd_clk_oe = 0: the pad floats)
    MOSI = usd_cmd_o   if usd_cmd_oe  else 1   (XDC PULLUP on USD_CMD)
    CS   = usd_dat3_o  if usd_dat3_oe else 1   (XDC PULLUP on USD_DAT[3])
    MISO = usd_dat0_i  <- this model (1 = released: XDC PULLUP on USD_DAT[0])

Timing, as a real card behaves in SPI mode 0:
  * MOSI is sampled on the RISING edge of SCK, only while CS is low;
  * MISO moves to its next bit `tod_ns` after the FALLING edge of SCK (tODLY;
    the SD spec allows up to 14 ns in default speed), only while CS is low.
    `tod_ns` must be shorter than one SCK period.
  * clocks with CS high are counted as the >= 74 power-up clocks a card needs
    before it will accept CMD0 (`require_init_clocks`).

Protocol (SD Physical Layer Simplified Spec v2+, SPI mode):
  CMD0 (R1 0x01; CRC7 CHECKED — a bad CRC gets no response, the card stays in
  SD mode), CMD8 (R7 echo of VHS + check pattern; CRC7 CHECKED — a bad CRC gets
  R1 with the COM_CRC_ERROR bit), CMD55 + ACMD41 (R1 0x01 for
  `acmd41_busy_polls` polls, then 0x00; an SDHC card asked without HCS stays
  busy forever, as the spec says), CMD58 (R3/OCR; CCS=1 for SDHC, 0 with
  `sdhc=False`), CMD59 (CRC on/off), CMD9/CMD10 (CSD v2 / v1, CID), CMD13 (R2),
  CMD16, CMD17, CMD18 + CMD12 (stuff byte, R1b), CMD24 and CMD25 (start tokens
  0xFE / 0xFC, stop token 0xFD, data response 0x05, then MISO held low for
  `write_busy_clocks` SCK clocks).

Every response is preceded by `ncr_bytes` of 0xFF (Ncr), and every read data
token by `read_latency_bytes` of 0xFF (Nac). Data CRC16 is always SENT; it is
CHECKED on writes only after CMD59 turns CRC on (SPI-mode default is off, and
the harness keeps it off: integrity is the store's slot CRC32).

Anything the host does that a real card would reject or ignore is appended to
`self.errors` as a string, so a bench can assert `model.errors == []`.
"""
from __future__ import annotations

import collections

import cocotb
from cocotb.triggers import FallingEdge, First, RisingEdge, Timer, ValueChange

BLOCK = 512

# R1 bits
R1_IDLE = 0x01
R1_ERASE_RESET = 0x02
R1_ILLEGAL = 0x04
R1_CRC_ERR = 0x08
R1_ERASE_SEQ = 0x10
R1_ADDR_ERR = 0x20
R1_PARAM_ERR = 0x40

TOKEN_START_SINGLE = 0xFE      # CMD17/18 read data and CMD24 write data
TOKEN_START_MULTI_WR = 0xFC    # CMD25 per-block
TOKEN_STOP_TRAN = 0xFD         # CMD25 end
DATA_RESP_ACCEPTED = 0x05      # xxx0_0101
DATA_RESP_CRC_ERR = 0x0B       # xxx0_1011
DATA_RESP_WRITE_ERR = 0x0D     # xxx0_1101


# --------------------------------------------------------------------------- #
# CRCs
# --------------------------------------------------------------------------- #
def crc7(data: bytes) -> int:
    """SD CRC7, G(x) = x^7 + x^3 + 1, MSB first, init 0."""
    crc = 0
    for byte in data:
        for i in range(7, -1, -1):
            fb = ((crc >> 6) & 1) ^ ((byte >> i) & 1)
            crc = (crc << 1) & 0x7F
            if fb:
                crc ^= 0x09
    return crc


def cmd_crc(index: int, arg: int) -> int:
    """The 6th command byte: CRC7 in [7:1], end bit 1."""
    return (crc7(bytes([0x40 | index]) + arg.to_bytes(4, "big")) << 1) | 1


def crc16(data: bytes) -> int:
    """SD data CRC16 (CRC-16/XMODEM: poly 0x1021, init 0)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def default_block(lba: int) -> bytes:
    """What a never-written block holds: a pattern that differs per LBA."""
    return bytes(((lba * 131) + i * 7 + (i >> 8)) & 0xFF for i in range(BLOCK))


def busy_bytes(nclocks: int) -> list[int]:
    """`nclocks` SCK clocks of MISO low, then released, as whole bytes (MSB
    first). 20 clocks -> [0x00, 0x00, 0x0F]."""
    out = []
    left = nclocks
    while left >= 8:
        out.append(0x00)
        left -= 8
    if left:
        out.append(0xFF >> left)
    return out


def _set_bits(bits: list[int], hi: int, lo: int, value: int) -> None:
    """bits[127..0] as a list indexed by bit number."""
    for i in range(hi - lo + 1):
        bits[lo + i] = (value >> i) & 1


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray(16)
    for n in range(128):
        if bits[n]:
            out[15 - n // 8] |= 1 << (n % 8)
    return bytes(out)


def _with_crc7(reg15: bytes) -> bytes:
    return reg15 + bytes([(crc7(reg15) << 1) | 1])


class SdCardModel:
    """One card, one slot. Start it with `.start()`.

    Parameters
    ----------
    sdhc : True = SDHC/SDXC (CCS=1, block addressing, CSD v2);
           False = SDSC v2 (CCS=0, byte addressing, CSD v1, 1 GiB).
    capacity_blocks : SDHC capacity in 512-byte blocks (a multiple of 1024).
    acmd41_busy_polls : ACMD41 polls answered 0x01 before the card goes ready.
    write_busy_clocks : SCK clocks of busy (MISO low) after each data response
                        and after a CMD25 stop token.
    """

    def __init__(self, dut, *, sdhc: bool = True,
                 capacity_blocks: int = 16 * 1024 * 1024,     # 8 GiB
                 acmd41_busy_polls: int = 3,
                 write_busy_clocks: int = 44,
                 ncr_bytes: int = 1,
                 read_latency_bytes: int = 2,
                 tod_ns: float = 2.0,
                 require_init_clocks: int = 74):
        self.dut = dut
        self.clk = dut.usd_clk_o
        self.clk_oe = dut.usd_clk_oe
        self.mosi = dut.usd_cmd_o
        self.mosi_oe = dut.usd_cmd_oe
        self.cs = dut.usd_dat3_o
        self.cs_oe = dut.usd_dat3_oe
        self.miso = dut.usd_dat0_i

        self.sdhc = sdhc
        self.capacity_blocks = capacity_blocks if sdhc else (1 << 30) // BLOCK
        self.acmd41_busy_polls = acmd41_busy_polls
        self.write_busy_clocks = write_busy_clocks
        self.ncr_bytes = ncr_bytes
        self.read_latency_bytes = read_latency_bytes
        self.tod_ns = tod_ns
        self.require_init_clocks = require_init_clocks

        self.blocks: dict[int, bytearray] = {}
        self.errors: list[str] = []
        self.cmd_log: list[tuple[str, int, int]] = []   # ("CMD"/"ACMD", index, arg)
        self.writes: list[int] = []                      # LBAs written, in order

        # card state
        self.spi_mode = False
        self.idle = True
        self.ready = False
        self.app_cmd = False
        self.crc_on = False
        self.acmd41_polls = 0
        self.init_clocks = 0

        # byte engine
        self.rx_state = "cmd"          # cmd | wr_token | wr_data
        self.cmd_buf: list[int] = []
        self.wr_buf = bytearray()
        self.wr_multi = False
        self.wr_lba = 0
        self.rd_multi_lba: int | None = None
        self.outq: collections.deque[int] = collections.deque()
        self.in_byte = 0
        self.in_bits = 0
        self.out_byte = 0xFF
        self.out_bits = 0
        self.selected = False
        self.rising_clocks = 0         # every SCK rising edge with the pad driven

        self._tasks = []

    # ------------------------------------------------------------------ #
    # pad resolution
    # ------------------------------------------------------------------ #
    def _cs_low(self) -> bool:
        return bool(int(self.cs_oe.value)) and not int(self.cs.value)

    def _mosi_bit(self) -> int:
        return int(self.mosi.value) if int(self.mosi_oe.value) else 1

    # ------------------------------------------------------------------ #
    # storage
    # ------------------------------------------------------------------ #
    def read_block(self, lba: int) -> bytes:
        blk = self.blocks.get(lba)
        return bytes(blk) if blk is not None else default_block(lba)

    def write_block(self, lba: int, data: bytes) -> None:
        assert len(data) == BLOCK
        self.blocks[lba] = bytearray(data)

    # ------------------------------------------------------------------ #
    # registers
    # ------------------------------------------------------------------ #
    def ocr(self) -> int:
        v = 0x00FF8000                           # 2.7-3.6 V window
        if self.ready:
            v |= 1 << 31                         # power-up done
            if self.sdhc:
                v |= 1 << 30                     # CCS (valid only once ready)
        return v

    def csd(self) -> bytes:
        bits = [0] * 128
        bits[0] = 1
        if self.sdhc:
            _set_bits(bits, 127, 126, 1)         # CSD_STRUCTURE = 1 (v2)
            _set_bits(bits, 119, 112, 0x0E)      # TAAC
            _set_bits(bits, 103, 96, 0x32)       # TRAN_SPEED 25 MHz
            _set_bits(bits, 95, 84, 0x5B5)       # CCC
            _set_bits(bits, 83, 80, 9)           # READ_BL_LEN 512
            _set_bits(bits, 69, 48, self.capacity_blocks // 1024 - 1)  # C_SIZE
            _set_bits(bits, 46, 46, 1)           # ERASE_BLK_EN
            _set_bits(bits, 45, 39, 0x7F)        # SECTOR_SIZE
            _set_bits(bits, 28, 26, 2)           # R2W_FACTOR
            _set_bits(bits, 25, 22, 9)           # WRITE_BL_LEN 512
        else:
            _set_bits(bits, 127, 126, 0)         # CSD_STRUCTURE = 0 (v1)
            _set_bits(bits, 119, 112, 0x26)
            _set_bits(bits, 103, 96, 0x32)
            _set_bits(bits, 95, 84, 0x5F5)
            _set_bits(bits, 83, 80, 9)           # READ_BL_LEN 512
            _set_bits(bits, 73, 62, 4095)        # C_SIZE
            _set_bits(bits, 49, 47, 7)           # C_SIZE_MULT -> 1 GiB
            _set_bits(bits, 25, 22, 9)
        return _with_crc7(_bits_to_bytes(bits)[:15])

    def cid(self) -> bytes:
        return _with_crc7(bytes([0x03]) + b"SD" + b"USD01" + bytes([0x10])
                          + (0x12345678).to_bytes(4, "big") + bytes([0x01, 0x9A]))

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    def start(self):
        self.miso.value = 1
        self._tasks = [cocotb.start_soon(self._rising()),
                       cocotb.start_soon(self._falling()),
                       cocotb.start_soon(self._cs_watch())]
        return self

    def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    async def _cs_watch(self):
        while True:
            now = self._cs_low()
            if now and not self.selected:
                self.selected = True
                self.in_bits = 0
                self.out_bits = 0
                self.out_byte = self._next_out_byte()
                self.miso.value = (self.out_byte >> 7) & 1
            elif not now and self.selected:
                self.selected = False
                self.in_bits = 0
                self.out_bits = 0
                self.out_byte = 0xFF
                self.miso.value = 1                  # released: pull-up
            await First(ValueChange(self.cs), ValueChange(self.cs_oe))

    async def _rising(self):
        while True:
            await RisingEdge(self.clk)
            if not int(self.clk_oe.value):
                continue
            self.rising_clocks += 1
            if not self.selected:
                self.init_clocks += 1
                continue
            self.in_byte = ((self.in_byte << 1) | self._mosi_bit()) & 0xFF
            self.in_bits += 1
            if self.in_bits == 8:
                self.in_bits = 0
                self._on_byte(self.in_byte)

    async def _falling(self):
        while True:
            await FallingEdge(self.clk)
            if not int(self.clk_oe.value) or not self.selected:
                continue
            self.out_bits += 1
            if self.out_bits == 8:
                self.out_bits = 0
                self.out_byte = self._next_out_byte()
            bit = (self.out_byte >> (7 - self.out_bits)) & 1
            if self.tod_ns:
                await Timer(self.tod_ns, unit="ns")
            if self.selected:
                self.miso.value = bit

    # ------------------------------------------------------------------ #
    # output queue
    # ------------------------------------------------------------------ #
    def _queue(self, data) -> None:
        self.outq.extend(data)

    def _respond(self, data) -> None:
        self._queue([0xFF] * self.ncr_bytes)
        self._queue(data)

    def _data_block(self, payload: bytes) -> list[int]:
        return ([0xFF] * self.read_latency_bytes + [TOKEN_START_SINGLE]
                + list(payload) + list(crc16(payload).to_bytes(2, "big")))

    def _next_out_byte(self) -> int:
        if not self.outq and self.rd_multi_lba is not None:
            lba = self.rd_multi_lba
            if lba >= self.capacity_blocks:
                self.errors.append(f"CMD18 streamed past the end of the card (LBA {lba})")
                self.rd_multi_lba = None
            else:
                self._queue(self._data_block(self.read_block(lba)))
                self.rd_multi_lba = lba + 1
        return self.outq.popleft() if self.outq else 0xFF

    # ------------------------------------------------------------------ #
    # input bytes
    # ------------------------------------------------------------------ #
    def _on_byte(self, b: int) -> None:
        if self.rx_state == "wr_token":
            if b == 0xFF:
                return
            if not self.wr_multi and b == TOKEN_START_SINGLE:
                self.rx_state, self.wr_buf = "wr_data", bytearray()
            elif self.wr_multi and b == TOKEN_START_MULTI_WR:
                self.rx_state, self.wr_buf = "wr_data", bytearray()
            elif self.wr_multi and b == TOKEN_STOP_TRAN:
                # One byte after the stop token, then busy (spec: Nbr).
                self._queue([0xFF] + busy_bytes(self.write_busy_clocks))
                self.rx_state = "cmd"
            else:
                self.errors.append(f"write: unexpected token 0x{b:02X} "
                                   f"({'CMD25' if self.wr_multi else 'CMD24'})")
            return
        if self.rx_state == "wr_data":
            self.wr_buf.append(b)
            if len(self.wr_buf) == BLOCK + 2:
                self._on_write_data(bytes(self.wr_buf[:BLOCK]),
                                    int.from_bytes(self.wr_buf[BLOCK:], "big"))
            return
        # rx_state == "cmd"
        if self.cmd_buf:
            self.cmd_buf.append(b)
            if len(self.cmd_buf) == 6:
                frame, self.cmd_buf = bytes(self.cmd_buf), []
                self._on_command(frame)
        elif (b & 0xC0) == 0x40:
            self.cmd_buf = [b]

    def _on_write_data(self, data: bytes, crc: int) -> None:
        if self.crc_on and crc16(data) != crc:
            resp = DATA_RESP_CRC_ERR
            self.errors.append(f"write LBA {self.wr_lba}: data CRC16 mismatch")
        elif self.wr_lba >= self.capacity_blocks:
            resp = DATA_RESP_WRITE_ERR
            self.errors.append(f"write past the end of the card (LBA {self.wr_lba})")
        else:
            resp = DATA_RESP_ACCEPTED
            self.write_block(self.wr_lba, data)
            self.writes.append(self.wr_lba)
        # The data response follows the CRC immediately, then busy.
        self._queue([resp] + busy_bytes(self.write_busy_clocks))
        if self.wr_multi and resp == DATA_RESP_ACCEPTED:
            self.wr_lba += 1
            self.rx_state = "wr_token"
        else:
            self.rx_state = "cmd"

    def _r1(self, extra: int = 0) -> int:
        return (R1_IDLE if self.idle else 0) | extra

    def _addr_to_lba(self, arg: int):
        """-> (lba, r1_error). SDHC: block address. SDSC: byte address."""
        if self.sdhc:
            lba = arg
        else:
            if arg % BLOCK:
                return None, R1_ADDR_ERR
            lba = arg // BLOCK
        if lba >= self.capacity_blocks:
            return None, R1_PARAM_ERR
        return lba, 0

    def _on_command(self, frame: bytes) -> None:
        index = frame[0] & 0x3F
        arg = int.from_bytes(frame[1:5], "big")
        crc_ok = frame[5] == cmd_crc(index, arg)
        app = self.app_cmd
        self.app_cmd = False
        self.cmd_log.append(("ACMD" if app else "CMD", index, arg))

        if not self.spi_mode:
            # SD mode: only CMD0 with CS low (and a valid CRC) enters SPI mode.
            if index != 0:
                self.errors.append(f"CMD{index} before CMD0 (card is in SD mode)")
                return
            if self.init_clocks < self.require_init_clocks:
                self.errors.append(f"CMD0 after only {self.init_clocks} power-up "
                                   f"clocks (needs {self.require_init_clocks})")
                return
            if not crc_ok:
                self.errors.append("CMD0 with a bad CRC7: ignored (SD mode)")
                return
            self.spi_mode = True
            self._go_idle()
            self._respond([R1_IDLE])
            return

        # CRC7: CMD0 and CMD8 always; everything once CMD59 turned CRC on.
        if (index in (0, 8) or self.crc_on) and not crc_ok:
            self.errors.append(f"CMD{index}: bad CRC7 0x{frame[5]:02X}")
            self._respond([self._r1(R1_CRC_ERR)])
            return

        if app and index == 41:
            self._acmd41(arg)
            return
        if index == 41:
            self._respond([self._r1(R1_ILLEGAL)])
            return

        if self.idle and index not in (0, 1, 8, 55, 58, 59):
            self.errors.append(f"CMD{index} while the card is still idle")
            self._respond([self._r1(R1_ILLEGAL)])
            return

        handler = getattr(self, f"_cmd{index}", None)
        if handler is None:
            self.errors.append(f"CMD{index}: not supported")
            self._respond([self._r1(R1_ILLEGAL)])
            return
        handler(arg)

    def _go_idle(self) -> None:
        self.idle, self.ready, self.acmd41_polls = True, False, 0
        self.rd_multi_lba = None
        self.rx_state = "cmd"
        self.outq.clear()

    # --- individual commands ------------------------------------------ #
    def _cmd0(self, arg):
        self._go_idle()
        self._respond([R1_IDLE])

    def _cmd1(self, arg):
        self._respond([self._r1(R1_ILLEGAL)])     # SDC: use ACMD41

    def _cmd8(self, arg):
        vhs, pattern = (arg >> 8) & 0xF, arg & 0xFF
        if vhs != 0x1:
            self.errors.append(f"CMD8: VHS 0x{vhs:X} is not 2.7-3.6 V")
        self._respond([self._r1(), 0x00, 0x00, vhs, pattern])

    def _cmd55(self, arg):
        self.app_cmd = True
        self._respond([self._r1()])

    def _acmd41(self, arg):
        hcs = (arg >> 30) & 1
        if self.sdhc and not hcs:
            # Spec: a high-capacity card asked without HCS never leaves busy.
            self._respond([self._r1()])
            return
        self.acmd41_polls += 1
        if self.acmd41_polls > self.acmd41_busy_polls:
            self.idle, self.ready = False, True
        self._respond([self._r1()])

    def _cmd58(self, arg):
        self._respond([self._r1()] + list(self.ocr().to_bytes(4, "big")))

    def _cmd59(self, arg):
        self.crc_on = bool(arg & 1)
        self._respond([self._r1()])

    def _cmd9(self, arg):
        self._respond([self._r1()])
        self._queue(self._data_block(self.csd()))

    def _cmd10(self, arg):
        self._respond([self._r1()])
        self._queue(self._data_block(self.cid()))

    def _cmd12(self, arg):
        was_reading = self.rd_multi_lba is not None
        self.rd_multi_lba = None
        # The byte right after CMD12 is a stuff byte (whatever was next on the
        # wire -- mid-data for a real card), then Ncr, R1, and a short busy.
        stuff = self.outq[0] if self.outq else 0x00
        self.outq.clear()
        self._queue([stuff])
        self._respond([self._r1(0 if was_reading else R1_ILLEGAL)] + busy_bytes(12))

    def _cmd13(self, arg):
        self._respond([self._r1(), 0x00])

    def _cmd16(self, arg):
        if not self.sdhc and arg != BLOCK:
            self._respond([self._r1(R1_PARAM_ERR)])
        else:
            self._respond([self._r1()])

    def _cmd17(self, arg):
        lba, err = self._addr_to_lba(arg)
        if err:
            self._respond([self._r1(err)])
            return
        self._respond([self._r1()])
        self._queue(self._data_block(self.read_block(lba)))

    def _cmd18(self, arg):
        lba, err = self._addr_to_lba(arg)
        if err:
            self._respond([self._r1(err)])
            return
        self._respond([self._r1()])
        self.rd_multi_lba = lba                  # blocks are generated as clocked

    def _cmd24(self, arg):
        lba, err = self._addr_to_lba(arg)
        if err:
            self._respond([self._r1(err)])
            return
        self._respond([self._r1()])
        self.rx_state, self.wr_multi, self.wr_lba = "wr_token", False, lba

    def _cmd25(self, arg):
        lba, err = self._addr_to_lba(arg)
        if err:
            self._respond([self._r1(err)])
            return
        self._respond([self._r1()])
        self.rx_state, self.wr_multi, self.wr_lba = "wr_token", True, lba
