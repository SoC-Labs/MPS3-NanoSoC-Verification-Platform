"""tests/integration/conftest.py — boundary shims for the cross-area
integration suite.

These tests live at the seam between three areas that Phase 1 of the
sub-repo refactor (``docs/SUBREPO_REFACTOR_PLAN.md`` §5.2) is pulling
apart:

  * the host verification library ``host/pyverify/`` — extraction target
    ``soclabs-mps3-pyverify``;
  * the in-tree DFX flow (``fpga/dfx/gen_manifest.py``); and
  * the integrator-internal cocotb test spine (``tests/common/``).

The whole point of this file is that the integration tests must stay green
in BOTH worlds, with **no per-test-file path hackery**:

  * pyverify still lives in-tree at ``host/pyverify/`` (today) — importable
    only because :func:`_ensure_pyverify_importable` puts it on ``sys.path``;
  * pyverify has been extracted to its own repo and ``pip install``-ed as
    the ``mps3-pyverify`` package (the Phase-1 end state) — importable with
    no path help at all, in which case that same helper is a **no-op** and
    the installed package wins.

Three helpers, each naming exactly one cross-area reach so the boundary is
visible instead of buried in a ``sys.path.insert`` at the top of a test:

  * :func:`_ensure_pyverify_importable` — the ONLY pyverify boundary. It is
    a strict fallback: ``host/pyverify/`` is added to ``sys.path`` *iff*
    ``import pyverify`` fails, so an installed package always takes
    precedence and the in-tree copy is used only when nothing is installed.
    This is what makes the reach-in "boundary-clean": the test bodies just
    ``import pyverify`` like any other consumer of the shipped package.
  * :func:`_ensure_test_support_importable` — adds ``tests/common/`` for the
    integrator-internal cocotb spine (``regmap``, ``ovlstore_header``).
    These are NOT pyverify and deliberately **stay in this integrator
    repo**; they never become part of the extracted package.
  * :func:`load_gen_manifest` / the :func:`gen_manifest` fixture — loads
    ``fpga/dfx/gen_manifest.py`` BY FILE PATH. This is a genuine cross-area
    integration point (the DFX manifest generator ↔ pyverify's validator)
    that stays in the integrator repo. ``fpga/dfx/`` is a directory of Tcl
    plus this one script with no package structure, so a by-path load is
    the correct spelling, not a smell — and keeping it behind a named
    helper makes that boundary explicit.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYVERIFY_SRC = _REPO_ROOT / "host" / "pyverify"        # in-tree fallback only
_TESTS_COMMON = _REPO_ROOT / "tests" / "common"          # stays in integrator
_GEN_MANIFEST_PATH = _REPO_ROOT / "fpga" / "dfx" / "gen_manifest.py"


def _ensure_pyverify_importable() -> None:
    """Make ``import pyverify`` work whether or not the package is installed.

    Strict fallback: only touch ``sys.path`` when the package genuinely
    cannot be imported, so a ``pip install``-ed ``mps3-pyverify`` (the
    post-extraction world) is never shadowed by the in-tree source copy.
    """
    try:
        import pyverify  # noqa: F401  (probe only)
    except ModuleNotFoundError:
        src = str(_PYVERIFY_SRC)
        if src not in sys.path:
            sys.path.insert(0, src)


def _ensure_test_support_importable() -> None:
    """Put the integrator-internal cocotb spine (``tests/common/``) on the
    path so ``import regmap`` / ``import ovlstore_header`` resolve. Unlike
    pyverify these are repo-local test support and are not extracted."""
    common = str(_TESTS_COMMON)
    if common not in sys.path:
        sys.path.insert(0, common)


# Run the path shims at conftest import time — pytest imports this conftest
# before collecting/importing the test modules in this directory, so the
# module-level ``import pyverify`` / ``import regmap`` in those files resolve.
_ensure_pyverify_importable()
_ensure_test_support_importable()


def load_gen_manifest():
    """Load ``fpga/dfx/gen_manifest.py`` by file path.

    ``fpga/dfx/`` has no ``__init__.py`` / package structure (it is a
    directory of Tcl + this one Python script), so it is imported by path
    rather than inventing a ``sys.path`` relationship the script never asks
    for. This is a deliberate cross-area boundary: the DFX flow lives in the
    integrator repo, and this integration test exercises its *actual output*
    through ``pyverify.overlay`` — see
    ``tests/integration/test_manifest_roundtrip.py``.
    """
    spec = importlib.util.spec_from_file_location("gen_manifest", _GEN_MANIFEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen_manifest():
    """The DFX manifest generator module, loaded by path (see
    :func:`load_gen_manifest`)."""
    return load_gen_manifest()
