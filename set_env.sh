#!/bin/bash
# =============================================================================
# set_env.sh — mps3-nanosoc-platform simulation environment (closes I24)
#
# Source this before running any cocotb bench target:
#   source set_env.sh
#   make -C tests                      # readiness summary + all READY benches
#   make -C tests BLOCK=sim_smoke run-one
#
# NOTHING SITE-SPECIFIC IS HARD-CODED HERE (this file is public). Every tool
# location and licence server comes from, in order of precedence:
#   1. the environment you sourced this from;
#   2. the same KEY in the repo-root tools.env (NOT tracked; see
#      tools.env.example) -- only plain `KEY = value` lines are read;
#   3. a LOCAL, git-ignored set_env.local.sh beside this file, sourced last,
#      for anything that needs real shell.
# Unset keys are simply skipped: whatever is already on PATH is used.
# =============================================================================

_SETENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"

# _setenv_key KEY -> the environment value, else the tools.env value, else "".
_setenv_key() {
    eval "_v=\${$1:-}"
    if [ -z "$_v" ] && [ -n "$_SETENV_DIR" ] && [ -f "$_SETENV_DIR/tools.env" ]; then
        _v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*[:?]\{0,1\}=[[:space:]]*//p" \
                "$_SETENV_DIR/tools.env" | tail -n 1)"
    fi
    printf '%s' "$_v"
}

# -----------------------------------------------------------------------------
# Python + cocotb — THE PINNED COMBO: Python 3.10 + cocotb 2.0.1
#
# DECISION (2026-07-04, W-SIM): adopt cocotb 2.0.1 and port the shared
# drivers in tests/common/ to 2.x-safe semantics. Rationale:
#   1. The commit-b3e8e99 diagnosis ("cocotb 2.0.1 ReadOnly-phase strictness
#      breaks the 1.x-era shared drivers") turned out to be only half right:
#      cocotb 1.7.2's scheduler has the SAME restriction (scheduler.py:596
#      raises "Write to object ... was scheduled during a read-only sync
#      phase"). The drivers were only ever `make -n` dry-run under 1.7.2,
#      never executed — so pinning 1.7.2 would NOT have avoided the driver
#      port. The road not taken (pin 1.7.2, keep drivers untouched) was a
#      mirage; the drivers needed the ReadOnly fix under either version.
#   2. VCS + cocotb 2.0.1 is the combo proven end-to-end (b3e8e99 Wave 3:
#      MDIO RTL compiled/elaborated/ran 51 us of sim under VCS; the only
#      failures were the driver ReadOnly writes).
#   3. python3.8 is EOL; python 3.10 + pytest 9 also runs the pure-Python
#      suite (verified).
#
# MPS3_PYTHON_BIN names the bin/ directory of the Python 3.10 environment that
# holds cocotb 2.0.1 (e.g. a conda env). Putting it at the head of PATH pins
# BOTH `cocotb-config` (which the per-block Makefiles resolve via
# `$(shell cocotb-config --makefiles)`) and `python3` to the same interpreter.
# -----------------------------------------------------------------------------
_py_bin="$(_setenv_key MPS3_PYTHON_BIN)"
[ -n "$_py_bin" ] && export PATH="$_py_bin:$PATH"

# -----------------------------------------------------------------------------
# VCS 2022.06-SP2 (primary simulator — SIM ?= vcs in every per-block Makefile)
# VCS_HOME and SNPSLMD_LICENSE_FILE: your site's install and licence servers.
# -----------------------------------------------------------------------------
_vcs_home="$(_setenv_key VCS_HOME)"
if [ -n "$_vcs_home" ]; then
    export VCS_HOME="$_vcs_home"
    export PATH="${VCS_HOME}/bin:${PATH}"
fi
_snps_lic="$(_setenv_key SNPSLMD_LICENSE_FILE)"
[ -n "$_snps_lic" ] && export SNPSLMD_LICENSE_FILE="$_snps_lic"
export VCS_TARGET_ARCH="${VCS_TARGET_ARCH:-amd64}"

# -----------------------------------------------------------------------------
# Questa fallback (documented, not the default): `make SIM=questa` in any
# per-block dir. QUESTA_BIN is appended, not prepended — VCS stays the default
# resolution. NOTE: the Questa path has not been proven green in this repo yet;
# VCS is the validated route. Verilator 4.028 is lint-only (too old for cocotb).
# -----------------------------------------------------------------------------
_questa_bin="$(_setenv_key QUESTA_BIN)"
[ -n "$_questa_bin" ] && export PATH="${PATH}:$_questa_bin"
_mgls_lic="$(_setenv_key MGLS_LICENSE_FILE)"
[ -n "$_mgls_lic" ] && export MGLS_LICENSE_FILE="$_mgls_lic"

# -----------------------------------------------------------------------------
# Board hub host (dataplane + JTAG relay). Deliberately NOT hardcoded here (this
# file is public): export MPS3_HUB (bare host, for the ssh dataplane relay) and
# MPS3_HW_URL ("tcp:<fqdn>:3121", for xsdb hw_server) in a LOCAL, git-ignored
# set_env.local.sh alongside this file. Unset => host tooling runs OpenOCD
# locally / uses a localhost placeholder (fails loudly, bakes no site hostname).
# -----------------------------------------------------------------------------
export MPS3_HUB="${MPS3_HUB:-}"
export MPS3_HW_URL="${MPS3_HW_URL:-}"
if [ -n "$_SETENV_DIR" ] && [ -f "$_SETENV_DIR/set_env.local.sh" ]; then
    . "$_SETENV_DIR/set_env.local.sh"
fi
unset _v _py_bin _vcs_home _snps_lic _questa_bin _mgls_lic

echo "[set_env] MPS3_HUB    = ${MPS3_HUB:-<unset — set in set_env.local.sh for board ops>}"
echo "[set_env] python3    = $(command -v python3)"
echo "[set_env] cocotb     = $(python3 -c 'import cocotb; print(cocotb.__version__)' 2>/dev/null || echo 'NOT FOUND')"
echo "[set_env] vcs        = $(command -v vcs || echo 'NOT FOUND (set VCS_HOME)')"
echo "[set_env] SNPSLMD_LICENSE_FILE = ${SNPSLMD_LICENSE_FILE:-<unset — set it in tools.env or the environment>}"
