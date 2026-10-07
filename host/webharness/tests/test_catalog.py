"""Drift guards for :mod:`webharness.catalog`.

The catalog transcribes three tables that already exist elsewhere in the repo:
the service bits and RM tables from ``firmware/clcd/clcd.{h,c}``, and the port
numbers from ``socket_harness.endpoints.REGISTRY``. Transcription is the right
call (this package must not depend on a MicroBlaze C compiler, and it must
import without socket_harness), but an un-guarded transcription rots.

So these tests **parse the C** and assert agreement — including a sweep over
all 65536 design ids, so the web page and the on-board glass can never disagree
about which services are up. If a firmware table changes, exactly the matching
assertion reddens and names the delta.

Board-free: file reads and pure data only.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from webharness import catalog

REPO = Path(__file__).resolve().parents[3]
CLCD_C = REPO / "firmware" / "clcd" / "clcd.c"
CLCD_H = REPO / "firmware" / "clcd" / "clcd.h"

pytestmark = pytest.mark.skipif(
    not (CLCD_C.is_file() and CLCD_H.is_file()),
    reason="firmware/clcd sources absent (branch without the CLCD wave)",
)


# --------------------------------------------------------------------------- #
# Minimal C readers — deliberately dumb regex, not a parser. Each asserts it
# found something, so a refactor that moves a table fails loudly here instead
# of silently comparing against an empty dict.
# --------------------------------------------------------------------------- #

def _fn_body(src: str, signature_fragment: str) -> str:
    """The brace-balanced body of the function whose definition contains
    ``signature_fragment``."""
    idx = src.index(signature_fragment)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces after %r" % signature_fragment)


def _case_returns(body: str) -> dict:
    """``case 0x0001u: return "x";`` -> ``{1: "x"}``."""
    out = {}
    for m in re.finditer(r'case\s+(0[xX][0-9A-Fa-f]+)u?\s*:\s*return\s+"([^"]*)"',
                         body):
        out[int(m.group(1), 16)] = m.group(2)
    assert out, "no case/return pairs parsed"
    return out


def _svc_bits() -> dict:
    src = CLCD_H.read_text()
    out = {}
    for m in re.finditer(r"CLCD_SVC_(\w+)\s*=\s*1u\s*<<\s*(\d+)", src):
        out[m.group(1)] = 1 << int(m.group(2))
    assert out, "no CLCD_SVC_* bits parsed from clcd.h"
    return out


def _rm_services_table():
    """Parse ``clcd_rm_services()`` into (seed_mask, {design: extra_mask})."""
    bits = _svc_bits()
    body = _fn_body(CLCD_C.read_text(), "uint32_t clcd_rm_services(")

    def mask_of(expr: str) -> int:
        names = re.findall(r"CLCD_SVC_(\w+)", expr)
        assert names, "no CLCD_SVC_* names in %r" % expr
        m = 0
        for n in names:
            assert n in bits, "unknown service bit CLCD_SVC_%s" % n
            m |= bits[n]
        return m

    seed_m = re.search(r"uint32_t\s+s\s*=\s*([^;]+);", body, re.S)
    assert seed_m, "could not find the seed 'uint32_t s = ...' line"
    seed = mask_of(seed_m.group(1))

    extras = {}
    # case 0x000Nu: [comment] s |= A | B ...; break;
    for m in re.finditer(
        r"case\s+(0[xX][0-9A-Fa-f]+)u?\s*:.*?s\s*\|=\s*([^;]+);", body, re.S
    ):
        extras[int(m.group(1), 16)] = mask_of(m.group(2))
    assert extras, "no 'case ...: s |= ...' arms parsed"
    return seed, extras


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #

def test_service_bits_match_firmware_header():
    bits = _svc_bits()
    ours = {
        "CTRL": catalog.SVC_CTRL, "TFTP": catalog.SVC_TFTP,
        "PUSH": catalog.SVC_PUSH, "XVC": catalog.SVC_XVC,
        "SWD": catalog.SVC_SWD, "JTAG": catalog.SVC_JTAG,
        "UART0": catalog.SVC_UART0, "UART1": catalog.SVC_UART1,
        "SWO": catalog.SVC_SWO,
    }
    assert ours == bits


def test_shell_services_match_the_firmware_seed():
    seed, _ = _rm_services_table()
    assert catalog.SHELL_SERVICES == seed


def test_rm_services_agree_over_every_design_id():
    """The sweep. 65536 ids, both tables, no exceptions — this is the assertion
    that makes 'the page and the glass agree' a fact rather than a hope."""
    seed, extras = _rm_services_table()
    mismatches = []
    for design in range(0x10000):
        want = seed | extras.get(design, 0)
        got = catalog.services_for(design)
        if want != got:
            mismatches.append((design, hex(want), hex(got)))
            if len(mismatches) > 8:
                break
    assert not mismatches, "clcd_rm_services() disagreement: %r" % (mismatches,)


def test_rm_names_match_firmware():
    table = _case_returns(_fn_body(CLCD_C.read_text(), "const char *clcd_rm_name("))
    assert catalog.RM_NAMES == table


def test_rm_caps_match_firmware():
    table = _case_returns(_fn_body(CLCD_C.read_text(), "const char *clcd_rm_caps("))
    assert catalog.RM_CAPS == table


def test_unknown_design_renders_the_whole_word_like_the_firmware():
    # clcd.c:231-242 — never lie about an unrecognised id; show all 32 bits,
    # version half included, because an unknown high half is part of WHY.
    assert catalog.rm_name(0x01000003) == "nanosoc_multicore"   # known design
    assert catalog.rm_name(0x0A00BEEF) == "rm?0A00BEEF"         # unknown design
    assert catalog.rm_caps(0x0A00BEEF) == "design unknown"


def test_design_half_survives_a_version_bump():
    # v2 encoding: the top 16 bits are the design VERSION. A re-versioned RM
    # must keep its name, makeup and service set.
    for ver in (0x0000, 0x0100, 0x0101, 0xFFFF):
        rm_id = (ver << 16) | 0x0003
        assert catalog.rm_name(rm_id) == "nanosoc_multicore"
        assert catalog.services_for(rm_id) == catalog.services_for(0x0003)


# --------------------------------------------------------------------------- #
# Registry agreement
# --------------------------------------------------------------------------- #

def test_ports_agree_with_the_socket_harness_registry():
    pytest.importorskip("socket_harness")
    checked = 0
    for spec in catalog.SERVICES:
        port = catalog.registry_port(spec.registry_name)
        if port is None:
            continue
        assert port == spec.port, (
            "%s: catalog says %d, registry says %d" % (spec.key, spec.port, port)
        )
        checked += 1
    assert checked == len(catalog.SERVICES), "every service should have a registry row"


def test_ports_agree_with_the_firmware_port_constants():
    """The stronger anchor: ``firmware/common/net_proto.h``'s ``MPS3_PORT_*``.

    Unlike the socket_harness registry this covers **every** service including
    the JTAG bridge, and it is what the shell itself listens on — so a
    disagreement here means the page would send a user to a dead port.
    """
    header = REPO / "firmware" / "common" / "net_proto.h"
    if not header.is_file():
        pytest.skip("firmware/common/net_proto.h absent")
    src = header.read_text()
    fw = {}
    for m in re.finditer(r"#define\s+MPS3_PORT_(\w+)\s+(\d+)u", src):
        fw[m.group(1).lower()] = int(m.group(2))
    assert fw, "no MPS3_PORT_* constants parsed"

    # catalog key -> the MPS3_PORT_* suffix naming the same service
    alias = {"ctrl": "control", "push": "raw_push"}
    checked = 0
    for spec in catalog.SERVICES:
        name = alias.get(spec.key, spec.key)
        if name not in fw:
            continue
        assert spec.port == fw[name], (
            "%s: catalog says %d, MPS3_PORT_%s says %d"
            % (spec.key, spec.port, name.upper(), fw[name])
        )
        checked += 1
    assert checked == len(catalog.SERVICES), (
        "every service should have a firmware port constant; matched %d of %d"
        % (checked, len(catalog.SERVICES))
    )


def test_no_registry_gap_remains():
    """The gap (JTAG 6921) was closed 2026-08-01 once the contract itself grew
    the row. The *mechanism* stays: this pins "no service outruns the registry",
    so the next one that does is visible instead of silently papered over."""
    assert catalog.registry_gap() == ()


def test_every_service_has_a_registry_row():
    pytest.importorskip("socket_harness")
    missing = [s.key for s in catalog.SERVICES
               if catalog.registry_port(s.registry_name) is None]
    assert missing == []


# --------------------------------------------------------------------------- #
# Cross-guard against the firmware's OWN Apps-page table
#
# firmware/clcd/clcd.c grew a `SERVICES[]` table for the on-panel
# "Applications & Ports" page — the direct twin of catalog.SERVICES. The two
# render differently ON PURPOSE (the panel has 40 columns and shows
# `nc <ip> 6930`; the web page shows the full pyverify/openocd invocation), so
# labels and commands are NOT compared. What must agree is the SET, the ORDER
# and the PORTS: if the panel grows a tenth service, the web page must not
# silently omit it.
# --------------------------------------------------------------------------- #

def _firmware_services_table():
    """Parse ``SERVICES[]`` from clcd.c -> [(bit_name, port_macro), ...]."""
    body = _fn_body(CLCD_C.read_text(), "} SERVICES[] =")
    rows = re.findall(
        r"\{\s*CLCD_SVC_(\w+)\s*,\s*\"[^\"]*\"\s*,\s*\"[^\"]*\"\s*,"
        r"\s*'.'\s*,\s*(MPS3_PORT_\w+)\s*\}",
        body,
    )
    assert rows, "no SERVICES[] rows parsed from clcd.c"
    return rows


def _firmware_port_macros():
    header = REPO / "firmware" / "common" / "net_proto.h"
    src = header.read_text()
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"#define\s+(MPS3_PORT_\w+)\s+(\d+)u", src)}


def test_service_set_and_order_match_the_firmware_apps_table():
    try:
        fw_rows = _firmware_services_table()
    except (ValueError, AssertionError):
        pytest.skip("clcd.c SERVICES[] table absent (pre-Apps-page branch)")

    bits = _svc_bits()
    fw_bits = [bits[name] for name, _ in fw_rows]
    our_bits = [s.bit for s in catalog.SERVICES]
    assert fw_bits == our_bits, (
        "the panel and the web page list different services (or in a different "
        "order): firmware=%r ours=%r" % (fw_bits, our_bits)
    )


def test_ports_match_the_firmware_apps_table():
    try:
        fw_rows = _firmware_services_table()
    except (ValueError, AssertionError):
        pytest.skip("clcd.c SERVICES[] table absent (pre-Apps-page branch)")

    macros = _firmware_port_macros()
    for (bit_name, port_macro), spec in zip(fw_rows, catalog.SERVICES):
        assert port_macro in macros, port_macro
        assert spec.port == macros[port_macro], (
            "%s: web page says %d, the panel renders %s = %d"
            % (spec.key, spec.port, port_macro, macros[port_macro])
        )


def test_unknown_rm_falls_back_the_same_way_the_panel_does():
    """The panel calls ``clcd_rm_services(rm_known ? rm_id : 0u)``; this module
    calls ``services_for(None)``. Both must land on the shell-only set."""
    assert catalog.services_for(None) == catalog.services_for(0x0000)
    assert catalog.services_for(None) == catalog.SHELL_SERVICES


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #

def test_service_rows_for_a_single_core_nanosoc():
    rows = {r["key"]: r for r in catalog.service_rows(0x01000001, "board")}
    assert rows["ctrl"]["present"] and rows["ctrl"]["scope"] == "shell"
    assert rows["uart0"]["present"] and rows["swo"]["present"]
    assert rows["jtag"]["present"]
    # single-core nanosoc has ONE UART -> no uart1
    assert not rows["uart1"]["present"]


def test_service_rows_for_eth_ss_have_no_dut_services():
    rows = {r["key"]: r for r in catalog.service_rows(0x0002, "board")}
    assert rows["ctrl"]["present"], "shell services survive any RM"
    for key in ("swd", "jtag", "uart0", "uart1", "swo"):
        assert not rows[key]["present"], key


def test_unreadable_rm_id_still_lists_shell_services():
    rows = {r["key"]: r for r in catalog.service_rows(None, "board")}
    assert all(rows[k]["present"] for k in ("ctrl", "tftp", "push", "xvc"))
    assert not rows["uart0"]["present"]


def test_absent_services_are_listed_not_hidden():
    rows = catalog.service_rows(0x0002, "board")
    assert len(rows) == len(catalog.SERVICES)


def test_commands_are_host_substituted():
    rows = {r["key"]: r for r in catalog.service_rows(0x0003, "192.168.10.101")}
    assert rows["uart0"]["command"] == "nc 192.168.10.101 6930"
    assert "192.168.10.101" in rows["ctrl"]["command"]
