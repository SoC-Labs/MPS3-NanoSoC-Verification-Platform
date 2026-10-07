#!/usr/bin/env bash
# assemble_sd.sh — assemble a shippable MPS3 config-SD bundle for a chosen
# HBI0309 board variant (A / B / C) and a nanoSoC full bitstream.
#
# The output tree mirrors the Arm MPS3 "Boardfiles" bundle layout (config.txt at
# the SD root; per-variant MB/HBI0309<rev>/board.txt; the FPGA app config +
# bitstream under an app dir named Nanosoc/). The bundle carries OUR nanoSoC
# config + bitstream only — no Arm application-note content.
#
# It does NOT touch a real SD card: it builds a directory tree you then copy onto
# the MPS3 config microSD's V2M_MPS3 vfat volume. See README.md. To WRITE one
# through the hub instead, use scripts/mps3_sd_update.sh (backup gate) ->
# `pyverify sd write` (one write, wait, verify) -- this script never writes.
#
# Usage:
#   ./assemble_sd.sh <A|B|C|ALL> [/path/to/nanosoc.bit] [--out DIR] [--with-images]
#                                [--variant NAME]
#
# Examples:
#   ./assemble_sd.sh C  build/board_bitstreams/nanosoc_mps3_top.bit
#   ./assemble_sd.sh ALL my.bit            # one SD tree containing all of A/B/C
#   ./assemble_sd.sh B                     # no bit -> leaves a named placeholder
#   ETH_SMB=0 ./assemble_sd.sh C my.bit    # disable the LAN9220 MCC SMB sideband
#   ./assemble_sd.sh C my.bit --variant ETH_SMB0     # one boot-lottery experiment
#
# --variant NAME applies the key deltas in variants/NAME/ on top of the
# templates: an SD-side EXPERIMENT, one variable at a time, so a boot-rate
# campaign (docs/BOOT_RATE.md) can name the configuration it measured and
# `pyverify boot-rate` can read the same keys back out of the same directory
# rather than being told them. With no --variant the bundle is byte-identical
# to the templates -- proven in host/pyverify/tests/test_bootrate.py.
#
# Backward-compatible legacy form (old single-variant script):
#   ./assemble_sd.sh <full.bit> [out_dir]  # a path-like 1st arg => variant C
#
# Env:
#   ETH_SMB=0   -> stamp FPGA_SMB/FPGA_LAN FALSE in nanosoc.txt (default: keep TRUE)
#
# Output (per variant V):  bundle/HBI0309<V>/   (ALL -> bundle/all/)
#   config.txt
#   MB/HBI0309<V>/board.txt
#   MB/HBI0309<V>/Nanosoc/nanosoc.txt
#   MB/HBI0309<V>/Nanosoc/nanosoc.bit          (or nanosoc.bit.PLACEHOLDER)
#   MB/HBI0309<V>/Nanosoc/images.txt           (only with --with-images)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TPL="$HERE/templates"
EXPDIR="$HERE/variants"   # experiment variants (NOT the A/B/C board revs)
OUT_ROOT="$HERE/bundle"
WITH_IMAGES=0
EXPERIMENT=""            # --variant NAME, an SD-config experiment

for f in config.txt board.txt nanosoc.txt images.txt; do
  [ -f "$TPL/$f" ] || { echo "assemble_sd.sh: missing template $TPL/$f" >&2; exit 1; }
done

# --- parse args (flags anywhere; up to two positionals) ---------------------
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out)          OUT_ROOT="${2:?--out needs a DIR}"; shift 2;;
    --with-images)  WITH_IMAGES=1; shift;;
    --variant)      EXPERIMENT="${2:?--variant needs a NAME}"; shift 2;;
    -h|--help)      sed -n '2,45p' "$0"; exit 0;;
    --)             shift; while [ $# -gt 0 ]; do POS+=("$1"); shift; done;;
    -*)             echo "assemble_sd.sh: unknown flag: $1" >&2; exit 2;;
    *)              POS+=("$1"); shift;;
  esac
done

sel="${POS[0]:-}"
[ -n "$sel" ] || { echo "usage: assemble_sd.sh <A|B|C|ALL> [nanosoc.bit] [--out DIR] [--with-images]" >&2; exit 2; }

# Decide whether the first positional is a variant selector or (legacy) a .bit.
UP="$(printf '%s' "$sel" | tr '[:lower:]' '[:upper:]')"
case "$UP" in
  A|B|C|ALL)
    VSEL="$UP"
    BIT="${POS[1]:-}"
    ;;
  *)
    # Legacy: `assemble_sd.sh <bit> [out_dir]` -> default to variant C.
    VSEL="C"
    BIT="$sel"
    [ -n "${POS[1]:-}" ] && OUT_ROOT="${POS[1]}"
    echo "assemble_sd.sh: note — '$sel' is not A/B/C/ALL; treating it as the" >&2
    echo "                bitstream and defaulting to variant C (legacy form)." >&2
    ;;
esac

if [ "$VSEL" = "ALL" ]; then
  VARIANTS=(A B C)
  BUNDLE="$OUT_ROOT/all"
else
  VARIANTS=("$VSEL")
  BUNDLE="$OUT_ROOT/HBI0309$VSEL"
fi

if [ -n "$BIT" ] && [ ! -f "$BIT" ]; then
  echo "assemble_sd.sh: no such bitstream: $BIT" >&2; exit 1
fi

# --- boot-lottery experiment variants ---------------------------------------
# variants/<NAME>/<template-basename> holds KEY: VALUE overrides; every key must
# ALREADY EXIST in the template. A key that does not is refused, loudly: an
# MPS3 config file accepts almost anything silently and fails at the I/O pads
# with no message, so a typo'd key would ship an SD that claims an experiment it
# is not running -- exactly the failure this whole flow exists to avoid.
list_variants() {
  [ -d "$EXPDIR" ] || return 0
  for d in "$EXPDIR"/*/; do
    [ -f "${d}variant.txt" ] && basename "$d"
  done | sort | tr '\n' ' '
}

if [ -n "$EXPERIMENT" ] && [ ! -f "$EXPDIR/$EXPERIMENT/variant.txt" ]; then
  echo "assemble_sd.sh: no such variant: $EXPERIMENT" >&2
  echo "                known variants: $(list_variants)" >&2
  echo "                (a variant is a directory under fpga/mps3_sd/variants/" >&2
  echo "                 holding variant.txt + per-template key overrides)" >&2
  exit 2
fi

apply_variant() {   # <template-basename> <dest-file>
  [ -n "$EXPERIMENT" ] || return 0
  local src="$EXPDIR/$EXPERIMENT/$1" dest="$2"
  [ -f "$src" ] || return 0
  local key val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|';'*|'#'*) continue;; esac
    case "$line" in *:*) ;; *) continue;; esac
    key="$(printf '%s' "${line%%:*}" | tr -d '[:space:]')"
    val="$(printf '%s' "${line#*:}" | sed 's/;.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//')"
    grep -q "^$key:" "$dest" || {
      echo "assemble_sd.sh: variant $EXPERIMENT: $1 has no key '$key' in the" >&2
      echo "                template -- refusing to add one the MCC would" >&2
      echo "                ignore silently." >&2
      exit 1
    }
    awk -v k="$key" -v v="$val" '
      index($0, k ":") == 1 {
        c = index($0, ";")
        printf "%-26s%s\n", k ": " v, (c ? substr($0, c) : "")
        next
      }
      { print }
    ' "$dest" > "$dest.tmp" && mv "$dest.tmp" "$dest"
  done < "$src"
  # Stamp provenance AFTER line 1 (a leading line before the first key is where
  # MCC parsers get fussy). An SD found in a board must be able to say which
  # experiment it is carrying.
  awk -v n="$EXPERIMENT" -v f="$1" 'NR==1 {
        print
        print ";VARIANT: " n " -- keys from fpga/mps3_sd/variants/" n "/" f
        next
      } { print }' "$dest" > "$dest.tmp" && mv "$dest.tmp" "$dest"
}

# nanosoc.txt Ethernet SMB/LAN sideband stamp (ETH_SMB=0 -> FALSE) ------------
stamp_nanosoc() {   # <dest>
  if [ "${ETH_SMB:-1}" = "0" ]; then
    sed -e 's/^FPGA_SMB: TRUE/FPGA_SMB: FALSE/' \
        -e 's/^FPGA_LAN: TRUE/FPGA_LAN: FALSE/' "$TPL/nanosoc.txt" > "$1"
  else
    cp "$TPL/nanosoc.txt" "$1"
  fi
}

# --- build ------------------------------------------------------------------
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"
cp "$TPL/config.txt" "$BUNDLE/config.txt"          # shared SD-root config
apply_variant config.txt "$BUNDLE/config.txt"

for V in "${VARIANTS[@]}"; do
  bdir="$BUNDLE/MB/HBI0309$V"
  ndir="$bdir/Nanosoc"
  mkdir -p "$ndir"
  sed "s/@BOARD@/HBI0309$V/g" "$TPL/board.txt" > "$bdir/board.txt"   # stamp BOARD line
  apply_variant board.txt "$bdir/board.txt"
  stamp_nanosoc "$ndir/nanosoc.txt"
  apply_variant nanosoc.txt "$ndir/nanosoc.txt"
  [ "$WITH_IMAGES" = 1 ] && cp "$TPL/images.txt" "$ndir/images.txt"
  if [ -n "$BIT" ]; then
    cp "$BIT" "$ndir/nanosoc.bit"
  else
    cat > "$ndir/nanosoc.bit.PLACEHOLDER" <<'PH'
PLACEHOLDER — no bitstream supplied.

Replace this file with your nanoSoC full bitstream, renamed to exactly
"nanosoc.bit" (the name nanosoc.txt's F0FILE expects). Build it with
fpga/monolithic/build_monolithic.tcl (nanosoc_mps3_top.bit) or
fpga/dfx/proof/build_proof.tcl (config_greybox.bit), then re-run:

    ./assemble_sd.sh <A|B|C|ALL> /path/to/that.bit
PH
  fi
done

# --- report -----------------------------------------------------------------
abs_bit=""; [ -n "$BIT" ] && abs_bit="$(cd "$(dirname "$BIT")" && pwd)/$(basename "$BIT")"
echo ""
echo "Assembled MPS3 config-SD bundle: $BUNDLE"
if command -v find >/dev/null; then
  ( cd "$BUNDLE" && find . -type f | sort | sed 's/^\./  /' )
fi
echo ""
echo "  variant(s) : ${VARIANTS[*]/#/HBI0309}"
echo "  bitstream  : ${abs_bit:-<none — placeholder written>}"
echo "  eth SMB/LAN: $([ "${ETH_SMB:-1}" = 0 ] && echo 'FALSE (ETH_SMB=0)' || echo 'TRUE (LAN9220 SMB path)')"
echo "  experiment : ${EXPERIMENT:-<none - templates unmodified>}"
if [ -n "$EXPERIMENT" ]; then
  sed -n 's/^DESCRIPTION:[[:space:]]*/               /p' "$EXPDIR/$EXPERIMENT/variant.txt"
  echo "               record it in the boot-rate run: --variant $EXPERIMENT"
fi
echo "  images.txt : $([ "$WITH_IMAGES" = 1 ] && echo included || echo 'omitted (use --with-images)')"
echo ""
echo "TO FLASH (do NOT delete the SD's stock Arm files, esp. mbb_v141.ebf):"
echo "  1. Mount the MPS3 config microSD (vfat volume V2M_MPS3)."
echo "  2. Copy the CONTENTS of $BUNDLE onto the SD root:"
echo "       cp -r $BUNDLE/config.txt $BUNDLE/MB  <SD>/"
echo "  3. Eject, insert into the MPS3, power-cycle (AUTORUN: TRUE auto-boots),"
echo "     or drop a reboot.txt if USB_REMOTE is TRUE. See README.md."
echo ""
echo "  Remote alternative (no physical card): scripts/mps3_sd_update.sh --bit"
echo "  <bit> --token <lease-token> --program   (captures a backup, then runs"
echo "  \`pyverify sd write\`: one write, wait, md5 read-back)."
