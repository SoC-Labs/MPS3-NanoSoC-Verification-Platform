#!/usr/bin/env python3
"""Tier-3 ping identity gate: {"op":"ping"} on 6900 must return a shell_id.

Exit 0 and print `shell_id=... rm_id=...` on success; non-zero otherwise. That
stdout shape is read by scripts/harness_regression.sh and must not change.

TWO PATHS, ON PURPOSE
    Normally this is a two-line wrapper over ``pyverify ping --line`` — the
    conformance-pinned ShellClient (its response shapes are held against the
    real firmware by tests/firmware_logic/test_fakeshell_conformance.py), rather
    than a private socket that can drift from the protocol without anything
    noticing.

    But harness_regression.sh ALSO runs this gate by piping the file itself into
    the hub's python over ssh::

        ssh <hub> python3 - <board_ip> < scripts/harness_gates/ping_check.py

    because the board's dataplane is reachable only from the hub and this repo
    is not checked out there. On that path there is nothing to import: no
    pyverify, no repo, no file on disk. So the raw-socket implementation stays,
    as the FALLBACK, and is used only when pyverify cannot be found. Both paths
    emit the identical line.
"""
import json
import os
import sys
from pathlib import Path

board = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "MPS3_BOARD_HOST", "192.168.10.101")
port = int(sys.argv[2]) if len(sys.argv) > 2 else 6900


def _find_pyverify():
    """Locate the package when we are a FILE in the repo (or beside a staged
    copy). Returns True if `import pyverify` will work afterwards."""
    if __file__ == "<stdin>":                       # piped: nothing on disk
        return False
    here = Path(__file__).resolve().parent
    for cand in (os.environ.get("PYVERIFY_DIR"),
                 here.parent.parent / "host" / "pyverify",
                 here):
        if cand and (Path(cand) / "pyverify" / "__init__.py").is_file():
            sys.path.insert(0, str(cand))
            return True
    return False


def _via_pyverify():
    from pyverify.cli import main
    return main(["ping", "--host", board, "--control-port", str(port), "--line"])


def _via_raw_socket():
    """The piped-to-a-bare-remote-python path. Dependency-free by necessity."""
    import socket
    try:
        s = socket.create_connection((board, port), timeout=6)
        s.sendall(b'{"op":"ping"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(256)
            if not chunk:
                break
            buf += chunk
        s.close()
        r = json.loads(buf.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 — report, don't traceback
        print("ping failed: %r" % e)
        return 1
    shell_id = r.get("shell_id")
    print("shell_id=%s rm_id=%s" % (shell_id, r.get("rm_id")))
    return 0 if shell_id else 1


if _find_pyverify():
    try:
        sys.exit(_via_pyverify())
    except ImportError:
        pass            # a broken checkout must not fail the gate differently
sys.exit(_via_raw_socket())
