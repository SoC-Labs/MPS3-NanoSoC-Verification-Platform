# Contributing

Thanks for your interest in the MPS3 nanoSoC verification platform. This guide
covers the few conventions that keep the tree reviewable and the gates honest.
You do **not** need an FPGA (or any Xilinx tools) to contribute to most of it —
the whole logic layer is provable on a normal Linux host.

## The one gate: `make check`

Every change must keep the board-free gate green:

```
make check-ci     # the board-free stages CI runs
make check        # the fuller 8-stage local gate
```

- `check-ci` needs only Python, `tclsh` and a C compiler. No simulator, no
  Vivado, no board. It already runs the **RM-boundary pin-check** and the
  **manifest-half overlay gates** (static_id lockstep, clearing fit, fielded-shell
  claims) — those are not "local only".
- `check` adds what a hosted runner cannot do: Verilator lint, the cocotb benches
  (needs a simulator), and the overlay **CRC/length round-trip**, which reads the
  gitignored `.bin` payloads and so self-skips on a fresh clone (regenerate with
  `make -C fpga/dfx overlays`).
- The exact stage-by-stage split, and the parity test that stops CI drifting
  weaker than a local `make check`, are in [docs/CI.md](docs/CI.md).

If a stage cannot run because a tool or the board is missing, it must **skip
loudly** — print what it skipped and why, and never report a silent pass. A gate
that prints a failure and exits 0 is worse than no gate.

## Repo conventions

- **Only `master` is public.** It is the branch mirrored to the public remote.
  Do feature work on a branch and merge into `master` once the gate is green.
- **Never `git add -A`.** Stage files explicitly. The tree carries multiple
  work-in-progress tracks at once, and a blanket add sweeps up scratch,
  local-only settings, or half-finished work.
- **Contracts are frozen interfaces.** Files under [docs/contracts/](docs/contracts/)
  define boundaries (net protocol, shell regmap, partition pins). Changing one is
  a deliberate, reviewed act — update the contract and the conformance gate
  together, don't drift the implementation away from it.
- **Keep it board-free where you can.** New logic should come with a host-runnable
  test (`firmware/test/` for C, `pytest` for Python) so it's covered by the gate
  without hardware.

## Adding a DUT (an RM)

Start from the skeleton, not from another RM: [`fpga/rp/_template/`](fpga/rp/_template/)
carries the frozen 35-port partition boundary, an out-of-context synthesis
recipe, the timing constraints, and the registration snippet for
`fpga/dfx/rm_list.tcl`. The step-by-step is
[docs/site/docs/guides/adding-an-rm.md](docs/site/docs/guides/adding-an-rm.md).

```sh
python3 fpga/rp/_template/new_rm.py <name> --design-id 0x00NN
make -C fpga/dfx pin-check       # a second, no Vivado -- run it before anything else
```

`pin-check` catches a drifted boundary in a second; without it the same mistake
surfaces as a `pr_verify` failure after a full synthesis, place and route. The
background — the boundary, `static_id`, `rm_id` and the gates — is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## The lab-IP boundary (important)

This platform is built *against* confidential vendor IP (Arm CoreSight SoC-400,
Cortex-M0, vendor memory compilers) that lives in shared, lab-managed trees
referenced by environment variables. **Never** edit those upstream trees, and
**never** commit their RTL — the confidential IP is deliberately git-ignored
(see `.gitignore`, `NOTICE`, and `fpga/rp/local_overrides/`).

If a fix appears to need an IP-library change: copy the affected file into the
project tree (e.g. `fpga/rp/local_overrides/<ip>/`), wire the flist to the local
copy, and document the deviation. The local copy stays git-ignored. See
[fpga/rp/local_overrides/coresight_soc400/README.md](fpga/rp/local_overrides/coresight_soc400/README.md).

## Reporting problems

- Bugs / feature ideas: open an issue.
- Anything security- or IP-sensitive: see [SECURITY.md](SECURITY.md) — do **not**
  open a public issue for those.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
