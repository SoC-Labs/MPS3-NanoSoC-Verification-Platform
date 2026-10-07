#!/bin/bash
# Build the rv32 Linux stack for linux_soc: vendor-toolchain wrapper -> Buildroot
# (kernel + glibc userland + OpenSBI + host-qemu) -> board DTB -> artifacts/.
#
# Buildroot is a long build (~30-60 min on 16 cores). Everything it needs is cached in
# buildroot/dl/ after the first run.
set -euo pipefail
cd "$(dirname "$0")"

# Buildroot hard-refuses a '.' (or an empty entry, which MEANS '.') in LD_LIBRARY_PATH.
# The lab env ships one with a trailing ':' -- strip it rather than clobber the var.
export LD_LIBRARY_PATH="$(printf '%s' "${LD_LIBRARY_PATH:-}" | tr ':' '\n' | grep -vE '^\.?$' | paste -sd: -)"

JOBS=${JOBS:-$(nproc)}

echo "== 1/4  toolchain wrapper (fixes the vendor gcc's broken default ISA/ABI)"
./mk_toolchain_wrapper.sh

echo "== 2/4  buildroot"
cp configs/mbv_linux_defconfig buildroot/configs/mbv_linux_defconfig
make -C buildroot BR2_EXTERNAL="$PWD/br2_external" BR2_DEFCONFIG=configs/mbv_linux_defconfig mbv_linux_defconfig

# Assert the ISA before burning an hour: F/D here would build a userland that boots in
# QEMU (which has an FPU) and illegal-instruction-traps on the FPU-less core.
CFLAGS_OUT=$(make -C buildroot -s printvars VARS=TOOLCHAIN_EXTERNAL_CFLAGS)
echo "   $CFLAGS_OUT"
case "$CFLAGS_OUT" in
    *rv32imac_zicsr_zifencei*ilp32*) : ;;
    *) echo "FATAL: ISA/ABI is not rv32imac/ilp32 -- refusing to build."; exit 1 ;;
esac
case "$CFLAGS_OUT" in
    *f*d*c_zicsr*|*imafd*) echo "FATAL: F/D in -march -- see configs/mbv_linux_defconfig"; exit 1 ;;
esac

make -C buildroot BR2_EXTERNAL="$PWD/br2_external" -j"$JOBS"

echo "== 3/4  board device tree"
dtc -I dts -O dtb -o mbv_soc.dtb mbv_soc.dts

echo "== 4/4  artifacts"
mkdir -p artifacts
cp buildroot/output/images/Image           artifacts/
cp buildroot/output/images/rootfs.cpio.gz  artifacts/
cp buildroot/output/images/rootfs.ext2     artifacts/
cp buildroot/output/images/fw_jump.elf     artifacts/   # OpenSBI (M-mode) for qemu-virt
cp mbv_soc.dtb                             artifacts/
cp mbv_soc.dts                             artifacts/
( cd artifacts && sha256sum ./* > SHA256SUMS )
ls -la artifacts/

echo
echo "Now: ./boot_qemu.sh   (boots artifacts on qemu-system-riscv32 -M virt)"
