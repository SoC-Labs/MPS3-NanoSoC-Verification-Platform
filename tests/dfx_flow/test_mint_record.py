"""tests/dfx_flow/test_mint_record.py

Board-free gates for the ONE mint entry point (`make -C fpga/dfx mint`) and for
the machine-readable record it leaves behind (`mint.json`).

WHY THIS FILE EXISTS
--------------------
The fielded shell `0xA8C1C535` was built by MANUAL STAGES, not by any script in
this repo (`fielded/0xA8C1C535/README.md`, "Provenance"). What survived the run
is three small files plus a pile of gitignored checkpoints, and the questions a
maintainer actually asks afterwards -- *which* source trees went in, *which*
firmware flag set was baked, *which* checkpoints were reused rather than
rebuilt -- have no answer anywhere on disk. `mint.json` is that answer, written
by the flow itself; these tests are what stop it from being decorative.

Four things are gated here, each with its control:

1. THE ABSOLUTE-PATH DEFECT. `build_dfx.tcl` wrote `overlay_inputs.txt` rows
   with absolute paths (`fielded/0xA8C1C535/overlay_inputs.txt` still carries
   ten of them, rooted at one workstation's home directory), so the hand-off
   record could not travel with the artefacts it names. The row paths must be
   RELATIVE to the build dir -- checked both on the Tcl that writes them and on
   the JSON that reads them.

2. THE SCHEMA. Every provenance slot is `{"value": ..., "reason": ...}` with
   EXACTLY ONE of the two non-null. A field that cannot be reconstructed is
   null WITH the reason it is null -- never invented, and never silently
   missing.

3. THE FIELDED RECORD ROUND-TRIPS. `fielded/0xA8C1C535/mint.json` is
   regenerated from the three tracked files it was reconstructed from
   (`static_id.txt`, `overlay_inputs.txt`, `MANIFEST.md5`) and must come back
   byte-identical modulo `generated_at`. A committed record that no longer
   matches its own inputs is a record that has started lying.

4. THE MINT PLAN. `make -n -C fpga/dfx mint ...` prints its stages, in order,
   naming inputs and outputs -- and prints NOTHING to do when every artefact is
   already present and newer than its inputs. Pure `make -n`; no Vivado.

Everything here is stdlib + tclsh + make. No simulator, no board, no Vivado.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DFX = REPO / "fpga" / "dfx"
MINT_RECORD = DFX / "tools" / "mint_record.py"
BUILD_DFX_TCL = DFX / "build_dfx.tcl"
RM_LIST_TCL = DFX / "rm_list.tcl"
OVERLAY_INPUTS_TCL = DFX / "tools" / "overlay_inputs.tcl"
FIELDED_DIR = REPO / "fielded" / "0xA8C1C535"

TCLSH = shutil.which("tclsh")
MAKE = shutil.which("make") or "make"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def run_record(*args, expect_rc=0):
    """Invoke fpga/dfx/tools/mint_record.py and return (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(MINT_RECORD)] + [str(a) for a in args],
        capture_output=True, text=True,
    )
    if expect_rc is not None:
        assert proc.returncode == expect_rc, (
            "mint_record.py %s -> rc=%d\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(str(a) for a in args), proc.returncode, proc.stdout, proc.stderr)
        )
    return proc


def run_tcl(script):
    """Run a Tcl snippet under tclsh, returning stripped stdout."""
    proc = subprocess.run([TCLSH], input=script, capture_output=True, text=True)
    assert proc.returncode == 0, "tclsh failed:\n%s\n%s" % (proc.stdout, proc.stderr)
    return proc.stdout.strip()


def make_n(*goals, **kwargs):
    """`make -n -C fpga/dfx <goals> VAR=VAL ...` -> CompletedProcess.

    -n only: this never runs Vivado, a sub-make, or anything else.
    """
    env = dict(os.environ)
    env.pop("MINT_HUB", None)
    env.update(kwargs.pop("env", {}))
    variables = ["%s=%s" % (k, v) for k, v in sorted(kwargs.items())]
    proc = subprocess.run(
        [MAKE, "-n", "-C", str(DFX)] + list(goals) + variables,
        capture_output=True, text=True, env=env,
    )
    return proc


def fake_build_tree(root, static_id="0xDEADBEEF", absolute_rows=True):
    """A synthetic DFX prod/ dir with the artefacts mint_record.py reads."""
    prod = root / "prod"
    prod.mkdir(parents=True)
    (prod / "static_id.txt").write_text(static_id + "\n")
    for name, body in (
        ("static_routed_locked.dcp", b"locked-static"),
        ("config_rm_greybox_routed.dcp", b"greybox-routed"),
        ("config_rm_greybox.bit", b"greybox-bit"),
    ):
        (prod / name).write_bytes(body)
    rows = []
    for key, name, rm_id in (
        ("rm_greybox", "greybox", "0x00000000"),
        ("rm_led", "led", "0x0100001E"),
    ):
        pair = "config_%s_pblock_rp_dut_partial" % key
        (prod / (pair + ".bin")).write_bytes(b"partial-" + key.encode())
        (prod / (pair + "_clear.bin")).write_bytes(b"clear-" + key.encode())
        if absolute_rows:
            p, c = str(prod / (pair + ".bin")), str(prod / (pair + "_clear.bin"))
        else:
            p, c = pair + ".bin", pair + "_clear.bin"
        rows.append(" ".join([key, name, rm_id, p, c]))
    (prod / "overlay_inputs.txt").write_text(
        "# rm_key rm_name rm_id partial_bin clearing_bin (static_id=%s)\n%s\n"
        % (static_id, "\n".join(rows))
    )
    return prod


def logical_lines(text):
    """Rejoin backslash-continued recipe lines from `make -n` output."""
    out, pending = [], ""
    for line in text.splitlines():
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        out.append(pending + line)
        pending = ""
    if pending:
        out.append(pending)
    return out


def slots(obj, prefix=""):
    """Yield (path, slot) for every provenance slot in a record."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"value", "reason"}:
            yield prefix, obj
            return
        for k, v in obj.items():
            for item in slots(v, "%s.%s" % (prefix, k) if prefix else k):
                yield item
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for item in slots(v, "%s[%d]" % (prefix, i)):
                yield item


# --------------------------------------------------------------------------
# 1. the absolute-path defect -- the CONTROL for the build_dfx.tcl fix
# --------------------------------------------------------------------------
def test_overlay_row_helper_exists():
    """The relativizer is a real, separately-testable proc -- not inline Tcl.

    build_dfx.tcl cannot be unit-tested (it is 800 lines of Vivado calls), so
    the one line whose correctness travels with the artefacts lives in a
    sourceable helper that tclsh alone can exercise.
    """
    assert OVERLAY_INPUTS_TCL.is_file(), (
        "missing %s -- the overlay_inputs row helper build_dfx.tcl and "
        "finish_partials.tcl must share" % OVERLAY_INPUTS_TCL
    )


@pytest.mark.skipif(TCLSH is None, reason="tclsh not installed")
def test_overlay_row_rel_relativizes():
    """overlay_row_rel <out_dir> <path> -> a path relative to out_dir."""
    out = run_tcl(
        'source %s\n'
        'puts [overlay_row_rel /a/b/prod /a/b/prod/config_rm_led_partial.bin]\n'
        'puts [overlay_row_rel /a/b/prod config_rm_led_partial.bin]\n'
        'puts [overlay_row_rel /a/b/prod /a/b/prod/sub/x.bin]\n'
        'puts [overlay_row_rel /a/b/prod /a/b/other/x.bin]\n' % OVERLAY_INPUTS_TCL
    ).splitlines()
    assert out[0] == "config_rm_led_partial.bin", "absolute row not relativized"
    assert out[1] == "config_rm_led_partial.bin", "already-relative row must pass through"
    assert out[2] == "sub/x.bin"
    assert out[3] == "../other/x.bin", "a path outside out_dir must still be relative"
    for line in out:
        assert not line.startswith("/"), "overlay_row_rel returned an ABSOLUTE path: %r" % line


def test_build_dfx_writes_relative_overlay_rows():
    """THE DEFECT: build_dfx.tcl must not put absolute paths in the hand-off.

    fielded/0xA8C1C535/overlay_inputs.txt carries ten rows rooted at
    /home/<user>/... -- so the record cannot be moved to the hub, cannot be
    replayed in another checkout, and cannot be committed. Both row-emitting
    sites (the full build and the incremental add) must go through
    overlay_row_rel.
    """
    src = BUILD_DFX_TCL.read_text()
    emit = [ln.strip() for ln in src.splitlines()
            if re.search(r"lappend\s+(overlay_rows|added_rows)", ln)]
    assert emit, "no overlay-row emitting line found in build_dfx.tcl"
    for ln in emit:
        assert "overlay_row" in ln, (
            "build_dfx.tcl emits an overlay_inputs.txt row WITHOUT relativizing "
            "the bitstream paths -- the row is absolute and the record is not "
            "portable:\n    %s" % ln
        )
    assert "tools/overlay_inputs.tcl" in src, (
        "build_dfx.tcl must source fpga/dfx/tools/overlay_inputs.tcl (the shared "
        "row helper) rather than carry its own copy"
    )


def test_finish_partials_writes_relative_overlay_rows():
    """The recovery path seeds the same file and must obey the same rule."""
    src = (DFX / "tools" / "finish_partials.tcl").read_text()
    emit = [ln.strip() for ln in src.splitlines() if "lappend rows" in ln]
    assert emit, "no row-emitting line found in finish_partials.tcl"
    for ln in emit:
        assert "overlay_row" in ln, (
            "finish_partials.tcl seeds overlay_inputs.txt with ABSOLUTE paths:\n"
            "    %s" % ln
        )


# --------------------------------------------------------------------------
# 2. explicit RM registration -- the rms/* path-match heuristic
# --------------------------------------------------------------------------
@pytest.mark.skipif(TCLSH is None, reason="tclsh not installed")
def test_every_rm_declares_its_synth_mode():
    """rm_list.tcl registers HOW each RM is synthesised. Explicitly.

    build_dfx.tcl used to decide by `string match "fpga/dfx/rms/*"` on
    wrapper_dir: anything under fpga/dfx/rms/ was assumed to be a single
    dependency-free .sv it could synthesise inline. rm_socscope lives there and
    is NOT: its sources come from $SOCSCOPE_HOME via a filelist. Inline-
    synthesising just the wrapper "succeeds" with the whole trace plane left as
    a black box, and poisons every downstream config.
    """
    out = run_tcl(
        'source %s\n'
        'foreach k [rm_all_names] { puts "$k [rm_field $k synth_mode]" }\n' % RM_LIST_TCL
    ).splitlines()
    modes = dict(line.split() for line in out)
    assert modes, "rm_list.tcl declared no RMs"
    for key, mode in sorted(modes.items()):
        assert mode in ("inline", "prebuilt"), \
            "RM %s has synth_mode=%r (want inline|prebuilt)" % (key, mode)
    assert modes.get("rm_socscope") == "prebuilt", (
        "rm_socscope is registered %r -- it reads $SOCSCOPE_HOME through a "
        "filelist and can NEVER be inline-synthesised from its wrapper alone"
        % modes.get("rm_socscope")
    )
    assert modes.get("rm_greybox") == "inline", \
        "rm_greybox is a single dependency-free .sv and should stay inline"


def test_build_dfx_has_no_path_match_heuristic():
    src = BUILD_DFX_TCL.read_text()
    assert 'string match "fpga/dfx/rms/*"' not in src, (
        "build_dfx.tcl still decides inline-vs-prebuilt synth by matching the "
        "wrapper_dir path -- register it in rm_list.tcl instead"
    )
    assert "synth_mode" in src, \
        "build_dfx.tcl must read the registered synth_mode from rm_list.tcl"


# --------------------------------------------------------------------------
# 3. the record: schema, relative paths, honest nulls
# --------------------------------------------------------------------------
def test_record_schema(tmp_path):
    prod = fake_build_tree(tmp_path / "build_x")
    out = tmp_path / "mint.json"
    run_record("record", "--build-dir", prod, "--repo", REPO,
               "--fw-flags", "PRODUCT=1 CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1",
               "--rm-set", "rm_greybox rm_led", "-o", out)
    rec = json.loads(out.read_text())

    for key in ("schema", "schema_version", "record_kind", "generated_at",
                "generated_by", "static_id", "build_dir", "sources", "firmware",
                "dcps", "overlay_inputs", "timestamps", "tools"):
        assert key in rec, "mint.json is missing required key %r" % key
    assert rec["schema"] == "mps3-mint-record"
    assert re.fullmatch(r"0x[0-9A-F]{8}", rec["static_id"]), rec["static_id"]
    assert rec["record_kind"] == "mint"

    # every provenance slot: exactly one of value/reason
    found = list(slots(rec))
    assert found, "no {value, reason} provenance slots in the record"
    for path, slot in found:
        assert (slot["value"] is None) != (slot["reason"] is None), (
            "slot %s must have EXACTLY ONE of value/reason non-null: %r"
            % (path, slot)
        )

    # the flag set is recorded as data, not as a sentence
    flags = rec["firmware"]["flags"]["value"]
    assert flags["TOUCH"] == "1" and flags["HWICAP_FIFO"] == "1", flags
    assert rec["firmware"]["product"]["value"] is True

    # every DCP in the build dir is checksummed
    names = {d["name"]: d for d in rec["dcps"]}
    assert "static_routed_locked.dcp" in names, names
    for d in rec["dcps"]:
        assert re.fullmatch(r"[0-9a-f]{32}", d["md5"]), d
        assert d["bytes"] > 0


def test_record_overlay_inputs_are_relative(tmp_path):
    """The build tree's rows are absolute; the RECORD's must not be."""
    prod = fake_build_tree(tmp_path / "build_x", absolute_rows=True)
    raw = (prod / "overlay_inputs.txt").read_text()
    assert " /" in raw, "fixture should contain absolute rows"

    out = tmp_path / "mint.json"
    run_record("record", "--build-dir", prod, "--repo", REPO, "-o", out)
    rec = json.loads(out.read_text())

    assert len(rec["overlay_inputs"]) == 2
    for row in rec["overlay_inputs"]:
        for field in ("partial", "clearing"):
            p = row[field]
            assert not p.startswith("/"), \
                "%s.%s is ABSOLUTE in mint.json: %s" % (row["rm_key"], field, p)
            assert not p.startswith(".."), \
                "%s.%s escapes the build dir: %s" % (row["rm_key"], field, p)
            assert (prod / p).is_file(), "%s does not resolve inside the build dir" % p
        assert re.fullmatch(r"0x[0-9A-F]{8}", row["rm_id"]), row


def test_verify_rejects_an_absolute_path(tmp_path):
    """Negative control: the gate must FAIL on a record it should reject."""
    prod = fake_build_tree(tmp_path / "build_x")
    out = tmp_path / "mint.json"
    run_record("record", "--build-dir", prod, "--repo", REPO, "-o", out)
    run_record("verify", out)                      # sanity: it passes as written

    rec = json.loads(out.read_text())
    rec["overlay_inputs"][0]["partial"] = "/home/someone/build/x_partial.bin"
    out.write_text(json.dumps(rec, indent=2))
    proc = run_record("verify", out, expect_rc=1)
    assert "absolute" in (proc.stdout + proc.stderr).lower()


def test_verify_rejects_an_invented_value(tmp_path):
    """A slot with BOTH a value and a reason is a record that guessed."""
    prod = fake_build_tree(tmp_path / "build_x")
    out = tmp_path / "mint.json"
    run_record("record", "--build-dir", prod, "--repo", REPO, "-o", out)
    rec = json.loads(out.read_text())
    rec["sources"]["nanosoc_m0_soc"] = {"value": "deadbee", "reason": "not recorded"}
    out.write_text(json.dumps(rec, indent=2))
    proc = run_record("verify", out, expect_rc=1)
    assert "reason" in (proc.stdout + proc.stderr).lower()


def test_unrecorded_sources_are_null_with_a_reason(tmp_path):
    """No SoCScope checkout on the box -> null + why, never a guess."""
    prod = fake_build_tree(tmp_path / "build_x")
    out = tmp_path / "mint.json"
    run_record("record", "--build-dir", prod, "--repo", REPO,
               "--socscope-home", tmp_path / "nope", "-o", out)
    rec = json.loads(out.read_text())
    ss = rec["sources"]["socscope"]
    assert ss["value"] is None
    assert ss["reason"] and "nope" not in ss["reason"].split()[0:1], ss
    assert len(ss["reason"]) > 20, "a reason must say WHY, not just 'unknown'"


# --------------------------------------------------------------------------
# 4. the fielded record round-trips from its three surviving inputs
# --------------------------------------------------------------------------
def test_fielded_mint_json_is_committed():
    assert (FIELDED_DIR / "mint.json").is_file(), (
        "fielded/0xA8C1C535/mint.json is missing -- the fielded shell has a "
        "static_id, a checksum manifest and ten overlay rows, and nothing that "
        "ties them together"
    )


def test_fielded_mint_json_round_trips(tmp_path):
    """Regenerate it from the three tracked files and diff.

    Hermetic on purpose: `reconstruct` reads static_id.txt, overlay_inputs.txt
    and MANIFEST.md5 and NOTHING else -- not the gitignored binaries (absent on
    a fresh clone), not docs/FIELDED_SHELL.md (owned elsewhere and free to
    change). So this test answers the same on any machine.
    """
    if not (FIELDED_DIR / "overlay_inputs.txt").is_file():
        pytest.skip("overlay_inputs.txt is not in this tree (the public export omits it: "
                    "it records lab paths); fetch it with fetch_fielded.sh")
    committed = json.loads((FIELDED_DIR / "mint.json").read_text())
    out = tmp_path / "mint.json"
    run_record("reconstruct", "--dir", FIELDED_DIR, "--repo", REPO, "-o", out)
    fresh = json.loads(out.read_text())

    for rec in (committed, fresh):
        rec.pop("generated_at", None)
    assert fresh == committed, (
        "fielded/0xA8C1C535/mint.json no longer matches what mint_record.py "
        "reconstructs from its own inputs -- regenerate it:\n"
        "  python3 fpga/dfx/tools/mint_record.py reconstruct "
        "--dir fielded/0xA8C1C535 -o fielded/0xA8C1C535/mint.json"
    )


def test_fielded_record_is_honest():
    rec = json.loads((FIELDED_DIR / "mint.json").read_text())
    assert rec["record_kind"] == "reconstructed", rec["record_kind"]
    assert rec["static_id"] == "0xA8C1C535"
    assert len(rec["overlay_inputs"]) == 10, "the fielded shell carries ten overlays"
    for row in rec["overlay_inputs"]:
        assert not row["partial"].startswith("/"), row
        assert "/home/" not in row["partial"] and "/home/" not in row["clearing"], (
            "a site-specific home directory survived into the committed record: %r" % row
        )
    # what genuinely was not recorded stays null, WITH the reason
    for name in ("repo", "nanosoc_m0_soc", "socscope"):
        slot = rec["sources"][name]
        if slot["value"] is None:
            assert slot["reason"], "sources.%s is null with no reason" % name
    assert rec["firmware"]["flags"]["value"] is None, (
        "the fielded firmware flag set was NOT recorded in the build dir; this "
        "record must not invent it (docs/FIELDED_SHELL.md is the authority)"
    )
    assert "FIELDED_SHELL" in rec["firmware"]["flags"]["reason"]
    # the two tracked inputs check out against their own checksum manifest
    assert rec["integrity"]["static_id_txt"] == "md5-matches-MANIFEST"
    assert rec["integrity"]["overlay_inputs_txt"] == "md5-matches-MANIFEST"


def test_fielded_record_has_no_site_specific_strings():
    text = (FIELDED_DIR / "mint.json").read_text()
    for needle in ("/home/", "srv03335", "mapstone-dev"):
        assert needle not in text, \
            "site-specific string %r baked into the committed record" % needle


# --------------------------------------------------------------------------
# 5. the mint plan -- pure `make -n`, no Vivado
# --------------------------------------------------------------------------
MINT_STAGES = [
    "== mint [1/8] preflight",
    "== mint [2/8] static shell",
    "== mint [3/8] RM synth checkpoints",
    "== mint [4/8] DFX prod",
    "== mint [5/8] overlays",
    "== mint [6/8] firmware",
    "== mint [7/8] mint record",
    "== mint [8/8] hub copy",
]


def _plan(tmp_path, **kw):
    args = dict(
        BUILD=tmp_path / "build_t",
        SHELL_PROJ=tmp_path / "shell_proj",
        OVERLAY_ROOT=tmp_path / "overlay",
        FW_WS=tmp_path / "vitis_fw",
        RM_SET="greybox led eth-ss",
        TOUCH=0,
        SHELL_TOUCH=0,
        MINT_HUB="",
    )
    args.update(kw)
    proc = make_n("mint", **args)
    assert proc.returncode == 0, \
        "make -n mint failed:\n%s\n%s" % (proc.stdout, proc.stderr)
    return proc.stdout


def test_mint_plan_lists_every_stage_in_order(tmp_path):
    plan = _plan(tmp_path)
    pos = -1
    for stage in MINT_STAGES:
        i = plan.find(stage)
        assert i >= 0, "stage %r missing from the plan:\n%s" % (stage, plan)
        assert i > pos, "stage %r is out of order in the plan" % stage
        pos = i


def test_mint_plan_names_inputs_and_outputs(tmp_path):
    plan = _plan(tmp_path)
    ins = [ln for ln in plan.splitlines() if "in :" in ln]
    outs = [ln for ln in plan.splitlines() if "out:" in ln]
    assert len(ins) >= len(MINT_STAGES) - 1, \
        "stages do not name their inputs:\n%s" % plan
    assert len(outs) >= len(MINT_STAGES) - 1, \
        "stages do not name their outputs:\n%s" % plan


def test_mint_plan_routes_the_requested_rm_set(tmp_path):
    """RM_SET takes short names OR rm_ keys and reaches build_dfx.tcl."""
    plan = _plan(tmp_path, RM_SET="greybox led nanosoc")
    assert "rm_greybox rm_led rm_nanosoc" in plan, plan
    plan2 = _plan(tmp_path, RM_SET="rm_greybox rm_eth_ss")
    assert "rm_greybox rm_eth_ss" in plan2, plan2


def test_mint_plan_stages_only_prebuilt_rms(tmp_path):
    """Inline RMs need no staged .dcp; prebuilt ones do (and only those)."""
    plan = _plan(tmp_path, RM_SET="greybox led eth-ss")
    assert "rm_eth_ss_synth.dcp" in plan, plan
    assert "rm_greybox_synth.dcp" not in plan, \
        "greybox is inline-synthesised by build_dfx.tcl; it must not be staged"


def test_mint_hub_copy_is_a_noop_without_mint_hub(tmp_path):
    plan = _plan(tmp_path, MINT_HUB="")
    assert "MINT_HUB is not set" in plan, plan
    assert "rsync" not in plan, "hub copy must not run without a destination"


def test_mint_hub_copy_has_no_default_host(tmp_path):
    """No site FQDN may be baked in -- MINT_HUB is the only way to name one."""
    text = (DFX / "Makefile").read_text()
    for needle in ("mapstone-dev", "srv03335", "/home/dam1n19", "/home/david"):
        assert needle not in text, \
            "fpga/dfx/Makefile bakes in the site-specific %r" % needle
    plan = _plan(tmp_path, MINT_HUB="user@host:/some/path")
    assert "rsync" in plan and "user@host:/some/path" in plan, plan


def test_mint_hub_copy_writes_a_per_static_subdirectory(tmp_path):
    """Two mints must never share a hub destination.

    The flat copy let the 0x72BB0A36 mint overwrite the only off-host copy of
    the then-fielded 0x3F1A560F locked static (2026-09-23). The recipe must name a
    subdirectory keyed by the static_id it reads from static_id.txt, and must
    carry the shell-project files a fielded/ MANIFEST lists."""
    text = (DFX / "Makefile").read_text()
    rule = text[text.index("\nmint-hub-copy:"):text.index("\nmint: mint-hub-copy")]
    assert '"$(MINT_HUB)/$$sid/"' in rule, rule
    assert "static_id.txt" in rule and "sid=" in rule, rule
    assert "$(MINT_HUB)/)" not in rule, "the flat destination is back:\n" + rule
    for f in ("shell_static_synth.dcp", "shell_harness.xsa", "static_stamp.json"):
        assert f in rule, "%s is not carried to the hub" % f
    plan = _plan(tmp_path, MINT_HUB="user@host:/some/path")
    assert "user@host:/some/path/<static_id>/" in plan, plan


def test_mint_refuses_a_touch_flag_mismatch(tmp_path):
    """SHELL_TOUCH (fabric) and TOUCH (firmware) move together, or not at all."""
    proc = make_n("mint", BUILD=tmp_path / "b", SHELL_PROJ=tmp_path / "s",
                  OVERLAY_ROOT=tmp_path / "o", FW_WS=tmp_path / "w",
                  RM_SET="greybox", SHELL_TOUCH=1, TOUCH=0, MINT_HUB="")
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, \
        "SHELL_TOUCH=1 TOUCH=0 is a silent half-mint and must not plan:\n%s" % combined
    assert "SHELL_TOUCH" in combined and "TOUCH" in combined


def test_mint_dryrun_launches_nothing(tmp_path):
    """DRYRUN=1 prints; it does not execute. (The old script's DRYRUN wrote.)"""
    build = tmp_path / "build_d"
    proc = make_n("mint", BUILD=build, SHELL_PROJ=tmp_path / "s",
                  OVERLAY_ROOT=tmp_path / "o", FW_WS=tmp_path / "w",
                  RM_SET="greybox", TOUCH=0, SHELL_TOUCH=0, MINT_HUB="", DRYRUN=1)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # `make -n` prints one LOGICAL recipe line as many physical ones; the DRYRUN
    # guard sits at the top of the shell block, so rejoin before judging.
    for logical in logical_lines(proc.stdout):
        stripped = logical.strip().lstrip("@")
        # a bare banner runs nothing; it is allowed to NAME the tool it would
        # have run (that is the point of a plan)
        if stripped.startswith(("#", "echo ", "mkdir ", "test ")):
            continue
        if "vivado" in stripped.lower() or "updatemem" in stripped.lower():
            assert "DRYRUN" in stripped, (
                "DRYRUN=1 plan contains a tool invocation that is not behind the "
                "DRYRUN guard:\n%s" % logical)


def test_mint_is_a_noop_when_everything_is_up_to_date(tmp_path):
    """make's own dependency tracking, not a hand-rolled 'already done' flag."""
    build = tmp_path / "build_n"
    shell_proj = tmp_path / "shell_proj"
    overlay = tmp_path / "overlay"
    ws = tmp_path / "vitis_fw"
    prod = build / "prod"
    prod.mkdir(parents=True)
    shell_proj.mkdir()
    overlay.mkdir()
    (ws / "shell_fw").mkdir(parents=True)

    # written in DEPENDENCY order: make compares mtimes, so an artefact created
    # before the thing it is derived from would (correctly) look stale
    (build / "preflight.stamp").write_text("x")
    # the source-content hash: taken before the shell is built (it is first in
    # mint.json's prerequisite list), so it goes first here too
    (build / "static_canon.json").write_text("x")
    (shell_proj / "shell_static_synth.dcp").write_text("x")
    (prod / "rm_eth_ss_synth.dcp").write_text("x")
    for name in ("static_id.txt", "overlay_inputs.txt",
                 "static_routed_locked.dcp",
                 "config_rm_greybox_routed.dcp", "config_rm_greybox.bit"):
        (prod / name).write_text("x")
    (overlay / "mps3_shell_static_id.c").write_text("x")
    (ws / "shell_fw" / "shell_fw.elf").write_text("x")
    (build / "fw_flags.stamp").write_text(
        "PRODUCT=1 TOUCH=0 SHELL_TOUCH=0 RM_SET=rm_greybox rm_led rm_eth_ss\n")
    (prod / "config_rm_greybox.mmi").write_text("x")
    (prod / "config_rm_greybox_fw.bit").write_text("x")
    (prod / "mint.json").write_text("x")

    proc = make_n("mint", BUILD=build, SHELL_PROJ=shell_proj, OVERLAY_ROOT=overlay,
                  FW_WS=ws, RM_SET="greybox led eth-ss", TOUCH=0, SHELL_TOUCH=0,
                  MINT_HUB="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for tool in ("vivado", "updatemem", "mint_record.py"):
        assert tool not in proc.stdout, (
            "a re-run with nothing changed still plans to run %r:\n%s"
            % (tool, proc.stdout)
        )


def test_legacy_rm_targets_still_resolve(tmp_path):
    """The explicit names people have in their shell history keep working."""
    for goal in ("rm-nanosoc-dcp", "rm-eth-ss-dcp", "rm-nanosoc-multicore-dcp",
                 "rm-socscope-dcp", "rm-nanosoc-upy-dcp",
                 "add-rm-eth-ss", "add-rm-socscope", "add-rm-nanosoc-upy",
                 "add-rm-nanosoc-multicore"):
        proc = make_n(goal, BUILD=tmp_path / "legacy", MINT_HUB="")
        combined = proc.stdout + proc.stderr
        assert "No rule to make target" not in combined, \
            "%s no longer resolves:\n%s" % (goal, combined)


def test_record_reads_the_product_expansion_from_the_firmware_makefile(tmp_path):
    """PRODUCT=1 is defined in ONE place; the record reads it there.

    The fielded flag set spent a year as five literals inside a mint script --
    "a definition that cannot be checked, reused by the firmware tests, or kept
    in step with the Makefile" (firmware/platform/Makefile). Re-typing them into
    mint_record.py would recreate exactly that, one file further out.
    """
    prod = fake_build_tree(tmp_path / "build_x")
    out = tmp_path / "mint.json"
    run_record("record", "--build-dir", prod, "--repo", REPO,
               "--product-makefile", REPO / "firmware" / "platform" / "Makefile",
               "--fw-flags", "TOUCH=0", "-o", out)
    flags = json.loads(out.read_text())["firmware"]["flags"]["value"]
    for name in ("CLCD", "CLCD_KVM", "HWICAP_FIFO", "WINDOWED"):
        assert flags.get(name) == "1", \
            "PRODUCT=1 expansion lost %s: %r" % (name, flags)
    assert flags["PRODUCT"] == "1"
    assert flags["TOUCH"] == "0", \
        "an explicit --fw-flags entry must beat the PRODUCT default"


def test_record_refuses_to_invent_a_flag_set(tmp_path):
    """Negative control: a Makefile with no PRODUCT block is an error, not {}."""
    prod = fake_build_tree(tmp_path / "build_x")
    empty = tmp_path / "NoProduct.mk"
    empty.write_text("CLCD ?= 1\n")
    proc = run_record("record", "--build-dir", prod, "--repo", REPO,
                      "--product-makefile", empty, "-o", tmp_path / "m.json",
                      expect_rc=1)
    assert "will not invent" in (proc.stdout + proc.stderr)


def test_makefile_registration_matches_rm_list(tmp_path):
    """The Makefile's staged-RM set and rm_list.tcl's `prebuilt` set are one set.

    Two lists of the same fact drift; this is the gate that says so. A build
    VARIANT (a `*_scaffold` key: same RM, different bake) is allowed to exist
    only in the Makefile, because it is not a separate RM.
    """
    registered = set(re.findall(r"^RM_SYNTH_TCL_(\w+)\s*:?=",
                                (DFX / "Makefile").read_text(), re.M))
    assert registered, "no RM synth registrations found in fpga/dfx/Makefile"
    out = run_tcl(
        'source %s\n'
        'foreach k [rm_all_names] { puts "$k [rm_field $k synth_mode]" }\n' % RM_LIST_TCL
    ).splitlines()
    modes = dict(line.split() for line in out)
    prebuilt = {k for k, m in modes.items() if m == "prebuilt"}

    missing = prebuilt - registered
    assert not missing, (
        "rm_list.tcl says these RMs need a pre-built checkpoint, but "
        "fpga/dfx/Makefile has no RM_SYNTH_TCL_<key> for them: %s" % sorted(missing))
    extra = {k for k in registered - prebuilt if not k.endswith("_scaffold")}
    assert not extra, (
        "fpga/dfx/Makefile stages these, but rm_list.tcl does not call them "
        "prebuilt: %s" % sorted(extra))


# --------------------------------------------------------------------------
# 6. the consumer: `make overlays` must read the rows build_dfx.tcl now writes
# --------------------------------------------------------------------------
def _overlay_fixture(tmp_path, relative):
    prod = tmp_path / "build" / "prod"
    prod.mkdir(parents=True)
    (tmp_path / "ovl").mkdir()
    (prod / "static_id.txt").write_text("0xDEADBEEF\n")
    pair = "config_rm_led_pblock_rp_dut_partial"
    (prod / (pair + ".bin")).write_bytes(bytes(range(256)) * 16)
    (prod / (pair + "_clear.bin")).write_bytes(bytes(range(256)) * 8)
    p = pair + ".bin" if relative else str(prod / (pair + ".bin"))
    c = pair + "_clear.bin" if relative else str(prod / (pair + "_clear.bin"))
    (prod / "overlay_inputs.txt").write_text(
        "# rm_key rm_name rm_id partial_bin clearing_bin\n"
        "rm_led led 0x0100001E %s %s\n" % (p, c))
    return prod


@pytest.mark.parametrize("relative", [True, False])
def test_overlays_reads_both_row_spellings(tmp_path, relative):
    """The whole point of relativizing rows is that the CONSUMER still works.

    Relative is what build_dfx.tcl writes now. Absolute is what every prod tree
    built before 2026-09-10 holds -- including the fielded one -- and those must
    keep re-keying without a re-mint. Real gen_manifest.py, real payloads, no
    Vivado.
    """
    prod = _overlay_fixture(tmp_path, relative)
    proc = subprocess.run(
        [MAKE, "-C", str(DFX), "overlays",
         "BUILD=%s" % prod.parent, "OVERLAY_ROOT=%s" % (tmp_path / "ovl")],
        capture_output=True, text=True)
    assert proc.returncode == 0, (
        "make overlays failed on %s rows:\n%s\n%s"
        % ("relative" if relative else "absolute", proc.stdout, proc.stderr))
    manifest = json.loads((tmp_path / "ovl" / "led" / "manifest.json").read_text())
    assert manifest["static_id"].lower() == "0xdeadbeef"
    assert (tmp_path / "ovl" / "led" / "led.bin").is_file()


# --------------------------------------------------------------------------
# 7. the documented shape stays true
# --------------------------------------------------------------------------
def test_example_record_verifies():
    """fpga/dfx/mint.json.example is checked by the same gate as a real one.

    An example that would not pass verify is documentation of a format nothing
    produces.
    """
    example = DFX / "mint.json.example"
    assert example.is_file(), "missing %s" % example
    run_record("verify", example)


def test_the_superseded_mint_script_is_gone_and_nothing_owned_points_at_it():
    """The one runbook is docs/BUILD_AND_MINT.md.

    Two mint scripts existed and had diverged by 144 lines, and the shell on the
    board was built by neither. A deleted script that is still cited from a live
    file is the same failure one step removed, so the files this lane owns must
    not send anyone to it.
    """
    assert not (DFX / "qspi_kvm_rekey").exists(), \
        "fpga/dfx/qspi_kvm_rekey/ is back -- the mint lives in fpga/dfx/Makefile"
    runbook = REPO / "docs" / "BUILD_AND_MINT.md"
    assert runbook.is_file(), "docs/BUILD_AND_MINT.md is the replacement and must exist"

    # Naming the old script in a sentence about history is fine and is how the
    # record is kept. Naming its PATH is an instruction, and the path is gone.
    owned = [DFX / "Makefile", DFX / "README.md", DFX / "build_dfx.tcl",
             DFX / "rm_list.tcl"] + sorted((DFX / "tools").glob("*"))
    for path in owned:
        if path.is_dir():
            continue
        lines = path.read_text(errors="ignore").splitlines()
        for i, line in enumerate(lines):
            if "qspi_kvm_rekey" not in line:
                continue
            window = " ".join(lines[i:i + 2]).lower()
            assert "delet" in window, (
                "%s:%d still points a reader at the deleted mint path:\n    %s"
                % (path, i + 1, line.strip()))
