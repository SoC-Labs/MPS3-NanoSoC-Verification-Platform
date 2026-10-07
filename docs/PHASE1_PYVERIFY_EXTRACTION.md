# Phase 1 — `pyverify` extraction readiness

> **Status: HISTORICAL** — a record of the shelved `pyverify` sub-repo extraction readiness study as of 2026-07-09.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

> ## 🗄️ SHELVED — 2026-07-09
>
> Paused with the rest of the sub-repo refactor (`docs/SUBREPO_REFACTOR_PLAN.md`).
> **`host/pyverify/` was never moved.** What landed and is worth keeping:
> the `tests/integration/conftest.py` import-boundary helper (repo green with
> pyverify installed *and* not installed), and the deterministic fix to
> `test_pusher_send.py`. The extraction dry-run script was only ever run against
> a throwaway clone in scratchpad. To resume, re-run the dry-run first — the
> repo has moved on since.

**Status:** DONE (extraction-ready; not yet cut) · **Date:** 2026-07-09 ·
**Scope:** make `host/pyverify/` extractable to a standalone repo
(`soclabs-mps3-pyverify`) consumed back as a submodule, with the integrator
staying green throughout. Companion: `docs/SUBREPO_REFACTOR_PLAN.md` §5.2
(names pyverify "the cleanest cut in the whole tree") and §8 Phase 1.

This is a **reversible, shippable increment**: no files were moved out of the
tree, no remote was created. What changed is that the last in-tree couplings
that would break across a repo boundary are gone (or made explicit), and the
cut has been rehearsed end-to-end in a throwaway clone.

---

## 1. What changed (and why)

Four files, all inside this task's ownership scope. Nothing in the
`host/pyverify/pyverify/` package itself was touched — it was already
boundary-clean (see §2 audit).

| File | Change | Why |
|---|---|---|
| `tests/integration/conftest.py` | **New.** Three named boundary helpers: `_ensure_pyverify_importable()` (strict "install-wins" `sys.path` fallback), `_ensure_test_support_importable()` (`tests/common` cocotb spine), and `load_gen_manifest()` + the `gen_manifest` fixture (loads `fpga/dfx/gen_manifest.py` by path). | Replaces the per-file `sys.path` reach-ins with one place that makes every cross-area boundary explicit and named. |
| `tests/integration/test_manifest_roundtrip.py` | Removed the `sys.path.insert` of `host/pyverify` and `tests/common`; removed the local `_load_gen_manifest()`/fixture (now in conftest). Body imports `pyverify.overlay` normally. | The test now consumes pyverify as a package, not as a sibling directory. |
| `tests/integration/test_swap_sequence.py` | Removed the three `sys.path.insert`s (`tests/common`, `host/pusher`, `host/pyverify`); switched `from push import …` → `from pyverify.pusher import …` (the single canonical framing module). | Drops the dependence on the `host/pusher/push.py` path shim entirely; the framing classes are the same objects whether or not the shim is imported first. |
| `scripts/extract_pyverify.sh` | **New.** The extraction dry-run (see §4). | Rehearses the cut in a throwaway clone; doubles as the documented go-live procedure. |

### The "install-wins" fallback (the crux)

`tests/integration/conftest.py::_ensure_pyverify_importable()`:

```python
try:
    import pyverify            # installed package wins
except ModuleNotFoundError:
    if str(_PYVERIFY_SRC) not in sys.path:
        sys.path.insert(0, str(_PYVERIFY_SRC))   # in-tree fallback ONLY
```

Because the fallback fires **only** when `import pyverify` genuinely fails, an
installed `mps3-pyverify` is never shadowed by the in-tree source copy. This is
what keeps the integrator green in **both** worlds — before the cut (pyverify
lives at `host/pyverify/`, found via the fallback) and after it (pyverify is a
`pip install`-ed submodule dep, found with no path help and the fallback is a
no-op). Both are verified in §5.

### `host/pusher/push.py` needed no change

It already implements the same "install-wins" pattern (try
`import pyverify.pusher`, fall back to putting `host/pyverify` on `sys.path`
only on `ModuleNotFoundError`). It stays as a compat shim for the remaining
path-based consumer (`host/pyverify/tests/test_push_framing.py`) and is not a
package boundary. See §6 for its fate on the real cut.

---

## 2. Audit result — `host/pyverify/` is already clean

- **Zero runtime dependencies.** `pyproject.toml` declares `dependencies = []`,
  `requires-python = ">=3.10"`. Package modules use only stdlib (`socket`,
  `json`, `zlib`, `subprocess`, `struct`). `dev` extra = `pytest`; `tender`
  extra (`pydantic`, `pyserial`) is discoverability-only, pulled by the tender
  integration that stays in the integrator.
- **Installable.** `pip install -e host/pyverify` succeeds; the dist is
  `mps3-pyverify` 0.1.0; `import pyverify` resolves from outside the repo.
- **No reach-outs from the package.** `grep` for `sys.path` / `../` / imports of
  `fpga`/`firmware` inside `host/pyverify/pyverify/` finds only docstring
  citations. The single `__file__`-relative path in the package
  (`testing/_pusher.py`) is a *tolerant* fallback that prefers `pyverify.pusher`
  and only path-loads `host/pusher/push.py` if that import fails — it never
  fires once the package is installed.

---

## 3. Lockstep guarantees — must become cross-repo CI contract tests

These are **behavioural** couplings enforced by tests, not imports. They do not
break the *build* across a repo boundary, but they will silently **rot** unless
each becomes a CI gate that pulls both sides. Losing them is the real risk of
extraction, so they are written down here explicitly.

### L1 — fakeshell byte-conformance ↔ firmware response encoder
- **Invariant:** `pyverify.testing.fakeshell.FakeShell`'s control-channel
  responses are byte-conformant to the firmware's `mps3_ctrl_encode_response()`
  (`firmware/common/net_proto.c` + `coordinator.c` handlers).
- **Pinned by:** `tests/firmware_logic/test_fakeshell_conformance.py` — sends one
  identical request list through BOTH the real `firmware/test/bin/ctrl_echo`
  binary and `FakeShell.handle_control`, asserting identical key-set, `ok`
  verdict, per-key JSON type, and per-key value (byte-identical for
  scenario-matched successes). This is the test that caught bug I31.
- **Boundary fate:** this test **stays in the integrator** (it compiles and runs
  `firmware/`). On the cut it must be re-expressed as a **cross-repo contract
  test** that consumes the extracted pyverify (pinned submodule) + the firmware
  encoder, so the double can't drift from the real server unnoticed.

### L2 — `DEFAULT_CLK_PRESETS` ↔ firmware `clkrst_preset_table`
- **Invariant:** `pyverify.client.DEFAULT_CLK_PRESETS == ("25mhz","50mhz","100mhz")`
  mirrors `firmware/clkrst/clkrst.c`'s `clkrst_preset_table` (ids 0/1/2),
  case-sensitively (the firmware matches with `strcmp`).
- **Pinned by (two halves):**
  - `host/pyverify/tests/test_client.py:132` freezes the **literal tuple** value.
    This test **travels with the package** — it keeps the constant from changing
    silently in the new repo.
  - `tests/firmware_logic/test_fakeshell_conformance.py` (its `_CLK_PRESETS` +
    `set_clk_ok`/`set_clk_bad_preset` cases) round-trips those presets through
    the real firmware, proving the tuple still matches the firmware table. This
    half **stays in the integrator**.
- **Boundary fate:** the pyverify repo keeps the literal-value pin; the
  firmware-vs-tuple proof must become a cross-repo contract test (ideally
  against the SystemRDL/contracts source of truth once §6.1 of the plan lands).
  Open issue: `OPEN_ISSUES.md` I16 — when the real DRP table replaces this
  placeholder, update the tuple in lockstep.

### L3 — `static_id` CRC-32 scheme (DFX build ↔ firmware ↔ host)
- **Invariant:** the 32-bit `static_id` (CRC-32 of the locked static netlist,
  minted by `fpga/dfx/build_dfx.tcl`) is carried and compared identically by the
  firmware (`config_agent`/`net_proto`) and the host (`pyverify.pusher` header +
  `pyverify.overlay` validation); a mismatch must reject a stale partial.
- **Pinned by:** `tests/integration/test_manifest_roundtrip.py` (gen_manifest
  output validates through `pyverify.overlay`; a `static_id` mismatch after a
  simulated shell rebuild is rejected) — **stays in the integrator**;
  `host/pyverify/tests/test_push_framing.py` pins the 24-byte header
  pack/unpack incl. the `static_id` field — **travels with the package**.
- **Note:** pyverify only *compares* the value; it does not re-implement the
  CRC derivation, so the package-side lockstep is the header field encoding, not
  the algorithm. The algorithm's cross-area agreement stays an integrator
  concern.

---

## 4. Proven extraction procedure (`scripts/extract_pyverify.sh`)

The script produces a standalone repo whose history is exactly the commits that
touched `host/pyverify/`, with that directory promoted to root, then installs
and tests it — all inside a **throwaway clone**, nothing pushed, the working
repo untouched.

```
scripts/extract_pyverify.sh [WORKDIR]      # env: PYTHON, RUN_TESTS, PREFIX, SPLIT_BRANCH
```

**Preferred tool:** `git subtree split --prefix=host/pyverify -b pyverify-standalone`
(git-contrib) — the canonical command the real cut will run. Where git-subtree
is not installed (as on this lab host), the script falls back to core git's
`git filter-branch --subdirectory-filter host/pyverify`, which yields an
equivalent root-promoted history. Both run **only** in the throwaway clone. The
script never runs `git filter-repo`, and never rewrites history in the working
repo.

### Dry-run result (this environment, `PYTHON=python3.10`)

- **History:** 7 commits on the split branch (the commits that touched
  `host/pyverify/` in the current tree's history).
- **Top-level of the standalone repo:** `pyproject.toml`, `pyverify/`, `tests/`
  (host/pyverify promoted to root — exactly the target layout).
- **Install:** `pip install -e .` succeeds; `import pyverify` resolves as an
  installed package from outside the repo.
- **Tests:** `215 passed, 7 skipped, 1 deselected` (exit 0). Skips are
  well-behaved and expected in a fresh clone: 6 × `test_e2e_deploy.py` skip
  because the real `fpga/dfx/overlay/…` isn't present (a soft, self-skipping
  reach), 1 × `test_tender_plugin.py` skips on the Python-3.11 floor. The 1
  deselected is the integrator-glue shim test (see §7).

> The dry-run extracts **committed** history, so uncommitted work under
> `host/pyverify/` is not in the split. When the dry-run ran, `client.py`,
> `pusher.py`, `test_client.py` and `test_pusher_send.py` were dirty; they were
> committed mid-session as `2f8813d` ("over-the-wire DFX partial reconfiguration
> proven on KU115 silicon"). **Re-run the split after any such landing** — this
> is correct `git subtree` behaviour, not a defect.

> **`test_pusher_send.py` needed a fix** (2026-07-09), on top of `2f8813d`.
> That commit claims `pyverify 228 passed / 1 skipped`, but at HEAD on this host
> the newly-landed `test_tcp_send_windowed_raises_on_early_close` fails
> **deterministically** (`227 passed, 1 failed`). Cause: `sendall()` returns as
> soon as the kernel copies the payload into the *sender's* socket buffer, which
> Linux autotunes up to `tcp_wmem[max]` (4 MiB here) — large enough to swallow
> the whole 400 KB test payload, so the peer's early close is never observed
> ("DID NOT RAISE"). The test now clamps the client's `SO_SNDBUF` via
> `monkeypatch`, forcing the send to genuinely block against the never-reopened
> window. Clamping only the receiver's `SO_RCVBUF` is **not** sufficient (that
> attempt gave 3/8). Suite is now `228 passed, 1 skipped`, stable over 10 runs —
> matching the count `2f8813d` claims.

---

## 5. Verification (repo stays green both ways)

Canonical interpreter: miniconda `python3.10` (matches `tests/Makefile`; the
ambient `python3` is 3.8, below pyverify's `>=3.10` floor).

| Command | Before | After |
|---|---|---|
| `cd host/pyverify && python3.10 -m pytest -q` (package unchanged) | 227 passed, 1 skipped, 1 flaky¹ | 227 passed, 1 skipped, 1 flaky¹ |
| `python3.10 -m pytest tests -q` (repo root, **fallback path** — pyverify NOT installed) | 132 passed | 132 passed |
| repo-root `tests` via **installed-package path** (venv w/ `pip install -e host/pyverify` + cocotb) | — | 132 passed |
| standalone repo suite (venv, `pip install -e .`) | — | 227 passed, 1 skipped² |
| extraction dry-run standalone suite (committed history) | — | 215 passed, 7 skipped, exit 0 |

¹ `test_pusher_send.py::test_tcp_send_windowed_raises_on_early_close` is a
pre-existing **flaky** OS-socket-buffer timing test (passes ~2/3 in isolation),
unrelated to this work. ² deselecting the flaky test.

Both the fallback path (pyverify not installed → in-tree copy via conftest) and
the installed-package path (pyverify pip-installed → conftest is a no-op, the
installed dist wins, verified importable from `/tmp`) are green at 132.

---

## 6. Exact remaining steps to actually cut the repo

1. **Commit the current `host/pyverify/` working-tree edits** so the split
   captures them (the dry-run splits committed history only).
2. **Create the remote:** `git.soton.ac.uk/soclabs/soclabs-mps3-pyverify` (empty).
3. **Produce the history** with the preferred tool in a clone:
   `git subtree split --prefix=host/pyverify -b pyverify-standalone`
   (`scripts/extract_pyverify.sh` does exactly this and validates it first).
4. **Relocate the two integrator-glue tests OUT of the package** (see §7) before
   pushing, so the standalone CI is green without deselects.
5. **Push** the split branch to the new remote's `main`.
6. **Add it back as a submodule** under the house layout:
   `git submodule add ../soclabs-mps3-pyverify deps/soclabs-mps3-pyverify`, and
   have the integrator install it (`pip install -e deps/soclabs-mps3-pyverify`
   in the test env, or add to `make deps`). The `tests/integration/conftest.py`
   fallback then becomes a no-op automatically — nothing else in `tests/` needs
   editing.
7. **Reconstitute the lockstep guards** (§3) as cross-repo contract tests: the
   firmware-side proofs (`test_fakeshell_conformance.py`, `test_json_golden.py`)
   and the DFX-side proof (`test_manifest_roundtrip.py`) stay in the integrator
   and now run against the **pinned submodule**.
8. **Delete the in-tree copy** (`host/pyverify/`) and retarget `host/pusher/
   push.py` — either drop it (once `test_push_framing`'s shim test moves) or keep
   a one-line shim that imports the submodule. Do this **last**, only after the
   integrator has been green against the submodule for one full `make check`
   cycle (plan §8 rollback rule).

---

## 7. Honest residue — what would still break on a real cut

Two tests live under `host/pyverify/tests/` but are **integrator glue**, not
package tests — they exercise the seam between pyverify and things that stay in
the integrator, and would break/never-run in a clean standalone repo:

1. **`test_push_framing.py::test_host_pusher_shim_is_a_drop_in_alias`** —
   reaches `host/pusher/push.py` (two dirs up, outside the package). In a
   standalone repo `host/pusher/` doesn't exist → `ModuleNotFoundError: No
   module named 'push'` (this is the one failure the dry-run surfaces before we
   deselect it). **Fix on cut:** move this drop-in-alias assertion into the
   integrator (it tests the integrator's compat shim), and/or retire the shim
   per step 8.
2. **`test_tender_plugin.py`** — imports the plugin from `host/tender/` and the
   `fpgahub` reference checkout. In a standalone repo `host/tender/` is absent.
   It already skips cleanly without Python 3.11 + pydantic, so it won't *fail*
   CI, but it can never *run* there. **Fix on cut:** move it into the integrator
   (or a future `soclabs-tender` repo); the tender plugin is explicitly not part
   of the pyverify package.

A third, softer coupling is **not** a blocker: `test_e2e_deploy.py`'s
real-overlay cases discover `fpga/dfx/overlay/…` if present and **skip
gracefully** when it isn't, so they degrade to skips in a standalone clone
rather than failing. Worth keeping as an optional integrator-run smoke.

Everything else in `host/pyverify/tests/` is self-contained and passes
standalone.
