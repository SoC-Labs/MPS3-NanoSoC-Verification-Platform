# Pinned nanosoc snapshot for the DFX RM build

## Why

`make -C fpga/dfx rm-nanosoc-dcp` OOC-synthesises the real nanosoc RM by sourcing
`$SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl` (default
`~/SoCLabs/nanosoc_m0_soc`). That upstream **working tree** is, as of
2026-07-15, mid-regeneration by the QSPI / micropython track and **does not
compile**:

- `build_soc/rtl/nanosoc.sv` is **uncommitted** — a QSPI-stack regen that
  instantiates `qspi_flash_ahb` / `u_qspi_apb_bridge` and carries an
  `ACCELERATOR_SUBSYSTEM` param, drifted from what the shipped `rm_nanosoc` was
  built against.
- The `nanosoc_arch_tech` submodule is checked out **ahead** of its recorded
  gitlink (`bbca9ce` vs the HEAD-recorded `d719b66`); the two are not mutually
  compatible.

The config that actually built the shipped `rm_nanosoc` is **committed HEAD of
`nanosoc_m0_soc` + every submodule at its recorded gitlink SHA**. In that config
`build_soc/rtl/nanosoc.sv` has **no QSPI stack** and the `exp_*` expansion port is
declared as an **inverted AHB target** (`input exp_hsel/haddr/htrans/hwrite/hsize/
hburst/hprot/hwdata/hmastlock/hready`, `output exp_hrdata/hresp/hreadyout`) — which
is exactly what the committed `rp_nanosoc_wrapper.sv` ties off
(`exp_hsel(1'b0)`, `exp_hreadyout()` open).

## The mechanism

`fpga/rp/nanosoc/pin_nanosoc_snapshot.sh` reconstructs that pinned config,
**read-only**, into a build-local dir (`fpga/dfx/build/nanosoc_snapshot`, gitignored)
by cascading `git archive` down the recorded gitlink SHAs — including the nested
tech submodules under `nanosoc_arch_tech` (which are byte-identical between
`d719b66` and `bbca9ce`, so the cascade stays fully pinned to what HEAD records).
It also copies the generated stage-0 bootrom (`imp/…/stage0`, `.gitignore`d, so
`git archive` cannot capture it) so the snapshot is self-contained. **It never
writes inside either upstream nanosoc tree.**

## Usage — no edit to the shared DFX Makefile/rm_list.tcl

Those two files carry another track's uncommitted edits, so the hook is an **env
override**, not an in-place change:

```sh
# materialise (or refresh) the snapshot and export NANOSOC_M0_SOC_HOME:
source fpga/rp/nanosoc/pin_nanosoc_snapshot.sh

# build the RM against the pinned snapshot:
make -C fpga/dfx rm-nanosoc-dcp SOCLABS_NANOSOC_SOC_DIR="$NANOSOC_M0_SOC_HOME"
```

`SOCLABS_NANOSOC_ARCH_TECH_DIR`, `SOCLABS_NANOSOC_GEN_DIR` and `FPGA_BOOTROM_DIR`
all default (in `fpga/dfx/Makefile`) to paths *under* `SOCLABS_NANOSOC_SOC_DIR`,
so overriding that one variable re-points the whole set at the snapshot.

The snapshot is keyed to the upstream HEAD SHA in `.pinned_from`; re-running the
script is a no-op unless HEAD moved or `FORCE=1` is set.

## Note on the RM wrapper

`git status` shows `fpga/rp/nanosoc/rp_nanosoc_wrapper.sv` as modified — those are
the QSPI track's uncommitted edits, which need the broken regen. For a build
against this pinned snapshot use the **committed** wrapper
(`git show HEAD:fpga/rp/nanosoc/rp_nanosoc_wrapper.sv`). This is a temporary
condition until the QSPI track commits/pins its tree.
