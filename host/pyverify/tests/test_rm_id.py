"""The v2 ``rm_id`` encoding (:mod:`pyverify.rm_id`) — docs/VERSIONING_PLAN.md §3.2.

    rm_id = (ver_major << 24) | (ver_minor << 16) | design_id[15:0]

This is one of the few places a literal 32-bit rm_id is *correct* to hard-code:
the subject under test IS the packing/unpacking, so the numbers are the fixture,
not a claim about which artefact is on a board. (Everywhere else — see
``test_e2e_deploy.py`` and ``scripts/harness_gates/check_rm_id_literals.py`` —
an expected rm_id is derived from the manifest / rm_list.tcl, because it changes
on every design version bump.)
"""
from __future__ import annotations

import pytest

from pyverify.rm_id import (
    DESIGN_ID_MASK,
    GREYBOX_RM_ID,
    RmVersion,
    design_id,
    format_rm_id,
    is_greybox,
    make_rm_id,
    parse_rm_id,
    same_design,
    version,
)

# The real library as of the v2 cutover, for round-trip checks. Written out
# rather than read from rm_list.tcl on purpose: this test is about the ENCODING
# ARITHMETIC, and reading the authority here would make it a tautology.
# (check_rm_id_literals.py is what keeps the repo's *pins* honest.)
NANOSOC_V1_0 = 0x01000001
ETH_SS_V1_0 = 0x01000002
MULTICORE_V1_0 = 0x01000003
UART_ECHO_V1_0 = 0x01000004
LED_V1_0 = 0x0100001E
REGDEMO_A_V1_0 = 0x010000A1
REGDEMO_B_V1_0 = 0x010000B2


class TestUnpack:
    def test_design_id_is_the_low_half(self):
        assert design_id(NANOSOC_V1_0) == 0x0001
        assert design_id(LED_V1_0) == 0x001E
        assert design_id(REGDEMO_B_V1_0) == 0x00B2
        assert design_id(UART_ECHO_V1_0) == 0x0004

    def test_version_is_the_high_half(self):
        assert version(NANOSOC_V1_0) == RmVersion(1, 0)
        assert version(0x01010001) == RmVersion(1, 1)
        assert version(0x0A0B0001) == RmVersion(10, 11)
        assert str(version(NANOSOC_V1_0)) == "1.0"

    def test_design_id_survives_a_version_bump(self):
        """The whole point of the split: the identity is version-invariant.

        Every one of these is still nanosoc — v1.1, v2.0, v255.255. Any code
        that answers "is this nanosoc?" by matching the full 32 bits says NO to
        all three, which is the bug class this module exists to retire.
        """
        for bumped in (0x01010001, 0x02000001, 0xFFFF0001):
            assert design_id(bumped) == design_id(NANOSOC_V1_0) == 0x0001
            assert same_design(bumped, NANOSOC_V1_0)

    def test_different_designs_are_never_the_same_design(self):
        ids = [NANOSOC_V1_0, ETH_SS_V1_0, MULTICORE_V1_0, UART_ECHO_V1_0,
               LED_V1_0, REGDEMO_A_V1_0, REGDEMO_B_V1_0]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                assert not same_design(a, b), f"0x{a:08X} vs 0x{b:08X}"

    def test_masking_is_not_a_wildcard(self):
        """A newer eth_ss must NOT be mistaken for nanosoc just because the
        version halves match."""
        assert not same_design(0x01010002, NANOSOC_V1_0)


class TestPack:
    def test_make_rm_id_round_trips(self):
        for rm_id in (NANOSOC_V1_0, LED_V1_0, REGDEMO_B_V1_0, UART_ECHO_V1_0):
            major, minor = version(rm_id)
            assert make_rm_id(design_id(rm_id), major, minor) == rm_id

    def test_make_rm_id_matches_the_tcl_arithmetic(self):
        assert make_rm_id(0x0001, 1, 0) == 0x01000001
        assert make_rm_id(0x001E, 1, 0) == 0x0100001E
        assert make_rm_id(0x0004, 1, 0) == 0x01000004
        assert make_rm_id(0x0000, 0, 0) == GREYBOX_RM_ID

    @pytest.mark.parametrize("design,major,minor", [
        (0x1_0000, 1, 0),   # design_id overflows 16 bits
        (0x0001, 256, 0),   # major overflows its 8-bit field
        (0x0001, 0, 256),   # minor overflows its 8-bit field
        (-1, 0, 0),
    ])
    def test_make_rm_id_rejects_out_of_range_fields(self, design, major, minor):
        with pytest.raises(ValueError):
            make_rm_id(design, major, minor)


class TestParse:
    @pytest.mark.parametrize("value", [
        0x01000001, "0x01000001", "0x0100_0001", " 0x01000001 ", 16777217,
    ])
    def test_accepts_every_form_an_rm_id_arrives_in(self, value):
        """Ints (registers/headers), hex strings (ping/manifest), and the
        underscore-grouped form overlay-manifest.md's example uses."""
        assert parse_rm_id(value) == NANOSOC_V1_0

    def test_rejects_junk(self):
        for bad in ("", "nonsense", None, 3.5, True):
            with pytest.raises((ValueError, TypeError)):
                parse_rm_id(bad)

    def test_format_matches_the_wire_rendering(self):
        """The shell emits "0x%08x" (net-protocol.md ping/swap); gen_manifest.py
        writes the same. Anything else would break string comparisons."""
        assert format_rm_id(LED_V1_0) == "0x0100001e"
        assert format_rm_id(GREYBOX_RM_ID) == "0x00000000"
        assert format_rm_id("0x0100_0001") == "0x01000001"


class TestGreybox:
    def test_greybox_is_exactly_zero(self):
        """The carve-out. The DFX decoupler clamps rm_id to DECOUPLED_VALUE 0x0,
        and firmware + pyverify.edge read an all-zero id as "no RM loaded" — so
        0 is reserved, and greybox is held at design 0x0000 @ v0.0 to keep it."""
        assert GREYBOX_RM_ID == 0x00000000
        assert is_greybox(0) and is_greybox("0x00000000")
        assert design_id(GREYBOX_RM_ID) == 0

    def test_a_versioned_greybox_would_not_be_greybox(self):
        """Why rm_list.tcl must never "promote" greybox to v1.0: 0x01000000 is
        not the decoupler's clamp value and would defeat "0 == nothing loaded"."""
        assert not is_greybox(0x01000000)

    def test_no_real_design_may_be_zero(self):
        for rm_id in (NANOSOC_V1_0, ETH_SS_V1_0, LED_V1_0, UART_ECHO_V1_0):
            assert not is_greybox(rm_id)
            assert design_id(rm_id) != 0


def test_mask_constant_is_the_low_16_bits():
    assert DESIGN_ID_MASK == 0xFFFF
