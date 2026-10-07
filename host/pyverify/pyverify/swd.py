"""``SwdDebugger`` — high-level SWD debug API for the nanoSoC DUT.

This is the operator-facing layer *above* :mod:`pyverify.debug`. Where
:class:`pyverify.debug.OpenOcdRemoteBitbangConfig` builds a single raw
``openocd`` argv, this module turns that primitive into the named debug
operations an operator actually wants — ``dpidr()``, ``halt()``,
``resume()``, ``read_pc()``, ``read_mem()``, ``write_mem()``,
``load_image()``, ``reset_halt()`` — parses the OpenOCD text back into
Python values, and (the load-bearing new bit) drives OpenOCD **on the
fpgahub host** over an ssh hop, because the board dataplane
(``192.168.10.101:6920``) is reachable only there.

Why this shape (proven flow, 2026-07-10)
----------------------------------------
The end-to-end chain that read ``SWD DPIDR 0x0bb11477`` /
``Cortex-M0 r0p0 detected`` / ``pc 0x378`` on silicon is::

    OpenOCD (on <hub-host>) -> remote_bitbang/TCP 6920 -> swd_server
      -> swd_bb -> nanoSoC SW-DP

OpenOCD here is **not a long-lived server** — each operation is a fresh
``openocd -c ... -c shutdown`` batch against the two committed cfgs
(``host/openocd/swd_remote_bitbang.cfg`` for the bare SW-DP / DPIDR, plus
``swd_cortex_m.cfg`` for the ``cortex_m`` target that halt/step/memory
need). We drive it by **shell-batch, not the OpenOCD telnet/tcl port**,
because: (a) the batch shape is exactly the HW-proven operator command in
``docs/USER_GUIDE.md`` §6 and the ``.cfg`` headers, so pyverify and the
hand-run command stay byte-identical; (b) no persistent daemon to babysit
across an ssh hop (a dropped telnet mid-swap is worse than a re-run); and
(c) ``remote_bitbang`` is a *correctness* channel, not a throughput one
(``docs/SWD_BRINGUP_PLAN.md`` §5 — seconds per DPIDR, minutes per KB), so
per-op process spawn is negligible next to the wire.

Hub relay
---------
Every op is wrapped ``ssh <hub> "<openocd ...>"`` by default
(``hub="<hub-host>"``), mirroring the ssh-wrap in
``host/tender/fpgahub_mps3_plugin.py`` and the fpgahub ``vivado_jtag`` /
``xvc_jtag_openocd`` reference plugins. cfg paths are **absolute** (built
from :attr:`SwdConfig.repo_dir`, which defaults to this checkout's root and is
overridden by ``MPS3_REPO_DIR`` for the hub's own path), so the remote command is
cwd-independent — functionally identical to the USER_GUIDE's
``cd <repo> && openocd -f host/openocd/...`` form. Set ``hub=None`` to run
OpenOCD on the local host instead (e.g. when pyverify itself already runs
on the hub).

Testability / board-free use
----------------------------
Same injectable-runner seam as :mod:`pyverify.debug`
(:data:`pyverify.debug.SubprocessRunner`): tests pass a fake runner and
assert on the argv / parse canned OpenOCD text. ``dry_run=True`` returns
the argv a method *would* run without executing it — the operator-preview
and the board-free argv check. **Nothing here talks to the board on its
own**; a real transaction needs OpenOCD on the hub + a live shell that has
reached ``SWAP_DONE`` (SWD is gated mid-swap — SWD_BRINGUP_PLAN §2c).

nanoSoC memory map (SWD_BRINGUP_PLAN §Appendix; RTL_ANATOMY.md
"Base addresses", from ``nanosoc_memmap.h``): the app lives in **IMEM at
``0x1000_0000``** (64KB), which *remaps to ``0x0000_0000`` post-boot* — so
``load_image`` targets the physical IMEM base ``0x1000_0000`` by default
(always addressable regardless of remap state; the bootrom flash->IMEM
copy uses the same base). DMEM is ``0x1800_0000`` (the reset SP
``0x1800_FC00`` = top of DMEM confirms it).
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

from .debug import SWD_REMOTE_BITBANG_PORT, SubprocessRunner, default_runner

__all__ = [
    "SWD_PORT",
    "DEFAULT_HUB",
    "DEFAULT_SHELL_HOST",
    "DEFAULT_REPO_DIR",
    "BARE_CFG_REL",
    "CORTEX_M_CFG_REL",
    # nanoSoC memory map / identity
    "IMEM_BASE",
    "IMEM_SIZE",
    "IMEM_REMAP_BASE",
    "DMEM_BASE",
    "DMEM_SIZE",
    "BOOTROM_BASE",
    "SCS_CPUID",
    "EXPECTED_DPIDR_CM0",
    "EXPECTED_DPIDR_CM0PLUS",
    "EXPECTED_CPUID_CM0",
    "EXPECTED_CPUID_CM0PLUS",
    # errors / parsing
    "SwdError",
    "parse_dpidr",
    "parse_reg",
    "parse_mdw",
    "parse_downloaded_bytes",
    # config + driver
    "SwdConfig",
    "SwdDebugger",
]

# net-protocol.md port map: 6920 = SWD, OpenOCD remote_bitbang.
SWD_PORT = SWD_REMOTE_BITBANG_PORT  # 6920

#: The fpgahub host the board dataplane is reachable from (USER_GUIDE §0).
#: Supplied via the ``MPS3_HUB`` env var (operators export it in a local,
#: git-ignored set_env.local.sh); ``None`` (unset) => run OpenOCD locally. No
#: site hostname is baked into the public tree.
DEFAULT_HUB = os.environ.get("MPS3_HUB") or None
#: The shell's default static IP (ARCHITECTURE_SPEC §3 / net-protocol.md).
DEFAULT_SHELL_HOST = "192.168.10.101"
#: Repo root on the executing host: ``MPS3_REPO_DIR`` if set (the checkout's
#: path ON THE HUB when OpenOCD runs there), else this checkout's own root.
DEFAULT_REPO_DIR = os.environ.get("MPS3_REPO_DIR") or str(
    Path(__file__).resolve().parents[3])

#: The two committed, HW-proven cfgs (relative to the repo root). Order
#: matters: the bare SW-DP transport first, then the cortex_m target.
BARE_CFG_REL = "host/openocd/swd_remote_bitbang.cfg"
CORTEX_M_CFG_REL = "host/openocd/swd_cortex_m.cfg"

# --- nanoSoC memory map (SWD_BRINGUP_PLAN §Appendix; RTL_ANATOMY.md) ------- #
#: IMEM physical base — where the app lives (remaps to 0x0 post-boot). The
#: default ``load_image`` target: physical IMEM is addressable regardless of
#: remap state, and matches the bootrom flash->IMEM copy base.
IMEM_BASE = 0x1000_0000
# 128 KB, NOT the 64 KB this constant claimed until 2026-07-18. Two independent
# sources agree: firmware/micropython/port/nanosoc.ld:19 declares
# `IMEM (rx) : ORIGIN = 0x00000000, LENGTH = 128K`, and the RM instantiates
# IMEM_RAM_ADDR_W = 17 (fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv:75).
# ADDR_W is byte-addressing here — cross-checked against DMEM, whose
# DMEM_RAM_ADDR_W = 16 matches DMEM_SIZE = 64 KB below. The stale "ADDR_W=14"
# comment predates the IMEM growth; anything sizing a buffer off it (e.g. an M0
# flash-loader payload window) would under-allocate by half.
IMEM_SIZE = 128 * 1024
#: Post-boot alias of IMEM (the running vector table / PC live here — e.g.
#: the proven halt PC 0x378 is a remapped-IMEM address).
IMEM_REMAP_BASE = 0x0000_0000
DMEM_BASE = 0x1800_0000
DMEM_SIZE = 64 * 1024
#: Bootrom physical base (aliased to 0x0800_0000 after boot).
BOOTROM_BASE = 0x0000_0000
#: Cortex-M SCS CPUID register (the identity read the plan uses as the
#: AP/AHB-AP proof — mdw 0xE000ED00 -> 0x410cc200 on the proven M0).
SCS_CPUID = 0xE000_ED00

#: DP/CPU identity discriminators (SWD_BRINGUP_PLAN §6): the proven DUT is a
#: Cortex-M0, but the lab migrated other cores to M0+ — reading these is a
#: feature of first-light, not an assertion this module enforces.
EXPECTED_DPIDR_CM0 = 0x0BB1_1477
EXPECTED_DPIDR_CM0PLUS = 0x0BC1_1477
EXPECTED_CPUID_CM0 = 0x410C_C200
EXPECTED_CPUID_CM0PLUS = 0x410C_C601


class SwdError(Exception):
    """An SWD op failed: OpenOCD exited non-zero, or its output did not
    carry the value the op asked for (e.g. no ``SWD DPIDR`` line). Carries
    the captured OpenOCD text so the operator can see *why* — the common
    causes are catalogued in ``docs/SWD_BRINGUP_PLAN.md`` §6 (Connection
    refused = off-hub / no shell; all-1s/0s DPIDR = DAP held in reset,
    ``RESET_CTRL=0x3``, gap G1; parity/shift = turnaround bug)."""


# --------------------------------------------------------------------------- #
# OpenOCD output parsing (pure functions — the load-bearing, board-free part)
# --------------------------------------------------------------------------- #

# OpenOCD logs Info/Error to stderr and command results to stdout; callers
# concatenate both before parsing. Formats pinned against OpenOCD 0.12.
_DPIDR_RE = re.compile(r"SWD DPIDR\s+(0x[0-9a-fA-F]+)")
_MDW_LINE_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+):\s+([0-9a-fA-F ]+?)\s*$", re.MULTILINE)
_DOWNLOADED_RE = re.compile(r"downloaded\s+(\d+)\s+bytes")


def parse_dpidr(text: str) -> int:
    """Pull the DPIDR value out of an OpenOCD ``init`` log.

    Matches the ``Info : SWD DPIDR 0x0bb11477`` line. Raises
    :class:`SwdError` if absent (connect failed / DAP in reset — see §6).
    """
    m = _DPIDR_RE.search(text)
    if not m:
        raise SwdError(
            "no 'SWD DPIDR' line in OpenOCD output (connect failed, or DAP "
            "held in reset — SWD_BRINGUP_PLAN §6). Output:\n" + text.strip()
        )
    return int(m.group(1), 16)


def parse_reg(text: str, name: str = "pc") -> int:
    """Pull a core register value from OpenOCD ``reg <name>`` output.

    OpenOCD prints e.g. ``pc (/32): 0x00000378``. Raises :class:`SwdError`
    if the named register isn't found.
    """
    # Register name then "(/<width>): 0x...."; anchor on the name to avoid a
    # different register's value.
    rx = re.compile(
        r"(?:^|\s)" + re.escape(name) + r"\s*\(/\d+\):\s*(0x[0-9a-fA-F]+)",
        re.MULTILINE,
    )
    m = rx.search(text)
    if not m:
        raise SwdError(
            f"register {name!r} not found in OpenOCD output:\n" + text.strip()
        )
    return int(m.group(1), 16)


def parse_mdw(text: str) -> "list[int]":
    """Flatten OpenOCD ``mdw`` output into an in-address-order word list.

    ``mdw`` prints lines like ``0x10000000: 1800fc00 00000189 000001cd``
    (up to 4/8 words per line, multiple lines for a longer count). Returns
    every word in the order printed. Raises :class:`SwdError` on no data.
    """
    words: "list[int]" = []
    for m in _MDW_LINE_RE.finditer(text):
        for tok in m.group(2).split():
            words.append(int(tok, 16))
    if not words:
        raise SwdError(
            "no 'mdw'-style memory words in OpenOCD output (read failed):\n"
            + text.strip()
        )
    return words


def parse_downloaded_bytes(text: str) -> "Optional[int]":
    """Return the byte count from OpenOCD's ``downloaded N bytes ...`` line
    after a ``load_image``, or ``None`` if the line is absent (some OpenOCD
    builds/paths word it differently — absence is not an error here)."""
    m = _DOWNLOADED_RE.search(text)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Config: builds the (optionally ssh-wrapped) OpenOCD argv
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SwdConfig:
    """Where/how to run OpenOCD for the nanoSoC SWD stack.

    Defaults follow the 2026-07-10 flow: OpenOCD on ``<hub-host>`` against
    SWD port 6920 on the shell at ``192.168.10.101``, using the two committed
    cfgs.

    .. warning::
       ``repo_dir`` is a path on the **executing host** — i.e. on the *hub*,
       not on the caller's box. These need not be the same filesystem: when
       the hub's checkout lives at a different path, the default (this
       checkout's root) resolves OpenOCD's ``-f`` cfg paths to files that are
       absent there and every op dies "file not found" before reaching the
       board. Stage a checkout on the hub and pass ``repo_dir=`` (CLI:
       ``--repo-dir``, env ``MPS3_REPO_DIR``) to point at it.
    """

    shell_host: str = DEFAULT_SHELL_HOST
    port: int = SWD_PORT
    openocd: str = "openocd"
    #: fpgahub host to ssh into. ``None`` => run OpenOCD on the local host.
    hub: Optional[str] = DEFAULT_HUB
    ssh: str = "ssh"
    #: -o flags matching the fpgahub debug plugins / tender (BatchMode keeps
    #: a password prompt from wedging; accept-new avoids a first-connect stall).
    ssh_opts: "tuple[str, ...]" = (
        "-oBatchMode=yes",
        "-oStrictHostKeyChecking=accept-new",
    )
    #: Repo root on the executing host (== the hub path). cfg paths resolve
    #: against this so the remote command needs no ``cd``.
    repo_dir: str = DEFAULT_REPO_DIR
    bare_cfg: str = BARE_CFG_REL
    cortex_cfg: str = CORTEX_M_CFG_REL
    #: SWD is slow (SWD_BRINGUP_PLAN §5); allow minutes, especially for
    #: load_image. Per-op override via :meth:`SwdDebugger` methods.
    timeout_s: float = 60.0

    # -- inner OpenOCD argv ------------------------------------------------- #

    def _cfg_abs(self, rel: str) -> str:
        return os.path.join(self.repo_dir, rel)

    def openocd_argv(
        self, ops: "Sequence[str]" = (), *, with_target: bool = False,
    ) -> "list[str]":
        """The bare ``openocd -c ... -f ... -c init <ops> -c shutdown`` argv
        (no ssh wrap). ``with_target`` adds the cortex_m cfg for
        halt/step/memory ops; leave it off for the pure DPIDR smoke.

        cfg paths are absolute (from :attr:`repo_dir`). ``set SHELL_HOST`` /
        ``set SHELL_SWD_PORT`` precede ``-f`` so the cfg picks them up.
        """
        argv = [
            self.openocd,
            "-c", f"set SHELL_HOST {self.shell_host}",
            "-c", f"set SHELL_SWD_PORT {self.port}",
            "-f", self._cfg_abs(self.bare_cfg),
        ]
        if with_target:
            argv += ["-f", self._cfg_abs(self.cortex_cfg)]
        argv += ["-c", "init"]
        for op in ops:
            argv += ["-c", op]
        argv += ["-c", "shutdown"]
        return argv

    def command_argv(
        self, ops: "Sequence[str]" = (), *, with_target: bool = False,
    ) -> "list[str]":
        """The full argv actually handed to the runner: the OpenOCD argv,
        ssh-wrapped onto :attr:`hub` unless ``hub is None``.

        ssh form (default): ``[ssh, *ssh_opts, hub, "<shlex-joined openocd>"]``
        — same wrap as ``fpgahub_mps3_plugin`` / the reference ``vivado_jtag``.
        Local form (``hub is None``): the OpenOCD argv verbatim.
        """
        inner = self.openocd_argv(ops, with_target=with_target)
        if self.hub is None:
            return inner
        remote_cmd = " ".join(shlex.quote(a) for a in inner)
        return [self.ssh, *self.ssh_opts, self.hub, remote_cmd]

    def command_str(
        self, ops: "Sequence[str]" = (), *, with_target: bool = False,
    ) -> str:
        """Shell-quoted string form of :meth:`command_argv`, for logging or
        pasting into a terminal."""
        return " ".join(shlex.quote(a) for a in self.command_argv(ops, with_target=with_target))


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #


def _as_words(data: "Union[int, Sequence[int]]") -> "list[int]":
    if isinstance(data, int):
        return [data]
    return [int(w) for w in data]


class SwdDebugger:
    """Named SWD debug operations over the proven OpenOCD/remote_bitbang stack.

    Usage (drives OpenOCD on the hub by default)::

        from pyverify.swd import SwdDebugger
        dbg = SwdDebugger()                       # <hub-host>, .101:6920
        print(hex(dbg.dpidr()))                   # 0x0bb11477
        dbg.reset_halt()
        dbg.load_image("firmware/app.bin")        # -> IMEM 0x10000000
        dbg.reset_run()                            # clears SysTick/NVIC (Rung 6)
        print(hex(dbg.read_pc()))

    Every method builds an OpenOCD ``-c`` batch, runs it via :attr:`runner`
    (default the real :func:`pyverify.debug.default_runner`), and — for
    query ops — parses the value out. ``dry_run=True`` makes every op return
    the argv it *would* run instead (operator preview / board-free check).

    Injection seams (all board-free-testable): ``runner`` (fake subprocess),
    ``config`` (or the per-field kwargs), ``dry_run``.
    """

    # Convenience: SwdDebugger(shell_host=..., hub=..., repo_dir=...) without
    # constructing a SwdConfig by hand.
    def __init__(
        self,
        config: Optional[SwdConfig] = None,
        *,
        runner: Optional[SubprocessRunner] = None,
        dry_run: bool = False,
        **config_overrides: object,
    ) -> None:
        if config is not None and config_overrides:
            raise TypeError(
                "pass either a SwdConfig or keyword overrides, not both"
            )
        if config is None:
            config = SwdConfig(**config_overrides)  # type: ignore[arg-type]
        self.config = config
        self.runner = runner
        self.dry_run = dry_run

    # -- low-level: run a batch of OpenOCD ops ------------------------------ #

    def _run(
        self,
        ops: "Sequence[str]",
        *,
        with_target: bool,
        timeout_s: "Optional[float]" = None,
        check: bool = True,
    ) -> "Union[list[str], object]":
        """Run an OpenOCD op batch (or, if :attr:`dry_run`, return its argv).

        Returns the ``CompletedProcess`` on a real run. Raises
        :class:`SwdError` (with the captured text) when ``check`` and the
        process exits non-zero.
        """
        argv = self.config.command_argv(ops, with_target=with_target)
        if self.dry_run:
            return argv
        run = self.runner if self.runner is not None else default_runner
        result = run(argv, timeout_s=timeout_s or self.config.timeout_s)
        if check and result.returncode != 0:
            raise SwdError(
                f"OpenOCD exited {result.returncode} for ops {list(ops)!r}.\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    @staticmethod
    def _text(result: object) -> str:
        """stdout+stderr of a CompletedProcess, for parsing."""
        out = getattr(result, "stdout", "") or ""
        err = getattr(result, "stderr", "") or ""
        return out + "\n" + err

    # -- query ops (parse a value back) ------------------------------------- #

    def dpidr(self, *, timeout_s: "Optional[float]" = None) -> "Union[int, list[str]]":
        """Read the SW-DP DPIDR — the first-light smoke (no target, no core
        firmware needed; the DP answers whenever the core is out of reset).
        Returns the 32-bit id (e.g. ``0x0bb11477`` = Cortex-M0 SW-DP)."""
        result = self._run((), with_target=False, timeout_s=timeout_s)
        if self.dry_run:
            return result  # argv
        return parse_dpidr(self._text(result))

    def read_pc(self, *, timeout_s: "Optional[float]" = None) -> "Union[int, list[str]]":
        """Halt (``reg`` needs a halted core) and read PC. On the proven DUT
        this returned ``0x378`` — a remapped-IMEM address, i.e. the app is
        running past the bootrom->IMEM handoff."""
        result = self._run(("halt", "reg pc"), with_target=True, timeout_s=timeout_s)
        if self.dry_run:
            return result
        return parse_reg(self._text(result), "pc")

    def read_mem(
        self, addr: int, count: int = 1, *, timeout_s: "Optional[float]" = None,
    ) -> "Union[list[int], list[str]]":
        """Read ``count`` 32-bit words from ``addr`` (``mdw``). Works on a
        running or halted core via the AHB-AP. E.g.
        ``read_mem(SCS_CPUID)`` -> ``[0x410cc200]`` (the CPUID identity
        proof); ``read_mem(IMEM_REMAP_BASE, 2)`` -> the live reset vectors."""
        op = f"mdw 0x{addr:08x} {count}"
        result = self._run((op,), with_target=True, timeout_s=timeout_s)
        if self.dry_run:
            return result
        words = parse_mdw(self._text(result))
        return words[:count]

    def batch_ops(
        self, ops: "Sequence[tuple]", *, timeout_s: "Optional[float]" = None,
    ) -> "list":
        """Run many memory ops in ONE OpenOCD session and return the reads.

        ``ops`` is a sequence of ``("w", addr, value)`` / ``("r", addr, count)``.
        The result is aligned to ``ops``: ``None`` for each write, a
        ``list[int]`` of words for each read.

        WHY THIS EXISTS. ``_run()`` has always accepted a *sequence* of ops and
        emitted one ``-c`` per op, so a whole batch costs a single ssh+OpenOCD
        spawn. Nothing used it: every caller went through ``read_mem``/
        ``write_mem``, one op per spawn. Measured on the board 2026-07-18:

            spawn (ssh + SWD connect + teardown)   ~0.92 s
            one-op-per-spawn                        0.753 s/op
            marginal per-op INSIDE one session      ~0.075 s/op   (10x cheaper)

        OpenOCD runs the ``-c`` ops strictly in order, so a batch preserves
        register-access ordering exactly as the one-at-a-time path would.
        """
        op_strs: "list[str]" = []
        for op in ops:
            kind = op[0]
            if kind == "w":
                _, addr, value = op
                op_strs.append(f"mww 0x{addr:08x} 0x{value & 0xFFFFFFFF:08x}")
            elif kind == "r":
                _, addr, count = op
                op_strs.append(f"mdw 0x{addr:08x} {count}")
            else:
                raise ValueError(f"batch_ops: unknown op kind {kind!r}")

        result = self._run(tuple(op_strs), with_target=True, timeout_s=timeout_s)
        if self.dry_run:
            return result

        n_read_words = sum(op[2] for op in ops if op[0] == "r")
        words = parse_mdw(self._text(result)) if n_read_words else []
        if len(words) < n_read_words:
            raise SwdError(
                f"batch_ops: expected {n_read_words} words from "
                f"{sum(1 for o in ops if o[0] == 'r')} read op(s), parsed "
                f"{len(words)}. OpenOCD output:\n{self._text(result)}"
            )

        out: "list" = []
        i = 0
        for op in ops:
            if op[0] == "r":
                out.append(words[i : i + op[2]])
                i += op[2]
            else:
                out.append(None)
        return out

    def cpuid(self, *, timeout_s: "Optional[float]" = None) -> "Union[int, list[str]]":
        """Read the SCS CPUID (``0xE000ED00``) — proves the DP *and* the
        AP/AHB-AP->core path (SWD_BRINGUP_PLAN §6). ``0x410cc200`` = M0."""
        result = self.read_mem(SCS_CPUID, 1, timeout_s=timeout_s)
        if self.dry_run:
            return result  # argv
        return result[0]  # type: ignore[index]

    # -- action ops --------------------------------------------------------- #

    def halt(self, *, timeout_s: "Optional[float]" = None) -> object:
        """Halt the core (DHCSR). Prefer this over reset-halt: vector-catch
        reset-halt does not work on this DUT (the DP shares the core reset
        domain — SWD_BRINGUP_PLAN §2a)."""
        return self._run(("halt",), with_target=True, timeout_s=timeout_s)

    def resume(self, *, timeout_s: "Optional[float]" = None) -> object:
        """Resume a halted core (``resume``)."""
        return self._run(("resume",), with_target=True, timeout_s=timeout_s)

    def write_mem(
        self,
        addr: int,
        data: "Union[int, Sequence[int]]",
        *,
        timeout_s: "Optional[float]" = None,
    ) -> object:
        """Write one or more 32-bit words starting at ``addr`` (``mww`` per
        word). ``data`` is a single int or a sequence of ints."""
        words = _as_words(data)
        ops = [f"mww 0x{addr + 4 * i:08x} 0x{w & 0xFFFFFFFF:08x}" for i, w in enumerate(words)]
        return self._run(ops, with_target=True, timeout_s=timeout_s)

    def load_image(
        self,
        path: "Union[str, os.PathLike[str]]",
        addr: "Optional[int]" = IMEM_BASE,
        *,
        halt: bool = True,
        verify: bool = False,
        reset: "Optional[str]" = None,
        timeout_s: "Optional[float]" = None,
    ) -> object:
        """Load a DUT firmware image over SWD — the high-value capability
        (seconds vs a full DFX re-synth; SWD_BRINGUP_PLAN §9).

        ``path`` is a ``.bin`` (needs ``addr``, default IMEM ``0x1000_0000``)
        or an ELF (pass ``addr=None`` to use the ELF's own load addresses).
        ``halt`` halts first (default — you cannot reliably load into a
        running core). ``verify`` re-reads and checks. ``reset`` may be
        ``"run"`` or ``"halt"`` to reset afterwards.

        **Landmine (Rung 6 / lab ``swd-load-systick-wedge``):** a fresh image
        started without a reset can wedge in ``Default_Handler`` because the
        *old* image's SysTick/NVIC state is still armed and ARMv6-M cannot
        clear an ACTIVE exception. Follow every load with a reset —
        ``reset="run"`` here, or :meth:`reset_run` — before running.
        """
        p = os.fspath(path)
        ops: "list[str]" = []
        if halt:
            ops.append("halt")
        if addr is None:
            ops.append(f"load_image {shlex.quote(p)}")
            if verify:
                ops.append(f"verify_image {shlex.quote(p)}")
        else:
            ops.append(f"load_image {shlex.quote(p)} 0x{addr:08x}")
            if verify:
                ops.append(f"verify_image {shlex.quote(p)} 0x{addr:08x}")
        if reset == "run":
            ops.append("reset run")
        elif reset == "halt":
            ops.append("reset halt")
        elif reset is not None:
            raise ValueError(f"reset must be None, 'run', or 'halt' (got {reset!r})")
        # load can take minutes over remote_bitbang (§5) — default to a
        # roomier timeout than a query op.
        return self._run(
            ops, with_target=True, timeout_s=timeout_s or max(self.config.timeout_s, 300.0),
        )

    def reset_run(self, *, timeout_s: "Optional[float]" = None) -> object:
        """Reset the core and let it run (``reset run``) — the SysTick/NVIC
        clear after a :meth:`load_image` (see its landmine note)."""
        return self._run(("reset run",), with_target=True, timeout_s=timeout_s)

    def reset_halt(self, *, timeout_s: "Optional[float]" = None) -> object:
        """Reset the core and halt (``reset halt``).

        Caveat (SWD_BRINGUP_PLAN §2a): vector-catch reset-halt is unreliable
        here — the SW-DP shares the core's single reset domain, so an srst
        that resets the core also resets the DP. If this flaps, use
        :meth:`reset_run` then :meth:`halt` instead."""
        return self._run(("reset halt",), with_target=True, timeout_s=timeout_s)
