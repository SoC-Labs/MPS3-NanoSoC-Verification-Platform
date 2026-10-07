#!/bin/bash
# qemu_smoke.sh — the rv32 build of mps3-harnessd, run by a real rv32 Linux
# kernel under qemu-system-riscv32 -M virt, answering the wire through slirp
# hostfwd (HARNESSD_CONTRACT.md §8; plan §4 HARNESSD "done when").
#
# -M virt has no shell fabric, so the binary is build/rv32-mock (the MOCK HAL:
# the same behavioural fabric the host tests use), seeded with a valid stage0
# block (--mock-s0) matching the image's /etc/mps3/static_id. What this proves is
# the rv32 cross-build: it links, it runs on the MBV's ISA subset under a real
# kernel, its sockets/poll()/clock_gettime/mmap work there, and every contract
# port answers. The UIO build is proven on silicon (B1), not here.
#
# Inputs (read-only; nothing under them is written):
#   ART   an image dir with Image + fw_jump.bin + rootfs.ext2 (default: the July
#         harness artifacts in the linux worktree)
#   QEMU  qemu-system-riscv32 (default: the Buildroot-built one beside ART's tree)
# The rootfs is COPIED to a scratch dir, the v0.7 daemons' S-scripts are removed
# from the copy (they would hold 6900/6910), and harnessd is added with a
# one-line S99 script. Usage:  tests/qemu_smoke.sh [boot-timeout-s]
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
HD=$(cd "$HERE/.." && pwd)
LINUX_WT=${LINUX_WT:-$(cd "$HD/../../../.." && pwd)}
ART=${ART:-$LINUX_WT/src/linux_harness/sw/artifacts}
QEMU=${QEMU:-$LINUX_WT/src/linux_soc/linux/buildroot/output/host/bin/qemu-system-riscv32}
BIN=${BIN:-$HD/build/rv32-mock/mps3-harnessd}
T=${1:-300}
W=${W:-$(mktemp -d "${TMPDIR:-/tmp}/harnessd_qemu.XXXXXX")}
OFF=${OFF:-31000}
SID=0x2B082E1B

for f in "$ART/Image" "$ART/fw_jump.bin" "$ART/rootfs.ext2" "$QEMU" "$BIN"; do
    [ -e "$f" ] || { echo "MISSING $f"; exit 2; }
done
command -v debugfs >/dev/null || { echo "no debugfs (e2fsprogs)"; exit 2; }

cp "$ART/rootfs.ext2" "$W/rootfs.ext2"
# the image's greybox clearing (16 ICAP NOPs: streaming it is a hardware no-op)
python3 -c "import sys; sys.stdout.buffer.write(bytes([0x20,0,0,0])*16)" > "$W/greybox_clear.bin"
cat > "$W/S99harnessd" <<EOF
#!/bin/sh
# qemu_smoke: mps3-harnessd (rv32-mock) in place of the v0.7 daemons
[ "\$1" = start ] || exit 0
echo "$SID" > /etc/mps3/static_id
/usr/sbin/mps3-harnessd --mock-s0 $SID --netif eth0 --state-dir /tmp/mps3 \\
    --run-marker /tmp/harnessd.boot --authorized-keys /tmp/ak --boot-health /nonexistent \\
    > /dev/console 2>&1 &
echo "harnessd-smoke: started pid \$!"
EOF
# debugfs `write` creates its target in the CURRENT directory (a path is not
# accepted as the destination name), hence the cd's.
debugfs -w "$W/rootfs.ext2" > "$W/debugfs.log" 2>&1 <<EOF
rm /etc/init.d/S90mps3d
rm /etc/init.d/S91mps3aux
rm /etc/init.d/S91mps3clcd
rm /etc/init.d/S10mps3dfx
cd /usr/sbin
write $BIN mps3-harnessd
sif mps3-harnessd mode 0100755
cd /etc/mps3
write $W/greybox_clear.bin greybox_clear.bin
cd /etc/init.d
write $W/S99harnessd S99harnessd
sif S99harnessd mode 0100755
EOF
debugfs -R "stat /usr/sbin/mps3-harnessd" "$W/rootfs.ext2" 2>/dev/null | grep -q "Type: regular" \
    || { echo "could not inject harnessd into the rootfs copy (see $W/debugfs.log)"; exit 2; }

FWD="hostfwd=tcp:127.0.0.1:$((OFF+6900))-:6900,hostfwd=tcp:127.0.0.1:$((OFF+6910))-:6910"
FWD="$FWD,hostfwd=tcp:127.0.0.1:$((OFF+6921))-:6921,hostfwd=tcp:127.0.0.1:$((OFF+2542))-:2542"
FWD="$FWD,hostfwd=udp:127.0.0.1:$((OFF+6899))-:6899"
nice -n 19 "$QEMU" -M virt -m 256M -nographic -no-reboot \
    -bios "$ART/fw_jump.bin" -kernel "$ART/Image" \
    -append "rootwait root=/dev/vda rw console=ttyS0 mps3_net=dhcp" \
    -drive "file=$W/rootfs.ext2,format=raw,id=hd0,if=none" -device virtio-blk-device,drive=hd0 \
    -netdev "user,id=n0,$FWD" -device virtio-net-device,netdev=n0 \
    < /dev/null > "$W/boot.log" 2>&1 &
QPID=$!
trap 'kill $QPID 2>/dev/null; wait $QPID 2>/dev/null' EXIT

i=0
until grep -q "harnessd-smoke: started" "$W/boot.log"; do
    sleep 2; i=$((i + 2))
    if [ $i -ge "$T" ] || ! kill -0 $QPID 2>/dev/null; then
        echo "FAIL: harnessd never started (see $W/boot.log)"; tail -20 "$W/boot.log"; exit 1
    fi
done
echo "booted in ~${i}s; probing (logs: $W)"
sleep 5
python3 "$HERE/qemu_probe.py" --offset "$OFF" --static-id "$SID" | tee "$W/probe.log"
rc=${PIPESTATUS[0]}
exit $rc
