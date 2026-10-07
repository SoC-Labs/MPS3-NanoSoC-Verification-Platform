"""Pure decode/encode of the CSR bitfields + the ``read_safe`` dump-page
correctness property for :mod:`socket_harness.registers`.

registers.py is PURE (no I/O): every field extract/place, every block
decode/encode is exercised with plain ints, and the operator facade
(:class:`RegisterAccess`) is driven against an in-process recording backend
so nothing here touches a socket, xsdb or a board.

The load-bearing guard is the ``DFXCTL.RM_STATUS.dut_eth_irq`` bit: it lives
at ``lsb=2`` — the exact RM_STATUS[2] the 2026-07-24 re-mint added
(``feat(shell): observe the DUT ethernet IRQ at DFXCTL.RM_STATUS[2]``).
Moving it reddens ONLY the dut_eth_irq assertion, a direct mutation target.
"""
from __future__ import annotations

import pytest

from socket_harness.endpoints import by_name
from socket_harness.registers import BLOCKS, RegField, RegisterAccess


# --------------------------------------------------------------------------- #
# An in-process RegisterBackend double that records every access, so the
# read_safe dump-page property is directly observable.
# --------------------------------------------------------------------------- #


class RecordingBackend:
    """Structural :class:`socket_harness.session.RegisterBackend`: records
    every addr read/written; reads return a scripted value (0 by default)."""

    def __init__(self, values: dict[int, int] | None = None) -> None:
        self._values = dict(values or {})
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []

    def read_word(self, addr: int) -> int:
        self.reads.append(addr)
        return self._values.get(addr, 0)

    def write_word(self, addr: int, val: int) -> None:
        self.writes.append((addr, val))

    def read_block(self, addr: int, nwords: int) -> list[int]:
        return [self.read_word(addr + 4 * i) for i in range(nwords)]


# --------------------------------------------------------------------------- #
# RegField — the pure extract/place primitive
# --------------------------------------------------------------------------- #


def test_regfield_extract_single_bit() -> None:
    f = RegField(lsb=2, width=1, name="dut_eth_irq")
    assert f.extract(0b100) == 1
    assert f.extract(0b011) == 0


def test_regfield_place_shifts_and_masks_to_width() -> None:
    f = RegField(lsb=2, width=1, name="dut_eth_irq")
    assert f.place(1) == 0b100
    # multi-bit place masks the value to `width` before shifting
    g = RegField(lsb=0, width=8, name="data")
    assert g.place(0xAB) == 0xAB
    assert g.place(0x1AB) == 0xAB          # high bits beyond width are masked off
    assert g.extract(0x1FF) == 0xFF


# --------------------------------------------------------------------------- #
# Block decode/encode — the headline transcription guards
# --------------------------------------------------------------------------- #


def test_dfxctl_rm_status_decodes_all_three_bits() -> None:
    d = BLOCKS["dfxctl"].decode("RM_STATUS", 0b101)
    assert d == {"rm_id_valid": 1, "dut_lockup": 0, "dut_eth_irq": 1}


def test_dfxctl_rm_status_dut_eth_irq_is_the_remint_bit2_mutation_target() -> None:
    # The load-bearing one: dut_eth_irq is RM_STATUS[2]. Moving the field to
    # lsb=1 reddens ONLY this, while rm_id_valid/dut_lockup stay correct.
    assert BLOCKS["dfxctl"].decode("RM_STATUS", 0b100) == {
        "rm_id_valid": 0, "dut_lockup": 0, "dut_eth_irq": 1,
    }
    assert BLOCKS["dfxctl"].decode("RM_STATUS", 0b010) == {
        "rm_id_valid": 0, "dut_lockup": 1, "dut_eth_irq": 0,
    }


def test_vphy_link_event_encode_ors_placed_fields() -> None:
    assert BLOCKS["vphy"].encode("LINK_EVENT", force_down=1, pulse=1) == 0b11
    assert BLOCKS["vphy"].encode("LINK_EVENT", pulse=1) == 0b10


def test_genchk_inject_encode_one_hot() -> None:
    assert BLOCKS["genchk"].encode("INJECT", giant=1) == 0b100
    assert BLOCKS["genchk"].encode("INJECT", bad_fcs=1) == 0b001


def test_encode_unknown_field_raises_value_error() -> None:
    with pytest.raises(ValueError):
        BLOCKS["vphy"].encode("LINK_EVENT", nonsense=1)


def test_reg_lookup_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError):
        BLOCKS["dfxctl"].reg("NOSUCHREG")


def test_block_addr_is_base_plus_offset() -> None:
    assert BLOCKS["dfxctl"].addr("RM_STATUS") == 0x44A10014
    assert BLOCKS["dfxctl"].addr("RM_ID") == 0x44A10010
    assert BLOCKS["genchk"].addr("INJECT") == 0x44A60004


# --------------------------------------------------------------------------- #
# RegisterAccess convenience ops (backend-agnostic)
# --------------------------------------------------------------------------- #


def test_rm_id_and_rm_status_convenience_ops() -> None:
    backend = RecordingBackend({0x44A10010: 0xD84A2E7A, 0x44A10014: 0b101})
    ra = RegisterAccess(backend)
    assert ra.rm_id() == 0xD84A2E7A
    assert ra.rm_status() == {"rm_id_valid": 1, "dut_lockup": 0, "dut_eth_irq": 1}


def test_genchk_inject_name_validation_rejects_unknown() -> None:
    ra = RegisterAccess(RecordingBackend())
    with pytest.raises(ValueError):
        ra.genchk(gen=True, chk=True, inject="nope")


def test_genchk_valid_inject_writes_ctrl_and_inject_and_returns_dict() -> None:
    backend = RecordingBackend()
    ra = RegisterAccess(backend)
    result = ra.genchk(gen=True, chk=True, inject="giant")
    assert isinstance(result, dict)
    written = {addr for addr, _ in backend.writes}
    # CTRL@0x00 and INJECT@0x04 of the GENCHK block must both have been written.
    assert 0x44A60000 in written
    assert 0x44A60004 in written


def test_read_reg_and_write_reg_roundtrip_through_backend() -> None:
    backend = RecordingBackend({0x44A10014: 0b101})
    ra = RegisterAccess(backend)
    assert ra.read_reg("dfxctl", "RM_STATUS") == {
        "rm_id_valid": 1, "dut_lockup": 0, "dut_eth_irq": 1,
    }
    ra.write_reg("vphy", "LINK_EVENT", force_down=1, pulse=1)
    assert (0x44A30008, 0b11) in backend.writes


# --------------------------------------------------------------------------- #
# dump_page — the read_safe correctness property (a CORRECTNESS guard, not a
# style one): read_safe=False offsets must NEVER be read.
# --------------------------------------------------------------------------- #


def test_dump_page_dfxctl_never_reads_console_pop_0x20() -> None:
    # DFXCTL 0x20 (CONSOLE_POP) is the anecdotal read-aliasing side-effect
    # offset (a read pops a console byte — dfx_ctl.sv); read_safe=False, so it
    # must be excluded from the page dump.
    backend = RecordingBackend()
    ra = RegisterAccess(backend)
    result = ra.dump_page("dfxctl")
    assert 0x44A10020 not in backend.reads
    assert "CONSOLE_POP" not in result
    # ...but the read_safe registers ARE dumped.
    assert 0x44A10014 in backend.reads
    assert "RM_STATUS" in result


def test_dump_page_uartbr_never_reads_the_destructive_rx_registers() -> None:
    # U0_RX/U1_RX/SWO_RX are destructive reads (each read pops the RX FIFO);
    # read_safe=False, so dump_page must skip all three.
    backend = RecordingBackend()
    ra = RegisterAccess(backend)
    result = ra.dump_page("uartbr")
    for destructive in (0x44A90000, 0x44A90008, 0x44A90010):
        assert destructive not in backend.reads
    assert "U0_RX" not in result
    assert "U1_RX" not in result
    assert "SWO_RX" not in result
    # FIFO_STATUS@0x14 is read-safe and MUST appear.
    assert 0x44A90014 in backend.reads


# --------------------------------------------------------------------------- #
# Cross-file base invariant: every modeled block's base == the endpoints
# registry csr_base for that CSR name.
# --------------------------------------------------------------------------- #


def test_block_base_matches_endpoints_csr_base_for_every_block() -> None:
    for name, block in BLOCKS.items():
        assert block.base == by_name(name).csr_base, name


def test_block_base_literals() -> None:
    assert BLOCKS["clkrst"].base == 0x44A00000
    assert BLOCKS["dfxctl"].base == 0x44A10000
    assert BLOCKS["vphy"].base == 0x44A30000
    assert BLOCKS["genchk"].base == 0x44A60000
    assert BLOCKS["uartbr"].base == 0x44A90000
