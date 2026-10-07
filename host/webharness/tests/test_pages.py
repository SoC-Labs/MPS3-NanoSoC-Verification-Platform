"""``webharness.pages`` — the rendered document.

Two properties matter enough to gate: the page is **self-contained** (a lab
host with no internet must render it identically), and rendering it **costs no
board access** (``:6900`` is single-client — merely opening the dashboard must
not contend with ``pyverify``).
"""
from __future__ import annotations

import re

import pytest

from webharness.backend import BackendInfo, FakeBackend
from webharness.pages import render_page


class NoTouchBackend:
    def info(self):
        return BackendInfo(mode="fake", target="192.168.10.101", live=False)

    def __getattr__(self, name):
        raise AssertionError("render_page touched backend.%s" % name)


def html(backend=None, host_label="192.168.10.101"):
    return render_page(backend=backend or FakeBackend(), host_label=host_label)


# --------------------------------------------------------------------------- #

def test_render_consults_only_backend_info():
    page = render_page(backend=NoTouchBackend(), host_label="board")
    assert "<!doctype html>" in page


def test_page_is_self_contained_no_external_resources():
    page = html()
    # No CDN scripts, stylesheets, fonts or images — nothing that a lab host
    # without internet (or a locked-down browser) would fail to fetch.
    assert not re.search(r'(src|href)\s*=\s*["\']\s*(https?:)?//', page)
    assert "@import" not in page
    assert "<link" not in page


def test_page_declares_both_themes():
    page = html()
    assert "prefers-color-scheme: dark" in page
    assert "--bg:" in page


def test_page_is_responsive_and_wide_tables_scroll_inside_themselves():
    page = html()
    assert 'name="viewport"' in page
    assert "overflow-x:auto" in page.replace(" ", "")


def test_page_shows_the_backend_mode_so_a_screenshot_is_self_describing():
    live = render_page(backend=FakeBackend(), host_label="b")
    assert "FAKE" in live, "a fake-backend screenshot must say so"


def test_page_carries_the_host_label_into_the_document():
    assert "192.168.10.101" in html(host_label="192.168.10.101")


def test_host_label_is_escaped():
    page = html(host_label='<script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_page_has_the_sections_the_dashboard_promises():
    page = html()
    for heading in ("Shell", "Resident RM", "Health", "DUT clock", "DUT reset",
                    "Applications &amp; ports", "Reset taxonomy",
                    "Diagnostic counters"):
        assert heading in page, heading


def test_page_states_the_no_power_sensor_fact():
    assert "no power sensor" in html().lower()


def test_page_lists_the_json_api_so_curl_users_are_not_second_class():
    page = html()
    for path in ("/api/status", "/api/services", "/api/clocks", "/api/resets",
                 "/api/diag"):
        assert path in page, path


def test_reset_button_names_the_register_it_pulses():
    assert "CLKRST.RESET_CTRL.dut_resetn" in html()
