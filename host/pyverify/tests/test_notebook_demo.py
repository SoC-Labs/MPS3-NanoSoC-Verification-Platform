"""E2E-3: ``host/notebooks/demo.md``'s facade sequence, headless.

Replays the demo notebook's cells 0-6 as one pytest against the FakeShell:

    0. connect + ping (shell_id / current rm_id)
    1+2. ``board.deploy("nanosoc")`` — validate -> push -> swap, verified
    3. ``result.reattach.apply(...)`` — consoles reopened for real (via an
       injected console executor pointing at the fake's ephemeral ports),
       SWD/LTX/VPHY steps return their descriptive hints
    4. scrape UART0: boot banner assert, then read_until("TEST COMPLETE")
    5. check: PASS in the test output + telemetry
    6. cleanup: ``board.close()`` drops consoles + control channel

The only deviations from the literal notebook are the ephemeral-port
plumbing a fake shell needs: ``console_factory`` maps the contract console
ports (6930-6932) to the fake's OS-assigned ones, and the re-attach
``console`` step gets an injected executor for the same reason (the
default executor would dial the contract ports on the shell host).
"""
from __future__ import annotations

import pytest

from pyverify.board import Mps3Board
from pyverify.client import ShellProtocolError
from pyverify.console import SWO_PORT, UART0_PORT, UART1_PORT, ConsoleReader
from pyverify.testing.fakeshell import FakeShell

from test_e2e_deploy import SYNTHETIC_RM_ID, SYNTHETIC_STATIC_ID, make_synthetic_overlay

UART0_TRANSCRIPT = (
    b"nanosoc boot\n"
    b"running dut selftest...\n"
    b"PASS\n"
    b"TEST COMPLETE\n"
)


def test_notebook_demo_sequence_headless(tmp_path):
    overlay_root = tmp_path / "overlay"
    # Uses the shared SYNTHETIC_RM_ID: a v2-shaped id whose design half is
    # outside rm_list.tcl's allocated range, so this fixture cannot be
    # confused with -- or go stale alongside -- the real nanosoc design.
    make_synthetic_overlay(overlay_root, rm_name="nanosoc")

    with FakeShell.ephemeral(
        static_id=SYNTHETIC_STATIC_ID,
        banners={"uart0": UART0_TRANSCRIPT},
        telemetry_lockup=False,
    ) as fake:
        port_map = {
            UART0_PORT: fake.uart0_port,
            UART1_PORT: fake.uart1_port,
            SWO_PORT: fake.swo_port,
        }

        def console_factory(host: str, port: int) -> ConsoleReader:
            return ConsoleReader(host, port_map.get(port, port), timeout=2.0)

        reattach_consoles: list[ConsoleReader] = []

        def reattach_console_executor(plan):
            readers = tuple(
                ConsoleReader(plan.shell_host, port_map[p], timeout=2.0).connect()
                for p in plan.console_reopen_ports
            )
            reattach_consoles.extend(readers)
            return readers

        # -- cell 0: setup -------------------------------------------------- #
        board = Mps3Board(
            fake.host,
            control_port=fake.control_port,
            tftp_port=fake.tftp_port,
            tcp_push_port=fake.raw_tcp_port,
            overlay_root=overlay_root,
            console_factory=console_factory,
        ).connect()
        try:
            info = board.ping()
            assert info.ok
            assert int(info.shell_id, 0) == SYNTHETIC_STATIC_ID
            assert int(info.rm_id, 0) == 0  # greybox at boot

            # -- cells 1+2: select DUT + load firmware ---------------------- #
            result = board.deploy("nanosoc")
            assert result.verified is True
            assert int(result.rm_id, 0) == SYNTHETIC_RM_ID
            assert int(board.ping().rm_id, 0) == SYNTHETIC_RM_ID

            # -- cell 3: re-attach ------------------------------------------ #
            outcomes = result.reattach.apply({"console": reattach_console_executor})
            # Consoles reopened for real: fresh TCP connects that see the
            # (fake) DUT stream from the top.
            assert len(outcomes["console"]) == 3
            outcomes["console"][0].assert_contains(b"nanosoc boot", timeout=5.0)
            # Tool-bound steps return their descriptive hints (spec §6.3).
            assert outcomes["ltx"] is None  # synthetic manifest carries no .ltx
            assert "SWD line reset" in outcomes["swd"]
            assert "link(event='up')" in outcomes["vphy"]

            # -- cell 4: run test — scrape the DUT console ------------------ #
            banner = board.uart0.assert_contains(b"nanosoc boot", timeout=5.0)
            assert banner.endswith(b"nanosoc boot")
            test_output = board.uart0.read_until(b"TEST COMPLETE", timeout=5.0)

            # -- cell 5: check result --------------------------------------- #
            assert b"PASS" in test_output, f"unexpected test output: {test_output!r}"
            # The notebook's "check" cell reads telemetry for the DUT-lockup
            # pin. Per net-protocol.md v0.6 the verb ALWAYS fails (this
            # platform has no power sensor), and `lockup` rides on that
            # failure line — the demo's pass/fail verdict comes from the
            # console transcript above, not from telemetry.
            telemetry = board.telemetry()
            assert telemetry.ok is False
            assert telemetry.err == "no power sensor"
            assert telemetry.lockup is False
        finally:
            for reader in reattach_consoles:
                reader.close()
            # -- cell 6: cleanup -------------------------------------------- #
            board.close()

        # The session closed its cached consoles and control channel...
        assert board._consoles == {}
        # ...and a closed board's control channel really is gone.
        with pytest.raises(ShellProtocolError):
            board.ping()

        # Server-side truth for the whole notebook run.
        assert [e.status for e in fake.push_events] == ["OK", "OK"]
        assert fake.swaps[-1]["final"] == "DONE"
        assert fake.current_rm_id == SYNTHETIC_RM_ID
