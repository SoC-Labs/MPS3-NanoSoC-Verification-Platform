#!/usr/bin/env bash
# =============================================================================
# tests/identify_iice/run_selftest.sh
#
# One-command reproduction of a sim-side IICE trace capture, for a human.
#
# OWNED BY STREAM B (INTERFACES.md §5). This is a convenience wrapper only --
# `make -C tests/identify_iice sim-fsdb` is the real entry point and CI uses
# that. All this adds is the module loads, which are the part nobody remembers.
#
#   ./run_selftest.sh              # gen (if needed) + sim-fsdb -> build/sim_iice.fsdb
#   ./run_selftest.sh sim-selftest # plain run, no FSDB, no Stream A dependency
#   ./run_selftest.sh sim-fsdb
#
# WHY BOTH MODULES, ALWAYS, AS A PAIR
# -----------------------------------
# The Novas FSDB dumper resolves libsscore_vcs<YYYYMM>.so at RUN time, keyed to
# the VCS release, and the two Verdi installs here ship DISJOINT sets of those:
#   vcs/T-2022.06-SP2      <-> verdi/T-2022.06-SP2       (libsscore_vcs202206)
#   vcs/W-2024.09-SP2-3-PC <-> verdi/X-2025.06-SP2       (libsscore_vcs202409)
# Load a VCS without its matching Verdi and the simulation still exits 0 and
# still prints VERDICT=PASS -- it just writes no FSDB. Makefile.sim detects that
# and fails the target, but loading the right pair avoids the round trip.
# See the header of Makefile.sim for the full measurement.
#
# ORDERING NOTE: load the modules BEFORE sourcing set_env.sh. set_env.sh sets
# VCS_HOME with ${VCS_HOME:-<the 2022.06 tree>}, so it defers to whatever the
# module already chose -- but only if the module ran first.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TARGET="${1:-sim-fsdb}"

# --- tools ------------------------------------------------------------------
if [ -f /usr/share/Modules/init/bash ]; then
  # shellcheck disable=SC1091
  source /usr/share/Modules/init/bash
  # NOTE: do NOT pipe `module load` anywhere -- it is a shell function, and a
  # pipe runs it in a subshell, so the environment changes are silently lost.
  module load vcs/W-2024.09-SP2-3-PC verdi/X-2025.06-SP2
else
  echo "WARNING: Environment Modules not found; relying on the ambient VCS/Verdi." >&2
fi

if [ -f "$REPO_ROOT/set_env.sh" ]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/set_env.sh"
fi

echo "== vcs        : $(command -v vcs || echo NOT-FOUND)"
echo "== VCS_HOME   : ${VCS_HOME:-<unset>}"
echo "== VERDI_HOME : ${VERDI_HOME:-<unset>}"

# --- the shadow module is generated; make sure it exists for sim-fsdb -------
if [ "$TARGET" = "sim-fsdb" ] && [ ! -f "$HERE/build/iice_shadow_IICE_SELFTEST.sv" ]; then
  echo "== build/iice_shadow_IICE_SELFTEST.sv absent -> running 'make gen' first"
  make -C "$HERE" gen
fi

make -C "$HERE" "$TARGET"

if [ "$TARGET" = "sim-fsdb" ]; then
  echo
  echo "== trace: $HERE/build/sim_iice.fsdb"
  echo "== to list its signals WITHOUT the Verdi GUI:"
  echo "==   fsdb2vcd $HERE/build/sim_iice.fsdb -o /tmp/t.vcd && sed -n '/\$scope/,/enddefinitions/p' /tmp/t.vcd"
  echo "== (fsdbqry does NOT work on this install: 'Only support streamlined"
  echo "==  header file in fsdbqry' -- see the header of Makefile.sim)"
fi
