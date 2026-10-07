#!/usr/bin/env python3
"""soc_model_reproducible.py — run the upstream soc_model generator with the
wall clock frozen, so two clean regenerations of the same YAML produce
byte-identical output.

A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license.

WHY THIS EXISTS
---------------
Every soc_model backend stamps its output with `datetime.datetime.now()` --
twelve call sites across nine files under
`$ETH_SS_HOME/nanosoc_arch_tech/nanosoc_gen/soc_model/backends/`, e.g.

    firmware.py:84       now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    toplevel.py:96       'generated_date': datetime.datetime.now()...
    build_info.py:150    val = int(time.time())      <- reaches a REGISTER RESET VALUE

So every generated .sv / .vh / .h / .ld / .mk / .flist differs from the previous
regeneration on its `Generated:` line even when nothing about the design changed.

That is not itself a functional bug -- a `//` comment does not synthesise -- but
it is the reason nobody could tell drift from noise while chasing the "the eth_ss
rebuild is dead" report: `diff -r` between two build_soc trees is all
timestamps, so a REAL difference has nowhere to show. It also defeats the
generator's own `write_if_changed()` (utils.py), so every regen rewrites every
file and re-triggers every downstream make and Vivado step.

`bootrom_gen.py` in the SAME tree already solved this the right way
(bootrom_gen.py:20-35: honour the reproducible-builds standard SOURCE_DATE_EPOCH,
otherwise omit the wall clock). The upstream fix is to do the same in a
`soc_model/utils.py` helper and route the twelve call sites through it. Until
that lands, this wrapper freezes the clock from the outside without modifying a
byte of the read-only tree.

USAGE (a drop-in for `python -m nanosoc_arch_tech.nanosoc_gen.soc_model`):

    cd $ETH_SS_HOME
    SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) \\
    python3 /path/to/soc_model_reproducible.py \\
        sys_desc/ethernet_ss_ahb.yaml \\
        --lib-dir nanosoc_arch_tech/sys_desc \\
        --lib-dir ethernet-mac-ahb/sys_desc --lib-dir sys_desc \\
        --build-dir <OUT>

SOURCE_DATE_EPOCH defaults to 0 (1970-01-01 UTC) when unset, which is the
conservative choice: a fixed, obviously-not-a-build-time value that no one will
mistake for provenance.

WHAT THIS DOES NOT FIX. The generated flists are manifests of ABSOLUTE paths, so
two builds in different directories differ there by construction. Compare them
with the build-dir prefix normalised, or build both into the same path.

Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
import datetime as _datetime_mod
import os
import runpy
import sys
import time as _time_mod

_MODULE = "nanosoc_arch_tech.nanosoc_gen.soc_model"


def _epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            print(f"soc_model_reproducible: ignoring non-integer "
                  f"SOURCE_DATE_EPOCH={raw!r}", file=sys.stderr)
    return 0


def _freeze(epoch: int) -> None:
    """Freeze every clock the generator can reach.

    The backends do `import datetime` and then call `datetime.datetime.now()` at
    render time, so rebinding the CLASS in the datetime module is enough --
    the lookup happens on each call.
    """
    frozen_naive = _datetime_mod.datetime.fromtimestamp(epoch, tz=_datetime_mod.timezone.utc)
    frozen_naive = frozen_naive.replace(tzinfo=None)
    real_datetime = _datetime_mod.datetime

    class _FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.fromtimestamp(epoch, tz=_datetime_mod.timezone.utc).replace(tzinfo=None)
            return cls.fromtimestamp(epoch, tz=tz)

        @classmethod
        def today(cls):
            return cls.now()

        @classmethod
        def utcnow(cls):
            return cls.now()

    _datetime_mod.datetime = _FrozenDateTime
    _time_mod.time = lambda: float(epoch)
    del frozen_naive, real_datetime


def main() -> int:
    epoch = _epoch()
    _freeze(epoch)
    stamp = _datetime_mod.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[soc_model-reproducible] clock frozen at SOURCE_DATE_EPOCH={epoch} ({stamp})",
          file=sys.stderr)
    # `python -m pkg` puts the CWD on sys.path; running this file BY PATH puts
    # this file's directory there instead, so the upstream package would not be
    # importable. Restore the -m behaviour (ETH_SS_HOME wins if it is set, so the
    # wrapper also works when invoked from somewhere else).
    root = os.environ.get("ETH_SS_HOME") or os.getcwd()
    if root not in sys.path:
        sys.path.insert(0, root)

    # argv[0] must look like the module so the generator's own usage text is right.
    sys.argv = [f"python -m {_MODULE}"] + sys.argv[1:]
    runpy.run_module(_MODULE, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
