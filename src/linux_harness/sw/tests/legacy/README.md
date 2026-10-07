# Retired Linux-harness tests (2026-09-23)

Nothing in this directory runs in any gate — not `make check`, not `make check-ci`,
not `make check-linux`, not CI. That is deliberate, and this file says why, so the
absence is a decision you can read rather than a skip you have to discover.

They were retired by the Linux harness plan
(`docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md`, decisions DL2 and DL6), in the HOST
lane of Wave L1. The code they test stays in the tree until the deletion PR (~10-26), so
they are kept runnable by hand.

## `v07_daemons/` — the v0.7 hand-ported daemon wire suites

| File | What it pinned |
|---|---|
| `run_wire_compat_host.sh` (*) + `wire_compat_test.py` + `wire_fixtures.json` | `mps3-ctrld`/`mps3-pushd` (net-protocol **v0.7**) built host-cc and driven over sockets. Was `make check` / `check-ci` stage 5 until 2026-09-23 |
| `golden_wire/` | the M2b golden-fixture suite (`wire_golden.json`, `replay.py`) — never in a gate |
| `aux_services_test.py`, `configd_tftp_test.py` | the aux daemons (xvcd/swdd/uartbrd) and `mps3-configd` — never in a gate |
| `qemu_service/` | the M3 swap service (`mps3-ctrld` + `mps3-pushd` + `mps3_dfx.ko` mock) inside a QEMU rv32 guest — never in a gate |
| `m6_compat/` | the M6 compat judge: fpgahub/pyverify clients + `wire_compat_test.py` against the v0.7 services in QEMU — never in a gate |

**Why retired.** They pin the protocol the hand-ported daemons spoke, which is not the
protocol any more: `{"op":"stats"}` answering `unknown op` (fixture
`stats-probe-unknown-op`), a 14-key `diag`, no `version`/`dutrx`/`log`/`touch_cal`/`reboot`.
Firmware is at v0.11. Under DL6 the daemons are replaced by `mps3-harnessd` — the
firmware's own service modules compiled for Linux — so a gate that holds Linux to v0.7
would fail the right implementation and pass the wrong one.

**What replaced it.** One conformance suite, several implementations:
`tests/firmware_logic/test_fakeshell_conformance.py` runs the same case table against
`ctrl_echo` (bare metal), a FakeShell over real sockets, and `mps3-harnessd`'s host build,
and `make check-linux` requires the harnessd run. See
`docs/planning/linux_lanes/HOST_CONTRACT.md` §1.

(*) `run_wire_compat_host.sh` still sits at `src/linux_harness/sw/tests/` until the HOST
landing commit: `docs/STATUS.md` row 111 cites that path, and the row is rewritten in the
same commit that moves the file (`docs/planning/linux_lanes/STATUS_ROWS_DRAFT.md`), so the
citation gate never sees a dead path. No gate runs it in either place.

**Running them by hand** (against the v0.7 daemons in `../../daemons/`):

```
src/linux_harness/sw/tests/run_wire_compat_host.sh          # 37/37 at retirement
# after the HOST landing: src/linux_harness/sw/tests/legacy/v07_daemons/run_wire_compat_host.sh
```

**The QEMU harnesses (`qemu_service/`, `m6_compat/`) no longer run**, and were retired
rather than left to skip: they boot the v0.7 image artefacts (`rootfs.ext2`,
`Image_nowfi`, `mps3_dfx.ko`, the v0.7 daemons), which the new image build no longer
produces (`docs/planning/linux_lanes/IMAGE_CONTRACT.md`). Their job is done by
`mps3-harnessd`'s host tests (`make check-linux`) and IMAGE's four-boot QEMU proof,
`src/linux_harness/sw/boot_qemu_harness.sh`. Their relative paths were fixed for the new
depth, so they still describe exactly what they ran.

## `linux_fork_boundary/` — the fork BD's RP boundary gate

`test_linux_fork_boundary.py` diffed the Linux fork's `shell_linux_bd.tcl` /
`shell_linux_top.sv` against `fpga/shell/boundary.yaml`. Under DL2 there is no fork: the
MicroBlaze V is a CPU variant of the ONE shell BD (`fpga/shell/bd/cpu_mbv.tcl`), so every
block and the whole RP boundary are shared by construction and drift is impossible. The
seam is gated by SHELL's `tests/shell_cpu_seam/` (`make check-linux-seam`).

At retirement it already failed one case (`test_dts_jtagbb_node_matches_the_bd`) because
IMAGE had begun generating `shell_linux.dts` from the regmap — exactly the drift a
retired fork accumulates. It is kept only as a record of what the fork was held to.
