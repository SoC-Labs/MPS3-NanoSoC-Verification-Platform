# The boot rate — measuring the MCC-config-from-SD lottery

The MPS3's MCC configures the KU115 from the config SD at every power-on. The
**same image** comes up lit on some power-ups and dark on others. This document
is how that gets a number instead of a folk memory, and how a change to the SD
config gets a **before and an after**.

> Home for the *measurement*. [`UNATTENDED_BOOT_PLAN.md`](UNATTENDED_BOOT_PLAN.md)
> is the historical route evaluation that named this as step 0 and is explicitly
> frozen ("kept for provenance; do not update"), so the method lives here.
> [`FIELDED_SHELL.md`](FIELDED_SHELL.md) remains the only authority for what is
> on the board.

## 1. What we actually know today

* Every plan in the tree says "about 1 time in 4". All of them quote **one**
  internal note recording **four** controlled attempts: one clean, two dark, one
  partial. Four samples. The 95% Wilson interval on 1/4 is **0.05 – 0.70** — it
  does not even exclude "usually fine".
* The MCC's `LOG.TXT` is **byte-identical on a good boot and a dark one** (the
  SMSC9220 / SCC / `mbb_v141` / `images.txt` complaints are benign and always
  present). The failure is at the **unlogged release-to-RUN step**, so no amount
  of log reading will find it. The log is still captured per iteration — as a
  `sha256`, so that "identical every time" stops being an assumption.
* **`--method mcc` works on `tty_00` (SUPERSEDED no-op theory).**
  - The earlier "proven no-op" was REBOOT sent to `tty_01`, the application's UART.
  - A paced REBOOT on the MCC's own console, `tty_00`, reloads the FPGA from the SD. W1 did
    this hands-free on 2026-09-24, when `0x72BB0A36` was fielded.
  - `pyverify boot-rate --method mcc` uses it:
    - it accepts fpgahub's reset only if the hub's reply names `tty_00`;
    - otherwise it falls back to its own paced write: a bare CR, then R-E-B-O-O-T at 100 ms per
      character;
    - it refuses if another process is reading `tty_00` or the `Cmd>` prompt doesn't come back.
  - Linux boots use a 180 s deadline, and each boot is confirmed from the stage0 status block.
* `--method msd` (MPS3 TRM 100765 §3.2: a `reboot.txt` on the MCC's USB
  mass-storage with `USB_REMOTE: TRUE`) is wired in the hub config but **has
  never been proven to reboot this board**. Proving it is W0 step 3, below.
* The only other reconfigure is a **physical power-cycle**.

## 2. The tool

```
python3 -m pyverify.cli boot-rate --host <board-ip> --holder <you> \
        --n 10 --method msd --deadline 90 --out run.json [--variant NAME]
python3 -m pyverify.cli boot-rate compare before.json after.json
python3 -m pyverify.cli boot-rate schema            # the versioned JSON schema
```

Per iteration it records `{iteration, t_reset, t_first_ping, time_to_ping_s,
outcome, static_id, version, mcc_log_sha, witness, notes}`; the summary carries
the rate, its interval, and the exact configuration under test.
(`host/pyverify/pyverify/bootrate.py`.)

### The four outcomes

| outcome | means |
|---|---|
| `up` | ping answered with a `shell_id` inside the deadline. |
| `dark` | the deadline passed with **nothing** answering. **This is the measurement** — recorded as data, never as a failed run. |
| `timeout` | something was there and never converged: the control channel accepted a connection but reported no `shell_id`, or the optional JTAG witness says the FPGA **is** configured. Configured+inert is a hung image, a different defect, and folding it into `dark` would poison the rate. |
| `hub-error` | the reboot never happened — the reset surface refused, **or** it reported ok and the shell went on answering across it. Excluded from the rate's denominator (`rate = up / (n - hub_error)`) and reported separately. |

### What it refuses, and why

* **No lease held by you** → refuses. `lease preflight` answers *yes* for an
  unheld board, which is right for a read-only probe and wrong for something
  that power-cycles a shared board ten times. `--holder` (or
  `MPS3_LEASE_HOLDER`) is **required**: the per-PID default holder the other
  verbs fall back to could never match a lease another process took.
* **`--method mcc`** → refuses if fpgahub's reply names a tty other than `tty_00` and the paced fallback can't run: a second reader, no `Cmd>` prompt, or no `fpga` group.
* **An unknown `--variant`** → refuses (exit 2). A record naming a variant that
  does not exist is a claim about a card nobody can check.

### The clock, and the down edge

The MSD reboot fires when the host's mount is released, so `reset` returns
*before* the board goes down — and the pre-reboot shell will happily answer a
ping for several seconds afterwards. So each iteration first waits for the shell
to **stop** answering (`--down-deadline`, default 30 s) and only then starts
counting. A shell that never stops answering is recorded as `hub-error` with the
note that the reset rebooted nothing. That is also the sharpest test available
for whether `--method msd` works at all (§4, W0 step 3).

`--no-await-down` waives it; the record then carries a note saying the first
answer may be the pre-reboot shell.

### The optional JTAG witness

DONE/USERCODE over JTAG is stronger evidence than a ping — a dark board and a
configured-but-hung board look identical from the network — but it needs `xsdb`
on the hub and the JTAG cable the swap path also wants. So it is a **hook, off
by default**:

```
--witness-cmd '<command printing {"configured": true, "usercode": "0x…"}>'
```

With no witness, `dark` means "nothing answered within the deadline", and every
dark record says so in its `notes`. Nothing is inferred that was not observed.

## 3. Reading the number honestly

The summary carries a 95% **Wilson** interval beside every rate, and `compare`
decides with a two-sided **Fisher exact** test (α = 0.05), not with interval
overlap. Both matter:

* At N=10, 7/10 is **0.40 – 0.89**. One campaign pins down very little.
* Overlapping intervals are *not* a test. 3/10 → 9/10 has overlapping intervals
  (0.11–0.60 vs 0.60–0.98) and an exact **p = 0.020**. A tool that read overlap
  as "no difference" would throw away a genuine fix, so `compare` reports both
  and says which one decided.

**What N=10 per arm can and cannot see** (exact two-sided p < 0.05):

| before | smallest "after" that is significant at N=10 |
|---|---|
| 0/10 | 5/10 |
| 1/10 | 7/10 |
| 2/10 | 8/10 |
| 3/10 | 9/10 |
| 4/10 | 10/10 |
| ≥6/10 | **nothing** — no N=10 result can demonstrate an improvement |

So the audit's N=10 is adequate **only if** the baseline really is as bad as the
folklore says and the fix is dramatic. If the baseline campaign comes back at
6/10 or better, stop and re-plan the sample size before running the variants:
N=20 per arm gets 5/20 → 15/20 to p = 0.004 and 6/20 → 14/20 to p = 0.026. Run
the baseline first, then decide — that is the one ordering decision this
document exists to force.

## 4. The board window (W0)

Take the lease first, and keep it for the whole window:

```bash
export MPS3_HUB=<hub-host>                     # no site host is baked in
HOLDER=claude-bootrate
TOKEN=$(python3 -m pyverify.cli lease acquire --holder "$HOLDER")
python3 -m pyverify.cli lease status
```

### W0 step 3 — prove `--method msd` actually reboots the board

One iteration is the whole proof: if `msd` is another no-op, the shell answers
straight across it and the run records `hub-error` with "the shell NEVER stopped
answering".

```bash
# dry run first: prints the plan, contacts nothing
python3 -m pyverify.cli boot-rate --host <board-ip> --holder "$HOLDER" \
        --n 1 --method msd --deadline 90 --dry-run

# the real single shot
python3 -m pyverify.cli boot-rate --host <board-ip> --holder "$HOLDER" \
        --token "$TOKEN" --n 1 --method msd --deadline 90 \
        --out msd_proof.json
```

Read `msd_proof.json`:

* `outcome: "up"` with a `down edge seen after …s` note → **msd reboots the
  board.** Proceed to step 4.
* `outcome: "hub-error"` with "the shell NEVER stopped answering" → **msd is a
  no-op too.** Stop: every remaining reconfigure is a physical power-cycle, and
  the campaign becomes a human-in-the-loop procedure (the tool still records it
  — drive it with `--n 1` per power-cycle and merge the records by hand).
* `outcome: "dark"` → it rebooted and did not come back. That is a *data point*,
  not a failure of the method; run step 4 and let the count decide.

### W0 step 4 — the baseline campaign (the number that does not exist yet)

The SD must already be carrying the configuration you are measuring — this tool
measures the card in the board, it never writes one.

```bash
python3 -m pyverify.cli boot-rate --host <board-ip> --holder "$HOLDER" \
        --token "$TOKEN" --n 10 --method msd --deadline 90 --gap 15 \
        --mcc-log <path-to-LOG.TXT-on-the-mounted-config-SD> \
        --note 'W0 step 4 baseline: default bundle, no variant' \
        --out baseline.json
```

Budget: 10 × (90 s deadline + 15 s gap) ≈ **18 minutes** worst case, less when
boots come up. `--mcc-log` is optional and site-specific — omit it and
`mcc_log_sha` is `null` (the record says why). Add `--token` and the lease is
heartbeaten each iteration, so a long campaign cannot outlive a short TTL.

Then, and only then, one variable at a time (§5), and:

```bash
python3 -m pyverify.cli boot-rate compare baseline.json eth_smb0.json
```

## 5. The experiment variants

The variables the audit named are SD **config-file keys**, so they are
directories, not hand edits:

```
fpga/mps3_sd/variants/<NAME>/
    variant.txt      DESCRIPTION: … + why this is a suspect
    config.txt       KEY: VALUE overrides applied to templates/config.txt
    nanosoc.txt      … and/or to templates/nanosoc.txt
```

| variant | changes | why |
|---|---|---|
| `ETH_SMB0` | `nanosoc.txt`: `FPGA_SMB`, `FPGA_LAN` → `FALSE` | the app-note LAN9220 SMB sideband. `templates/nanosoc.txt` already documents "if the board fails to program with these TRUE (MCC hangs on SMB), set both to FALSE" — a named suspect for a config step that sometimes doesn't finish. Equivalent to the pre-existing `ETH_SMB=0` env knob, and a test keeps the two in step. |
| `MB_SMB0` | `config.txt`: `FPGA_SMB` → `FALSE` | the **motherboard-level** SMB flag at the SD root. `FPGA_SMB` exists in *both* files and the audit's "ETH_SMB=0 in config.txt" is ambiguous between them, so both are shipped separately. Run one at a time. |
| `AUTORUNDELAY10` | `config.txt`: `AUTORUNDELAY` → `10` | the MCC's pre-autorun key-press window (3 s today). The dark boots are at the release-to-RUN step; more slack before the MCC commits is a one-key, zero-hardware question. |

Nothing in the tree documents any other MCC key that plausibly bears on this, so
there are no further variants — a suspect nobody can cite is a guess, and the
right move is a new directory with its rationale in `variant.txt`, not a
parameter invented at the command line. The MCC BIOS (`mbb_v141.ebf`) revision
is the audit's third variable and is **out of scope here**: it is a human step
with a physical card.

Build a card that carries one:

```bash
fpga/mps3_sd/assemble_sd.sh C <shell.bit> --variant ETH_SMB0
#   -> fpga/mps3_sd/bundle/HBI0309C/ ; copy onto the SD (see fpga/mps3_sd/README.md)
#   or write it through the hub: pyverify sd write (one write, wait, verify)
```

With no `--variant` the bundle is **byte-identical** to the templates — proven,
not asserted, in `host/pyverify/tests/test_bootrate.py`. A variant may only
override keys that already exist in the template; a key that does not is refused
loudly, because an MPS3 config file accepts almost anything silently and fails
at the I/O pads with no message. Each patched file carries a `;VARIANT: …`
banner, so a card found in a board can say which experiment it is.

Then name it in the run — the harness reads the keys back out of that same
directory into the record, so the record cannot claim a configuration the
directory does not describe:

```bash
python3 -m pyverify.cli boot-rate … --variant ETH_SMB0 --out eth_smb0.json
```

## 6. The run record

Versioned: `"schema": "mps3.boot-rate/1"`. A record of another version is
**refused**, not half-read — these files are only worth keeping if a run from
months ago still means what it said. `boot-rate schema` prints the full JSON
Schema; `compare` validates both sides before it will say anything.

`compare` names **every** campaign field that changed (`variant`, `method`,
`deadline_s`, `await_down`, `n`, and each variant key), and says so out loud
when more than one moved — an experiment that changed two things at once is
still worth printing, but it must not be read as one. Two campaigns with no
recorded difference are labelled a **repeatability pair**, not a before/after.

## 7. What this cannot tell you

* **Dark vs slow, without a witness.** Nothing distinguishes "never configured"
  from "would have come up at 91 s" except DONE/USERCODE. Pick the deadline once
  and keep it; the record carries it so a later run can be compared like for
  like.
* **Why.** This counts; it does not explain. If the rate is real and no SD key
  moves it, the next instrument is the MCC console watched *during* the
  configuration attempt, and after that it is an MCC-firmware question rather
  than one this repo can answer.
* **Anything about the DUT.** `up` means the *shell* answered. The RM is not
  loaded by a power-on.
