# `fpga/rp/local_overrides/eth_ss/` — documented deviations from the read-only ethernet-subsystem tree

The ethernet-subsystem-ahb checkout (`$ETH_SS_HOME`, see `tools.env.example`) is
**read-only** to this repo: it is another project's tree, shared with other
engineers and other builds. When a fix belongs there, this directory holds the
patch *and* a local way to get the right result meanwhile, so nothing here ever
depends on someone having remembered to edit a tree we do not own.

Two deviations are recorded. Both are reproducibility defects found while
answering "why can the fielded eth_ss image not be rebuilt?" — see
`docs/planning/ETHSS_REPRODUCIBILITY.md` for the full evidence and
`tests/eth_ss_repro/` for the gate.

---

## Deviation 1 — the FPGA flow silently deletes each firmware target's compiler flags

**Upstream:** `$ETH_SS_HOME/fpga/Makefile:230-232`

```make
firmware: soc_model
	$(MAKE) -C $(ETH_SS_HOME) firmware TARGET=arm-none-eabi CPU=$(CPU) \
	    GNU_CC_EXTRA_FLAGS='$(FW_BOARD_DEF)'
```

`GNU_CC_EXTRA_FLAGS` is passed on the make **command line**. A command-line
variable overrides a `:=` assignment in *every* sub-makefile at *every* level,
so this does not add the board defines to the firmware build — it **replaces**
each target's own flags with them:

| target | declares (`GNU_CC_EXTRA_FLAGS :=`) | what the FPGA flow compiles with |
|---|---|---|
| `firmware/bootloader/makefile:28` | `-Os -flto -mthumb-interwork` | *(gone)* |
| `firmware/eth_app/udp_echo/makefile:15` | `-flto -ffunction-sections -fdata-sections -Wl,-Map=…` | *(gone)* |

So `make -C fpga build_design` and `make bootloader` compile **different
binaries out of byte-identical source**, silently. That is the whole of the
"the eth_ss build is not reproducible / the rebuilt image is dead" report — it
was never a race, a hash seed, or a timestamp; it was an entry point.

Measured on `ethernet-subsystem-ahb` @ `a61d57a`:

| build | `bootloader.bin` |
|---|---|
| leaf flags (`-Os -flto -mthumb-interwork`) | **1012 bytes** |
| flags clobbered by the FPGA flow | **1064 bytes** |

and on the `ethss-main-build` worktree @ `426f034` the clobbered build is
**byte-identical (md5 `568f86cb…`) to the archived 2026-08-25 "dead control"
artefact** while the leaf-flag build is 996 bytes — i.e. the two "mystery
variants A and B" in the project record are exactly these two entry points.

**The fix (upstream):** `patches/0001-fpga-Makefile-do-not-clobber-firmware-flags.patch`
and `patches/0002-testcode.mk-add-NANOSOC_EXTRA_CFLAGS-append-seam.patch` —
two lines. Add an append seam to the shared template and pass *that*:

```make
NANOSOC_EXTRA_CFLAGS  ?=
GNU_CC_EXTRA_FLAGS    += $(NANOSOC_EXTRA_CFLAGS)
```

`tests/eth_ss_repro` proves the semantics in three lines of GNU make, with no
toolchain and no `$ETH_SS_HOME`, so that reasoning is held in CI.

**Meanwhile (local):** `build_eth_ss_bootrom.sh` builds the bootloader and its
boot ROM through the leaf makefile and **appends** the board defines to whatever
that makefile declares. It never re-types upstream's flags — it asks make for
them through `eth_ss_probe.mk` — so it cannot drift, and it never writes inside
`$ETH_SS_HOME` (every artefact goes under `--out`).

```
ETH_SS_HOME=/path/to/ethernet-subsystem-ahb \
  ./build_eth_ss_bootrom.sh --out <dir> \
      --board-defs "-DBOARD_MPS3=1 -DETH_PHY_ADVERTISE=0x0041"
```

`--clobber-like-upstream` reproduces the unpatched behaviour; it exists so the
gate has a control that is seen to fail, and must never be used to produce an
image.

---

## Deviation 2 — every generated file carries a wall clock

**Upstream:** twelve `datetime.datetime.now()` / `time.time()` call sites across
nine files under `$ETH_SS_HOME/nanosoc_arch_tech/nanosoc_gen/soc_model/backends/`
(`firmware.py:84`, `toplevel.py:96`, `ahb.py:723,754`, `soc_config_pkg.py:169,204`,
`chip.py:90`, `system.py:88`, `docs.py:80`, `discovery.py:392`,
`build_info.py:150,259,277`).

Every regeneration therefore rewrites every `.sv`, `.vh`, `.h`, `.ld`, `.mk` and
`.flist` with a new `Generated:` line, which:

* makes `diff -r` between two `build_soc` trees all noise, so a **real** change
  has nowhere to show — this is why the earlier investigation could not tell
  drift from randomness and reached for "non-determinism";
* defeats the generator's own `write_if_changed()` (`soc_model/utils.py`), so
  every regen re-triggers every downstream make and Vivado step;
* in `build_info.py:150` a wall clock reaches a **register reset value** (only
  when a design declares a `build_info` block; `ethernet_ss_ahb.yaml` does not).

`bootrom_gen.py` in the same tree already does this correctly
(`bootrom_gen.py:20-35`: honour `SOURCE_DATE_EPOCH`, else omit the wall clock).
The upstream fix is a `soc_model/utils.py` helper of the same shape with the
twelve call sites routed through it.

**Meanwhile (local):** `soc_model_reproducible.py` is a drop-in for
`python -m nanosoc_arch_tech.nanosoc_gen.soc_model` that freezes the clock from
outside the read-only tree. Verified: two clean regenerations at different wall
clocks, `TZ`s and `PYTHONHASHSEED`s produce a **byte-identical** `build_soc`
(excluding the two files noted below).

```
cd $ETH_SS_HOME
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) \
python3 .../soc_model_reproducible.py sys_desc/ethernet_ss_ahb.yaml \
    --lib-dir nanosoc_arch_tech/sys_desc --lib-dir ethernet-mac-ahb/sys_desc \
    --lib-dir sys_desc --build-dir <out>
```

### Known-and-left-alone

* `interconnect/*/ipxact/*.xml` — Arm's `BuildBusMatrix.pl`
  (`$ARM_IP_LIBRARY_PATH/.../cmsdk_ahb_busmatrix/bin/`) emits the IP-XACT
  `remapStates` in **Perl hash order**, so `remap_0` and `remap_n0` swap places
  run to run (observed 3/6 vs 3/6 over six clean runs). The generated **Verilog
  is unaffected** — the RTL hashed identically across all six — and nothing in
  `$ETH_SS_HOME/fpga/` reads the IP-XACT, so this is cosmetic here. It is vendor
  IP under `$ARM_IP_LIBRARY_PATH` and must not be edited.
* `interconnect/*/logs/*.log` — the same vendor script's log: its own run date
  and absolute paths.
* `flist/*.flist` — manifests of absolute paths, so two builds in *different*
  directories differ there by construction. Compare with the build-dir prefix
  normalised, or build both into the same path.

---

## Retiring this directory

When `patches/0001` and `patches/0002` land upstream,
`tests/eth_ss_repro::test_deviation_record_matches_upstream` goes red and says
so. Delete this directory and that bench together.
