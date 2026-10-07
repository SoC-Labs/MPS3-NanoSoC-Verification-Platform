"""``webharness.api`` — the whole HTTP surface as ONE pure function.

:func:`handle` takes ``(method, path, body, backend)`` and returns a
:class:`Response`. It opens no sockets, reads no clock it was not given, and
touches no global state — every test in ``tests/test_api.py`` drives it
directly with a :class:`~webharness.backend.FakeBackend` and asserts on the
returned bytes. :mod:`webharness.server` is then a thin
``BaseHTTPRequestHandler`` that does nothing but call this and write the
result, exactly the split ``pyverify.display_http`` uses.

Routes
------

======  ==========================  ===================================
GET     ``/``                       the page (HTML)
GET     ``/healthz``                server liveness; never contacts the board
GET     ``/api/status``             identity + reachability + DUT makeup
GET     ``/api/services``           the attachable-services directory
GET     ``/api/clocks``             presets + what the contract really says
POST    ``/api/clock``              ``{"preset":"50mhz"}`` -> set_clk
GET     ``/api/resets``             reset taxonomy + which are actionable here
POST    ``/api/reset``              ``{"target":"dut"}`` -> reset
GET     ``/api/diag``               the shell's 14 diag counters
GET     ``/api/console/<key>``      how to attach to one console (+ the ws seam)
======  ==========================  ===================================

HTTP status discipline (mirrors ``display_http``): a **transport** failure is
``502`` (the board did not answer); an **application** decline that the shell
itself returned — ``"bad target"``, ``"unknown preset"`` — is ``200`` with
``ok:false``, because the exchange succeeded and the answer is "no". A
malformed request never reaches the board: ``400``. Every body is JSON except
``/``, and every JSON body carries ``ok``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import catalog, control

__all__ = ["Response", "handle", "API_VERSION", "rm_id_indicates_loaded"]

API_VERSION = "1"

_JSON = "application/json"
_HTML = "text/html; charset=utf-8"

#: Backend ``err`` strings that mean "the board did not answer" rather than
#: "the board said no" — these map to 502. Produced by
#: :meth:`webharness.backend.ShellBackend._call`.
_TRANSPORT_ERR_PREFIXES = ("shell unreachable", "shell protocol error")


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes

    def json(self) -> Any:
        """Parse the body as JSON — a test convenience; raises on the HTML page."""
        return json.loads(self.body.decode("utf-8"))


def _json_response(status: int, payload: Dict[str, Any]) -> Response:
    body = json.dumps(payload, sort_keys=False, indent=None).encode("utf-8") + b"\n"
    return Response(status=status, content_type=_JSON, body=body)


def _err(status: int, msg: str, **extra: Any) -> Response:
    payload: Dict[str, Any] = {"ok": False, "err": msg}
    payload.update(extra)
    return _json_response(status, payload)


def _status_for(result: Dict[str, Any]) -> int:
    """200 unless the backend reported a transport failure."""
    if result.get("ok"):
        return 200
    err = str(result.get("err", ""))
    if any(err.startswith(p) for p in _TRANSPORT_ERR_PREFIXES):
        return 502
    return 200


def rm_id_indicates_loaded(rm_id: str) -> bool:
    """An empty / all-zero ``rm_id`` means "no RM loaded".

    Transcribed from ``pyverify.edge._rm_id_indicates_loaded`` (private there,
    so not imported): greybox is the ONE id held at exactly ``0x00000000`` and
    no real design may take design 0, so this stays a full 32-bit test after
    the v2 re-encoding — masking to the design half here would be wrong. A
    non-numeric id counts as loaded ("we don't recognise it but something
    answered"). ``tests/test_api.py`` asserts this agrees with pyverify's copy
    over a spread of ids, so the transcription cannot drift silently.
    """
    if not rm_id:
        return False
    try:
        return int(rm_id, 0) != 0
    except ValueError:
        return True


def _parse_rm_id(rm_id: str) -> Optional[int]:
    try:
        return int(rm_id, 0)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Route implementations
# --------------------------------------------------------------------------- #

def _route_status(backend: Any) -> Response:
    ping = backend.ping()
    telem = backend.telemetry()

    reachable = bool(ping.get("ok"))
    shell_id = str(ping.get("shell_id", "") or "")
    rm_id_s = str(ping.get("rm_id", "") or "")
    rm_num = _parse_rm_id(rm_id_s)
    loaded = reachable and rm_id_indicates_loaded(rm_id_s)

    rm: Dict[str, Any] = {
        "loaded": loaded,
        "rm_id": rm_id_s or None,
        "design": ("0x%04X" % catalog.rm_design(rm_num)) if rm_num is not None else None,
        "name": catalog.rm_name(rm_num) if (loaded and rm_num is not None) else None,
        "caps": catalog.rm_caps(rm_num) if (loaded and rm_num is not None) else None,
    }

    # telemetry is ALWAYS ok:false (there is no power sensor by construction —
    # four independent dead-ends, net-protocol.md v0.6). Report that as a fact
    # of the platform, not as an error the user should chase. `lockup` is the
    # one real field on that line, and it is only MEANINGFUL for RMs that drive
    # dut_lockup (multicore does; nanosoc/eth_ss/OOC RMs hard-tie it to 0).
    lockup_meaningful = rm_num is not None and catalog.rm_design(rm_num) == 0x0003
    telemetry = {
        "power_sensor": False,
        "power_note": "no power sensor on this platform (by construction)",
        "lockup": telem.get("lockup") if reachable else None,
        "lockup_meaningful": lockup_meaningful,
        "lockup_note": ("driven by this RM" if lockup_meaningful else
                        "hard-tied 0 by this RM — False means 'cannot report', "
                        "not 'healthy'"),
    }

    payload = {
        "ok": reachable,
        "api": API_VERSION,
        "backend": backend.info().to_dict(),
        "shell": {
            "reachable": reachable,
            "static_id": shell_id or None,
            "err": "" if reachable else str(ping.get("err", "")),
        },
        "rm": rm,
        "telemetry": telemetry,
        "cached": bool(ping.get("cached", False)),
        "err": "" if reachable else str(ping.get("err", "")),
    }
    return _json_response(_status_for(ping), payload)


def _route_services(backend: Any, host_label: str) -> Response:
    ping = backend.ping()
    rm_id_s = str(ping.get("rm_id", "") or "")
    rm_num = _parse_rm_id(rm_id_s) if ping.get("ok") else None
    if rm_num is not None and not rm_id_indicates_loaded(rm_id_s):
        # greybox / nothing loaded: shell services only, and say why.
        rm_num = 0x0000

    rows = catalog.service_rows(rm_num, host_label)
    payload = {
        "ok": True,
        "host": host_label,
        "rm_id": rm_id_s or None,
        "rm_known": rm_num is not None,
        "services": rows,
        "registry_gap": list(catalog.registry_gap()),
        "note": ("Shell services are up regardless of the resident RM; DUT "
                 "services need the RM to have the CPU/UART behind them. "
                 "rm_id unreadable => shell services only."),
    }
    return _json_response(200, payload)


def _route_clocks(backend: Any) -> Response:
    return _json_response(200, {
        "ok": True,
        "presets": control.clock_rows(),
        "input_mhz": control.CLK_INPUT_MHZ,
        "contract": control.CLK_CONTRACT,
        "backend": backend.info().to_dict(),
    })


def _route_set_clock(backend: Any, body: bytes) -> Response:
    parsed, err = _parse_json_object(body)
    if err:
        return _err(400, err)
    preset = parsed.get("preset")
    if not isinstance(preset, str):
        return _err(400, "bad args: 'preset' (string) is required")
    if control.find_preset(preset) is None:
        # Rejected HERE, without touching the board. The shell's own strcmp
        # table is fail-closed too (clkrst.c:126-140) — driving an unknown
        # preset through to prove that is pyverify's job, not this page's.
        return _err(400, "unknown preset %r (want one of %s)"
                    % (preset, ", ".join(control.preset_names())))
    result = backend.set_clk(preset)
    payload = {
        "ok": bool(result.get("ok")),
        "preset": preset,
        "locked": bool(result.get("locked", False)),
        "err": str(result.get("err", "")),
    }
    if payload["ok"] and not payload["locked"]:
        payload["warn"] = ("MMCM did not report lock within the bounded poll — "
                           "the DUT clock may not have settled")
    return _json_response(_status_for(result), payload)


def _route_resets(backend: Any) -> Response:
    return _json_response(200, {
        "ok": True,
        "targets": list(control.RESET_TARGETS),
        "kinds": control.reset_reference(),
        "note": ("Only target='dut' is accepted by the :6900 reset verb "
                 "(coordinator.c:238 answers 'bad target' otherwise). The other "
                 "kinds are real but are driven by the swap path or the tender, "
                 "not by this server."),
        "backend": backend.info().to_dict(),
    })


def _route_do_reset(backend: Any, body: bytes) -> Response:
    parsed, err = _parse_json_object(body)
    if err:
        return _err(400, err)
    target = parsed.get("target", "dut")
    if not isinstance(target, str):
        return _err(400, "bad args: 'target' must be a string")
    if target not in control.RESET_TARGETS:
        return _err(400, "bad target %r (this server drives only: %s)"
                    % (target, ", ".join(control.RESET_TARGETS)))
    result = backend.reset(target)
    return _json_response(_status_for(result), {
        "ok": bool(result.get("ok")),
        "target": target,
        "err": str(result.get("err", "")),
    })


def _route_diag(backend: Any) -> Response:
    result = backend.diag()
    payload = dict(result)
    payload["ok"] = bool(result.get("ok"))
    return _json_response(_status_for(result), payload)


def _route_console(key: str, host_label: str) -> Response:
    spec = catalog.by_key(key)
    if spec is None or spec.proto != "tcp" or not spec.key.startswith(("uart", "swo")):
        return _err(404, "no console named %r (try: %s)"
                    % (key, ", ".join(s.key for s in catalog.SERVICES
                                      if s.key.startswith(("uart", "swo")))))
    return _json_response(200, {
        "ok": True,
        "key": spec.key,
        "label": spec.label,
        "host": host_label,
        "port": spec.port,
        "command": spec.command(host_label),
        "socat": "socat -,raw,echo=0 tcp:%s:%d" % (host_label, spec.port),
        # The browser-terminal seam. Deliberately advertised as unavailable
        # rather than stubbed with a route that 500s: a WebSocket<->TCP relay
        # plus a vendored terminal emulator is the next increment (README §6),
        # and until it exists the honest answer is the command above.
        "websocket": {
            "available": False,
            "path": "/ws/console/%s" % spec.key,
            "reason": "WebSocket relay not implemented yet — see README §6",
        },
    })


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def _parse_json_object(body: bytes) -> Tuple[Dict[str, Any], str]:
    """Parse a request body as a JSON object.

    An empty body is an empty object (so ``POST /api/reset`` with no body means
    "the default target"). ``"bad json"`` is the wire contract's own string for
    an unparseable line — reused deliberately so an operator sees one
    vocabulary across the HTTP and TCP surfaces.
    """
    if not body or not body.strip():
        return {}, ""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, "bad json"
    if not isinstance(parsed, dict):
        return {}, "bad json: body must be a JSON object"
    return parsed, ""


def handle(
    method: str,
    path: str,
    body: bytes,
    backend: Any,
    *,
    host_label: str = "",
) -> Response:
    """Route one request. Pure: the only I/O is whatever ``backend`` does.

    ``host_label`` is the board host as a user would type it in a command
    (``nc <host> 6930``). It defaults to the backend's target so the rendered
    commands are copy-pasteable without extra configuration.
    """
    if not host_label:
        host_label = backend.info().target or "<shell-host>"

    # Strip a query string; this API takes no query params today, but a browser
    # cache-buster (?t=…) must not 404 the page.
    path = path.split("?", 1)[0]

    if method == "GET":
        if path in ("/", "/index.html"):
            from .pages import render_page
            html = render_page(backend=backend, host_label=host_label)
            return Response(200, _HTML, html.encode("utf-8"))
        if path == "/healthz":
            # Deliberately does NOT contact the board: this answers "is the web
            # server alive", which must stay true when the board is not.
            return _json_response(200, {
                "ok": True, "api": API_VERSION,
                "backend": backend.info().to_dict(),
            })
        if path == "/api/status":
            return _route_status(backend)
        if path == "/api/services":
            return _route_services(backend, host_label)
        if path == "/api/clocks":
            return _route_clocks(backend)
        if path == "/api/resets":
            return _route_resets(backend)
        if path == "/api/diag":
            return _route_diag(backend)
        if path.startswith("/api/console/"):
            return _route_console(path[len("/api/console/"):], host_label)

    elif method == "POST":
        if path == "/api/clock":
            return _route_set_clock(backend, body)
        if path == "/api/reset":
            return _route_do_reset(backend, body)

    return _err(404, "no route for %s %s" % (method, path))
