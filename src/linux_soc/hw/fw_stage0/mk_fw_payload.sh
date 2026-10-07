#!/bin/bash
# mk_fw_payload.sh -- wrap a kernel + DTB into an OpenSBI FW_PAYLOAD (one blob:
# OpenSBI + kernel + DTB), the payload of stage0's 1-region boot image.
#
# CALLED BY IMAGE's src/linux_harness/sw/build.sh (step 7), which passes every
# input explicitly (OS_SRC, XTOOL, KERNEL, DTB, OUT) and renames the result to
# artifacts/fw_payload_1region.bin. build.sh is the one recipe for the blob;
# mk_1region.sh is retired. The defaults below only make a manual run against
# this tree's own build/ work.
#
# FW_PAYLOAD embeds the FDT (FW_FDT_PATH), so a1 is irrelevant at the hand-off:
#     stage0_pack.py --out boot.img --pc 0x80000000 --a1 0 fw_payload.bin@0x80000000
# The kernel sits at a 4 MiB payload offset, so the blob is ~kernel + 4 MiB.
# Board-free: builds out-of-tree, touches nothing in the Buildroot tree.
set -euo pipefail

SW=${SW:-$(cd "$(dirname "$0")/../../../linux_harness/sw" && pwd)}
OS_SRC=${OS_SRC:-$SW/build/buildroot/output/build/opensbi-1.6}
XTOOL=${XTOOL:-$SW/build/buildroot/output/host/bin/riscv32-amd-linux-gnu-}
KERNEL=${KERNEL:-$SW/artifacts/Image_slim}
DTB=${DTB:-$SW/artifacts/shell_linux.dtb}
OUT=${OUT:-$SW/artifacts}

[ -d "$OS_SRC" ]     || { echo "no OpenSBI source at $OS_SRC (build the harness first)"; exit 1; }
[ -x "${XTOOL}gcc" ] || { echo "no toolchain at ${XTOOL}gcc"; exit 1; }
[ -r "$KERNEL" ]     || { echo "no kernel at $KERNEL"; exit 1; }
[ -r "$DTB" ]        || { echo "no dtb at $DTB"; exit 1; }

export LD_LIBRARY_PATH="$(printf '%s' "${LD_LIBRARY_PATH:-}" | tr ':' '\n' | grep -vE '^\.?$' | paste -sd: -)"
BLD=$(mktemp -d)
trap 'rm -rf "$BLD"' EXIT

make -s -C "$OS_SRC" O="$BLD" \
    CROSS_COMPILE="$XTOOL" PLATFORM=generic PLATFORM_RISCV_XLEN=32 \
    FW_PAYLOAD_PATH="$KERNEL" FW_FDT_PATH="$DTB"

SRC="$BLD/platform/generic/firmware/fw_payload.bin"
[ -r "$SRC" ] || { echo "build produced no fw_payload.bin"; exit 1; }
cp "$SRC" "$OUT/fw_payload.bin"
echo "fw_payload: $OUT/fw_payload.bin ($(stat -c%s "$OUT/fw_payload.bin") B) = OpenSBI + $(basename "$KERNEL") + $(basename "$DTB")"
echo "pack it:  stage0_pack.py --out boot.img --pc 0x80000000 --a1 0 $OUT/fw_payload.bin@0x80000000"
