# `tests/eth_ss_repro` — the eth_ss firmware/bootrom build must be reproducible

Guards the defect described in `docs/planning/ETHSS_REPRODUCIBILITY.md` and
routed around by `fpga/rp/local_overrides/eth_ss/`: the ethernet-subsystem FPGA
flow passes `GNU_CC_EXTRA_FLAGS` on the make command line, which **replaces**
each firmware target's own compiler flags instead of adding to them, so the same
source compiles to a different bootloader depending on which entry point built
it.

Pure host tools: `arm-none-eabi-gcc` + `python3` + GNU `make`. **No Vivado, no
simulator, no board.**

```
# green here
ETH_SS_HOME=/path/to/ethernet-subsystem-ahb python -m pytest tests/eth_ss_repro -q

# the control: reproduce the unpatched upstream behaviour -- must be RED
ETH_SS_HOME=/path/to/ethernet-subsystem-ahb ETHSS_REPRO_CONTROL=1 \
  python -m pytest tests/eth_ss_repro -q
```

Collected by `make check` / `make check-ci` stage 3 (`pytest tests`) with no
Makefile change — that stage walks `tests/` recursively.

| test | needs the ARM toolchain + `ETH_SS_HOME` |
|---|---|
| `test_two_clean_builds_are_byte_identical` | yes |
| `test_board_defines_are_appended_not_substituted` | yes — **this is the one the control turns red** |
| `test_the_clobber_actually_changes_the_binary` | yes |
| `test_bootrom_gen_is_byte_stable_for_a_fixed_hex` | yes |
| `test_deviation_record_matches_upstream` | yes |
| `test_a_command_line_variable_deletes_a_leaf_makefiles_own_flags` | **no** |
| `test_the_append_seam_adds_without_deleting` | **no** |

The last two reduce the mechanism to three lines of GNU make, so the reasoning
behind `fpga/rp/local_overrides/eth_ss/patches/000{1,2}` runs in CI even though
CI has neither the toolchain nor the source tree. The other five **skip with a
printed reason** rather than failing, so a green CI never claims to have proved
something it could not run.
