#!/usr/bin/env python3
"""dfx_jobs.py -- DFX stage 4 with the RM configs as N concurrent Vivado processes.

    make -C fpga/dfx mint ... DFX_JOBS=4          (or `make prod DFX_JOBS=4`)

WHY. A mint routes every RM against ONE locked static. Once that static is
routed and locked (the reference config, rm_greybox), each other RM config --
open the locked static, link the RM, place, route, pr_verify against the
reference, write its partial/clearing pair and .ltx -- depends on nothing but
the locked static and the reference routed checkpoint. The one-process flow
still does them one after another: 12 of them are ~5 h of a 13-RM mbv mint.

HOW. The Makefile keeps its stage-4 recipe and swaps only the tool in front of
the Vivado arguments (DFX_JOBS unset or 1: the Vivado binary itself, byte for
byte as before). This script receives that same argument list and runs
build_dfx.tcl in phases (tools/dfx_jobs.tcl):

  1. ref     ONE Vivado (cwd = the prod dir, as today): reference config, lock,
             static_id, the reference's bitstreams + stamps + static .ltx.
  2. workers up to --jobs Vivados at once, one per remaining RM, each in its own
             cwd with its own log: the incremental add path (DFX_ADD_RMS=<rm>,
             the locked static, the reference routed dcp) + DFX_ROW_FILE.
             A failing worker does not stop the others.
  3. finish  ONE short Vivado: all rows -> overlay_inputs.txt -> static_id.txt.

Then <prod>/build.log is ASSEMBLED -- ref log, each worker's log in RM order,
the finish log, a timing/memory table -- so the Makefile's existing gates
(grep ^DFX_*_GATE_FAILED, DFX_BUILD_COMPLETE, the .ltx gates) read one file,
never interleaved.

FAILURE. Any failed worker: the others finish, then this exits 1 naming every
failed config (reason + log). static_id.txt is NOT written, so make's stage-4
target stays missing and re-running the same make command RESUMES: the ref
phase and every finished config are kept (same locked static, same static_id),
only the missing configs run again. A resume is refused -- and the stage starts
over -- when an input changed (build_dfx.tcl and the Tcl it sources, the shell
checkpoint, the Vivado), exactly as make would re-run the one-process stage.

MEMORY. A worker is launched only while MemAvailable >= --mem-per-job-gb
(default 16: the P-mint's mbv configs peaked at 7.6 GB each). With none of ours
running it launches anyway, loudly -- the one-process flow would have too. Each
job's peak is measured (wait4 ru_maxrss of its process tree, and Vivado's own
"Memory (MB): peak") and written to dfx_jobs/summary.json.

Recovery of a one-process stage 4 that died AFTER the lock (static_id.txt
written, overlay_inputs.txt not): `--adopt-locked` makes the ref phase re-derive
static_id from the locked checkpoint instead of re-routing, then runs the rest.

Python 3.8, stdlib only. Tests: fpga/dfx/tools/tests/test_dfx_jobs.py (a fake
Vivado running the real build_dfx.tcl under tclsh).
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import zlib

STATE = "dfx_jobs"
GATE_RE = re.compile(r"^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED.*$", re.M)
PEAK_RE = re.compile(r"Memory \(MB\): peak = ([0-9.]+)")
ERROR_RE = re.compile(r"^ERROR:.*$", re.M)
# The env the phases are steered by. Stripped from the caller's environment so
# a stray export can never turn a phase into something else.
PHASE_ENV = ("DFX_PHASE", "DFX_ADD_RMS", "DFX_ROW_FILE", "DFX_REUSE_LOCKED",
             "DFX_STATIC_ID_FILE", "DFX_REF_ROUTED", "DFX_ADOPT_LOCKED")
# What build_dfx.tcl reads (besides the RM checkpoints): a change to any of
# these re-runs the one-process stage 4 under make, so it voids a resume here.
TCL_INPUTS = ("fpga/dfx/build_dfx.tcl", "fpga/dfx/rm_list.tcl", "fpga/dfx/dfx_floorplan.xdc",
              "fpga/dfx/tools/dfx_jobs.tcl", "fpga/dfx/tools/overlay_inputs.tcl",
              "fpga/dfx/tools/static_stamp.tcl", "fpga/dfx/tools/debug_probes.tcl",
              "fpga/dfx/tools/vivado_version.tcl", "scripts/harness_gates/check_hdpr_reports.py",
              "fpga/shell/boundary.yaml", "fpga/shell/tools/xdc_gate.tcl",
              # read by the reference config only (build_config, first_rm=1)
              "fpga/shell/constraints/mps3_harness_timing.xdc",
              "fpga/shell/constraints/mbv/mbv_timing.xdc")


class JobsError(Exception):
    pass


def say(msg):
    sys.stdout.write("[dfx_jobs %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def crc32_file(path):
    crc = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return "0x%08X" % (crc & 0xFFFFFFFF)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_fp(path):
    """size + mtime: enough to notice a re-staged checkpoint without hashing 50 MB."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def read_record(path):
    """dfx_jobs.tcl's key/value record -> dict (value = rest of the line)."""
    rec = {}
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            rec[parts[0]] = parts[1] if len(parts) > 1 else ""
    return rec


def read_row(path, static_id):
    """-> the 5-cell overlay row, or None (absent, torn, or another static's)."""
    try:
        rec = read_record(path)
    except OSError:
        return None
    if rec.get("static_id") != static_id or "row" not in rec:
        return None
    row = rec["row"].split(" ")
    return row if len(row) == 5 else None


def mem_available_gb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024.0 * 1024.0)
    except OSError:
        pass
    return None


def parse_vivado_argv(argv):
    """The stage-4 recipe's Vivado arguments -> their parts. Exactly the ones
    the Makefile passes; anything else is refused rather than guessed at."""
    out = {"extra": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-tclargs":
            out["tclargs"] = argv[i + 1:]
            break
        if a in ("-mode", "-source", "-journal", "-log"):
            if i + 1 >= len(argv):
                raise JobsError("vivado argument %s has no value" % a)
            out[a[1:]] = argv[i + 1]
            i += 2
            continue
        out["extra"].append(a)
        i += 1
    for k in ("source", "log", "tclargs"):
        if k not in out:
            raise JobsError("the Vivado arguments carry no -%s: %r" % (k, argv))
    if os.path.basename(out["source"]) != "build_dfx.tcl":
        raise JobsError("DFX_JOBS runs build_dfx.tcl only, not %s" % out["source"])
    if len(out["tclargs"]) < 3:
        raise JobsError("build_dfx.tcl needs -tclargs <repo_root> <out_dir> <static_dcp> [...]")
    if out.get("mode", "batch") != "batch":
        raise JobsError("DFX_JOBS needs -mode batch")
    return out


def split_rms(s):
    return [x for x in re.split(r"[,\s]+", s or "") if x]


class Job(object):
    def __init__(self, name, cwd, log, env):
        self.name, self.cwd, self.log, self.env = name, cwd, log, env
        self.proc = None
        self.t0 = self.t1 = None
        self.rc = None
        self.maxrss_kb = 0
        self.ok = False
        self.reason = ""
        self.skipped = False

    def wall(self):
        return (self.t1 or time.time()) - self.t0 if self.t0 else 0.0

    def log_text(self):
        try:
            with open(self.log, errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def log_peak_mb(self):
        peaks = [float(x) for x in PEAK_RE.findall(self.log_text())]
        return max(peaks) if peaks else None

    def summary(self):
        return {"name": self.name, "ok": self.ok, "skipped": self.skipped, "rc": self.rc,
                "reason": self.reason, "wall_s": round(self.wall(), 1),
                "maxrss_gb": round(self.maxrss_kb / (1024.0 * 1024.0), 2),
                "log_peak_mb": self.log_peak_mb(), "log": self.log}


class Runner(object):
    def __init__(self, args, viv):
        self.args = args
        self.viv = viv
        self.tclargs = viv["tclargs"]
        self.repo = os.path.abspath(self.tclargs[0])
        self.out = os.path.abspath(self.tclargs[1])
        self.static_dcp = os.path.abspath(self.tclargs[2])
        self.rm_subset = split_rms(self.tclargs[3]) if len(self.tclargs) > 3 else []
        self.state = os.path.join(self.out, STATE)
        self.final_log = os.path.abspath(viv["log"])
        self.source = os.path.abspath(viv["source"])
        self.jobs = []
        self.static_id = None
        self.reference = None
        self.running = {}
        self.max_concurrency = 0
        self.t_start = time.time()
        base = dict(os.environ)
        for k in PHASE_ENV:
            base.pop(k, None)
        self.base_env = base

    # --- identity of the inputs a resume must not outlive --------------------
    def fingerprint(self):
        fp = {"vivado": os.path.abspath(self.args.vivado), "source": self.source,
              "static_dcp": [self.static_dcp, file_fp(self.static_dcp)],
              "tclargs": self.tclargs[:3] + self.tclargs[4:],
              "shell_cpu": os.environ.get("DFX_SHELL_CPU", ""), "inputs": {}}
        for rel in TCL_INPUTS:
            p = os.path.join(self.repo, rel)
            fp["inputs"][rel] = sha256_file(p) if os.path.isfile(p) else None
        return fp

    def ref_complete(self):
        """-> (static_id, reference, rm_names) of a finished, still-valid ref
        phase, or None."""
        rec_p = os.path.join(self.state, "ref.rec")
        fp_p = os.path.join(self.state, "ref.fp.json")
        if not (os.path.isfile(rec_p) and os.path.isfile(fp_p)):
            return None
        if os.path.exists(os.path.join(self.state, "finish.done")):
            return None
        try:
            rec = read_record(rec_p)
            with open(fp_p) as fh:
                fp = json.load(fh)
        except (OSError, ValueError):
            return None
        if fp != self.fingerprint():
            say("RESUME REFUSED: an input changed since the ref phase (%s) -- starting over" % fp_p)
            return None
        sid, ref = rec.get("static_id"), rec.get("reference_rm")
        locked = os.path.join(self.out, "static_routed_locked.dcp")
        routed = os.path.join(self.out, "config_%s_routed.dcp" % ref)
        if not (sid and ref and os.path.isfile(locked) and os.path.isfile(routed)):
            return None
        if crc32_file(locked) != sid:
            say("RESUME REFUSED: static_routed_locked.dcp no longer has CRC %s -- starting over" % sid)
            return None
        if read_row(os.path.join(self.state, ref + ".row"), sid) is None:
            return None
        return sid, ref, split_rms(rec.get("rm_names", ""))

    # --- running one Vivado ---------------------------------------------------
    def vivado_cmd(self, log, journal):
        cmd = [self.args.vivado] + self.viv["extra"] + ["-mode", self.viv.get("mode", "batch"),
                                                       "-source", self.source,
                                                       "-journal", journal, "-log", log]
        return cmd + ["-tclargs"] + self.tclargs

    def start(self, job):
        os.makedirs(job.cwd, exist_ok=True)
        journal = os.path.splitext(job.log)[0] + ".jou"
        stdout = open(os.path.join(os.path.dirname(job.log), "stdout.txt"), "w")
        job.t0 = time.time()
        try:
            job.proc = subprocess.Popen(self.vivado_cmd(job.log, journal), cwd=job.cwd, env=job.env,
                                        stdout=stdout, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        except OSError as e:
            job.t1, job.rc, job.reason = time.time(), 127, "cannot run %s: %s" % (self.args.vivado, e)
            return False
        finally:
            stdout.close()
        self.running[job.proc.pid] = job
        self.max_concurrency = max(self.max_concurrency, len(self.running))
        return True

    def reap(self, block):
        """Collect every finished job; -> the list reaped."""
        done = []
        for pid, job in list(self.running.items()):
            try:
                wpid, status, ru = os.wait4(pid, 0 if block and not done else os.WNOHANG)
            except ChildProcessError:
                wpid, status, ru = pid, 0, None
            if wpid == 0:
                continue
            job.t1 = time.time()
            job.proc.returncode = job.rc = (os.WEXITSTATUS(status) if os.WIFEXITED(status)
                                            else -os.WTERMSIG(status) if os.WIFSIGNALED(status) else -1)
            job.maxrss_kb = ru.ru_maxrss if ru is not None else 0
            del self.running[pid]
            done.append(job)
        return done

    def run_one(self, job):
        if self.start(job):
            while job.proc.pid in self.running:
                self.reap(block=True)
        return job

    def kill_all(self, signum=None, frame=None):
        for pid in list(self.running):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 10
        while self.running and time.time() < deadline:
            self.reap(block=False)
            time.sleep(0.2)
        for pid in list(self.running):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        if signum is not None:
            say("interrupted (signal %d): every running Vivado was stopped; re-run to resume" % signum)
            self.assemble_log()
            sys.exit(128 + signum)

    @staticmethod
    def judge(job, marker):
        """ok iff exit 0, the phase's COMPLETE marker at the start of a line, and
        no gate failure. Vivado exits 0 after some Tcl errors: the marker is the
        proof, never the return code alone."""
        if job.proc is None:
            return False                      # never started; start() left the reason
        text = job.log_text()
        gate = GATE_RE.search(text)
        if job.rc not in (0, None) and job.rc < 0:
            job.reason = "killed by signal %d" % -job.rc
        elif gate:
            job.reason = gate.group(0).strip()
        elif job.rc != 0:
            err = ERROR_RE.search(text)
            job.reason = "exit %d%s" % (job.rc, (": " + err.group(0).strip()) if err else "")
        elif not re.search(r"^%s" % re.escape(marker), text, re.M):
            err = ERROR_RE.search(text)
            job.reason = "never printed %s%s" % (marker, (": " + err.group(0).strip()) if err else "")
        else:
            job.ok = True
        return job.ok

    # --- the three phases -----------------------------------------------------
    def phase_ref(self):
        job = Job("ref:" + (self.rm_subset[0] if self.rm_subset else "reference"), self.out,
                  os.path.join(self.state, "ref", "build.log"), dict(self.base_env, DFX_PHASE="ref"))
        if self.args.adopt_locked:
            job.env["DFX_ADOPT_LOCKED"] = "1"
        os.makedirs(os.path.dirname(job.log), exist_ok=True)
        self.jobs.append(job)
        say("ref phase%s: %s (log %s)" % (" (ADOPT the locked static)" if self.args.adopt_locked else "",
                                         "reference config -> lock -> static_id -> reference bitstreams",
                                         job.log))
        self.run_one(job)
        rec_p = os.path.join(self.state, "ref.rec")
        if self.judge(job, "DFX_PHASE_REF_COMPLETE") and not os.path.isfile(rec_p):
            job.ok, job.reason = False, "no %s" % rec_p
        if not job.ok:
            raise JobsError("the REFERENCE config failed (%s) -- nothing else can run without the "
                            "locked static. Log: %s" % (job.reason, job.log))
        rec = read_record(rec_p)
        with open(os.path.join(self.state, "ref.fp.json"), "w") as fh:
            json.dump(self.fingerprint(), fh, indent=1, sort_keys=True)
        say("ref phase OK in %s: static_id %s (%s)" % (hms(job.wall()), rec["static_id"],
                                                     "adopted" if self.args.adopt_locked else "minted"))
        return rec["static_id"], rec["reference_rm"], split_rms(rec.get("rm_names", ""))

    def worker_job(self, rm):
        d = os.path.join(self.state, rm)
        env = dict(self.base_env,
                   DFX_ADD_RMS=rm,
                   DFX_REUSE_LOCKED=os.path.join(self.out, "static_routed_locked.dcp"),
                   DFX_STATIC_ID_FILE=os.path.join(self.state, "static_id.txt"),
                   DFX_REF_ROUTED=os.path.join(self.out, "config_%s_routed.dcp" % self.reference),
                   DFX_ROW_FILE=os.path.join(self.state, rm + ".row"))
        return Job(rm, d, os.path.join(d, "build.log"), env)

    def worker_done(self, rm):
        """A finished worker from an earlier run of THIS lock: a valid row, its
        pair on disk, and the RM checkpoint it linked unchanged since."""
        row = read_row(os.path.join(self.state, rm + ".row"), self.static_id)
        if row is None:
            return False
        for rel in row[3:5]:
            b = os.path.join(self.out, rel)
            if not (os.path.isfile(b) and os.path.isfile(os.path.splitext(b)[0] + ".bit")):
                return False
        try:
            with open(os.path.join(self.state, rm + ".launch.json")) as fh:
                launched = json.load(fh)
        except (OSError, ValueError):
            return False
        synth = os.path.join(self.out, rm + "_synth.dcp")
        return launched.get("synth_dcp") is None or launched.get("synth_dcp") == file_fp(synth)

    def phase_workers(self, rms):
        todo = []
        for rm in rms:
            job = self.worker_job(rm)
            self.jobs.append(job)
            if self.worker_done(rm):
                job.ok, job.skipped = True, True
                say("%s: already built against %s in an earlier run -- kept (log %s)"
                    % (rm, self.static_id, job.log))
            else:
                todo.append(job)
        # Longest first (the prebuilt OOC checkpoint's size is the cost proxy;
        # inline RMs are the small demo RMs): the makespan of a greedy pool is
        # set by the job started last.
        order = {rm: i for i, rm in enumerate(rms)}
        todo.sort(key=lambda j: (-(file_fp(os.path.join(self.out, j.name + "_synth.dcp")) or [0])[0],
                                 order[j.name]))
        say("%d RM config(s) to route, %d kept; up to %d at once (order: %s)"
            % (len(todo), len(rms) - len(todo), self.args.jobs, " ".join(j.name for j in todo)))
        warned = False
        while todo or self.running:
            while todo and len(self.running) < self.args.jobs:
                avail = mem_available_gb()
                if (avail is not None and avail < self.args.mem_per_job_gb and self.running):
                    if not warned:
                        say("memory: %.1f GB available < %.1f GB per job -- holding %s until one finishes"
                            % (avail, self.args.mem_per_job_gb, todo[0].name))
                        warned = True
                    break
                if avail is not None and avail < self.args.mem_per_job_gb:
                    say("WARNING: memory: %.1f GB available < %.1f GB per job, and nothing of ours is "
                        "running -- starting %s anyway (the one-process flow would have)"
                        % (avail, self.args.mem_per_job_gb, todo[0].name))
                warned = False
                job = todo.pop(0)
                with open(os.path.join(self.state, job.name + ".launch.json"), "w") as fh:
                    json.dump({"synth_dcp": file_fp(os.path.join(self.out, job.name + "_synth.dcp")),
                               "static_id": self.static_id, "started": time.time()}, fh)
                if not self.start(job):
                    say("FAILED %s -- %s" % (job.name, job.reason))
                    continue
                say("START %s (pid %d, %d running, log %s)" % (job.name, job.proc.pid,
                                                              len(self.running), job.log))
            for job in self.reap(block=False):
                row_p = os.path.join(self.state, job.name + ".row")
                if self.judge(job, "DFX_ADD_COMPLETE") and read_row(row_p, self.static_id) is None:
                    job.ok, job.reason = False, "no valid row in %s" % row_p
                s = job.summary()
                say("%s %s in %s (peak %.2f GB rss%s)%s" % (
                    "DONE" if job.ok else "FAILED", job.name, hms(job.wall()), s["maxrss_gb"],
                    ", Vivado %.0f MB" % s["log_peak_mb"] if s["log_peak_mb"] else "",
                    "" if job.ok else " -- " + job.reason))
            if self.running:
                time.sleep(self.args.poll)

    def phase_finish(self):
        job = Job("finish", os.path.join(self.state, "finish"),
                  os.path.join(self.state, "finish", "build.log"),
                  dict(self.base_env, DFX_PHASE="finish"))
        self.jobs.append(job)
        say("finish: every row -> overlay_inputs.txt -> static_id.txt (log %s)" % job.log)
        self.run_one(job)
        sid_p = os.path.join(self.out, "static_id.txt")
        if self.judge(job, "DFX_BUILD_COMPLETE"):
            try:
                got = open(sid_p).read().strip()
            except OSError:
                got = None
            if got != self.static_id or not os.path.isfile(os.path.join(self.out, "overlay_inputs.txt")):
                job.ok, job.reason = False, "static_id.txt=%r / overlay_inputs.txt not as minted" % got
        if not job.ok:
            raise JobsError("the finish phase failed (%s). Log: %s" % (job.reason, job.log))
        open(os.path.join(self.state, "finish.done"), "w").write(self.static_id + "\n")

    # --- the one log ------------------------------------------------------------
    def assemble_log(self):
        parts = []
        for job in self.jobs:
            if not os.path.isfile(job.log):
                continue
            if parts:
                parts.append("\n#### DFX_JOBS: -------- %s -- %s --------\n" % (job.name, job.log))
            parts.append(job.log_text())
        if not parts:
            return
        parts.append("\n#### DFX_JOBS summary (dfx_jobs/summary.json)\n")
        for job in self.jobs:
            s = job.summary()
            parts.append("#### %-24s %-7s %9s  rss %6.2f GB  vivado-peak %8s MB%s\n" % (
                s["name"], "kept" if s["skipped"] else ("ok" if s["ok"] else "FAILED"),
                hms(s["wall_s"]), s["maxrss_gb"],
                "-" if s["log_peak_mb"] is None else "%.0f" % s["log_peak_mb"],
                "" if s["ok"] else "  " + s["reason"]))
        tmp = self.final_log + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("".join(parts))
        os.replace(tmp, self.final_log)

    def write_summary(self, ok, failed):
        s = {"ok": ok, "static_id": self.static_id, "jobs": self.args.jobs,
             "mem_per_job_gb": self.args.mem_per_job_gb, "max_concurrency": self.max_concurrency,
             "wall_s": round(time.time() - self.t_start, 1), "failed": failed,
             "phases": [j.summary() for j in self.jobs]}
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "summary.json"), "w") as fh:
            json.dump(s, fh, indent=1)

    # --- the whole stage --------------------------------------------------------
    def run(self):
        os.makedirs(self.out, exist_ok=True)
        # Every Vivado runs in its own session (so a stop can take its whole
        # process tree), which also takes it out of make's process group: a ^C
        # at make reaches THIS process only. It must pass it on, from the first
        # job -- the ref phase included.
        signal.signal(signal.SIGTERM, self.kill_all)
        signal.signal(signal.SIGINT, self.kill_all)
        signal.signal(signal.SIGHUP, self.kill_all)
        resumed = self.ref_complete()
        if resumed:
            self.static_id, self.reference, rec_rms = resumed
            sid_p = os.path.join(self.out, "static_id.txt")
            if os.path.isfile(sid_p) and open(sid_p).read().strip() != self.static_id:
                raise JobsError("%s says %s but the locked static is %s -- refusing to resume "
                                "into a tree that disagrees with itself"
                                % (sid_p, open(sid_p).read().strip(), self.static_id))
            say("RESUME: the ref phase is done (static_id %s, locked static unchanged) -- kept"
                % self.static_id)
        else:
            self.fresh_start()
            self.static_id, self.reference, rec_rms = self.phase_ref()
        rms = self.rm_subset or rec_rms
        if not rms or rms[0] != self.reference:
            raise JobsError("the RM set %r does not start with the reference %s" % (rms, self.reference))
        self.phase_workers(rms[1:])
        failed = [j for j in self.jobs if not j.ok]
        if not failed and crc32_file(os.path.join(self.out, "static_routed_locked.dcp")) != self.static_id:
            raise JobsError("static_routed_locked.dcp changed while the workers ran -- no row can be trusted")
        if failed:
            for j in failed:
                say("DFX_JOBS_FAILED rm=%s :: %s (log %s)" % (j.name, j.reason, j.log))
            raise JobsError(
                "%d of %d RM config(s) FAILED: %s. The other configs are done and KEPT, against the "
                "SAME locked static %s. Fix the cause and re-run the same make command: it resumes, "
                "and only these configs run again. static_id.txt was NOT written, so no later stage "
                "can mistake this tree for a finished one."
                % (len(failed), len(rms) - 1, " ".join(j.name for j in failed), self.static_id))
        self.phase_finish()
        self.write_summary(True, [])
        self.assemble_log()
        say("STAGE 4 DONE in %s: static_id %s, %d configs, at most %d Vivados at once; log %s"
            % (hms(time.time() - self.t_start), self.static_id, len(rms), self.max_concurrency,
               self.final_log))

    def fresh_start(self):
        """A stage 4 that re-runs from the reference re-mints the static: every
        file the LAST lock left that names it goes, before anything is built --
        never after, so a failure can not leave an old id beside a new lock."""
        import shutil
        if os.path.isdir(self.state):
            shutil.rmtree(self.state)
        os.makedirs(self.state)
        if os.path.isfile(self.final_log):
            os.replace(self.final_log, os.path.splitext(self.final_log)[0] + "_%d.backup.log" % os.getpid())
        if self.args.adopt_locked:
            return      # the ref phase checks static_id.txt against the lock it adopts
        for name in ("static_id.txt", "overlay_inputs.txt"):
            p = os.path.join(self.out, name)
            if os.path.exists(p):
                os.remove(p)
                say("fresh stage 4: removed the previous %s (it names the static this run replaces)" % p)


def hms(sec):
    sec = int(sec)
    return "%dh%02dm%02ds" % (sec // 3600, sec % 3600 // 60, sec % 60) if sec >= 3600 \
        else "%dm%02ds" % (sec // 60, sec % 60)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run stage 4 in phases; everything after -- is the Vivado argument list")
    r.add_argument("--jobs", type=int, required=True, help="RM configs routed at once (>= 1)")
    r.add_argument("--vivado", required=True, help="the Vivado binary the one-process flow would run")
    r.add_argument("--mem-per-job-gb", type=float, default=16.0,
                   help="launch a worker only while MemAvailable >= this (default 16)")
    r.add_argument("--poll", type=float, default=5.0, help=argparse.SUPPRESS)
    r.add_argument("--adopt-locked", action="store_true",
                   help="the static is already locked in the prod dir: re-derive static_id, do not re-route")
    r.add_argument("vivado_args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 2
    va = a.vivado_args[1:] if a.vivado_args[:1] == ["--"] else a.vivado_args
    try:
        if a.jobs < 1:
            raise JobsError("--jobs must be >= 1")
        for k in ("DFX_ADD_RMS", "DFX_PHASE", "DFX_ROW_FILE"):
            if os.environ.get(k):
                raise JobsError("%s is set in the environment (%s): that would make stage 4 something "
                                "other than a mint. Unset it." % (k, os.environ[k]))
        runner = Runner(a, parse_vivado_argv(va))
        try:
            runner.run()
        except JobsError:
            runner.write_summary(False, [j.name for j in runner.jobs if not j.ok])
            runner.assemble_log()
            raise
    except JobsError as e:
        sys.stderr.write("dfx_jobs: error: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
