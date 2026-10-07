# tender — Edge Device API supervisor for the MPS3 platform

**Direction (2026-07-04, OPEN_ISSUES I3):** the tender is an **Edge Device API
supervisor** in the Hardware Hub fleet — see `docs/HARDWARE_HUB_INTEGRATION.md`.
It makes the MPS3/KU115 another `board` in the Hub's `{site→gateway→pi→board→
channel}` tree, leased over WireGuard `wg0` with the same grants/arbiter as the
Zynq boards. The plugin below is its **out-of-band** backend (MCC/JTAG/power);
the **in-band** channels (`mgmt`/`console`/`xvc`/`swd`/`dut-net`) ride `wg0` to
the shell. A deployed board self-boots headless; the edge is for
bootstrap/recovery + being a fleet citizen. The role split, channel map, and the
new `dfx-swap` reset kind are in the integration doc.

The "tender" (ARCHITECTURE_SPEC.md §4.3, IMPLEMENTATION_PLAN.md §0.2) owns the
physical/USB side of an MPS3 board: full-image JTAG programming (bring-up +
recovery), MCC USB-MSD image management, and reboots. **It is not a new
framework** — per IMPLEMENTATION_PLAN.md §0.2 the base is an existing fpgahub
node
(`~/SoCLabs/fpgahub`, reference-only, **do not modify** — see
that repo's own `docs/BOARD_CONTROLS.md` for the plugin catalogue), the same
code already managing Pynq-Z2/ZC702 boards. This directory adds the pieces
specific to this platform.

## What's here

- `edge_notes.md` — how `host/pyverify/pyverify/edge.py`'s Edge Device API
  *model* (enumerate.channels/status.fpga/reset logic, no sockets) slots into
  the real `edge-wrapperd` shape described above, the channel -> `wg0`-port
  map, and how `openFPGALoader`/MCC below map onto the Hub's privileged
  (`G`) reset/program verbs. Reference-only, like this file.
- `fpgahub_mps3_plugin.py` — a `fpgahub.program.ProgramPlugin` (`program_full`,
  openFPGALoader JTAG) plus a `platform_deploy` convenience wrapper. Full
  docstrings and design rationale are in the module itself; short version:
  - `program_full` is a real, fpgahub-shaped plugin (mirrors
    `fpgahub.program_plugins.vivado_jtag.VivadoJtag`) — genuinely fits
    fpgahub's `ProgramPlugin` ABC because it *is* "load a literal
    bitstream over JTAG", just via `openFPGALoader` instead of Vivado
    (the Pi 5 has no Vivado install).
  - `sd_install` / `mcc_reboot` are **not reimplemented** — they already
    exist and are hardware-proven
    (`fpgahub.program_plugins.sd_install.SdInstallProgram`,
    `fpgahub.reset_plugins.mps3.Mps3MccReboot`). The module documents the
    config.toml block that references them.
  - `platform_deploy` (overlay push+swap via `pyverify`) does **not** fit
    `ProgramPlugin` — see the module docstring for why (fpgahub's
    dispatcher pre-parses a Xilinx `.bit` ASCII header before calling any
    program plugin; our DFX partial `.bin` has no such header). It's
    exposed as a CLI (`python -m pyverify.cli deploy`, `host/pyverify/`)
    that fpgahub's existing generic manifest-`Action` runner
    (`fpgahub.actions`, reference-only) can shell out to with no
    fpgahub-side code change, plus a Python convenience wrapper
    (`run_platform_deploy`) for callers driving fpgahub programmatically.

## Deployment shape (Phase 0.2 acceptance target)

Per IMPLEMENTATION_PLAN.md §0.2, Phase 0.2's acceptance gate is validated
against a **Pynq-Z2 first** (deploy-over-SSH + XVC to the on-board FT2232 +
SWD), *before* any MPS3 hardware is involved — i.e. this is exercising the
generic tender/fpgahub-node plumbing, not anything MPS3-specific. Concretely,
on the Pi 5:

1. A normal fpgahub install (server or client role — D1 keeps both
   topologies: "Pi-5 tender per bench pod **and** shared-server farm, same
   fpgahub node code").
2. This repo's `host/tender/fpgahub_mps3_plugin.py` copied into (or
   `PYTHONPATH`-added alongside) that fpgahub's `program_plugins`/import
   path, so `register(ProgramFullOpenFpgaLoader())` runs at daemon startup
   the same way the shipped plugins do (see
   `fpgahub.program_plugins.__init__` for the import-registers-plugin
   pattern this mirrors).
3. `host/pyverify/` installed (`pip install -e host/pyverify`) **into the
   interpreter that runs the fpgahub daemon** — `run_platform_deploy`
   launches `sys.executable -m pyverify.cli` (its own interpreter, never a
   bare PATH `python`); a manifest-`Action` form instead resolves whatever
   `python` its shell context provides. Needed only for the
   `platform_deploy` action, not for `program_full`.
4. `openocd`, `openFPGALoader`, and an XVC-capable `hw_server`/`xvcd` on the
   tender (IMPLEMENTATION_PLAN.md §0.2: "fpgahub node + openocd +
   openFPGALoader + xvcd").
5. A board config.toml block per `fpgahub`'s existing MPS3 example
   (`packaging/templates/config.server.toml`, reference-only) extended with
   this platform's `program.full` (`program_full`) and, once Phase 3 lands,
   `program.deploy` (`platform_deploy`) methods.

None of steps 1-5 happen in this task — this directory ships the plugin
code (real argv construction *and* real asyncio subprocess runners behind
injectable seams; unit-tested against the reference fpgahub checkout in
`host/pyverify/tests/test_tender_plugin.py`, which needs a Python 3.11+
interpreter with pydantic — fpgahub's own floor) for A2/A3/A6 to review
and for whoever brings up the physical Pi 5 to wire in.

## Open items for A6

- Confirm `openFPGALoader` flag names (`--board`/`--cable`/`--serial`/
  `--freq`) against the version actually pinned on the tender image, once
  Phase 0.3's `openFPGALoader --detect` against the KU115 has run — see the
  `ProgramFullOpenFpgaLoaderParams` docstring in `fpgahub_mps3_plugin.py`.
- Decide whether `platform_deploy` should eventually become a first-class
  fpgahub dispatch verb (would need an fpgahub-side change, out of this
  repo's read-only scope for that project) or stay a manifest-`Action`
  wrapping the `pyverify` CLI — see the module docstring's "3.
  platform_deploy" section for the full argument.
- fpgahub's `ProgramPlugin.accepts_overlay` (PYNQ `.dtbo`) and
  `fpgahub.bitstream.BitstreamHeader` (Xilinx `.bit` ASCII header) share
  names with this platform's own "overlay" (clearing+partial+manifest
  triple) and "bitstream header" (ICAP partial framing,
  `pusher.push.BitstreamHeader`) concepts. Purely a naming collision across
  two codebases, not a bug, but worth a glossary note somewhere shared.
