#!/usr/bin/env python3
"""Tier-3 swap / swap-away gate: push a Reconfigurable Module (RM) over the wire
and assert the shell reports ``{"ok":true, "verified":true, "rm_id":<expected>}``.

WHAT IT DRIVES
    ``python -m pyverify.cli deploy`` -- the ONE pusher. It owns the
    6900-swap-request / 6910-stream ordering, the "never abort mid-transfer"
    timeouts and the manifest reading, and its response shapes are held against
    the real firmware by tests/firmware_logic/test_fakeshell_conformance.py.
    This gate adds the *judgement*, not a protocol.

    ``--dry-run`` prints the exact command it would run and exits 0, so the
    command can be read BEFORE a board window rather than during one.

WHAT THIS ADDS OVER `deploy`'s OWN EXIT CODE
    deploy returns 0 iff the shell said ok. This gate also asserts:
      * ``verified``  -- the shell read rm_id BACK from the RP after releasing
                         DECOUPLE (bug #5 ordering: a swap can report ok before
                         the readback proves the RP actually took the config).
      * ``rm_id`` is EXACTLY the RM we asked for -- a swap that silently lands on
        greybox / the wrong RM (or never left the resident one) is the escape.
    Anything else fails LOUD (non-zero exit), so harness_regression stops.

USAGE
    # via the hub (harness_regression with MPS3_BOARD_VIA_HUB=1 passes --via-hub)
    swap_check.py --rm regdemo_b --expect-rm-id 0x010000B2 --via-hub <hub-host>

    # directly, when already ON the hub (empty/omitted --via-hub)
    swap_check.py --rm regdemo_b --expect-rm-id 0x010000B2

    # look before you leap
    swap_check.py --rm regdemo_b --expect-rm-id 0x010000B2 --dry-run

THERE IS NO DEFAULT --prod. The old one named ``fpga/dfx/build_v3/prod``, a
build directory that had been dead for two mints and does not exist in a fresh
clone; the fielded shell's artefact dir now comes from ``pyverify.fielded``
(which reads ``docs/FIELDED_SHELL.md`` + ``fielded/<static_id>/``).

NOTE: --expect-rm-id is compared for EXACT 32-bit equality against DFXCTL.RM_ID,
    so it must carry the design VERSION too. Since the v2 encoding
    (docs/VERSIONING_PLAN.md §3.2) rm_id is
        { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
    -- e.g. regdemo_b is design 0x00B2 at v1.0 => 0x010000B2, NOT 0xB2.

PRECONDITION: the caller holds the board lease (harness_regression runs
``scripts/mps3_board.sh preflight``, which now execs ``pyverify lease
preflight``). NEVER interrupt a swap -- the pusher's timeout is generous on
purpose; this gate's own subprocess timeout sits above it so it never races the
transfer.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

REPO_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BOARD_DEFAULT = os.environ.get("MPS3_BOARD_HOST", "192.168.10.101")
SSH_OPTS = ["-o", "ControlPath=none", "-o", "BatchMode=yes"]


def _to_int(v) -> int:
    """rm_id arrives as a hex string ("0x010000b2") over the wire; be liberal."""
    if isinstance(v, int):
        return v
    return int(str(v).strip(), 0)


def default_prod(repo: str) -> str:
    """The fielded shell's artefact dir, resolved -- never a build-dir literal.

    Falls back to a NAMED error rather than a guess: a gate that invents a path
    reports 'no such file' three layers down instead of 'you have not said which
    prod dir'."""
    sys.path.insert(0, os.path.join(repo, "host", "pyverify"))
    from pyverify.fielded import load
    return str(load(repo).prod_dir)


def build_cmd(args) -> list:
    """The argv to run: ``pyverify deploy`` locally, or over ssh on the hub.

    Hub: the board dataplane is reachable ONLY from the hub, and this repo is
    NOT shared to the hub (verified 2026-07-10 -- /home is not NFS-mounted
    there). So the harness STAGES the RM bins + the manifest into --prod ON the
    hub, and we run the hub's python with PYTHONPATH pointing at the staged
    pyverify package (``--pyverify-dir``, default: the staged --prod dir)."""
    deploy = [
        "-m", "pyverify.cli", "deploy",
        "--host", args.board,
        "--prod", args.prod,
        "--rm", args.rm,
        "--src", "tcp", "--pusher-transport", "tcp",
        "--client-timeout", str(args.timeout),
        # A regression gate must NEVER rewrite the board's boot default
        # (net-protocol.md v0.13: `deploy` persists to the user microSD by
        # default). harness_regression.sh stages the pyverify package fresh
        # on every run, so the staged CLI always knows this flag.
        "--no-persist",
    ]
    if args.windowed:
        deploy.append("--windowed")
    if args.via_hub:
        pyverify_dir = args.pyverify_dir or args.prod
        remote = "PYTHONPATH=%s python3 %s" % (
            shlex.quote(pyverify_dir),
            " ".join(shlex.quote(a) for a in deploy),
        )
        return ["ssh"] + SSH_OPTS + [args.via_hub, remote]
    return [sys.executable] + deploy


def parse_verdict(out: str):
    """The shell's verdict, from either result shape.

    ``pyverify deploy`` prints one JSON object. The legacy staged pusher printed
    ``swap response: {...}``; a hub that still carries only that copy must keep
    working, so both are accepted and the LAST parseable one wins."""
    verdict = None
    for line in out.splitlines():
        line = line.strip()
        payload = None
        if line.startswith("swap response:"):
            payload = line.split(":", 1)[1].strip()
        elif line.startswith("{") and line.endswith("}"):
            payload = line
        if payload is None:
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            verdict = parsed
    return verdict


def judge(resp: dict, expect_rm_id: int):
    """(exit_code, message) for one shell verdict. Pure, so the mapping is
    testable without a board."""
    ok = bool(resp.get("ok"))
    verified = bool(resp.get("verified"))
    try:
        rm_id = _to_int(resp.get("rm_id"))
    except (TypeError, ValueError):
        rm_id = None

    if not ok:
        return 1, "FAIL: shell reported ok=false for the swap."
    if not verified:
        return 1, ("FAIL: swap not verified -- rm_id was NOT read back after "
                   "DECOUPLE released (bug #5 ordering). ok=true alone does not "
                   "prove the RP took the config.")
    if rm_id != expect_rm_id:
        return 1, ("FAIL: rm_id mismatch -- shell reports %s, expected 0x%08X. "
                   "The swap landed on the WRONG RM (or never left the resident "
                   "one)." % (resp.get("rm_id"), expect_rm_id))
    return 0, "OK: resident and verified (rm_id 0x%08X) on hardware." % rm_id


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rm", required=True, help="RM name, e.g. regdemo_b")
    ap.add_argument("--expect-rm-id", required=True, type=lambda v: int(v, 0),
                    help="full 32-bit rm_id the shell must read back, version "
                         "included, e.g. 0x010000B2 (design 0x00B2 @ v1.0)")
    ap.add_argument("--prod", default=None,
                    help="DFX prod dir with the manifest + bins (default: the "
                         "FIELDED shell's, resolved via pyverify.fielded)")
    ap.add_argument("--repo", default=REPO_DEFAULT,
                    help="repo root, for resolving the default --prod and pyverify")
    ap.add_argument("--board", default=BOARD_DEFAULT)
    ap.add_argument("--via-hub", default="",
                    help="fpgahub host to ssh through; empty => run locally (on-hub)")
    ap.add_argument("--pyverify-dir", default=None, dest="pyverify_dir",
                    help="where the pyverify package lives ON THE HUB "
                         "(default: the staged --prod dir)")
    ap.add_argument("--windowed", action="store_true",
                    help="use the lock-step window-grant handshake -- REQUIRED "
                         "against a real FIFO-mode shell")
    ap.add_argument("--timeout", type=float, default=1200.0,
                    help="subprocess ceiling; sits ABOVE the pusher's own so it "
                         "never races an in-flight transfer")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the exact command that would run, and exit 0")
    a = ap.parse_args(argv)

    if a.prod is None:
        try:
            a.prod = default_prod(a.repo)
        except Exception as exc:  # noqa: BLE001 - name it, do not traceback
            print("FAIL: no --prod given and the fielded prod dir could not be "
                  "resolved: %s" % exc)
            return 1

    cmd = build_cmd(a)
    where = "hub %s" % a.via_hub if a.via_hub else "local (on-hub)"
    print("== Tier-3 swap gate: %s -> expect rm_id 0x%08X  [%s] =="
          % (a.rm, a.expect_rm_id, where))
    print("   $ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    if a.dry_run:
        print("   DRY RUN -- nothing was run and no board was touched.")
        return 0

    env = dict(os.environ)
    if not a.via_hub:
        pv = a.pyverify_dir or os.path.join(a.repo, "host", "pyverify")
        env["PYTHONPATH"] = pv + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(cmd, cwd=(a.repo if not a.via_hub else None),
                              capture_output=True, text=True, timeout=a.timeout,
                              env=env)
    except subprocess.TimeoutExpired:
        print("\nFAIL: the pusher did not finish within %.0fs -- do NOT assume it "
              "is safe to touch the board; a transfer may still be in flight."
              % a.timeout)
        return 1
    except OSError as e:
        print("\nFAIL: could not launch the pusher: %r" % e)
        return 1

    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)
    sys.stdout.flush()

    resp = parse_verdict(proc.stdout)
    if resp is None:
        print("\nFAIL: no parseable shell verdict (pusher exit %d). The swap "
              "request never returned one." % proc.returncode)
        return 1

    print("   shell verdict: ok=%s verified=%s rm_id=%s"
          % (resp.get("ok"), resp.get("verified"), resp.get("rm_id")))
    code, message = judge(resp, a.expect_rm_id)
    print("\n" + message)
    return code


if __name__ == "__main__":
    sys.exit(main())
