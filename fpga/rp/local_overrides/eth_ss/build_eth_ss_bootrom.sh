#!/usr/bin/env bash
#-----------------------------------------------------------------------------
# build_eth_ss_bootrom.sh — build the ethernet-subsystem stage-0 bootloader and
# its synthesizable boot ROM REPRODUCIBLY, without mutating the read-only
# ethernet-subsystem source tree and without the upstream flag clobber.
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.
#
# THE DEVIATION THIS FILE RECORDS -- see README.md for the full evidence.
#   $ETH_SS_HOME/fpga/Makefile:230-232
#       firmware: soc_model
#           $(MAKE) -C $(ETH_SS_HOME) firmware TARGET=arm-none-eabi CPU=$(CPU) \
#               GNU_CC_EXTRA_FLAGS='$(FW_BOARD_DEF)'
#   GNU_CC_EXTRA_FLAGS is passed on the make COMMAND LINE, and a command-line
#   variable overrides a `:=` assignment in every sub-makefile at every level.
#   So the FPGA flow silently REPLACES each firmware target's own optimisation
#   flags with the board -D defines: the bootloader loses `-Os -flto
#   -mthumb-interwork`, udp_echo loses `-flto -ffunction-sections
#   -fdata-sections -Wl,-Map=...`. Building the SAME source through the two
#   supported entry points therefore yields two DIFFERENT binaries (1048 vs 996
#   bytes for the bootloader), which is the "the build is not reproducible"
#   report this override answers.
#
# This script builds through the leaf makefile and APPENDS the board defines to
# the flags the leaf declares, which is what the upstream rule meant to do.
# All output goes under --out; ETH_SS_HOME is only ever read.
#
# Usage:
#   ETH_SS_HOME=/path/to/ethernet-subsystem-ahb \
#     build_eth_ss_bootrom.sh --out DIR [--cpu m0plus|m0] \
#                             [--board-defs "-DBOARD_MPS3=1 ..."] \
#                             [--clobber-like-upstream]
#
#   --clobber-like-upstream  reproduce the UNPATCHED behaviour (flags replaced,
#                            not appended). This exists so tests/eth_ss_repro
#                            has a control that is seen to fail; never use it
#                            to produce an image.
#
# Env:
#   ETH_SS_HOME        REQUIRED. The ethernet-subsystem-ahb checkout (READ-ONLY).
#                      No default on purpose -- a default that names one
#                      workstation's home directory is invisible there and wrong
#                      everywhere else (same rule as tools.env.example).
#   SOURCE_DATE_EPOCH  optional; forwarded to bootrom_gen.py, which already
#                      honours it (bootrom_gen.py:28).
#   TOOLCHAIN_BIN      optional; prepended to PATH if set.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$HERE/eth_ss_probe.mk"

OUT=""
CPU="m0plus"
BOARD_DEFS=""
CLOBBER=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out)                   OUT="$2"; shift 2 ;;
        --cpu)                   CPU="$2"; shift 2 ;;
        --board-defs)            BOARD_DEFS="$2"; shift 2 ;;
        --clobber-like-upstream) CLOBBER=1; shift ;;
        -h|--help)               sed -n '2,60p' "$0"; exit 0 ;;
        *) echo "build_eth_ss_bootrom.sh: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

[ -n "$OUT" ] || { echo "build_eth_ss_bootrom.sh: --out DIR is required" >&2; exit 2; }
: "${ETH_SS_HOME:?build_eth_ss_bootrom.sh: ETH_SS_HOME is not set (the ethernet-subsystem-ahb checkout)}"
[ -d "$ETH_SS_HOME" ] || { echo "build_eth_ss_bootrom.sh: ETH_SS_HOME does not exist: $ETH_SS_HOME" >&2; exit 2; }
[ -n "${TOOLCHAIN_BIN:-}" ] && export PATH="$TOOLCHAIN_BIN:$PATH"
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    echo "build_eth_ss_bootrom.sh: arm-none-eabi-gcc not on PATH (set TOOLCHAIN_BIN)" >&2; exit 2; }

PY="${PYTHON:-python3}"
OUT="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"
SHIM="$OUT/.flagprobe_shim"
mkdir -p "$SHIM/firmware/build" && : > "$SHIM/firmware/build/testcode.mk"

# --- resolve everything FROM UPSTREAM, so nothing is re-typed here -----------
probe() {  # probe <makefile> <VAR> [extra make args...]
    local mf="$1" var="$2"; shift 2
    make -s -C "$ETH_SS_HOME" -f "$PROBE" __soclabs_print \
         INCLUDE_MAKEFILE="$mf" VAR="$var" CPU="$CPU" "$@" 2>/dev/null | tail -1
}

BUILD_DIR="$(probe "$ETH_SS_HOME/Makefile" BUILD_DIR)"
CPU_PRODUCT="$(probe "$ETH_SS_HOME/Makefile" CPU_PRODUCT)"
NANOSOC_CPU="$(probe "$ETH_SS_HOME/Makefile" NANOSOC_CPU)"
BOOTROM_ADDRW="$(probe "$ETH_SS_HOME/Makefile" BOOTROM_ADDRW)"
BOOTROM_MODULE="$(probe "$ETH_SS_HOME/Makefile" BOOTROM_MODULE)"
BOOTROM_REGION="$(probe "$ETH_SS_HOME/Makefile" BOOTROM_REGION_MODULE)"
BOOTROM_GEN_PY="$(probe "$ETH_SS_HOME/Makefile" BOOTROM_GEN_PY)"

LEAF="$ETH_SS_HOME/firmware/bootloader"
DECLARED_FLAGS="$(make -s -C "$LEAF" -f "$PROBE" __soclabs_print \
    INCLUDE_MAKEFILE="$LEAF/makefile" VAR=GNU_CC_EXTRA_FLAGS \
    SHIM_ARCH_TECH="$SHIM" SOCLABS_PROJECT_DIR="$ETH_SS_HOME" 2>/dev/null | tail -1)"

if [ "$CLOBBER" = "1" ]; then
    # UNPATCHED behaviour: the leaf's own flags are thrown away. Control only.
    FLAGS="$BOARD_DEFS"
else
    FLAGS="$DECLARED_FLAGS${BOARD_DEFS:+ $BOARD_DEFS}"
fi

echo "[eth_ss-repro] ETH_SS_HOME     = $ETH_SS_HOME"
echo "[eth_ss-repro] cpu             = $CPU ($NANOSOC_CPU, $CPU_PRODUCT), build_soc = $BUILD_DIR"
echo "[eth_ss-repro] declared flags  = $DECLARED_FLAGS"
echo "[eth_ss-repro] board defines   = ${BOARD_DEFS:-(none)}"
echo "[eth_ss-repro] effective flags = $FLAGS$([ "$CLOBBER" = 1 ] && echo '   <-- CLOBBERED (upstream behaviour)')"

# --- bootloader --------------------------------------------------------------
# SOFTWARE_BUILD_DIR redirects every artefact out of the read-only source tree.
make -s -C "$LEAF" all_gcc \
    TOOL_CHAIN=gcc \
    CPU_PRODUCT="$CPU_PRODUCT" \
    NANOSOC_CPU="$NANOSOC_CPU" \
    SOCLABS_PROJECT_DIR="$ETH_SS_HOME" \
    SOCLABS_NANOSOC_ARCH_TECH_DIR="$ETH_SS_HOME/nanosoc_arch_tech" \
    SOCLABS_NANOSOC_FIRMWARE_TECH_DIR="$ETH_SS_HOME/nanosoc_arch_tech/firmware" \
    FIRMWARE_CONFIG_DIR="$ETH_SS_HOME/$BUILD_DIR/firmware" \
    SOFTWARE_BUILD_DIR="$OUT/firmware" \
    GNU_CC_EXTRA_FLAGS="$FLAGS"

HEX="$OUT/firmware/bootloader/out/bootloader.hex"
[ -f "$HEX" ] || { echo "build_eth_ss_bootrom.sh: no bootloader.hex produced" >&2; exit 1; }

# --- boot ROM ----------------------------------------------------------------
mkdir -p "$OUT/bootrom"
"$PY" "$ETH_SS_HOME/$BOOTROM_GEN_PY" \
    -a "$BOOTROM_ADDRW" \
    -i "$HEX" \
    -t gcc \
    -m "$BOOTROM_MODULE" \
    -v "$OUT/bootrom/$BOOTROM_MODULE.sv" \
    -b "$OUT/bootrom/bootrom.bintxt" \
    -R "$BOOTROM_REGION" \
    -r "$OUT/bootrom/$BOOTROM_REGION.v" >/dev/null

echo "[eth_ss-repro] bootloader.bin  = $(wc -c < "$OUT/firmware/bootloader/out/bootloader.bin") bytes"
echo "[eth_ss-repro] done: $OUT/bootrom/$BOOTROM_MODULE.sv"
