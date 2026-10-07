"""fpga/dfx/tools/tests/test_dfx_jobs.py -- DFX_JOBS (MINT-SPEED lane, 2026-09-24).

Stage 4's RM configs as N concurrent Vivados (tools/dfx_jobs.py + tools/dfx_jobs.tcl).
Each claim below has a negative control:

  1. THE DEFAULT IS UNMOVED. DFX_JOBS unset and DFX_JOBS=1 plan the same bytes
     (`make -n`, every stage-4 entry point); DFX_JOBS=4 changes exactly the tool
     in front of the stage-4 Vivado arguments and nothing else; a bad value is
     refused at parse time.
  2. SAME ARTEFACTS. Under a fake Vivado that runs the REAL build_dfx.tcl
     (tests/fake_vivado.py: deterministic design model, Vivado-style echo), the
     parallel stage 4 writes the same static_routed_locked.dcp (so the same
     static_id), the same routed checkpoints, partial/clearing .bin pairs,
     static_stamp.json and overlay_inputs.txt as the one-process flow.
  3. FAILURE ISOLATION. A failing config (Tcl error, a DFX_*_GATE_FAILED gate, a
     SIGKILL) does not stop the others; the stage fails naming it; NO
     static_id.txt / overlay_inputs.txt is written; the unanchored Makefile grep
     for the COMPLETE marker finds nothing; the re-run resumes and runs ONLY the
     failed config.
  4. The reference failing stops everything; a changed input voids a resume; a
     re-run after success is a fresh mint; the memory gate serialises; a dead
     one-process stage 4 can be adopted without re-routing (static_id kept).
  5. The real `make prod` / `make mint-prod` recipes, gates included, pass with
     DFX_JOBS under the fake Vivado, and a finished tree is a no-op.

Board-free, Vivado-free: stdlib + pytest + tclsh (+ make).
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest


def _find_repo(start):
    p = pathlib.Path(start).resolve()
    for cand in [p] + list(p.parents):
        if (cand / "fpga" / "dfx" / "Makefile").is_file():
            return cand
    raise RuntimeError("repo root not found above %s" % start)


REPO = _find_repo(__file__)
DFX = REPO / "fpga" / "dfx"
TOOLS = DFX / "tools"
BUILD_DFX = DFX / "build_dfx.tcl"
FAKE = pathlib.Path(__file__).resolve().parent / "fake_vivado.py"
JOBS = TOOLS / "dfx_jobs.py"
MAKE = shutil.which("make")
TCLSH = shutil.which("tclsh")
RMS = "rm_greybox rm_led rm_regdemo_a rm_regdemo_b rm_uart_echo"

needs_tcl = pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
needs_make = pytest.mark.skipif(not MAKE, reason="make not installed")

FAKE_ENV = ("FAKE_VIVADO_FAIL", "FAKE_VIVADO_GATE_FAIL", "FAKE_VIVADO_KILL", "FAKE_VIVADO_SLEEP",
            "FAKE_VIVADO_XDC_DROP", "FAKE_VIVADO_AXSS_SLR1",
            "FAKE_VIVADO_TRACE", "FAKE_VIVADO_RUNS", "DFX_JOBS", "DFX_PHASE", "DFX_ADD_RMS",
            "DFX_ROW_FILE", "DFX_SHELL_CPU", "MINT_HUB", "SHELL_CPU", "VIVADO", "VIVADO_VER")


def env_with(**kw):
    env = dict(os.environ)
    for k in FAKE_ENV:
        env.pop(k, None)
    env.update({k: str(v) for k, v in kw.items()})
    return env


def shell_dcp(tmp, tag="shellA"):
    p = tmp / "shell.dcp"
    p.write_text("FAKEDCP\nstatic=%s\n" % tag)
    return p


def vivado_args(out, dcp, rms=RMS):
    return ["-mode", "batch", "-source", str(BUILD_DFX), "-journal", str(out / "build.jou"),
            "-log", str(out / "build.log"), "-tclargs", str(REPO), str(out), str(dcp), rms, "u_rp_dut"]


def one_process(out, dcp, rms=RMS, **env):
    """The one-process stage 4 (DFX_JOBS unset): the fake Vivado directly."""
    out.mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, str(FAKE)] + vivado_args(out, dcp, rms), cwd=str(out),
                          env=env_with(**env), capture_output=True, text=True)


def parallel(out, dcp, jobs=3, rms=RMS, extra=(), **env):
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(JOBS), "run", "--jobs", str(jobs), "--poll", "0.02",
           "--vivado", str(FAKE)] + list(extra) + ["--"] + vivado_args(out, dcp, rms)
    return subprocess.run(cmd, cwd=str(out), env=env_with(**env), capture_output=True, text=True)


ARTEFACT = re.compile(r"(\.bin$|_routed\.dcp$|^static_routed_locked\.dcp$|^static_id\.txt$|"
                      r"^overlay_inputs\.txt$|^static_stamp\.json$|^pr_verify_.*\.rpt$|_synth\.dcp$)")


def artefacts(out):
    """name -> bytes of everything a later stage consumes (a .bit carries a
    wall-clock header, as Vivado's does; its .bin twin is compared instead).
    pr_verify reports name their inputs by path, so the dir is normalised."""
    got = {}
    for p in sorted(out.iterdir()):
        if p.is_file() and ARTEFACT.search(p.name):
            got[p.name] = p.read_bytes().replace(str(out).encode(), b"<OUT>")
    return got


def runs(path):
    return path.read_text().split("\n")[:-1] if path.exists() else []


# ---------------------------------------------------------------------------
# 1. make -n: the default is unmoved, DFX_JOBS=4 swaps exactly one tool
# ---------------------------------------------------------------------------
def make_n(tmp, *goals, **kw):
    base = dict(BUILD=tmp / "b", SHELL_PROJ=tmp / "s", OVERLAY_ROOT=tmp / "o", FW_WS=tmp / "w",
                MINT_HUB="", SHELL_TOUCH=1, TOUCH=1)
    base.update(kw)
    args = ["%s=%s" % (k, v) for k, v in sorted(base.items())]
    return subprocess.run([MAKE, "-n", "--no-print-directory", "-C", str(DFX)] + list(goals) + args,
                          capture_output=True, text=True, env=env_with())


PLANS = [("mint",), ("mint-prod",), ("prod",), ("mint", "DRYRUN=1")]


@needs_make
@pytest.mark.parametrize("cpu", ["", "mbv"])
@pytest.mark.parametrize("goal", PLANS, ids=lambda g: "+".join(g))
def test_dfx_jobs_1_plans_exactly_what_unset_plans(tmp_path, goal, cpu):
    kw = dict(a.split("=", 1) for a in goal[1:])
    if cpu:
        kw["SHELL_CPU"] = cpu
    unset = make_n(tmp_path, goal[0], **kw)
    one = make_n(tmp_path, goal[0], DFX_JOBS=1, **kw)
    assert unset.returncode == 0, unset.stderr
    assert (one.stdout, one.stderr, one.returncode) == (unset.stdout, unset.stderr, unset.returncode)
    assert "tools/dfx_jobs.py" not in unset.stdout, "the default plan runs dfx_jobs.py"
    assert re.search(r"&& (DFX_SHELL_CPU=mbv )?\S+/vivado -mode batch", unset.stdout), \
        "the one-process stage 4 no longer runs Vivado directly"


@needs_make
@pytest.mark.parametrize("cpu", ["", "mbv"])
@pytest.mark.parametrize("goal", PLANS, ids=lambda g: "+".join(g))
def test_dfx_jobs_4_swaps_only_the_stage4_tool(tmp_path, goal, cpu):
    kw = dict(a.split("=", 1) for a in goal[1:])
    if cpu:
        kw["SHELL_CPU"] = cpu
    unset = make_n(tmp_path, goal[0], **kw).stdout.split("\n")
    four = make_n(tmp_path, goal[0], DFX_JOBS=4, **kw).stdout.split("\n")
    assert len(unset) == len(four)
    changed = [(a, b) for a, b in zip(unset, four) if a != b]
    assert 1 <= len(changed) <= 2, changed           # the tool line (+ its DRYRUN echo)
    for a, b in changed:
        m = re.search(r"(\S+/vivado) -mode batch", a)
        assert m, a
        viv = m.group(1)
        swapped = "%s %s run --jobs 4 --mem-per-job-gb 16 --vivado %s --" % ("python3", JOBS, viv)
        assert b == a.replace(viv + " -mode batch", swapped + " -mode batch", 1), (a, b)
    # the recipe's own gates read the (assembled) build.log exactly as before
    for needle in ("DFX_BUILD_COMPLETE", "_GATE_FAILED", "ltx_sidecar.py"):
        assert sum(needle in ln for ln in unset) == sum(needle in ln for ln in four)


@needs_make
@pytest.mark.parametrize("bad", ["0", "abc", "-2", "2x"])
def test_dfx_jobs_bad_value_refused_at_parse_time(tmp_path, bad):
    p = make_n(tmp_path, "mint", DFX_JOBS=bad)
    assert p.returncode != 0 and "want a positive integer" in p.stderr, p.stderr


# ---------------------------------------------------------------------------
# 2. the parallel stage 4 writes what the one-process stage 4 writes
# ---------------------------------------------------------------------------
@needs_tcl
def test_parallel_stage4_equals_the_one_process_flow(tmp_path):
    dcp = shell_dcp(tmp_path)
    leg = one_process(tmp_path / "leg", dcp)
    assert leg.returncode == 0, (tmp_path / "leg" / "build.log").read_text()[-3000:]
    par = parallel(tmp_path / "par", dcp, jobs=3, FAKE_VIVADO_SLEEP=0.3)
    assert par.returncode == 0, par.stdout + par.stderr
    a, b = artefacts(tmp_path / "leg"), artefacts(tmp_path / "par")
    assert sorted(a) == sorted(b)
    assert len([n for n in a if n.endswith(".bin")]) == 5 * 2 + 1       # 5 pairs + the full image
    for name in a:
        assert a[name] == b[name], "%s differs between the one-process and the parallel stage 4" % name
    s = json.loads((tmp_path / "par" / "dfx_jobs" / "summary.json").read_text())
    assert s["ok"] and s["max_concurrency"] == 3, s
    log = (tmp_path / "par" / "build.log").read_text()
    # every COMPLETE marker is a real line, none an echo of the Tcl that prints it
    assert [ln for ln in log.split("\n") if "DFX_BUILD_COMPLETE" in ln] == \
        [ln for ln in log.split("\n") if ln.startswith("DFX_BUILD_COMPLETE static_id=")]
    assert len([ln for ln in log.split("\n") if ln.startswith("DFX_BUILD_COMPLETE")]) == 1
    # one log per job, assembled in RM order after the reference
    order = [m.group(1) for m in re.finditer(r"^#### DFX_JOBS: -------- (\S+) --", log, re.M)]
    assert order == RMS.split()[1:] + ["finish"], order


# ---------------------------------------------------------------------------
# 3. failure isolation, then resume
# ---------------------------------------------------------------------------
@needs_tcl
def test_a_failed_config_does_not_stop_the_others_and_the_rerun_resumes(tmp_path):
    dcp = shell_dcp(tmp_path)
    out, runlog = tmp_path / "par", tmp_path / "runs.txt"
    bad = parallel(out, dcp, jobs=2, FAKE_VIVADO_FAIL="rm_regdemo_a:route_design",
                   FAKE_VIVADO_RUNS=runlog)
    assert bad.returncode == 1
    assert "1 of 4 RM config(s) FAILED: rm_regdemo_a" in bad.stderr, bad.stderr
    assert "DFX_JOBS_FAILED rm=rm_regdemo_a :: exit 1: ERROR: [Fake 1-1] route_design" in bad.stdout
    for rm in ("rm_led", "rm_regdemo_b", "rm_uart_echo"):          # the others ran to the end
        assert (out / "dfx_jobs" / (rm + ".row")).is_file(), rm
        assert (out / ("config_%s_pblock_rp_dut_partial.bin" % rm)).is_file(), rm
    # NOTHING says the stage finished: make's target is absent, and the Makefile's
    # (unanchored) COMPLETE grep finds nothing in the assembled log
    assert not (out / "static_id.txt").exists() and not (out / "overlay_inputs.txt").exists()
    assert "DFX_BUILD_COMPLETE" not in (out / "build.log").read_text()
    assert "rm_regdemo_a" in (out / "build.log").read_text()
    assert sorted(runs(runlog)) == sorted(["ref -", "- rm_led", "- rm_regdemo_a", "- rm_regdemo_b",
                                           "- rm_uart_echo"])
    # the re-run: only the failed config, then finish -- same static
    runlog.unlink()
    good = parallel(out, dcp, jobs=2, FAKE_VIVADO_RUNS=runlog)
    assert good.returncode == 0, good.stdout + good.stderr
    assert runs(runlog) == ["- rm_regdemo_a", "finish -"], runs(runlog)
    assert "RESUME" in good.stdout
    leg = one_process(tmp_path / "leg", dcp)
    assert leg.returncode == 0
    assert artefacts(tmp_path / "leg") == artefacts(out)


@needs_tcl
def test_gate_failure_and_a_killed_worker_are_both_named(tmp_path):
    dcp = shell_dcp(tmp_path)
    out = tmp_path / "par"
    r = parallel(out, dcp, jobs=3, FAKE_VIVADO_GATE_FAIL="rm_led", FAKE_VIVADO_KILL="rm_regdemo_b")
    assert r.returncode == 1
    assert "2 of 4 RM config(s) FAILED" in r.stderr, r.stderr
    assert re.search(r"DFX_JOBS_FAILED rm=rm_led :: DFX_HDPR_GATE_FAILED rm=rm_led", r.stdout), r.stdout
    assert "DFX_JOBS_FAILED rm=rm_regdemo_b :: killed by signal 9" in r.stdout, r.stdout
    assert (out / "dfx_jobs" / "rm_uart_echo.row").is_file()
    s = json.loads((out / "dfx_jobs" / "summary.json").read_text())
    assert not s["ok"] and sorted(s["failed"]) == ["rm_led", "rm_regdemo_b"], s
    # the Makefile's gate grep sees the gate line in the assembled log
    assert re.search(r"^DFX_HDPR_GATE_FAILED", (out / "build.log").read_text(), re.M)


@needs_tcl
def test_the_reference_failing_stops_everything(tmp_path):
    dcp = shell_dcp(tmp_path)
    runlog = tmp_path / "runs.txt"
    r = parallel(tmp_path / "par", dcp, FAKE_VIVADO_FAIL="rm_greybox:route_design",
                 FAKE_VIVADO_RUNS=runlog)
    assert r.returncode == 1 and "the REFERENCE config failed" in r.stderr, r.stderr
    assert runs(runlog) == ["ref -"], "a worker ran without a locked static"


# ---------------------------------------------------------------------------
# 4. resume rules, memory gate, adopt
# ---------------------------------------------------------------------------
@needs_tcl
def test_a_changed_input_voids_the_resume(tmp_path):
    dcp = shell_dcp(tmp_path)
    out, runlog = tmp_path / "par", tmp_path / "runs.txt"
    assert parallel(out, dcp, FAKE_VIVADO_FAIL="rm_led:place_design").returncode == 1
    st = dcp.stat()
    os.utime(str(dcp), ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))   # the shell was rebuilt
    r = parallel(out, dcp, FAKE_VIVADO_RUNS=runlog)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESUME REFUSED" in r.stdout and runs(runlog)[0] == "ref -", runs(runlog)
    assert len(runs(runlog)) == 6


@needs_tcl
def test_a_rerun_after_success_is_a_fresh_mint(tmp_path):
    dcp = shell_dcp(tmp_path)
    out, runlog = tmp_path / "par", tmp_path / "runs.txt"
    assert parallel(out, dcp, rms="rm_greybox rm_led").returncode == 0
    first = artefacts(out)
    r = parallel(out, dcp, rms="rm_greybox rm_led", FAKE_VIVADO_RUNS=runlog)
    assert r.returncode == 0, r.stdout + r.stderr
    assert runs(runlog) == ["ref -", "- rm_led", "finish -"], runs(runlog)
    assert "removed the previous" in r.stdout and list(out.glob("build_*.backup.log"))
    assert artefacts(out) == first           # deterministic fake: same inputs, same static


@needs_tcl
def test_the_memory_gate_holds_workers_back(tmp_path):
    r = parallel(tmp_path / "par", shell_dcp(tmp_path), jobs=4, extra=["--mem-per-job-gb", "1e9"])
    assert r.returncode == 0, r.stdout + r.stderr
    s = json.loads((tmp_path / "par" / "dfx_jobs" / "summary.json").read_text())
    assert s["max_concurrency"] == 1, s
    assert "starting rm_led anyway" in r.stdout


@needs_tcl
def test_adopt_a_one_process_stage4_that_died_after_the_lock(tmp_path):
    dcp = shell_dcp(tmp_path)
    out = tmp_path / "dead"
    dead = one_process(out, dcp, FAKE_VIVADO_FAIL="rm_regdemo_a:route_design")
    assert dead.returncode == 1
    sid = (out / "static_id.txt").read_text()
    locked = (out / "static_routed_locked.dcp").read_bytes()
    assert not (out / "overlay_inputs.txt").exists()
    r = parallel(out, dcp, jobs=2, extra=["--adopt-locked"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ADOPTED the locked static" in (out / "dfx_jobs" / "ref" / "build.log").read_text()
    assert (out / "static_id.txt").read_text() == sid
    assert (out / "static_routed_locked.dcp").read_bytes() == locked, "adopt re-locked the static"
    assert one_process(tmp_path / "leg", dcp).returncode == 0
    assert artefacts(tmp_path / "leg") == artefacts(out)


@needs_tcl
def test_adopt_refuses_a_tree_whose_static_id_disagrees(tmp_path):
    dcp = shell_dcp(tmp_path)
    out = tmp_path / "dead"
    assert one_process(out, dcp, FAKE_VIVADO_FAIL="rm_led:route_design").returncode == 1
    (out / "static_id.txt").write_text("0xDEADBEEF\n")
    r = parallel(out, dcp, extra=["--adopt-locked"])
    assert r.returncode == 1 and "REFERENCE" in r.stderr
    assert "refusing to adopt" in (out / "dfx_jobs" / "ref" / "build.log").read_text()


@needs_tcl
def test_a_worker_leaves_the_shared_files_alone(tmp_path):
    """DFX_ROW_FILE = one RM worker: no static_id.txt rewrite, no overlay_inputs.txt
    merge; its row goes to its own file. Without it the add path is today's."""
    dcp = shell_dcp(tmp_path)
    out = tmp_path / "prod"
    assert one_process(out, dcp, rms="rm_greybox rm_led").returncode == 0
    before = {n: (out / n).read_bytes() for n in ("static_id.txt", "overlay_inputs.txt")}
    os.utime(str(out / "static_id.txt"), (1, 1))
    add = dict(DFX_ADD_RMS="rm_uart_echo", DFX_REUSE_LOCKED=out / "static_routed_locked.dcp",
               DFX_STATIC_ID_FILE=out / "static_id.txt", DFX_REF_ROUTED=out / "config_rm_greybox_routed.dcp",
               DFX_ROW_FILE=tmp_path / "w.row")
    r = one_process(out, dcp, rms="", **add)
    assert r.returncode == 0
    assert {n: (out / n).read_bytes() for n in before} == before and (out / "static_id.txt").stat().st_mtime == 1
    row = (tmp_path / "w.row").read_text().split("\n")
    assert row[0] == "static_id " + before["static_id.txt"].decode().strip()
    assert row[1].startswith("row rm_uart_echo uart_echo ")
    add["DFX_ADD_RMS"] = "rm_uart_echo rm_regdemo_b"
    two = one_process(out, dcp, rms="", **add)
    assert two.returncode == 1 and "one RM worker's record" in (out / "build.log").read_text()
    del add["DFX_ROW_FILE"]                                  # today's add-rm: merges
    add["DFX_ADD_RMS"] = "rm_regdemo_b"
    assert one_process(out, dcp, rms="", **add).returncode == 0
    assert "rm_regdemo_b regdemo_b" in (out / "overlay_inputs.txt").read_text()


@needs_tcl
def test_tcl_row_records_read_back_in_python_and_refuse_another_static(tmp_path):
    tcl = """
source %s
dfx_jobs_write_row %s/r.row 0x12345678 {rm_led led 0x0100001E a.bin a_clear.bin}
puts [dfx_jobs_read_row %s/r.row 0x12345678]
puts "stale=<[dfx_jobs_read_row %s/r.row 0x87654321]>"
puts "absent=<[dfx_jobs_read_row %s/none.row 0x12345678]>"
""" % (TOOLS / "dfx_jobs.tcl", tmp_path, tmp_path, tmp_path, tmp_path)
    out = subprocess.run([TCLSH], input=tcl, capture_output=True, text=True).stdout
    assert "rm_led led 0x0100001E a.bin a_clear.bin" in out and "stale=<>" in out and "absent=<>" in out
    sys.path.insert(0, str(TOOLS))
    import dfx_jobs
    assert dfx_jobs.read_row(str(tmp_path / "r.row"), "0x12345678") == \
        ["rm_led", "led", "0x0100001E", "a.bin", "a_clear.bin"]
    assert dfx_jobs.read_row(str(tmp_path / "r.row"), "0x87654321") is None
    assert not list(tmp_path.glob("*.tmp*")), "the atomic write left its temp file"


def test_the_marker_literal_is_never_echoed():
    """Vivado echoes every top-level command it sources into the log. The one-
    process flow prints DFX_BUILD_COMPLETE from its LAST top-level line only;
    dfx_jobs.tcl prints it from a proc, so it must be sourced -notrace."""
    src = BUILD_DFX.read_text().split("\n")
    hits = [i for i, ln in enumerate(src) if "DFX_BUILD_COMPLETE" in ln]
    assert [src[i] for i in hits] == [ln for ln in src if ln.startswith('puts "DFX_BUILD_COMPLETE')]
    assert hits == [max(i for i, ln in enumerate(src) if ln.strip())], "the marker moved off the last line"
    assert re.search(r"^source -notrace \$repo_root/fpga/dfx/tools/dfx_jobs\.tcl$", BUILD_DFX.read_text(), re.M)


# ---------------------------------------------------------------------------
# 5. the real recipes, gates included
# ---------------------------------------------------------------------------
@needs_make
@needs_tcl
@pytest.mark.parametrize("jobs", ["", "2"])
def test_make_prod_runs_green_under_the_fake_vivado(tmp_path, jobs):
    dcp = shell_dcp(tmp_path)
    args = ["prod", "BUILD=%s" % (tmp_path / "b"), "STATIC_DCP=%s" % dcp, "VIVADO=%s" % FAKE,
            "RM_SUBSET=rm_greybox rm_led rm_regdemo_a"] + (["DFX_JOBS=" + jobs] if jobs else [])
    r = subprocess.run([MAKE, "--no-print-directory", "-C", str(DFX)] + args,
                       capture_output=True, text=True, env=env_with())
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ("PROD BUILD OK", "DFX_LTX_GATE_OK", "DFX_STATIC_LTX_GATE_OK"):
        assert needle in r.stdout
    assert ("STAGE 4 DONE" in r.stdout) == bool(jobs)


@needs_make
@needs_tcl
def test_mint_prod_with_dfx_jobs_then_a_noop(tmp_path):
    sp, b = tmp_path / "sp", tmp_path / "b"
    sp.mkdir()
    b.mkdir()
    (sp / "shell_static_synth.dcp").write_text("FAKEDCP\nstatic=shellB\n")
    (b / "preflight.stamp").write_text("")
    args = ["mint-prod", "BUILD=%s" % b, "SHELL_PROJ=%s" % sp, "VIVADO=%s" % FAKE,
            "RM_SET=greybox led regdemo_a", "DFX_JOBS=2"]
    r = subprocess.run([MAKE, "--no-print-directory", "-C", str(DFX)] + args,
                       capture_output=True, text=True, env=env_with())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MINTED static_id = 0x" in r.stdout and "STAGE 4 DONE" in r.stdout
    again = subprocess.run([MAKE, "--no-print-directory", "-C", str(DFX)] + args,
                           capture_output=True, text=True, env=env_with())
    assert again.returncode == 0 and "Nothing to be done" in again.stdout, again.stdout


@needs_tcl
def test_control_mint_prod_refuses_a_full_bit_whose_slrs_disagree_on_usr_access(tmp_path):
    """RC1 (2026-09-24): the full .bit must write ONE USR_ACCESS word in EVERY SLR
    stream. The stage-4 gate walks the packet stream (tools/bit_identity.py)."""
    sp, b = tmp_path / "sp", tmp_path / "b"
    sp.mkdir()
    b.mkdir()
    (sp / "shell_static_synth.dcp").write_text("FAKEDCP\nstatic=shellB\n")
    (b / "preflight.stamp").write_text("")
    args = ["mint-prod", "BUILD=%s" % b, "SHELL_PROJ=%s" % sp, "VIVADO=%s" % FAKE,
            "RM_SET=greybox led", "DFX_JOBS=2"]
    r = subprocess.run([MAKE, "--no-print-directory", "-C", str(DFX)] + args, capture_output=True,
                       text=True, env=env_with(FAKE_VIVADO_AXSS_SLR1="0x82404000"))
    assert r.returncode != 0
    assert "does not write ONE USR_ACCESS word in EVERY SLR stream" in r.stdout, r.stdout + r.stderr
    # the real word's top byte is VERSION's major (gen_version.py ver32), not a literal
    major = int((REPO / "VERSION").read_text().strip().split(".")[0])
    assert "DIFFERENT USR_ACCESS values ['0x%02x" % major in r.stderr, r.stderr
    good = subprocess.run([sys.executable, str(TOOLS / "bit_identity.py"),
                           str(b / "prod" / "config_rm_greybox.bit")], capture_output=True, text=True)
    assert good.returncode == 1                      # the file really is the bad one
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "preflight.stamp").write_text("")
    r = subprocess.run([MAKE, "--no-print-directory", "-C", str(DFX), "mint-prod", "BUILD=%s" % ok,
                        "SHELL_PROJ=%s" % sp, "VIVADO=%s" % FAKE, "RM_SET=greybox led", "DFX_JOBS=2"],
                       capture_output=True, text=True, env=env_with())
    assert r.returncode == 0 and "in all 2 SLR stream(s)" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# the XDC gate (FLOW_CONTRACT §2 rule 10): a dropped XDC command fails the config
# ---------------------------------------------------------------------------
@needs_tcl
@pytest.mark.parametrize("where,stage", [("rm_greybox:read_checkpoint", "link"),
                                         ("rm_led:opt_design", "opt")])
def test_control_a_dropped_xdc_command_fails_the_one_process_flow(tmp_path, where, stage):
    dcp = shell_dcp(tmp_path)
    clean = one_process(tmp_path / "clean", dcp)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    log = (tmp_path / "clean" / "build.log").read_text()
    assert log.count("INFO: xdc_gate -- no dropped XDC commands") == 2 * len(RMS.split())
    assert "INFO: xdc_gate -- 1 XDC file(s) use only XDC commands" in log   # the shell timing XDC
    assert (tmp_path / "clean" / "config_rm_led_pblock_rp_dut_partial.bin").exists()
    out = tmp_path / "bad"
    r = one_process(out, dcp, FAKE_VIVADO_XDC_DROP=where)
    assert r.returncode != 0
    rm = where.split(":")[0]
    log = (out / "build.log").read_text()
    assert re.search(r"^DFX_XDC_GATE_FAILED rm=%s stage=%s$" % (rm, stage), log, re.M), log[-3000:]
    assert "Designutils 20-1307" in log
    if rm == "rm_greybox":        # the reference config: nothing is minted
        assert not (out / "static_id.txt").exists()
    assert not (out / ("config_%s_pblock_rp_dut_partial.bin" % rm)).exists()


@needs_tcl
def test_control_a_dropped_xdc_command_is_named_by_dfx_jobs(tmp_path):
    out = tmp_path / "par"
    r = parallel(out, shell_dcp(tmp_path), jobs=2, FAKE_VIVADO_XDC_DROP="rm_led:opt_design")
    assert r.returncode == 1
    assert re.search(r"DFX_JOBS_FAILED rm=rm_led :: DFX_XDC_GATE_FAILED rm=rm_led stage=opt",
                     r.stdout), r.stdout
    assert re.search(r"^DFX_XDC_GATE_FAILED", (out / "build.log").read_text(), re.M)
