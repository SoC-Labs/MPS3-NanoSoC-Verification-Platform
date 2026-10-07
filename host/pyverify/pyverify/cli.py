"""``python -m pyverify.cli`` — command-line entry point for pyverify.

Wires the concrete :class:`pyverify.pusher.BitstreamPusher` into
:class:`pyverify.swap.SwapOrchestrator` (which itself depends only on the
``Pusher`` protocol — a test seam, see ``swap.py``'s module docstring).
Referenced by ``host/tender/fpgahub_mps3_plugin.py``'s ``platform_deploy``
action (fpgahub shells out to ``python -m pyverify.cli deploy ...`` from a
manifest Action) and by ``host/notebooks/demo.md``.

pyverify is the FRONT DOOR to the board. Everything a board session needs is
a verb here, over ONE dialect each:

    lease acquire|release|cancel|status|heartbeat|preflight
        the fpgahub board lease (:mod:`pyverify.lease`). ``acquire`` prints the
        BARE token on stdout — ``TOKEN=$(pyverify lease acquire …)`` — and its
        progress on stderr, exactly as ``scripts/mps3_board.sh acquire`` did.
    sd write <bundle>
        the config-SD write, under the one-write-wait discipline
        (:mod:`pyverify.sd`).
    sd field <bit>
        fielding with nobody at the board (:mod:`pyverify.fielding`): ONE SD
        write, fpgahubd's journal witness (``program dispatched ... ok=True
        sha256=<prefix>``), then the paced MCC REBOOT on tty_00 with the
        single-reader check. Exit 0 ok, 1 a gate refused before the write, 2
        usage, 3 hub unreachable, 4 no witness (NO REBOOT), 5 REBOOT refused,
        unacknowledged or the FPGA did not configure.
    ping | version | diag
        the read-only control-channel verbs, over the conformance-pinned
        :class:`~pyverify.client.ShellClient` rather than a hand-rolled socket.
    deploy
        validate -> swap -> push -> await -> persist; ``--overlay <dir>`` or
        the DFX prod-dir shape ``--prod <dir> --rm <name>``. After a verified
        swap the pair is committed to the user microSD (net-protocol.md v0.13)
        unless ``--no-persist``; a commit failure is a stderr WARNING and the
        exit code stays 0.
    usd status|format [--wipe] [--yes]|clear|rescan
        the user-microSD overlay store (net-protocol.md v0.13).

``deploy`` exit codes (all failures are clean one-line stderr messages,
never tracebacks):

    0  swap confirmed by the shell (JSON result on stdout)
    1  the shell/validator rejected the deploy (SwapError /
       OverlayValidationError — e.g. static_id mismatch, swap NAK)
    2  bad overlay manifest (didn't parse / missing keys)
    3  network/transport failure — shell unreachable, control-channel
       connect refused, TFTP/raw-TCP push timeout or server abort

The other verbs use the same three codes: 0 ok, 1 the far end refused (a gate,
a lease that is not ours, an md5 mismatch), 3 the far end is unreachable or
unconfigured (no ``MPS3_HUB``, control channel refused).

The ``edge`` verb group (``edge channels`` / ``edge status``) is the CLI
front-end for :mod:`pyverify.edge` — the MPS3 Edge Device API model
(``docs/HARDWARE_HUB_INTEGRATION.md``). ``edge channels`` needs no network
at all (the channel table is static per-board shape, not live state).
``edge status`` optionally opens a real :class:`~pyverify.client.ShellClient`
against ``--host`` to pull ``rp`` truth (``{"op":"ping"}`` -> ``rm_id``,
net-protocol.md); omitting ``--host`` reports an honest "no shell reachable"
status (``rp.loaded: false``) instead of guessing, exactly like
:meth:`pyverify.edge.EdgeDeviceApi.status_fpga` does when ``shell=None``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import ShellClient
from .edge import EdgeDeviceApi, channel_to_dict, fpga_status_to_dict
from .fielded import FieldedError, overlay_from_prod_dir
from .lease import Lease, LeaseClient, LeaseError, hub_runner_from_env
from .overlay import Overlay, OverlayManifestError, OverlayValidationError
from .pusher import (
    DEFAULT_ACK_WINDOW, DEFAULT_PUSH_TIMEOUT_S, RAW_TCP_PORT, TFTP_PORT, BitstreamPusher,
    PushChoice, PushError, choose_push,
)
from .swap import Pusher, SwapError, SwapOrchestrator


def _build_pusher(
    host: str,
    transport: str,
    *,
    tftp_port: int = TFTP_PORT,
    tcp_port: int = RAW_TCP_PORT,
    windowed: bool = False,
    window: int = DEFAULT_ACK_WINDOW,
    timeout_s: float = DEFAULT_PUSH_TIMEOUT_S,
    partial_retry_s: float = 0.0,
) -> Pusher:
    return BitstreamPusher(
        host=host, transport=transport,  # type: ignore[arg-type]
        tftp_port=tftp_port, tcp_port=tcp_port,
        windowed=windowed, window=window,
        timeout_s=timeout_s, partial_retry_s=partial_retry_s,
    )


def _resolve_deploy_overlay(args: argparse.Namespace) -> Overlay:
    """``--overlay <dir>`` (overlay-manifest.md layout) or ``--prod <dir> --rm
    <name>`` (the DFX prod-dir shape: static_id.txt + overlay_inputs.txt).

    The second form is what ``scripts/mps3_push.py`` used to parse inline
    beside its own copy of the framing and the sockets; reading it through
    :func:`pyverify.fielded.overlay_from_prod_dir` means the ONE pusher drives
    both artefact shapes."""
    if args.overlay:
        return Overlay.load(Path(args.overlay))
    if not (args.prod and args.rm):
        raise OverlayManifestError(
            "need --overlay <dir>, or --prod <dir> together with --rm <name>")
    return overlay_from_prod_dir(Path(args.prod), args.rm)


def _choose_push(client: ShellClient, args: argparse.Namespace) -> PushChoice:
    """The push transport, from what the RUNNING shell says it is
    (:func:`pyverify.pusher.choose_push` -- the ONE rule, shared with
    :class:`pyverify.board.Mps3Board`):

    - ``windowed`` in ``version.features`` (bare-metal PRODUCT, built
      ``HWICAP_FIFO=1 WINDOWED=1``): ``tcp + tcp + windowed`` -- a plain push
      deadlocks it (every silicon sweep used that triple;
      scripts/mps3_w2_proofs.sh ``detect_push_transport``);
    - ``version.impl == "linux"`` (mps3-harnessd): ``tcp + tcp + plain`` with a
      30 s push inactivity limit -- 6910 PARKS a partial that arrives while the
      outgoing clearing is still streaming into the ICAP, where TFTP rejects it
      (silicon B1 v4 2026-09-25: swapping away from nanosoc, a DAP RM, failed
      "TFTP ERROR 0: rejected");
    - anything else, or a shell too old to answer ``version``: the historical
      tftp/plain default.

    Each explicit flag (``--pusher-transport``, ``--src``,
    ``--windowed``/``--no-windowed``) overrides its own part of the choice; an
    explicit TFTP push to a Linux harness gets the busy-ICAP partial re-push
    instead. One stderr line says what was chosen and why (the soak and the B1
    runner parse it: keep its shape).
    """
    choice = choose_push(client, transport=args.pusher_transport, src=args.src,
                         windowed=args.windowed)
    if choice.windowed_dropped:
        print("deploy: --windowed needs --pusher-transport tcp; not windowing a tftp push",
              file=sys.stderr)
    print(f"deploy: transport={choice.transport} src={choice.src} "
          f"windowed={choice.windowed} ({choice.why})", file=sys.stderr)
    return choice


def _select_transport(client: ShellClient, args: argparse.Namespace) -> "tuple[str, str, bool]":
    """``(transport, src, windowed)`` of :func:`_choose_push` (kept for callers
    that only want the triple)."""
    return _choose_push(client, args).as_tuple()


def _quiet_logger(name: str) -> None:
    """Give ``name`` a NullHandler once, so an unconfigured CLI process does
    not get logging's last-resort stderr copy of a warning the CLI prints
    itself. A caller that configures logging still receives the records."""
    import logging
    log = logging.getLogger(name)
    if not any(isinstance(h, logging.NullHandler) for h in log.handlers):
        log.addHandler(logging.NullHandler())


def _deploy_client(args: argparse.Namespace) -> ShellClient:
    """The deploy's ONE control connection, opened and SETTLED: its first exchange
    is a ``ping``, and a connection the shell closes or resets unanswered is
    re-opened, :data:`_REFUSED_ATTEMPTS` x :data:`_REFUSED_GAP_S`, like
    :func:`_shell_call_retrying`. 6900 serves one client, so a deploy opened right
    after another connection closed (a soak's probe, the previous deploy) can be
    turned away until the shell has reaped it; on Linux that arrives as an RST.
    Only the ping is ever re-sent -- once it has answered, the connection is ours
    and every later failure is a real one. A refused connect (nothing listening)
    raises at once, as before."""
    import time
    from .client import CHANNEL_CLOSED_TEXT, ShellChannelClosed
    last: "Exception | None" = None
    for _ in range(_REFUSED_ATTEMPTS):
        client = ShellClient(args.host, port=args.control_port,
                             timeout=args.client_timeout).connect()
        try:
            client.ping()
            return client
        except (ShellChannelClosed, ConnectionResetError, BrokenPipeError) as exc:
            client.close()
            last = exc
        except BaseException:
            client.close()
            raise
        time.sleep(_REFUSED_GAP_S)
    raise ShellChannelClosed(
        "%s: the connection was turned away unanswered %d times (last: %s) -- is "
        "another client holding it?" % (CHANNEL_CLOSED_TEXT, _REFUSED_ATTEMPTS, last))


def _cmd_deploy(args: argparse.Namespace) -> int:
    try:
        overlay = _resolve_deploy_overlay(args)
    except (OverlayManifestError, FieldedError) as exc:
        print(f"deploy: bad overlay manifest: {exc}", file=sys.stderr)
        return 2

    try:
        client = _deploy_client(args)
    except OSError as exc:
        print(f"deploy: cannot reach shell control channel at "
              f"{args.host}:{args.control_port}: {exc}", file=sys.stderr)
        return 3
    try:
        with client:
            choice = _choose_push(client, args)
            src = choice.src
            push_timeout = getattr(args, "push_timeout", None)
            pusher = _build_pusher(
                args.host, choice.transport,
                tftp_port=args.tftp_port, tcp_port=args.tcp_push_port,
                windowed=choice.windowed, window=args.window,
                timeout_s=choice.timeout_s if push_timeout is None else push_timeout,
                partial_retry_s=choice.partial_retry_s,
            )
            orchestrator = SwapOrchestrator(client, pusher)
            # The CLI reports the persist outcome itself (below); keep logging's
            # last-resort handler from printing the same warning a second time.
            _quiet_logger("pyverify.swap")
            result = orchestrator.deploy(overlay, rm_slot=args.rm_slot, src=src,
                                         persist=args.persist)
    except (SwapError, OverlayValidationError) as exc:
        print(f"deploy: {exc}", file=sys.stderr)
        return 1
    except (PushError, OSError) as exc:
        # OSError covers the control-channel socket (connection refused/
        # timed out/unreachable); PushError wraps the same class of
        # failure inside the TFTP/raw-TCP push. Either way: a clean
        # message + exit 3, not a traceback.
        what = (
            "bitstream push failed"
            if isinstance(exc, PushError)
            else f"cannot reach shell control channel at {args.host}:{args.control_port}"
        )
        print(f"deploy: {what}: {exc}", file=sys.stderr)
        return 3

    persist = result.persist
    if persist is not None and persist.status == "failed":
        # A warning, never a failure: the swap stands (net-protocol.md v0.13).
        print(f"deploy: WARNING: the swap stands, but the commit to the user "
              f"microSD failed: {persist.err}", file=sys.stderr)
    elif persist is not None and persist.warning:
        print(f"deploy: WARNING: not persisted to the user microSD: {persist.reason}",
              file=sys.stderr)
    # (no card / no `usd` feature: skipped SILENTLY, as the contract says --
    # the JSON's "persist" still records why)
    print(json.dumps({
        "ok": True,
        "rm_id": result.rm_id,
        "verified": result.verified,
        "ltx_path": result.reattach.ltx_path,
        # additive (v0.13): what the persist step did
        "persist": None if persist is None else {
            "status": persist.status, "slot": persist.slot,
            "reason": persist.reason, "err": persist.err,
        },
    }))
    return 0


def _cmd_display(args: argparse.Namespace) -> int:
    """Flip (or query) the on-board CLCD panel owner over the 6900 control
    channel (net-protocol.md ``display``). ``owner`` omitted => query.

    ``--confirm`` waits for the handover to COMMIT instead of reporting the
    send-now reply. The distinction is not cosmetic: `display` answers with
    ``CLCDKVM.STATUS.owner``, the committed owner, and a flip takes a drain ->
    panel reset -> settle -> grant (~7-9 ms), so the reply to a flip routinely
    names the owner that is going AWAY (net-protocol.md "Display" says so, and
    ends "a client confirms the landing with a follow-up query"). Without
    ``--confirm`` this verb prints that reply verbatim, which is the honest
    wire truth and is exactly what you do NOT want to paste into a bring-up log
    as evidence that the DUT got the panel.

    Exit codes mirror ``deploy``'s discipline (clean one-line stderr, never a
    traceback): 0 = ok, 1 = the shell declined the verb (``ok:false`` — e.g.
    ``clcd_kvm not present`` on a pre-Wave-4 bitstream, or a bad owner), 3 =
    the control channel is unreachable. Under ``--confirm`` a request the shell
    ACCEPTED but that did not commit inside ``--timeout`` exits **2**: that is
    INCONCLUSIVE, not a failure, and it must not be reported as either a
    landed flip (0) or a refusal (1). docs/CLCD_KVM_PLAN.md "Proving it" keys
    the board procedure on exactly that distinction.
    """
    client = ShellClient(args.host, port=args.control_port)
    try:
        with client:
            if args.owner is None:
                resp = client.display_owner()
            elif args.confirm:
                settled = client.display_settled(
                    args.owner, timeout=args.timeout,
                    interval=args.poll_interval)
                print(json.dumps({
                    "ok": settled.ok,
                    "requested": settled.requested,
                    "owner": settled.owner,
                    "landed": settled.landed,
                    "polls": settled.polls,
                    "waited_s": round(settled.waited_s, 3),
                    "err": settled.err,
                }))
                if not settled.ok:
                    return 1
                return 0 if settled.landed else 2
            else:
                resp = client.display(args.owner)
    except OSError as exc:
        print(
            f"display: cannot reach shell control channel at "
            f"{args.host}:{args.control_port}: {exc}",
            file=sys.stderr,
        )
        return 3

    print(json.dumps({"ok": resp.ok, "owner": resp.owner, "err": resp.err}))
    return 0 if resp.ok else 1


def _cmd_edge_channels(args: argparse.Namespace) -> int:
    api = EdgeDeviceApi(board=args.board)
    channels = [channel_to_dict(c) for c in api.enumerate_channels()]
    print(json.dumps({"board": args.board, "channels": channels}, indent=2))
    return 0


def _cmd_edge_status(args: argparse.Namespace) -> int:
    shell: ShellClient | None = None
    if args.host:
        # Real socket open, mirroring _cmd_deploy above.
        shell = ShellClient(args.host, port=args.control_port)
        try:
            shell.connect()
        except OSError as exc:
            print(
                f"edge status: cannot reach shell control channel at "
                f"{args.host}:{args.control_port}: {exc}",
                file=sys.stderr,
            )
            return 3
    try:
        api = EdgeDeviceApi(board=args.board, shell=shell)
        status = api.status_fpga()
    finally:
        if shell is not None:
            shell.close()

    print(json.dumps(fpga_status_to_dict(status), indent=2))
    return 0


# --------------------------------------------------------------------------- #
# lease — the fpgahub board lease, one dialect (pyverify.lease)
# --------------------------------------------------------------------------- #


def _lease_client(args: argparse.Namespace) -> LeaseClient:
    runner = getattr(args, "_runner", None) or hub_runner_from_env()
    return LeaseClient(runner, chassis=args.chassis, target=args.target)


def _holder(args: argparse.Namespace) -> "str | None":
    return args.holder or os.environ.get("MPS3_LEASE_HOLDER")


def _cmd_lease(args: argparse.Namespace) -> int:
    """Every lease verb, sharing one error discipline: a hub that is not
    configured or not reachable is exit 3, a hub that REFUSED is exit 1, and
    neither is ever a traceback."""
    try:
        client = _lease_client(args)
    except LeaseError as exc:
        print(f"lease: {exc}", file=sys.stderr)
        return 3

    verb = args.lease_verb
    try:
        if verb == "acquire":
            holder = _holder(args) or _default_holder()
            lease = client.acquire(
                holder, ttl=args.ttl, tier=args.tier,
                poll_s=args.poll, timeout_s=args.acquire_timeout,
                allow_preemptible=args.allow_preemptible,
                log=lambda m: print(m, file=sys.stderr),
            )
            # ONLY the token on stdout: TOKEN=$(pyverify lease acquire ...).
            print(lease.token)
            return 0
        if verb == "release":
            client.release(args.token, holder=_holder(args) or _default_holder())
            print("released")
            return 0
        if verb == "cancel":
            holder = _holder(args) or _default_holder()
            removed = client.cancel(holder)
            print("cancelled" if removed else "no matching wait to cancel")
            return 0
        if verb == "heartbeat":
            client.heartbeat(args.token, holder=_holder(args) or _default_holder(),
                             ttl=args.ttl)
            print("extended")
            return 0
        if verb == "status":
            st = client.status()
            print(json.dumps({"board": client.chassis, "held": st.held,
                              "holder": st.holder, "user": st.user,
                              "raw": st.raw}))
            return 0
        if verb == "preflight":
            holder = _holder(args)
            if client.preflight(holder):
                print(f"preflight OK: {client.chassis} is unheld or held by us")
                return 0
            st = client.status()
            print(
                f"preflight REFUSE: {client.chassis} held by {st.holder} -- do "
                f"not touch the board (no bitstream load, no xsdb stop, no 6910 "
                f"stream)", file=sys.stderr)
            return 1
    except LeaseError as exc:
        print(f"lease {verb}: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled lease verb {verb!r}")  # pragma: no cover


def _default_holder() -> str:
    """A holder that identifies THIS run. Never a bare hostname: several agents
    share one host and one unix user, and "held by me" must not be guessable
    from the user alone (that mistake clobbered a live board once)."""
    import socket as _sock
    return "%s-%s-%d" % (
        os.environ.get("USER", "user"), _sock.gethostname().split(".")[0], os.getpid())


# --------------------------------------------------------------------------- #
# sd — the config-SD write (pyverify.sd)
# --------------------------------------------------------------------------- #


def _cmd_sd_write(args: argparse.Namespace) -> int:
    from .sd import SdWriteError, SdWriter, sd_field_hint

    try:
        runner = getattr(args, "_runner", None) or hub_runner_from_env()
        lease = LeaseClient(runner, chassis=args.chassis, target=args.target)
    except LeaseError as exc:
        print(f"sd write: {exc}", file=sys.stderr)
        return 3

    writer = SdWriter(
        runner, lease=lease, target=lease.target, wait_s=args.wait,
        state_dir=args.state_dir, log=lambda m: print(m, file=sys.stderr),
    )
    holder = _holder(args) or _default_holder()
    try:
        result = writer.write(
            args.bundle,
            holder=holder,
            lease=(Lease(token=args.token, holder=_holder(args) or _default_holder(),
                         target=lease.target) if args.token else None),
            backup_dir=args.backup,
            require_backup=not args.no_backup,
            verify_path=args.verify_path,
            force=args.force,
        )
    except SdWriteError as exc:
        print(f"sd write: {exc}", file=sys.stderr)
        return 1
    except LeaseError as exc:
        print(f"sd write: {exc}", file=sys.stderr)
        return 3

    print(json.dumps({
        "ok": result.ok,
        "bundle": str(result.bundle),
        "target": result.target,
        "timed_out": result.timed_out,
        "waited_s": result.waited_s,
        "verified": result.verified,
        "verify_reason": result.verify_reason,
        "md5": result.md5,
        "fielded": result.fielded,
        "next": sd_field_hint(result.bundle, holder),
    }))
    return 0


def _cmd_sd_field(args: argparse.Namespace) -> int:
    """``sd field``: ONE SD write -> the journal witness -> the paced MCC REBOOT
    (:mod:`pyverify.fielding`). The record is JSON on stdout (and ``--out``);
    progress is on stderr."""
    import re
    import shlex

    from . import fielding as fl
    from .bootrate import BootRateInputError, MccTtyResetter
    from .lease import DEFAULT_TARGET, SshHubRunner

    holder = _holder(args)
    if not holder:
        print("sd field: --holder (or MPS3_LEASE_HOLDER) is required: this writes the "
              "config SD and reboots a shared board, so the lease it runs under must be "
              "named, not guessed", file=sys.stderr)
        return fl.EXIT_USAGE
    if args.witness_deadline <= 0 or args.poll <= 0 or args.since <= 0:
        print("sd field: --witness-deadline, --poll and --since must be > 0", file=sys.stderr)
        return fl.EXIT_USAGE
    if args.expect_sha256 and not re.fullmatch(r"[0-9a-fA-F]{12,64}", args.expect_sha256):
        print("sd field: --expect-sha256 wants 12-64 hex digits (the bit's sha256 or "
              "its prefix)", file=sys.stderr)
        return fl.EXIT_USAGE
    target = args.target or os.environ.get("MPS3_LEASE_TARGET") or DEFAULT_TARGET
    tty = args.mcc_tty or "/dev/%s/tty_00" % target
    runner: Any = getattr(args, "_runner", None)
    if runner is None and args.hub:
        runner = SshHubRunner(args.hub)

    def _mcc(run: Any) -> Any:
        if args.no_reboot:
            return None
        return MccTtyResetter(run, tty_path=tty, pace_s=args.mcc_pace,
                              settle_s=args.mcc_settle, python=args.hub_python,
                              capture_s=args.mcc_capture, log_path=args.mcc_log)

    try:
        planned = _mcc(runner or (lambda *a, **k: None))     # validates --mcc-pace
    except BootRateInputError as exc:
        print(f"sd field: {exc}", file=sys.stderr)
        return fl.EXIT_USAGE
    if args.journal_file:
        jdesc = ("stdin (exactly-timestamped lines only)" if args.journal_file == "-"
                 else f"the capture file {args.journal_file} on the hub")
    else:
        jdesc = fl.CommandJournal(lambda *a, **k: None, cmd=args.journal_cmd,
                                  unit=args.journal_unit).describe()
    plan = [
        "sd field plan:",
        f"  board        : {target} (lease held by {holder}; never acquired or released here)",
        f"  hub          : {args.hub or 'MPS3_HUB / MPS3_ON_HUB'}",
        f"  bit          : {args.bit} (a path ON THE HUB; its sha256 keys the witness"
        + (f", must start {args.expect_sha256.lower()}" if args.expect_sha256 else "") + ")",
        "  gates        : lease held by the holder; bit readable; journal readable; no "
        "SD write in flight; one tty_00 reader -- all BEFORE the card is touched",
        "  write        : " + ("SKIPPED (--already-written; witness looked for in the "
                               f"last {args.since:g} min)" if args.already_written else
                               "$ " + " ".join(shlex.quote(a) for a in
                                               fl.program_argv(target, args.bit))
                               + "   ONCE, never retried"),
        f"  witness      : `program dispatched: board={target} method=sd ... ok=True ... "
        f"sha256=<first 12>` via {jdesc}; deadline {args.witness_deadline:g} s, poll "
        f"{args.poll:g} s",
        "  reboot       : " + ("NONE (--no-reboot)" if planned is None else
                               planned.describe()
                               + (f"; boot log captured {args.mcc_capture:g} s"
                                  + (f" -> {args.mcc_log}" if args.mcc_log else "")
                                  if args.mcc_capture > 0 else "")),
    ]
    if args.dry_run:
        print("\n".join(plan))
        print("\n(dry run: nothing was contacted, nothing was written)")
        return fl.EXIT_OK
    try:
        runner = runner or hub_runner_from_env()
    except LeaseError as exc:
        print(f"sd field: {exc}", file=sys.stderr)
        return fl.EXIT_HUB
    if args.journal_file == "-":
        journal: Any = fl.StreamJournal(sys.stdin)
    elif args.journal_file:
        journal = fl.FileJournal(runner, args.journal_file)
    else:
        journal = fl.CommandJournal(runner, cmd=args.journal_cmd, unit=args.journal_unit)
    print("\n".join(plan), file=sys.stderr)
    fielder = fl.SdFielder(
        runner, target=target,
        lease=LeaseClient(runner, chassis=args.chassis, target=target),
        holder=holder, journal=journal, mcc=_mcc(runner),
        witness_deadline_s=args.witness_deadline, poll_s=args.poll,
        log=lambda m: print(m, file=sys.stderr))
    result = fielder.field(args.bit, expect_sha256=args.expect_sha256,
                           already_written=args.already_written,
                           since_s=args.since * 60.0, reboot=not args.no_reboot)
    text = json.dumps(result.record, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return result.exit_code


# --------------------------------------------------------------------------- #
# ping / version / diag — the read-only control-channel verbs
# --------------------------------------------------------------------------- #


def _shell_read(args: argparse.Namespace, call, label: str):
    """Open the control channel, run one read-only verb, close. Exit 3 on an
    unreachable shell, exactly like ``deploy``."""
    client = ShellClient(args.host, port=args.control_port, timeout=args.timeout)
    try:
        with client:
            return call(client)
    except OSError as exc:
        print(
            f"{label}: cannot reach shell control channel at "
            f"{args.host}:{args.control_port}: {exc}",
            file=sys.stderr,
        )
        return None


#: 6900 serves ONE client: a connection opened just after another one closed can
#: be accepted and closed UNANSWERED until the server notices the first has gone.
#: :func:`_shell_call_retrying` retries that (like ``slot._one_line``): 20 x 50 ms.
_REFUSED_ATTEMPTS = 20
_REFUSED_GAP_S = 0.05


def _shell_call_retrying(args: argparse.Namespace, call, label: str):
    """:func:`_shell_read` for a verb sent right after another 6900 connection
    (``usd format`` after its state read): a connection the shell closes
    unanswered (``ShellChannelClosed``) or resets is retried, 20 x 50 ms, the way
    ``slot._one_line`` does. A refused connect (nothing listening) is not retried:
    exit 3 at once, like :func:`_shell_read`. A retry re-sends only a request the
    shell did not answer; 6900 turns a second client away before reading it."""
    import time
    from .client import ShellChannelClosed
    last: "Exception | None" = None
    for _ in range(_REFUSED_ATTEMPTS):
        client = ShellClient(args.host, port=args.control_port, timeout=args.timeout)
        try:
            with client:
                return call(client)
        except (ShellChannelClosed, ConnectionResetError, BrokenPipeError) as exc:
            last = exc
        except OSError as exc:
            print(f"{label}: cannot reach shell control channel at "
                  f"{args.host}:{args.control_port}: {exc}", file=sys.stderr)
            return None
        time.sleep(_REFUSED_GAP_S)
    print(f"{label}: the shell control channel at {args.host}:{args.control_port} kept "
          f"closing the connection unanswered ({last}): is another client holding it?",
          file=sys.stderr)
    return None


def _cmd_ping(args: argparse.Namespace) -> int:
    """``{"op":"ping"}`` -> shell_id / rm_id.

    Replaces ``scripts/harness_gates/ping_check.py``'s hand-rolled socket. The
    ``--line`` form keeps that gate's exact ``shell_id=… rm_id=…`` stdout, which
    ``scripts/harness_regression.sh`` reads."""
    resp = _shell_read(args, lambda c: c.ping(), "ping")
    if resp is None:
        return 3
    if args.line:
        print(f"shell_id={resp.shell_id} rm_id={resp.rm_id}")
    else:
        print(json.dumps({"ok": resp.ok, "shell_id": resp.shell_id,
                          "rm_id": resp.rm_id}))
    return 0 if resp.shell_id else 1


def _cmd_version(args: argparse.Namespace) -> int:
    """``version`` -> the FIRMWARE identity. ``ping`` cannot give this: one
    static_id serves many harness releases (a firmware-only bump re-bakes the
    bitstream via updatemem without changing the static routing), so a board can
    report the expected static_id and still run an image built with the wrong
    flags (client.py's VersionResponse)."""
    resp = _shell_read(args, lambda c: c.version(), "version")
    if resp is None:
        return 3
    print(json.dumps({
        "ok": resp.ok, "harness": resp.harness, "ver32": resp.ver32,
        "sha": resp.sha, "dirty": resp.dirty, "lmb_kb": resp.lmb_kb,
        "features": list(resp.features),
        # additive (ILA-mint finding #15): the fabric cross-check the raw reply and
        # ShellClient.version() always carried. usr_access null = the fabric value
        # could not be read; skew true/false/null is the FIRMWARE's verdict, and
        # skew_verdict spells the three states out ("ok" | "SKEW" | "unchecked")
        "usr_access": resp.usr_access or None, "skew": resp.skew,
        "skew_verdict": resp.skew_verdict,
        # additive (Linux): the identity lock's reason; null = none reported
        "id_skew": resp.id_skew,
        # additive: which ENGINE answered ("bare-metal" when the key is absent)
        "impl": resp.impl,
    }))
    return 0 if resp.ok else 1


def _cmd_diag(args: argparse.Namespace) -> int:
    """``diag`` -> the always-on counters. NOTE 6900 is PARKED during a swap, so
    this is an idle-time verb; during a swap read the same counters from the
    DMEM mailbox over JTAG-MDM."""
    resp = _shell_read(args, lambda c: c.diag(), "diag")
    if resp is None:
        return 3
    from dataclasses import asdict
    out = asdict(resp)
    # `present` (the keys the shell actually sent) is a frozenset: JSON has no
    # set, so it goes out sorted. Additive: every pre-existing key is unchanged.
    out["present"] = sorted(out.get("present") or ())
    print(json.dumps(out))
    return 0 if resp.ok else 1



def _cmd_set_clk(args: argparse.Namespace) -> int:
    """``{"op":"set_clk","preset":...}`` -> retune the DUT clock (MMCM DRP at
    0x44AB_0000). The preset is checked against the firmware's table
    (``client.DEFAULT_CLK_PRESETS``) unless ``--any-preset``. Exit 0 only when
    the shell says ok AND the MMCM re-locked. B5 of the ILA proof ladder drives
    this with the XVC target OPEN (scripts/mps3_ila_proofs.sh)."""
    from .client import DEFAULT_CLK_PRESETS
    presets = None if args.any_preset else DEFAULT_CLK_PRESETS
    try:
        resp = _shell_read(args, lambda c: c.set_clk(args.preset, presets=presets), "set-clk")
    except ValueError as exc:
        print(f"set-clk: {exc}", file=sys.stderr)
        return 2
    if resp is None:
        return 3
    print(json.dumps({"ok": resp.ok, "preset": args.preset, "locked": resp.locked}))
    return 0 if (resp.ok and resp.locked) else 1


def _cmd_reset(args: argparse.Namespace) -> int:
    """``{"op":"reset","target":...}`` -> one of the shell-driven resets
    (default: the DUT). Used to replay a boot while an ILA is armed."""
    resp = _shell_read(args, lambda c: c.reset(args.target), "reset")
    if resp is None:
        return 3
    print(json.dumps({"ok": resp.ok, "target": args.target}))
    return 0 if resp.ok else 1


def _cmd_dut_console(args: argparse.Namespace) -> int:
    """The DUT's console on TCP 6930/6931 (net-protocol.md; the relay taps the
    DUT's UART). ``--send`` types a line PACED at ``--pace-ms`` per byte
    (default 20: the DUT's RX has little or no buffering and no flow control, and
    line-rate input arrives as plausible-looking WRONG commands -- ILA-mint
    finding #17), then prints what comes back for ``--seconds``, raw, to
    stdout. Exit 3 when the port cannot be reached."""
    import socket
    import time
    from .console import UART0_PORT, UART1_PORT, ConsoleReader
    port = args.port if args.port is not None else (UART1_PORT if args.uart == 1 else UART0_PORT)
    pace_s = max(0.0, args.pace_ms) / 1000.0
    con = ConsoleReader(args.host, port, timeout=args.timeout, pace_s=pace_s)
    try:
        con.connect()
    except OSError as exc:
        print(f"dut-console: cannot reach {args.host}:{port}: {exc}", file=sys.stderr)
        return 3
    try:
        if args.send is not None:
            eol = b"" if args.no_eol else b"\r"
            n = con.write_line(args.send, eol=eol)
            print(f"dut-console: sent {n} B to {args.host}:{port} at {args.pace_ms:g} ms/char",
                  file=sys.stderr)
        deadline = time.monotonic() + max(0.0, args.seconds)
        out = sys.stdout.buffer
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            con.timeout = min(left, 0.5)
            try:
                chunk = con.read()
            except socket.timeout:
                continue
            if not chunk:
                break
            out.write(chunk)
            out.flush()
    except OSError as exc:
        print(f"dut-console: {args.host}:{port}: {exc}", file=sys.stderr)
        return 3
    finally:
        con.close()
    return 0


def _cmd_dut_eth(args: argparse.Namespace) -> int:
    """``dutrx`` -> the frames the DUT TRANSMITTED (net-protocol.md v0.10).

    The other half of DUT ethernet. Reception has been silicon-proven since
    2026-07-30; the return path did not exist until the capture block was
    written, so a DUT could be talked to and could not answer.

    Reads up to --frames whole frames (following `more` across 256-byte chunks),
    stopping early when the FIFO is empty, and always prints the DROP COUNTERS
    beside them: the block cannot backpressure the
    bridge, so it drops, and `rx + drop_full + drop_giant` is the only place a
    lossy capture shows up. Reading zero frames is a RESULT, not a failure --
    an idle DUT looks exactly like that -- so it exits 0; a DECLINED verb
    ("dut_egress not present": a bitstream with no capture block) exits 1.
    """
    if args.frames < 1:
        print(f"dut-eth: --frames must be >= 1, got {args.frames}", file=sys.stderr)
        return 2

    def read(client):
        frames = []
        last = None
        for _ in range(args.frames):
            frame, last = client.read_dut_frame()
            if frame is None:
                break       # FIFO empty (or the verb declined) -- a result
            frames.append(frame)
            if last.frames == 0:
                break       # nothing else waiting; never spin on an idle FIFO
        return frames, last

    result = _shell_read(args, read, "dut-eth")
    if result is None:
        return 3
    frames, last = result
    if last is None:                      # pragma: no cover - frames >= 1 always reads
        return 3
    out = {
        "ok": last.ok,
        "frames_read": len(frames),
        "waiting": last.frames,
        "rx": last.rx,
        "drop_full": last.drop_full,
        "drop_giant": last.drop_giant,
        "ovf": last.ovf,
        "desync": last.desync,
    }
    if not last.ok:
        out["err"] = last.err
    else:
        out["frames"] = [{"len": len(f), "hex": f.hex()} for f in frames]
    print(json.dumps(out))
    return 0 if last.ok else 1


# --------------------------------------------------------------------------- #
# net-protocol v0.11 -- stats / log / touch-cal / reboot
# --------------------------------------------------------------------------- #


def _cmd_stats(args: argparse.Namespace) -> int:
    """``stats`` -> the board state in one line (fpgahub's key shape). Printed in
    WIRE order so the line can be forwarded as-is. Exit 1 on a pre-v0.11 shell
    (``unknown op``)."""
    resp = _shell_read(args, lambda c: c.stats(), "stats")
    if resp is None:
        return 3
    print(json.dumps(resp.raw if resp.raw is not None else {"ok": resp.ok}))
    return 0 if resp.ok else 1


def _cmd_log(args: argparse.Namespace) -> int:
    """``log`` -> the shell console from its RAM ring. Prints the text (or, with
    ``--json``, the reassembled chunk metadata). ``--off`` resumes a tail."""
    result = _shell_read(args, lambda c: c.read_log(args.off), "log")
    if result is None:
        return 3
    text, last, nxt = result
    if args.json:
        print(json.dumps({"ok": last.ok, "from": args.off, "next_off": nxt,
                          "dropped": last.dropped, "more": last.more,
                          "text": text.decode("latin-1"),
                          **({"err": last.err} if not last.ok else {})}))
    else:
        sys.stdout.write(text.decode("latin-1"))
        if not last.ok:
            print(f"log: {last.err}", file=sys.stderr)
    return 0 if last.ok else 1


def _cmd_touch_cal(args: argparse.Namespace) -> int:
    """``touch_cal`` get / set / raw / default (v0.11)."""
    from dataclasses import asdict
    try:
        resp = _shell_read(args, lambda c: c.touch_cal(args.act, args.coeffs), "touch-cal")
    except ValueError as exc:
        print(f"touch-cal: {exc}", file=sys.stderr)
        return 2
    if resp is None:
        return 3
    print(json.dumps(asdict(resp)))
    return 0 if resp.ok else 1


def _cmd_reboot(args: argparse.Namespace) -> int:
    """``reboot`` -> warm-restart the shell via its watchdog (v0.11). Without
    ``--wait`` prints the reply and exits; with it, waits ``in_ms`` + 2 s, then
    confirms with a fresh ``stats`` that ``up_ms`` restarted."""
    resp = _shell_read(args, lambda c: c.reboot(), "reboot")
    if resp is None:
        return 3
    out: "dict[str, Any]" = {"ok": resp.ok, "in_ms": resp.in_ms}
    if not resp.ok:
        out["err"] = resp.err
        print(json.dumps(out))
        return 1
    if args.wait:
        import time as _time
        _time.sleep(resp.in_ms / 1000.0 + 2.0)
        after = _shell_read(args, lambda c: c.stats(), "reboot")
        out["after_up_ms"] = None if after is None else after.up_ms
        out["rebooted"] = bool(after is not None and after.ok
                               and after.up_ms < resp.in_ms + 10_000)
        print(json.dumps(out))
        return 0 if out["rebooted"] else 1
    print(json.dumps(out))
    return 0


# --------------------------------------------------------------------------- #
# boot-rate — measure the MCC-config-from-SD boot lottery (pyverify.bootrate)
# --------------------------------------------------------------------------- #


def _cmd_boot_rate(args: argparse.Namespace) -> int:
    """Run a campaign, or compare two of them, or print the record schema.

    Exit codes follow the file's discipline: 0 the campaign ran (a 0% boot rate
    is a RESULT, not a failure), 1 a gate refused (no lease; a second reader on
    the MCC tty, or fpgahub's reset.mcc writing to another tty -- the record is
    still written, marked ``aborted``), 2 bad input (unknown variant, unreadable
    record), 3 the hub is unreachable or unconfigured.
    """
    from . import bootrate as br
    from .lease import SshHubRunner

    verb = getattr(args, "boot_verb", None)
    if verb == "schema":
        print(json.dumps(br.RUN_RECORD_SCHEMA, indent=2))
        return 0
    if verb == "compare":
        try:
            before = br.load_record(args.before)
            after = br.load_record(args.after)
            result = br.compare(before, after)
        except br.BootRateInputError as exc:
            print(f"boot-rate compare: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2) if args.json
              else br.format_compare(result))
        return 0

    # -- a campaign ---------------------------------------------------------- #
    if not args.host:
        print("boot-rate: --host <board-ip> is required (the ping probe is how "
              "a boot is counted). `boot-rate compare`/`schema` need no board.",
              file=sys.stderr)
        return 2
    try:
        br.check_method(args.method)            # refused BEFORE anything, dry run included
        delta = br.variant_delta(args.variant) if args.variant else None
        if args.expect_slot and args.expect_slot.upper() not in ("A", "B"):
            raise br.BootRateInputError("--expect-slot must be A or B")
        if args.deadline is not None and args.deadline <= 0:
            raise br.BootRateInputError("--deadline must be greater than 0")
    except br.BootRateError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 1
    except br.BootRateInputError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 2

    from .lease import DEFAULT_TARGET
    target = args.target or os.environ.get("MPS3_LEASE_TARGET") or DEFAULT_TARGET
    # The runner is only BUILT here (no command runs until the campaign):
    # --hub names the hub to ssh into; otherwise MPS3_HUB / MPS3_ON_HUB.
    runner: Any = getattr(args, "_runner", None)
    if runner is None and args.hub:
        runner = SshHubRunner(args.hub)

    def _make_resetter(run: Any) -> Any:
        if args.method != br.MCC_METHOD:
            return br.HubResetter(run, target=target)
        return br.MccRebootResetter(
            run, target=target, tty_path=args.mcc_tty, route=args.mcc_route,
            pace_s=args.mcc_pace, settle_s=args.mcc_settle, python=args.hub_python)

    try:
        # validates --mcc-route / --mcc-pace before anything, dry run included
        planned_reset = _make_resetter(runner or (lambda *a, **k: None))
    except br.BootRateInputError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 2
    stage0_target = (args.stage0_ssh if args.stage0_ssh
                     and args.stage0_ssh.lower() != "none" else None)

    holder = _holder(args)
    deadline_txt = (f"{args.deadline}s per boot" if args.deadline is not None
                    else f"{br.LINUX_DEADLINE_S:g}s per boot (Linux)" if args.linux
                    else f"{br.DEFAULT_DEADLINE_S:g}s per boot, "
                         f"{br.LINUX_DEADLINE_S:g}s if the board reports impl=linux")
    plan = [
        "boot-rate plan:",
        f"  board        : {args.chassis or 'MPS3_CHASSIS/default'} "
        f"(lease target {target})",
        f"  hub          : {args.hub or 'MPS3_HUB / MPS3_ON_HUB'}"
        f" (lease {'token given: heartbeat only' if args.token else 'NOT acquired or released here'})",
        f"  holder       : {holder or '(NOT SET -- required)'}",
        f"  shell        : {args.host}:{args.control_port}",
        f"  iterations   : {args.n}",
        f"  reset method : {args.method}"
        + (f" -- {planned_reset.describe()}" if hasattr(planned_reset, "describe") else ""),
        f"  deadline     : {deadline_txt} (poll {args.poll}s, "
        f"gap {args.gap}s, down-window {args.down_deadline}s)",
        f"  engine       : {'Linux (flag)' if args.linux else 'from version.impl at the start'}",
        f"  Linux checks : os_up_ms fresh, identify mode run (UDP {args.identify_port}), "
        f"stage0 {'via ssh devmem on ' + stage0_target if stage0_target else 'NOT read'}"
        f" (boot_count 1, slot {args.expect_slot or 'default'}, confirmed)",
        f"  SD variant   : {args.variant or '(none -- default bundle)'}",
    ]
    if delta:
        plan.append(f"                 {delta['description']}")
        for fname, keys in sorted(delta["keys"].items()):
            for key, value in sorted(keys.items()):
                plan.append(f"                 {fname}: {key} -> {value}")
    plan += [
        f"  MCC log      : {args.mcc_log or '(none -- mcc_log_sha will be null)'}",
        f"  witness      : {args.witness_cmd or '(none -- JTAG DONE/USERCODE not used)'}",
        f"  output       : {args.out or '(stdout summary only)'}",
        f"  evidence     : {args.evidence_dir + '/boot_rate_<YYYYMMDD>.csv + .md' if args.evidence_dir else '(none -- --evidence-dir ' + br.EVIDENCE_DIR_HINT + ')'}",
        "",
        "  The SD must ALREADY carry this variant: this tool measures the card "
        "in the board, it does not write one.",
        "  Assemble + write it with: fpga/mps3_sd/assemble_sd.sh C <bit> "
        f"--variant {args.variant or '<NAME>'}  ->  pyverify sd write",
    ]
    if args.dry_run:
        print("\n".join(plan))
        print("\n(dry run: nothing was contacted, nothing was written)")
        return 0

    try:
        runner = runner or hub_runner_from_env()
        lease = LeaseClient(runner, chassis=args.chassis, target=target)
    except LeaseError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 3

    stage0 = None
    if stage0_target:
        from .mailbox import SshDevmemReader, read_stage0_status

        reader = SshDevmemReader(stage0_target)
        stage0 = lambda: read_stage0_status(reader)          # noqa: E731
    witness = br.CommandWitness(args.witness_cmd) if args.witness_cmd else None
    runner_obj = br.BootRateRunner(
        probe=br.ShellProbe(args.host, port=args.control_port,
                            timeout=args.probe_timeout,
                            identify_port=args.identify_port, linux=args.linux),
        resetter=_make_resetter(runner),
        lease=lease, holder=holder, runner=runner,
        mcc_log_path=args.mcc_log, witness=witness,
        host=args.host, control_port=args.control_port,
        log=lambda m: print(m, file=sys.stderr),
        stage0=stage0,
        stage0_via=(f"ssh devmem via {stage0_target}" if stage0_target else None),
    )
    print("\n".join(plan), file=sys.stderr)
    try:
        record = runner_obj.run(
            n=args.n, method=args.method, deadline_s=args.deadline,
            poll_s=args.poll, gap_s=args.gap,
            down_deadline_s=args.down_deadline, await_down=not args.no_await_down,
            variant=args.variant, token=args.token,
            notes=(args.note or []),
            linux=(True if args.linux else None),
            expect_slot=args.expect_slot, confirm_grace_s=args.confirm_grace,
        )
    except br.BootRateError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 1
    except br.BootRateInputError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 2
    except LeaseError as exc:
        print(f"boot-rate: {exc}", file=sys.stderr)
        return 3

    if args.out:
        Path(args.out).write_text(json.dumps(record, indent=2) + "\n")
        print(f"boot-rate: run record written to {args.out}", file=sys.stderr)
    if args.evidence_dir:
        csv_path, md_path = br.write_evidence(record, args.evidence_dir)
        print(f"boot-rate: evidence written to {csv_path} and {md_path}",
              file=sys.stderr)
    print(json.dumps(record["summary"], indent=2))
    if record["campaign"].get("aborted"):
        print(f"boot-rate: {record['campaign']['aborted']}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyverify", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)

    deploy = sub.add_parser("deploy", help="validate -> swap -> push -> await an overlay onto a shell")
    deploy.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
    deploy.add_argument("--overlay", default=None,
                        help="overlay directory (overlay-manifest.md layout)")
    deploy.add_argument("--prod", default=None,
                        help="DFX prod dir (static_id.txt + overlay_inputs.txt); "
                             "use with --rm. Defaults come from "
                             "pyverify.fielded, never from a hardcoded build dir")
    deploy.add_argument("--rm", default=None,
                        help="RM name inside --prod, e.g. regdemo_b")
    deploy.add_argument("--rm-slot", type=int, default=0, dest="rm_slot")
    # --src / --pusher-transport / --windowed default to AUTO: read from the
    # running shell's version.features ('windowed' => tcp + tcp + windowed,
    # else tftp + tftp + plain). Each flag, given, overrides its own part.
    deploy.add_argument("--src", default=None, choices=("tftp", "tcp"),
                        help="bitstream source the shell pulls from (default: auto)")
    deploy.add_argument(
        "--pusher-transport", default=None, choices=("tftp", "tcp"), dest="pusher_transport",
        help="how the host pushes (default: auto from version.features)",
    )
    deploy.add_argument("--control-port", type=int, default=6900, dest="control_port")
    # A REAL shell (HWICAP FIFO mode + fire-and-hose off) needs BOTH the tcp
    # transport AND the windowed handshake, or the push starves the
    # single-threaded MicroBlaze and the connection resets. Proven on silicon
    # 2026-07-17. Such a shell reports 'windowed' in version.features, and the
    # AUTO default (above) picks tcp + tcp + windowed from it; a FakeShell
    # without that feature keeps getting tftp/plain.
    deploy.add_argument("--windowed", action="store_true", default=None,
                        help="tcp only: force the lock-step window-grant handshake "
                             "(default: auto from version.features; see net-protocol.md / "
                             "OVER_THE_WIRE_DEPLOY_STATUS.md).")
    deploy.add_argument("--no-windowed", action="store_false", dest="windowed",
                        help="force a plain (non-windowed) push")
    deploy.add_argument("--window", type=int, default=DEFAULT_ACK_WINDOW,
                        help=f"windowed ACK window in bytes (default "
                             f"{DEFAULT_ACK_WINDOW}); MUST equal the shell BSP tcp_wnd.")
    deploy.add_argument("--client-timeout", type=float, default=180.0,
                        dest="client_timeout",
                        help="control-channel timeout (s). The shell parks the "
                             "6900 connection for the WHOLE reconfiguration, so "
                             "the 5 s ShellClient default cannot survive a real "
                             "swap. Default 180.")
    deploy.add_argument("--push-timeout", type=float, default=None, dest="push_timeout",
                        help="push inactivity limit (s): per TFTP packet, per 64 KiB "
                             "TCP chunk. Default auto: 2 s, or 30 s on a Linux harness "
                             "over TCP, where a partial that arrives while the outgoing "
                             "clearing still streams into the ICAP is PARKED (its send "
                             "blocks for that whole stream).")
    deploy.add_argument(
        "--tftp-port", type=int, default=TFTP_PORT, dest="tftp_port",
        help="TFTP push port (net-protocol.md default 69); overridable to "
             "target a fake shell on an ephemeral port",
    )
    deploy.add_argument(
        "--tcp-push-port", type=int, default=RAW_TCP_PORT, dest="tcp_push_port",
        help="raw-TCP push port (net-protocol.md default 6910); overridable "
             "to target a fake shell on an ephemeral port",
    )
    deploy.add_argument(
        "--no-persist", action="store_false", dest="persist", default=True,
        help="do not commit the deployed pair to the user microSD (default: "
             "persist after a verified swap; skipped silently with no card)",
    )
    deploy.set_defaults(func=_cmd_deploy)

    display = sub.add_parser(
        "display",
        help="flip/query the on-board CLCD panel owner (net-protocol.md 'display')",
    )
    display.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
    display.add_argument("--control-port", type=int, default=6900, dest="control_port")
    display.add_argument(
        "owner", nargs="?", default=None, choices=("harness", "dut", "toggle"),
        help="new panel owner; omit to QUERY the current owner (read-only)",
    )
    display.add_argument(
        "--confirm", action="store_true",
        help="wait for the handover to COMMIT (re-query until "
             "CLCDKVM.STATUS.owner is the requested one) instead of printing "
             "the send-now reply, which routinely still names the OUTGOING "
             "owner. Exit 0 = landed, 2 = accepted but not committed in time "
             "(INCONCLUSIVE), 1 = the shell declined.",
    )
    display.add_argument(
        "--timeout", type=float, default=2.0,
        help="--confirm budget in seconds (default 2.0). The hardware handover "
             "is ~7-9 ms; the budget is generous because the path to the board "
             "is a network, not because the KVM is slow.",
    )
    display.add_argument(
        "--poll-interval", type=float, default=0.05, dest="poll_interval",
        help="--confirm re-query interval in seconds (default 0.05)",
    )
    display.set_defaults(func=_cmd_display)

    edge = sub.add_parser(
        "edge", help="MPS3 Edge Device API (docs/HARDWARE_HUB_INTEGRATION.md)",
    )
    edge_sub = edge.add_subparsers(dest="edge_verb", required=True)

    edge_channels = edge_sub.add_parser(
        "channels", help="enumerate.channels — list this board's Channel descriptors",
    )
    edge_channels.add_argument("--board", default="mps3_01", help="board name (e.g. mps3_01)")
    edge_channels.set_defaults(func=_cmd_edge_channels)

    edge_status = edge_sub.add_parser(
        "status", help="status.fpga — two-level shell/rp FpgaStatus",
    )
    edge_status.add_argument("--board", default="mps3_01", help="board name (e.g. mps3_01)")
    edge_status.add_argument(
        "--host", default=None,
        help="shell host/IP for rp truth over the control channel (net-protocol.md); "
             "omit for an offline/no-shell status (rp.loaded reported false)",
    )
    edge_status.add_argument("--control-port", type=int, default=6900, dest="control_port")
    edge_status.set_defaults(func=_cmd_edge_status)

    flash = sub.add_parser(
        "flash",
        help="program the packed MicroPython image onto the board QSPI flash via "
             "the DUT controller over SWD (input: flash_pack.py .bin)",
    )
    flash.add_argument("--image", required=True,
                       help="packed flash image (.bin from firmware/micropython/flash_pack.py)")
    flash.add_argument("--host", default=None,
                       help="shell host/IP; the SWD path reaches the DUT qspi controller (swd.py)")
    flash.add_argument("--hub", default=None,
                       help="fpgahub host to ssh into; omit to run OpenOCD on the local host")
    flash.add_argument("--base", type=lambda x: int(x, 0), default=0,
                       help="flash byte offset to program at (default 0x0)")
    flash.add_argument("--verify-only", action="store_true", dest="verify_only",
                       help="read-back-verify an already-flashed image; no erase/program")
    flash.add_argument("--repo-dir", default=None, dest="repo_dir",
                       help="repo root ON THE EXECUTING HOST (the hub, unless "
                            "--hub is disabled). OpenOCD's cfg paths resolve "
                            "against this, so it must point at a checkout that "
                            "actually exists THERE — the hub does not "
                            "necessarily share the caller's home directory.")
    flash.add_argument("--clk-div", type=int, default=4, dest="clk_div",
                       help="QSPI CLK_DIV (SCLK=HCLK/(2*CLK_DIV)); default 4. "
                            "The controller resets to 1, which garbles RDID on "
                            "silicon, so the host always programs this first.")
    flash.add_argument("--no-verify", action="store_true", dest="no_verify",
                       help="skip the post-program read-back verify (NOT recommended)")
    flash.set_defaults(func=_cmd_flash)

    # ---------------------------------------------------------------------- #
    # lease
    # ---------------------------------------------------------------------- #
    lease_p = sub.add_parser(
        "lease",
        help="fpgahub board lease (docs/internal/MPS3_BOARD_LEASE.md)",
        description="The board is shared and the lease is ADVISORY -- nothing "
                    "gates JTAG or hub-local ethernet today -- so honour it by "
                    "convention. NOTE the two names: `status` addresses the "
                    "CHASSIS (--chassis, env MPS3_CHASSIS, default mps3) because that is what "
                    "`lease show` accepts; acquire/release/cancel/heartbeat "
                    "address the LEASE TARGET (--target, env MPS3_LEASE_TARGET, "
                    "default mps3_pl). "
                    "Both are real; each verb takes exactly one of them.",
    )
    lease_sub = lease_p.add_subparsers(dest="lease_verb", required=True)

    def _lease_common(sp):
        sp.add_argument("--chassis", default=None,
                        help="chassis/board name for `lease show` (env MPS3_CHASSIS)")
        sp.add_argument("--target", default=None,
                        help="lease target for acquire/release/cancel (env "
                             "MPS3_LEASE_TARGET)")
        sp.add_argument("--holder", default=None,
                        help="lease holder (env MPS3_LEASE_HOLDER). Identify "
                             "THIS run: several agents share one unix user, and "
                             "matching on the user alone once cost a live board.")
        sp.set_defaults(func=_cmd_lease)
        return sp

    acq = _lease_common(lease_sub.add_parser(
        "acquire", help="take the lease, BLOCKING through queued responses; "
                        "prints the bare token on stdout"))
    acq.add_argument("--ttl", type=int, default=3600, help="lease TTL (s)")
    acq.add_argument("--tier", default="interactive",
                     choices=("interactive", "background"),
                     help="interactive (default) is NOT revocable; a background "
                          "lease can be pre-empted mid-swap, which is the exact "
                          "corruption the lease exists to prevent")
    acq.add_argument("--allow-preemptible", action="store_true",
                     dest="allow_preemptible",
                     help="required to actually use --tier background")
    acq.add_argument("--poll", type=float, default=20.0,
                     help="seconds between re-acquires while queued. This fpgahub's "
                          "QUEUED response carries no token, so `lease wait` cannot "
                          "be used for the initial FCFS wait -- we re-acquire, which "
                          "keeps FCFS position (the server keys the queue by holder).")
    acq.add_argument("--acquire-timeout", type=float, default=3600.0,
                     dest="acquire_timeout",
                     help="give up after this long AND cancel our queue entry")

    rel = _lease_common(lease_sub.add_parser(
        "release", help="release the lease (needs BOTH --token and --holder)"))
    rel.add_argument("--token", required=True,
                     help="token from acquire. Without a matching --holder the "
                          "hub replies 'no lease to release' and the board stays HELD.")

    can = _lease_common(lease_sub.add_parser(
        "cancel", help="cancel a stray QUEUE entry by holder (no token needed)"))

    hb = _lease_common(lease_sub.add_parser(
        "heartbeat", help="extend the lease during a long operation"))
    hb.add_argument("--token", required=True)
    hb.add_argument("--ttl", type=int, default=None)

    _lease_common(lease_sub.add_parser("status", help="who holds the board (JSON)"))
    _lease_common(lease_sub.add_parser(
        "preflight",
        help="exit 0 iff it is safe for US to touch the board (unheld, or held "
             "by --holder EXACTLY). Deliberately does not probe the board: the "
             "only JTAG way to ask 'is a swap running?' halts the MicroBlaze."))

    # ---------------------------------------------------------------------- #
    # sd
    # ---------------------------------------------------------------------- #
    sd_p = sub.add_parser(
        "sd", help="MPS3 config-SD operations",
        description="sd_install ALWAYS times the client out (~12 MB over "
                    "USB-MSC) and that is NOT a failure. One write, wait, then "
                    "reset -- a retry mid-write corrupts the SD and darkens the "
                    "board.",
    )
    sd_sub = sd_p.add_subparsers(dest="sd_verb", required=True)
    sdw = sd_sub.add_parser(
        "write", help="write a bitstream/bundle to the config SD (no witness, no "
                      "REBOOT: `sd field` is the whole fielding)")
    sdw.add_argument("bundle", help="the .bit to install (path ON the executing host)")
    sdw.add_argument("--token", default=None, help="lease token (proof the board is ours)")
    sdw.add_argument("--holder", default=None, help="lease holder (env MPS3_LEASE_HOLDER)")
    sdw.add_argument("--chassis", default=None)
    sdw.add_argument("--target", default=None)
    sdw.add_argument("--backup", default=None,
                     help="directory holding a CAPTURED, verified backup of the "
                          "current SD. sd_install overwrites in place; on "
                          "2026-07-16 the previous nanosoc.bit was overwritten "
                          "with no backup and is gone.")
    sdw.add_argument("--no-backup", action="store_true", dest="no_backup",
                     help="waive the backup gate (says so, loudly, in the log)")
    sdw.add_argument("--wait", type=float, default=300.0,
                     help="settle interval after the write (s); the hub keeps "
                          "writing long after the client returns")
    sdw.add_argument("--verify-path", default=None, dest="verify_path",
                     help="path of the written file on the MOUNTED SD, for an "
                          "md5 read-back. Omitted, the write is reported "
                          "UNVERIFIED rather than assumed good -- the fpgahub "
                          "API exposes no read-back of its own.")
    sdw.add_argument("--state-dir", default=None, dest="state_dir",
                     help="where the in-flight marker lives (env MPS3_STATE_DIR)")
    sdw.add_argument("--force", action="store_true",
                     help="clear a stale in-flight marker first. Only when you "
                          "are CERTAIN no write is running.")
    sdw.set_defaults(func=_cmd_sd_write)

    sdf = sd_sub.add_parser(
        "field",
        help="ONE SD write, fpgahubd's journal witness, then the paced MCC REBOOT",
        description="Field a new base with nobody at the board (ILA handoff "
                    "2026-09-24 §3). (1) ONE `fpgahub target program <target> <bit> "
                    "--method sd --yes --no-skip-if-loaded`; the client always times "
                    "out and it is never retried. (2) Poll fpgahubd's journal for "
                    "`program dispatched: board=<target> method=sd ... ok=True ... "
                    "sha256=<first 12 of the bit's sha256>`; ok=False, another "
                    "sha256, a skip, a second write or no line by the deadline = NO "
                    "REBOOT. (3) The paced MCC REBOOT on tty_00: refuses with a second "
                    "reader or without an intact Cmd>, then CR, 1 s, R-E-B-O-O-T at "
                    "100 ms/char, CR, and the MCC boot log up to 'FPGA configuration "
                    "complete'. Every gate is checked before the card is touched. "
                    "Exit 0 ok, 1 refused before the write, 2 usage, 3 hub "
                    "unreachable, 4 no witness (NO REBOOT), 5 REBOOT refused / "
                    "unacknowledged / did not configure.",
    )
    sdf.add_argument("bit", help="the .bit to write: a path ON THE HUB (fpgahubd reads "
                                 "it; its sha256 is read there too)")
    sdf.add_argument("--holder", default=None,
                     help="the lease holder that must hold the board (env "
                          "MPS3_LEASE_HOLDER; required)")
    sdf.add_argument("--chassis", default=None)
    sdf.add_argument("--target", default=None,
                     help="fpgahub board name (default MPS3_LEASE_TARGET / mps3_pl)")
    sdf.add_argument("--hub", default=None,
                     help="the hub to ssh into (overrides MPS3_HUB; MPS3_ON_HUB=1 when "
                          "on it)")
    sdf.add_argument("--expect-sha256", default=None, dest="expect_sha256",
                     help="refuse before writing unless the bit's sha256 starts with "
                          "this (e.g. linux_bundle.json's flashable_bit.sha256)")
    sdf.add_argument("--already-written", action="store_true", dest="already_written",
                     help="skip the write: the card was written already (a refused "
                          "REBOOT to retry, or W1's `sudo fpgahub target program` by "
                          "hand); the witness is looked for in the last --since minutes")
    sdf.add_argument("--since", type=float, default=20.0,
                     help="--already-written: minutes to look back (default 20)")
    sdf.add_argument("--no-reboot", action="store_true", dest="no_reboot",
                     help="write and witness only; no MCC REBOOT")
    sdf.add_argument("--witness-deadline", type=float,
                     default=300.0, dest="witness_deadline",
                     help="seconds to wait for the journal witness (default 300; the "
                          "write takes ~68 s)")
    sdf.add_argument("--poll", type=float, default=10.0,
                     help="seconds between journal reads (default 10)")
    sdf.add_argument("--journal-cmd", default="journalctl", dest="journal_cmd",
                     help="the journalctl to run on the hub (default `journalctl`, "
                          "readable without sudo by wheel/adm/systemd-journal; e.g. "
                          "'sudo -n journalctl')")
    sdf.add_argument("--journal-unit", default="fpgahubd", dest="journal_unit")
    sdf.add_argument("--journal-file", default=None, dest="journal_file",
                     help="read the journal from this capture file ON THE HUB instead "
                          "(a privileged `journalctl -u fpgahubd -f -o short-iso > "
                          "FILE` started first), or '-' for stdin (exactly-timestamped "
                          "lines only)")
    sdf.add_argument("--mcc-tty", default=None, dest="mcc_tty",
                     help="the MCC console on the hub (default /dev/<target>/tty_00)")
    sdf.add_argument("--mcc-pace", type=float, default=0.1, dest="mcc_pace",
                     help="seconds between REBOOT characters (default 0.1; below "
                          "0.05 is refused: the MCC drops bursts)")
    sdf.add_argument("--mcc-settle", type=float, default=1.0, dest="mcc_settle",
                     help="seconds between the bare CR and REBOOT (default 1)")
    sdf.add_argument("--mcc-capture", type=float, default=150.0, dest="mcc_capture",
                     help="seconds of MCC boot log to capture after the REBOOT, to "
                          "'FPGA configuration complete' (default 150; 0 = only wait "
                          "for 'Rebooting')")
    sdf.add_argument("--mcc-log", default=None, dest="mcc_log",
                     help="append the MCC boot log to this file ON THE HUB")
    sdf.add_argument("--hub-python", default="python3", dest="hub_python",
                     help="python3 on the hub that runs the paced MCC write (3.6+)")
    sdf.add_argument("--out", default=None, help="also write the JSON record here")
    sdf.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="print the plan; contact nothing")
    sdf.set_defaults(func=_cmd_sd_field)

    # ---------------------------------------------------------------------- #
    # boot-rate
    # ---------------------------------------------------------------------- #
    boot = sub.add_parser(
        "boot-rate",
        help="measure the MCC-config-from-SD boot rate (docs/BOOT_RATE.md)",
        description="Reboot the board N times through the hub's reset surface, "
                    "poll ping until a deadline, and record what came back -- a "
                    "rate with a denominator, a 95%% interval, and the SD "
                    "configuration under test. `--method mcc` is the MCC REBOOT "
                    "on the MCC console tty_00, paced one character per 100 ms "
                    "after a bare CR, with exactly one reader (W1, 2026-09-24; "
                    "the old 'no-op' was REBOOT sent to tty_01). On the Linux "
                    "harness each boot is checked (fresh kernel, identify "
                    "mode run, stage0 slot/boot_count/confirm) and the deadline "
                    "defaults to 180 s. Refuses without a lease HELD BY "
                    "--holder: this reboots a shared board. It never acquires "
                    "or releases one.",
    )
    boot.add_argument("--host", default=None,
                      help="shell host/IP for the ping probe (net-protocol.md)")
    boot.add_argument("--control-port", type=int, default=6900, dest="control_port")
    boot.add_argument("--n", type=int, default=10,
                      help="iterations (default 10 -- the audit's N; at N=10 the "
                           "95%% interval is about half the scale, and the "
                           "summary says so)")
    boot.add_argument("--method", default="msd",
                      help="fpgahub reset method (default msd: TRM 100765 3.2 "
                           "reboot.txt + USB_REMOTE), or 'mcc': the MCC REBOOT "
                           "on tty_00 (see --mcc-route). mcc is the one proven "
                           "hands-free reload (W1, 2026-09-24).")
    boot.add_argument("--mcc-route", default="tty", dest="mcc_route",
                      choices=("auto", "fpgahub", "tty"),
                      help="how --method mcc is issued: 'tty' (default) = a "
                           "paced write to --mcc-tty run on the hub (refuses "
                           "with a second reader or without an intact Cmd>); "
                           "'fpgahub' = fpgahub's reset.mcc, accepted only if "
                           "its reply names tty_00; 'auto' = fpgahub when it "
                           "names tty_00, else the paced write. fpgahub's "
                           "reset.mcc was found to send a burst the MCC drops "
                           "(ILA handoff defect 6), so both are opt-in")
    boot.add_argument("--mcc-tty", default=None, dest="mcc_tty",
                      help="the MCC console on the hub (default "
                           "/dev/<target>/tty_00)")
    boot.add_argument("--mcc-pace", type=float, default=0.1, dest="mcc_pace",
                      help="seconds between REBOOT characters (default 0.1, "
                           "W1's; below 0.05 is refused: the MCC drops bursts)")
    boot.add_argument("--mcc-settle", type=float, default=1.0, dest="mcc_settle",
                      help="seconds between the bare CR and REBOOT (default 1)")
    boot.add_argument("--hub-python", default="python3", dest="hub_python",
                      help="python3 on the hub that runs the paced MCC write")
    boot.add_argument("--hub", default=None,
                      help="the hub to ssh into for fpgahub and the MCC write "
                           "(overrides MPS3_HUB; MPS3_ON_HUB=1 when on it)")
    boot.add_argument("--deadline", type=float, default=None,
                      help="seconds to wait for ping after each reset (default "
                           "90; 180 on the Linux harness: MCC reload + ~24 s "
                           "uSD load + ~20 s to 6900 ~ 100 s)")
    boot.add_argument("--linux", action="store_true",
                      help="the board runs the Linux harness (else read from "
                           "version.impl before the first reset)")
    boot.add_argument("--expect-slot", default=None, dest="expect_slot",
                      help="Linux: the stage0 slot each boot must come from (A "
                           "or B; default the card's default slot)")
    boot.add_argument("--stage0-ssh", default="mps3-linux", dest="stage0_ssh",
                      help="Linux: ssh target for the stage0 status block read "
                           "(pyverify mailbox's devmem path; default the "
                           "mps3-linux alias, 'none' to skip -- slot and "
                           "confirm are then recorded as unchecked)")
    boot.add_argument("--identify-port", type=int, default=6899,
                      dest="identify_port")
    boot.add_argument("--confirm-grace", type=float, default=20.0,
                      dest="confirm_grace",
                      help="Linux: seconds after 6900 answers to wait for "
                           "stage0's att_confirm (default 20)")
    boot.add_argument("--evidence-dir", default=None, dest="evidence_dir",
                      help="write boot_rate_<YYYYMMDD>.csv + .md here (the "
                           "write-up's names; the Linux count belongs in "
                           "docs/evidence/2026-09-linux-soak/)")
    boot.add_argument("--poll", type=float, default=5.0,
                      help="seconds between probes (default 5)")
    boot.add_argument("--gap", type=float, default=10.0,
                      help="settle seconds between iterations (default 10)")
    boot.add_argument("--down-deadline", type=float, default=30.0,
                      dest="down_deadline",
                      help="seconds to wait for the shell to STOP answering "
                           "before counting the boot. A reset the shell survives "
                           "is recorded as hub-error, not as an instant boot -- "
                           "that is the --method mcc signature.")
    boot.add_argument("--no-await-down", action="store_true", dest="no_await_down",
                      help="do not require an observed down edge (the first "
                           "answer may then be the PRE-reboot shell; the record "
                           "says so)")
    boot.add_argument("--variant", default=None,
                      help="the fpga/mps3_sd/variants/<NAME> config the SD is "
                           "carrying. Its key deltas are READ from that "
                           "directory into the record; an unknown name is "
                           "refused rather than recorded as a claim.")
    boot.add_argument("--out", default=None, help="write the run record here (JSON)")
    boot.add_argument("--mcc-log", default=None, dest="mcc_log",
                      help="path of the MCC LOG.TXT on the EXECUTING host (the "
                           "mounted config SD), captured as a sha256 per "
                           "iteration. No default: the mount point is "
                           "site-specific, exactly like `sd write --verify-path`.")
    boot.add_argument("--witness-cmd", default=None, dest="witness_cmd",
                      help="OPTIONAL command printing {\"configured\":bool,"
                           "\"usercode\":\"0x..\"} -- a JTAG DONE/USERCODE "
                           "witness. Off by default and never required; with it, "
                           "configured+inert is recorded as timeout, not dark.")
    boot.add_argument("--probe-timeout", type=float, default=3.0,
                      dest="probe_timeout")
    boot.add_argument("--token", default=None,
                      help="lease token; given, the lease is heartbeaten each "
                           "iteration (a long campaign can outlive a short TTL)")
    boot.add_argument("--note", action="append", default=None,
                      help="free-text note to store in the record (repeatable)")
    boot.add_argument("--holder", default=None,
                      help="lease holder (env MPS3_LEASE_HOLDER). REQUIRED: this "
                           "verb must prove the board is ours.")
    boot.add_argument("--chassis", default=None)
    boot.add_argument("--target", default=None)
    boot.add_argument("--dry-run", action="store_true", dest="dry_run",
                      help="print the plan and touch nothing at all")
    boot.set_defaults(func=_cmd_boot_rate, boot_verb=None)

    boot_sub = boot.add_subparsers(dest="boot_verb")
    boot_cmp = boot_sub.add_parser(
        "compare", help="before/after two run records, naming the variable that "
                        "changed")
    boot_cmp.add_argument("before")
    boot_cmp.add_argument("after")
    boot_cmp.add_argument("--json", action="store_true", help="machine-readable")
    boot_cmp.set_defaults(func=_cmd_boot_rate, boot_verb="compare")
    boot_schema = boot_sub.add_parser(
        "schema", help="print the versioned JSON schema for a run record")
    boot_schema.set_defaults(func=_cmd_boot_rate, boot_verb="schema")

    # ---------------------------------------------------------------------- #
    # ping / version / diag / dut-eth
    # ---------------------------------------------------------------------- #
    for name, fn, helptext in (
        ("ping", _cmd_ping, "{\"op\":\"ping\"} -> shell_id / rm_id (read-only, swap-safe)"),
        ("version", _cmd_version, "the RUNNING firmware's build identity + feature flags"),
        ("diag", _cmd_diag, "the shell's always-on diagnostic counters"),
        ("dut-eth", _cmd_dut_eth,
         "read the frames the DUT TRANSMITTED out of the egress FIFO (dutrx)"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
        sp.add_argument("--control-port", type=int, default=6900, dest="control_port")
        sp.add_argument("--timeout", type=float, default=6.0)
        if name == "ping":
            sp.add_argument("--line", action="store_true",
                            help="print `shell_id=... rm_id=...` instead of JSON "
                                 "(the shape scripts/harness_regression.sh greps)")
        if name == "dut-eth":
            # NOTE every read CONSUMES: the block's DATA port is destructive, so
            # a frame this command prints is gone from the FIFO. --frames is a
            # CAP, not a demand; reading fewer (an idle DUT) is a result.
            sp.add_argument("--frames", type=int, default=1, metavar="N",
                            help="read at most N whole frames (default 1); "
                                 "stops early when the FIFO is empty. EVERY "
                                 "READ CONSUMES -- the block's DATA port is "
                                 "destructive, so a frame printed here is gone")
        sp.set_defaults(func=fn)

    # ---------------------------------------------------------------------- #
    # net-protocol v0.11: stats / log / touch-cal / reboot
    # ---------------------------------------------------------------------- #
    def _v011(name, fn, helptext):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
        sp.add_argument("--control-port", type=int, default=6900, dest="control_port")
        sp.add_argument("--timeout", type=float, default=6.0)
        sp.set_defaults(func=fn)
        return sp
    _v011("stats", _cmd_stats, "the board state in one line (v0.11; fpgahub's shape)")
    lg = _v011("log", _cmd_log, "the shell console from its RAM ring (v0.11)")
    lg.add_argument("--off", type=int, default=0, help="stream offset to read from")
    lg.add_argument("--json", action="store_true", help="print metadata + text as JSON")
    tc = _v011("touch-cal", _cmd_touch_cal, "touch calibration get/set/raw/default (v0.11)")
    tc.add_argument("act", choices=("get", "set", "raw", "default"))
    tc.add_argument("coeffs", nargs="*", type=int, metavar="N",
                    help="for set: ax bx cx ay by cy shift")
    rb = _v011("reboot", _cmd_reboot, "warm-restart the shell via its watchdog (v0.11)")
    rb.add_argument("--wait", action="store_true",
                    help="wait in_ms + 2 s and confirm up_ms restarted")

    # ---------------------------------------------------------------------- #
    # set-clk / reset -- the two write verbs the ILA proof runner drives
    # ---------------------------------------------------------------------- #
    sc = sub.add_parser("set-clk", help="retune the DUT clock to a preset (25mhz/50mhz/100mhz)")
    sc.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
    sc.add_argument("--control-port", type=int, default=6900, dest="control_port")
    sc.add_argument("--timeout", type=float, default=6.0)
    sc.add_argument("--preset", required=True, help="firmware/clkrst/clkrst.c preset name")
    sc.add_argument("--any-preset", action="store_true",
                    help="send a preset the client-side table does not know")
    sc.set_defaults(func=_cmd_set_clk)
    rs = sub.add_parser("reset", help="shell-driven reset (default target: dut)")
    rs.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
    rs.add_argument("--control-port", type=int, default=6900, dest="control_port")
    rs.add_argument("--timeout", type=float, default=6.0)
    rs.add_argument("--target", default="dut")
    rs.set_defaults(func=_cmd_reset)

    dc = sub.add_parser("dut-console",
                        help="the DUT's console on 6930/6931: type a line (PACED) and read back")
    dc.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
    dc.add_argument("--uart", type=int, choices=(0, 1), default=0,
                    help="0 = TCP 6930 (UART0), 1 = TCP 6931 (UART1)")
    dc.add_argument("--port", type=int, default=None, help="override the TCP port")
    dc.add_argument("--send", default=None, help="type this line (paced), then a CR")
    dc.add_argument("--no-eol", action="store_true", dest="no_eol",
                    help="with --send: no CR after the text")
    dc.add_argument("--pace-ms", type=float, default=20.0, dest="pace_ms",
                    help="ms between typed bytes (default 20: the DUT's RX has no flow "
                         "control; do not lower it without a reason)")
    dc.add_argument("--seconds", type=float, default=3.0, help="how long to read and print")
    dc.add_argument("--timeout", type=float, default=5.0, help="connect timeout (s)")
    dc.set_defaults(func=_cmd_dut_console)

    # ---------------------------------------------------------------------- #
    # net-protocol v0.13: the user microSD overlay store
    # ---------------------------------------------------------------------- #
    usd = sub.add_parser(
        "usd", help="the user microSD overlay store: status | format [--wipe] | clear | rescan")
    usd_sub = usd.add_subparsers(dest="usd_verb", required=True)

    def _usd_verb(name, helptext):
        sp = usd_sub.add_parser(name, help=helptext)
        sp.add_argument("--host", required=True, help="shell host/IP (net-protocol.md)")
        sp.add_argument("--control-port", type=int, default=6900, dest="control_port")
        sp.add_argument("--timeout", type=float, default=30.0,
                        help="control-channel timeout (s); format/clear write the card")
        sp.set_defaults(func=_cmd_usd)
        return sp

    _usd_verb("status", "card + store state, the default, the power-on decision")
    fmt = _usd_verb(
        "format",
        "make the card a harness card: writes only into a 0xDA partition or "
        "onto a truly blank card (--wipe: erase the card first)")
    fmt.add_argument("--wipe", action="store_true",
                     help="EXPLICIT WIPE (confirm erase-all): zero the partition "
                          "tables of ANY card, then write a fresh 0xDA-only MBR. "
                          "Bare metal only. Asks you to type ERASE unless --yes")
    fmt.add_argument("--yes", action="store_true",
                     help="skip the interactive ERASE confirmation (asked for --wipe, "
                          "and for a plain format of a card whose store holds a "
                          "default, which the format discards)")
    _usd_verb("clear", "invalidate the default (greybox at the next power-on)")
    _usd_verb("rescan", "re-probe the card now")

    _add_linux_verbs(sub)
    return parser


#: What an operator must type to confirm a destructive ``usd format``.
USD_WIPE_TYPED_CONFIRM = "ERASE"


def _usd_json(resp) -> "dict[str, Any]":
    if not resp.ok:
        return {"ok": False, "err": resp.err}
    return dict(resp.raw) if resp.raw else {"ok": True, "state": resp.state}


def _typed_erase(what: str) -> bool:
    """Ask the operator to type ERASE (prompt and warning on stderr: stdout
    carries only the JSON reply). No terminal to type on is a NO."""
    print(what, file=sys.stderr)
    print(f"Type {USD_WIPE_TYPED_CONFIRM} to continue: ", end="", file=sys.stderr, flush=True)
    try:
        typed = input()
    except (EOFError, OSError):
        typed = ""
    return typed.strip() == USD_WIPE_TYPED_CONFIRM


def _cmd_usd(args: argparse.Namespace) -> int:
    """``usd`` (net-protocol.md v0.13): status, format, clear, rescan.

    Two formats DESTROY something, so both need ``--yes`` or the operator
    typing ERASE (with neither, or no terminal to type on, the command exits 2
    and sends nothing):

    - ``format --wipe`` (``confirm:"erase-all"``) erases the partition table of
      whatever card is in the slot. A Linux harness refuses it with
      ``wipe disabled``.
    - A plain ``format`` (``confirm:"erase"``) on a card whose store holds a
      default (state ``valid`` or ``stale``): rule (a) re-initialises the store
      and DISCARDS that default. The card state is read first, on its own short
      connection, so the control channel is not held while a person decides.
      With no default on the card there is nothing to lose and no prompt.

    Exit codes: 0 ok, 1 the shell refused (the error NAME is printed), 2 not
    confirmed, 3 unreachable."""
    from .client import USD_CONFIRM_FORMAT, USD_CONFIRM_WIPE

    verb = args.usd_verb
    resp = None
    if verb == "format" and not args.yes:
        if args.wipe:
            ok = _typed_erase(
                "usd format --wipe ERASES THE WHOLE USER microSD's partition table "
                "(every partition on it becomes unreachable).")
        else:
            # The state read and -- when the card holds no default, so there is
            # nothing to ask -- the format go on ONE connection: a second 6900
            # connection opened at once can be turned away unanswered (the
            # first-install rehearsal failed 2 of 4 runs that way). Only when a
            # person must decide is the connection closed first.
            def check_then_format(c):
                st = c.usd()
                if st.ok and st.state in ("valid", "stale"):
                    return st, None
                return st, c.usd_format(USD_CONFIRM_FORMAT)

            got = _shell_call_retrying(args, check_then_format, "usd")
            if got is None:
                return 3
            status, resp = got
            if resp is None:
                d = status.default
                held = ("rm_id %s, slot %s" % (d.rm_id, d.slot)) if d is not None else "a default"
                ok = _typed_erase(
                    "usd format re-initialises the store on this card and DISCARDS its "
                    "%s default (%s): the board then boots the greybox."
                    % (status.state, held))
            else:
                ok = True        # no default on the card: nothing to lose (already sent)
        if not ok:
            print("usd: format NOT confirmed; nothing was sent", file=sys.stderr)
            return 2

    if resp is None:
        if verb == "status":
            call = lambda c: c.usd()                                   # noqa: E731
        elif verb == "format":
            confirm = USD_CONFIRM_WIPE if args.wipe else USD_CONFIRM_FORMAT
            call = lambda c: c.usd_format(confirm)                     # noqa: E731
        elif verb == "clear":
            call = lambda c: c.usd_clear()                             # noqa: E731
        else:
            call = lambda c: c.usd_rescan()                            # noqa: E731
        resp = _shell_call_retrying(args, call, "usd")
        if resp is None:
            return 3
    print(json.dumps(_usd_json(resp)))
    if not resp.ok:
        print(f"usd {verb}: refused: {resp.err}", file=sys.stderr)
        return 1
    return 0


def _cmd_flash(args: argparse.Namespace) -> int:
    """Program the packed MicroPython image onto the board SST26 via the DUT's
    qspi controller over SWD. Halts the M0 (so the host owns the controller
    registers), programs + read-back-verifies, then resumes. BOARD-UNPROVEN:
    the fake-controller unit tests cover the command sequencing; real
    SWD-over-AHB throughput and SST26 timing are not exercised here."""
    from .qspi_flash import QspiFlashProgrammer, QspiFlashError
    from .swd import SwdDebugger

    try:
        image = Path(args.image).read_bytes()
    except OSError as exc:
        print(f"flash: cannot read image {args.image}: {exc}", file=sys.stderr)
        return 2

    swd_kwargs = {"hub": args.hub}
    if args.host:
        swd_kwargs["shell_host"] = args.host
    if args.repo_dir:
        swd_kwargs["repo_dir"] = args.repo_dir
    swd = SwdDebugger(**swd_kwargs)
    prog = QspiFlashProgrammer(swd, clk_div=args.clk_div)
    try:
        swd.halt()
        if args.verify_only:
            res = prog.verify_image(image, base=args.base)
            print(f"flash verify OK: {res.length} B match at 0x{args.base:X}")
        else:
            res = prog.program_image(image, base=args.base, do_verify=not args.no_verify)
            print(
                f"flash OK: {res.bytes_programmed} B, {res.pages_programmed} pages, "
                f"{res.sectors_erased} sectors erased ({res.jedec})"
                + ("" if res.verify is None else "; verify OK")
            )
    except QspiFlashError as exc:
        print(f"flash FAILED: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"flash: board/SWD path unreachable: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            swd.resume()
        except Exception:
            pass
    return 0


def main(argv: "list[str] | None" = None, *, runner: Any = None) -> int:
    """``runner`` is the hub-command injection seam (:class:`pyverify.lease.HubRunner`
    or any ``(argv, timeout=None) -> RunResult`` callable). Tests pass an
    in-process fpgahub double; on a real host it is built from ``MPS3_HUB`` /
    ``MPS3_ON_HUB``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if runner is not None:
        args._runner = runner
    func: Any = args.func
    return func(args)


# --------------------------------------------------------------------------- #
# The Linux harness (MicroBlaze V + mps3-harnessd): ssh / console / netboot /
# mailbox / identify / csr-liveness. docs/LINUX_HARNESS.md is the user guide.
# Every one of these is additive: no existing verb changed.
# --------------------------------------------------------------------------- #


def _cmd_ssh(args: argparse.Namespace) -> int:
    """A key-only root shell (or one command) on the Linux harness. Uses the
    ``mps3-linux`` ssh-config alias; ``--direct`` needs no config (``-J
    $MPS3_HUB root@192.168.10.101``). ``--config-stanza`` prints the block to
    add to ``~/.ssh/config``; ``--print`` prints the argv instead of running it."""
    import shlex
    from .linux import LinuxHarnessError, ssh_argv, ssh_config_stanza
    if args.config_stanza:
        print(ssh_config_stanza(os.environ.get("MPS3_HUB") or "<hub-host>"), end="")
        return 0
    try:
        argv = ssh_argv(args.target, direct=args.direct, batch=bool(args.command),
                        tty=not args.command)
    except LinuxHarnessError as exc:
        print(f"ssh: {exc}", file=sys.stderr)
        return 3
    if args.command:
        argv += [" ".join(args.command)]
    if args.print:
        print(" ".join(shlex.quote(a) for a in argv))
        return 0
    os.execvp(argv[0], argv)    # pragma: no cover  (replaces this process)
    return 0                    # pragma: no cover


def _cmd_console(args: argparse.Namespace) -> int:
    """The serial console (FPGA UART lane 2) through fpgahub's TTY share.
    ``--send`` types a line at the paced rate and prints what comes back for
    ``--seconds``; with neither, an interactive session (Ctrl-] quits)."""
    import shlex
    from .linux import ConsoleShare, LinuxHarnessError, _Stream, read_for, send_paced
    share = ConsoleShare(target=args.target, tty=args.tty,
                         runner=getattr(args, "_runner", None))
    try:
        host, port = share.endpoint()
        attach = share.attach_argv(host, port)
    except (LinuxHarnessError, LeaseError) as exc:
        print(f"console: {exc}", file=sys.stderr)
        return 3
    if args.print:
        print(json.dumps({"tty": share.tty, "share": f"{host}:{port}",
                          "attach": " ".join(shlex.quote(a) for a in attach) if attach
                          else f"tcp {host}:{port}"}))
        return 0
    try:
        stream = _Stream(host, port, attach)
    except OSError as exc:
        print(f"console: cannot attach to {host}:{port}: {exc}", file=sys.stderr)
        return 3
    pace = args.pace_ms / 1000.0
    try:
        if args.send is not None:
            send_paced(stream, args.send.encode() + b"\r", pace)
            read_for(stream, args.seconds, sys.stdout.buffer)
            return 0
        return _console_interactive(stream, pace)
    finally:
        stream.close()


def _console_interactive(stream, pace: float) -> int:     # pragma: no cover (a tty)
    import select
    import termios
    import tty as _tty
    from .linux import send_paced
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    print("[console: Ctrl-] quits; input is paced -- do not paste]", file=sys.stderr)
    try:
        if old is not None:
            _tty.setraw(fd)
        while True:
            ready, _, _ = select.select([fd, stream], [], [])
            if stream in ready:
                data = stream.recv(4096)
                if data:
                    os.write(sys.stdout.fileno(), data)
            if fd in ready:
                data = os.read(fd, 64)
                if not data or b"\x1d" in data:
                    return 0
                send_paced(stream, data, pace)
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _cmd_netboot(args: argparse.Namespace) -> int:
    """Push a boot blob into stage0's TFTP rescue server (STAGE0's
    ``stage0_push.py``), locally or ON the hub (``--via-hub``). Exit codes are
    stage0_push.py's, unchanged: 0 accepted, 1 bad local image, 2 rejected by
    stage0, 3 not stage0 / unreachable, 4 transfer failed. Before the tool runs,
    a missing blob or tool is 2 (bad input), a failed hub staging step 3."""
    import shlex
    import subprocess
    from .linux import stage0_push_argv
    via = args.via_hub
    if via == "":
        via = os.environ.get("MPS3_HUB")
        if not via:
            print("netboot: --via-hub needs a host or MPS3_HUB", file=sys.stderr)
            return 3
    tool = Path(args.tool) if args.tool else None
    extra = list(args.extra or [])
    if args.status:
        extra = ["--status"] + extra
    elif args.blob is None:
        print("netboot: give a boot image to push, or --status", file=sys.stderr)
        return 2
    blob = None if args.status else args.blob
    cmds = stage0_push_argv(blob, host=args.host, tool=tool, extra=extra, via_hub=via,
                            python=args.python)
    push_tool = Path(cmds[-1][1]) if via is None else Path(cmds[1][4])
    from .linux import STAGE0_PUSH_EXIT
    if not args.dry_run:
        if blob is not None and not Path(blob).is_file():
            print(f"netboot: no such blob: {args.blob}", file=sys.stderr)
            return 2
        if not push_tool.is_file():
            print(f"netboot: STAGE0's push tool is not at {push_tool} "
                  "(set MPS3_STAGE0_PUSH or --tool)", file=sys.stderr)
            return 2
    for argv in cmds:
        print("+ " + " ".join(shlex.quote(a) for a in argv), file=sys.stderr)
        if args.dry_run:
            continue
        rc = subprocess.call(argv)
        if argv is not cmds[-1]:
            if rc != 0:
                print(f"netboot: staging on the hub failed (rc={rc})", file=sys.stderr)
                return 3
            continue
        if not args.status:
            print("netboot: %s" % STAGE0_PUSH_EXIT.get(rc, f"stage0_push.py exit {rc}"),
                  file=sys.stderr)
        return rc                   # STAGE0's exit codes, unchanged (its contract §9)
    return 0


def _cmd_mailbox(args: argparse.Namespace) -> int:
    """Read the diag mailbox and/or the stage0 status block from the running
    Linux harness over ssh (BusyBox devmem). The JTAG path is
    ``scripts/mps3_diag.tcl``."""
    from dataclasses import asdict
    from .linux import LinuxHarnessError, ssh_argv
    from .mailbox import (SSH_DEVMEM_TIMEOUT_S, MailboxError, SshDevmemReader, find_diag,
                          prefetch_mailboxes, read_stage0_status)
    timeout = args.timeout if args.timeout is not None else SSH_DEVMEM_TIMEOUT_S
    try:
        reader = SshDevmemReader(
            args.target, ssh_argv=ssh_argv(args.target, direct=args.direct, batch=True),
            timeout=timeout)
    except LinuxHarnessError as exc:
        print(f"mailbox: {exc}", file=sys.stderr)
        return 3
    lmbs = (args.lmb_kb,) if args.lmb_kb else (128, 256, 512, 1024)
    out = {}
    try:
        # ONE ssh session for everything below (B1 v4: a login alone is ~14 s
        # on a loaded MicroBlaze V); find_diag / read_stage0_status are then
        # served from that snapshot.
        prefetch_mailboxes(reader, args.what, lmbs)
        if args.what in ("diag", "both"):
            mb = find_diag(reader, lmbs)
            out["diag"] = {"base": "0x%05X" % mb.base, "layout": mb.layout,
                           "version": mb.version, "lmb_kb": mb.lmb_kb,
                           "counters": mb.as_wire()}
        if args.what in ("stage0", "both"):
            st = read_stage0_status(reader)
            out["stage0"] = {"base": "0x%05X" % st.base, "blank": st.blank,
                             "layout": st.layout_source, **st.summary(),
                             "fields": dict(st.fields)}
    except MailboxError as exc:
        print(f"mailbox: {exc}", file=sys.stderr)
        if out:
            print(json.dumps(out))
        return 1
    print(json.dumps(out))
    return 0


def _cmd_identify(args: argparse.Namespace) -> int:
    """UDP 6899 ``identify`` -- what answers at this address, independent of
    the single-client 6900 channel."""
    from .identify import IdentifyError, identify
    try:
        r = identify(args.host, args.port, timeout=args.timeout)
    except IdentifyError as exc:
        print(f"identify: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"identify: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(r.raw))
    return 0 if r.ok else 1


#: The tier-3 CSR liveness probes (scripts/harness_gates/tier3_csr_liveness.tcl):
#: (name, address, pattern). SWO_CFG first -- side-effect-free.
_CSR_PROBES = (
    ("UARTBR.SWO_CFG", 0x44A90018, 0x00000037),
    ("DFXCTL.DECOUPLE", 0x44A10000, 0x00000001),
)


def _cmd_csr_liveness(args: argparse.Namespace) -> int:
    """Tier-3 gate 1 over SSH on the Linux harness: write-readback-restore two
    R/W CSRs with devmem (bug #1: a dead CSR decode reads 0). The JTAG form is
    tier3_csr_liveness.tcl; on the MicroBlaze V this is its fallback."""
    from .linux import LinuxHarnessError, ssh_argv
    from .mailbox import MailboxError, SshDevmemReader
    try:
        rd = SshDevmemReader(args.target,
                             ssh_argv=ssh_argv(args.target, direct=args.direct, batch=True))
    except LinuxHarnessError as exc:
        print(f"csr-liveness: {exc}", file=sys.stderr)
        return 3
    fails = 0
    try:
        for name, addr, pattern in _CSR_PROBES:
            got = rd.write_readback_restore(addr, pattern)
            ok = got == pattern
            fails += not ok
            print("   %-5s %-18s wrote 0x%08x read 0x%08x%s"
                  % ("OK" if ok else "FAIL", name, pattern, got,
                     "" if ok else " -- CSR decode DEAD (bug #1)"))
    except MailboxError as exc:
        print(f"csr-liveness: {exc}", file=sys.stderr)
        return 3
    if fails:
        print("FAIL: CSR decode is not live -- STOP, do not swap.")
        return 1
    print("OK: shell CSR decode is live on hardware (via ssh/devmem).")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    """The TOFU first-key claim: TFTP-put an ``authorized_keys`` file to the
    harness (UDP 69, no MPS3 header). Accepted only while the board is
    UNCLAIMED; afterwards the harness answers TFTP error 2 and this exits 1.
    Undo on the board with ``mps3-unclaim`` from the serial console."""
    from .pusher import PushError, tftp_put
    try:
        data = Path(args.key).read_bytes()
    except OSError as exc:
        print(f"claim: cannot read {args.key}: {exc}", file=sys.stderr)
        return 2
    lines = [ln for ln in data.decode("ascii", "replace").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines or not all(ln.split()[0].startswith(("ssh-", "ecdsa-", "sk-")) for ln in lines):
        print(f"claim: {args.key} does not look like an OpenSSH PUBLIC key file", file=sys.stderr)
        return 2
    if len(data) > 16 * 1024:
        print("claim: an authorized_keys claim is capped at 16 KiB", file=sys.stderr)
        return 2
    try:
        tftp_put(data, args.host, args.port, filename="authorized_keys", timeout_s=args.timeout)
    except PushError as exc:
        text = str(exc)
        if "error 2" in text.lower() or "access violation" in text.lower():
            print(f"claim: REFUSED -- the board is already claimed ({text})", file=sys.stderr)
            return 1
        print(f"claim: {text}", file=sys.stderr)
        return 3
    print(f"claim: accepted -- {len(lines)} key(s) now authorised for root@{args.host}")
    return 0


def _provisioned_static_id(image_path: Path, args: argparse.Namespace) -> "tuple[int, str]":
    """Which static the slot image was PROVISIONED for -- never guessed from the
    board (that would make the harness's static_id check vacuous). ``--static-id``
    wins; else the provenance that travels beside the image: FLOW's
    ``linux_bundle.json`` (targets.ethernet.provisioned.static_id) or IMAGE's
    ``version`` (static_id=...)."""
    if args.static_id:
        return int(args.static_id, 0), "--static-id"
    d = image_path.resolve().parent
    bundle = d / "linux_bundle.json"
    if bundle.is_file():
        sid = json.loads(bundle.read_text())["targets"]["ethernet"]["provisioned"]["static_id"]
        return int(str(sid), 0), str(bundle)
    version = d / "version"
    if version.is_file():
        for ln in version.read_text().splitlines():
            if ln.startswith("static_id="):
                return int(ln.split("=", 1)[1].strip(), 0), str(version)
    raise ValueError(f"no --static-id and no linux_bundle.json / version beside {image_path}: "
                     "which static was this image provisioned for?")


def _cmd_slot(args: argparse.Namespace) -> int:
    """The user-microSD boot slots (net-protocol v0.14 "Slot images"): stage a
    Linux image into the INACTIVE slot over Ethernet, verify, flip the default.
    A CLAIMED board refuses the mutations (push/commit/rollback) from anything
    but itself, so those go through an ssh tunnel to its 127.0.0.1: ``--via-ssh``
    forces it, ``--no-ssh`` forbids it, and by default ``identify`` decides.
    Exit: 0 ok, 1 the harness refused / the job failed, 2 a bad local image or
    argument, 3 unreachable (or the tunnel would not come up)."""
    from .identify import IdentifyError, identify
    from .slot import (SshTunnel, SshTunnelError, SlotError, image_info, push_slot_image,
                       slot_request, wait_job)
    image = info = None
    sid = 0
    if args.act == "push":
        if not args.image:
            print("slot push: which image? (pyverify slot push linux_slot.img)", file=sys.stderr)
            return 2
        path = Path(args.image)
        try:
            image = path.read_bytes()
            info = image_info(image)
            sid, src = _provisioned_static_id(path, args)
        except (OSError, ValueError, KeyError, SlotError) as exc:
            print(f"slot push: {exc}", file=sys.stderr)
            return 2

    use_ssh = args.via_ssh
    if args.act in ("push", "commit", "rollback") and not args.via_ssh and not args.no_ssh:
        try:
            use_ssh = identify(args.host, args.identify_port, timeout=1.0, retries=1).ssh_claimed is True
        except (IdentifyError, OSError):
            use_ssh = False        # no identify (bare metal / older image): the raw ports
    if use_ssh and args.act == "push" and args.via == "tftp":
        print("slot push: a TFTP push cannot ride an ssh tunnel; use --via tcp", file=sys.stderr)
        return 2

    def run(host: str, port: int, push_port: "int | None") -> "dict[str, Any]":
        if args.act == "push":
            print(f"slot push: {args.image} ({len(image)} B, table CRC 0x{info['hdr_crc']:08x}, "
                  f"provisioned for 0x{sid:08x} per {src}) -> {host}:{push_port or 6910}",
                  file=sys.stderr)
            push_slot_image(image, host, static_id=sid, slot=args.slot, via=args.via,
                            port=push_port, timeout_s=args.push_timeout)
            return wait_job(host, port=port, timeout_s=args.timeout) if args.wait else \
                slot_request(host, "status", port=port)
        st = slot_request(host, args.act, args.slot, port=port)
        if args.act == "verify" and args.wait:
            st = wait_job(host, port=port, timeout_s=args.timeout)
        return st

    st: "dict[str, Any] | None" = None
    try:
        if use_ssh:
            rport = args.port
            rpush = args.push_port or 6910
            print(f"slot {args.act}: the board is claimed -> ssh tunnel via {args.ssh_target} "
                  f"to its 127.0.0.1:{rport}/{rpush}", file=sys.stderr)
            with SshTunnel(args.ssh_target, (rport, rpush)) as tun:
                st = run("127.0.0.1", tun.local(rport), tun.local(rpush))
        else:
            st = run(args.host, args.port, args.push_port)
    except SshTunnelError as exc:
        # A tunnel that LEAKED on close (a forward still bound) fails the run
        # even though the act went through: print the act's result, then the leak.
        if st is not None:
            print(json.dumps(st))
        print(f"slot {args.act}: {exc}", file=sys.stderr)
        return 3
    except SlotError as exc:
        print(f"slot {args.act}: {exc}", file=sys.stderr)
        if exc.reply is not None:
            print(json.dumps(exc.reply))
            if exc.reply.get("err") == "slot locked: board claimed (use ssh)":
                print("slot: the board is claimed -- retry with --via-ssh", file=sys.stderr)
        return 1
    except (ConnectionError, OSError, PushError) as exc:
        print(f"slot {args.act}: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(st))
    return 0


def _add_linux_verbs(sub) -> None:
    sp = sub.add_parser("ssh", help="key-only root ssh to the Linux harness (mps3-linux)")
    sp.add_argument("--target", default="mps3-linux", help="ssh destination / config alias")
    sp.add_argument("--direct", action="store_true",
                    help="no ssh config: -J $MPS3_HUB root@192.168.10.101")
    sp.add_argument("--print", action="store_true", help="print the ssh argv, do not run it")
    sp.add_argument("--config-stanza", action="store_true",
                    help="print the ~/.ssh/config block for the mps3-linux alias")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="optional remote command")
    sp.set_defaults(func=_cmd_ssh)

    cp = sub.add_parser("console", help="the harness serial console via the fpgahub TTY share")
    cp.add_argument("--target", default=None,
                    help="fpgahub board/target name (env MPS3_LEASE_TARGET, default mps3_pl)")
    cp.add_argument("--tty", default=None,
                    help="host tty (default lane 2 of the target: /dev/<target>/tty_02)")
    cp.add_argument("--send", default=None, help="type this line (paced) and print the reply")
    cp.add_argument("--seconds", type=float, default=5.0, help="with --send: how long to read")
    cp.add_argument("--pace-ms", type=float, default=20.0, dest="pace_ms",
                    help="per-byte input pacing (uartlite RX FIFO is 16 deep)")
    cp.add_argument("--print", action="store_true", help="print the share endpoint only")
    cp.set_defaults(func=_cmd_console)

    nb = sub.add_parser("netboot", help="push a boot blob to stage0's TFTP rescue server")
    nb.add_argument("blob", nargs="?", default=None,
                    help="boot image (stage0_pack.py format, the same bytes as a uSD slot)")
    nb.add_argument("--status", action="store_true",
                    help="only read + print stage0's status block (TFTP RRQ stage0.status)")
    nb.add_argument("--host", default="192.168.10.101", help="the board in rescue")
    nb.add_argument("--tool", default=None, help="path to STAGE0's stage0_push.py")
    nb.add_argument("--via-hub", nargs="?", const="", default=None, dest="via_hub",
                    help="copy + run the push ON the hub (default host: MPS3_HUB)")
    nb.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the commands, run nothing")
    nb.add_argument("--python", default="python3",
                    help="interpreter that runs the push tool (on the hub with --via-hub; "
                         "the hub's system python may be too old -- point at a 3.8+)")
    nb.add_argument("--push-arg", action="append", default=[], dest="extra",
                    metavar="ARG", help="extra argument passed verbatim to "
                                        "stage0_push.py (repeatable)")
    nb.set_defaults(func=_cmd_netboot)

    mb = sub.add_parser("mailbox", help="read the diag mailbox / stage0 status block over ssh")
    mb.add_argument("--what", choices=("diag", "stage0", "both"), default="both")
    mb.add_argument("--target", default="mps3-linux")
    mb.add_argument("--direct", action="store_true")
    mb.add_argument("--lmb-kb", type=int, default=None, dest="lmb_kb",
                    help="scan only this LMB size's anchors (default: 128 then bare-metal sizes)")
    mb.add_argument("--timeout", type=float, default=None,
                    help="seconds for the ONE ssh session the read takes "
                         "(default: pyverify.mailbox.SSH_DEVMEM_TIMEOUT_S = 90)")
    mb.set_defaults(func=_cmd_mailbox)

    ip = sub.add_parser("identify", help="UDP 6899 identify probe")
    ip.add_argument("--host", required=True)
    ip.add_argument("--port", type=int, default=6899)
    ip.add_argument("--timeout", type=float, default=2.0)
    ip.set_defaults(func=_cmd_identify)

    cp2 = sub.add_parser("claim", help="TOFU: claim an unclaimed Linux harness with your ssh public key")
    cp2.add_argument("--host", default="192.168.10.101")
    cp2.add_argument("--port", type=int, default=69)
    cp2.add_argument("--key", default=os.path.expanduser("~/.ssh/id_ed25519.pub"),
                     help="OpenSSH public key file (default ~/.ssh/id_ed25519.pub)")
    cp2.add_argument("--timeout", type=float, default=2.0)
    cp2.set_defaults(func=_cmd_claim)

    sl = sub.add_parser("slot", help="Linux harness: stage / verify / flip the user-microSD boot slots")
    sl.add_argument("act", choices=("status", "push", "commit", "rollback", "verify"))
    sl.add_argument("image", nargs="?", default=None,
                    help="push: the S0LB slot image (linux_slot.img)")
    sl.add_argument("--host", default="192.168.10.101")
    sl.add_argument("--port", type=int, default=6900, help="the control channel")
    sl.add_argument("--push-port", type=int, default=None, dest="push_port",
                    help="push: 6910 (tcp) / 69 (tftp) unless given")
    sl.add_argument("--via", choices=("tcp", "tftp"), default="tcp")
    sl.add_argument("--slot", choices=("A", "B"), default=None,
                    help="push: the slot you expect to write (refused if it is not the "
                         "target); commit/rollback: the slot you expect to become default; "
                         "verify: which slot (default: the one that is not the default)")
    sl.add_argument("--static-id", default=None, dest="static_id",
                    help="push: the static the image was provisioned for (default: read "
                         "linux_bundle.json or version beside the image)")
    sl.add_argument("--no-wait", action="store_false", dest="wait",
                    help="push/verify: return at once instead of waiting for the card read-back")
    sl.add_argument("--timeout", type=float, default=3600.0,
                    help="push/verify: how long to wait for the card job -- the write and "
                         "the read-back (~41-50 min for a 29 MB image on the board, measured "
                         "26-29 Sep) (s)")
    sl.add_argument("--push-timeout", type=float, default=600.0, dest="push_timeout",
                    help="push: the longest the transfer may STALL (connect, each 64 KiB "
                         "chunk, each TFTP ACK) -- not a bound on the whole push. The user "
                         "microSD stalls > 30 s mid-push (26 and 29 Sep: the old 30 s default "
                         "tore both pushes at ~27 MB) (s)")
    sl.add_argument("--via-ssh", action="store_true", dest="via_ssh",
                    help="push/commit/rollback through an ssh tunnel to the board's 127.0.0.1 "
                         "(needed once the board is claimed; default: ask identify)")
    sl.add_argument("--no-ssh", action="store_true", dest="no_ssh",
                    help="never tunnel, even when the board reports claimed")
    sl.add_argument("--ssh-target", default="mps3-linux", dest="ssh_target",
                    help="the ssh destination / config alias for --via-ssh")
    sl.add_argument("--identify-port", type=int, default=6899, dest="identify_port",
                    help="where to ask whether the board is claimed")
    sl.set_defaults(func=_cmd_slot)

    cl = sub.add_parser("csr-liveness",
                        help="tier-3 CSR write-readback over ssh/devmem (Linux harness)")
    cl.add_argument("--target", default="mps3-linux")
    cl.add_argument("--direct", action="store_true")
    cl.set_defaults(func=_cmd_csr_liveness)


if __name__ == "__main__":
    raise SystemExit(main())
