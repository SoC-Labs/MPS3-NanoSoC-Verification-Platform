#!/bin/bash
# Build mbv_soc.dtb from mbv_soc.dts, and ASSERT the boot memory map is actually
# consistent with the artifacts we intend to load. An OpenSBI/kernel/DTB/initrd
# address collision is the classic silent boot hang -- it produces no error, just a
# dead serial port. So we compute it instead of trusting it.
#
# The kernel and DTB load addresses are NOT free parameters: OpenSBI 1.6 fw_jump is
# built FW_PIC for platform=generic, and its next-stage addresses are baked in
# PC-relative to its own load address:
#     fw_next_addr = _fw_start + 0x00400000   (kernel)
#     fw_next_arg1 = _fw_start + 0x02200000   (DTB)
# (read out with objdump; see mbv_soc.dts header). Loading OpenSBI at the DDR base
# therefore fixes the other two.
#
# Usage: ./mk_dtb.sh
set -euo pipefail
cd "$(dirname "$0")"

DTS=mbv_soc.dts
DTB=mbv_soc.dtb
ART=artifacts

# --- the contract (hw/ADDRESS_MAP.md sec.1 + the fw_jump offsets above) -------
DDR_BASE=$((0x80000000))
DDR_SIZE=$((0x40000000))            # 1 GiB
SBI_LOAD=$DDR_BASE                  # 0x8000_0000
KERNEL_LOAD=$((SBI_LOAD + 0x400000))   # 0x8040_0000 -- FORCED by fw_next_addr
DTB_LOAD=$((SBI_LOAD + 0x2200000))     # 0x8220_0000 -- FORCED by fw_next_arg1
INITRD_LOAD=$((0x84000000))            # free choice, must clear everything above
DDR_END=$((DDR_BASE + DDR_SIZE))

for f in "$ART/Image" "$ART/rootfs.cpio.gz" "$ART/fw_jump.bin"; do
    [ -f "$f" ] || { echo "MISSING $f"; exit 1; }
done

# --- real sizes off the real artifacts ----------------------------------------
INITRD_SIZE=$(stat -c%s "$ART/rootfs.cpio.gz")
SBI_SIZE=$(stat -c%s "$ART/fw_jump.bin")
INITRD_END=$((INITRD_LOAD + INITRD_SIZE))

# The kernel's *effective* size is the Image header's image_size field (offset 16,
# u64 LE) -- it INCLUDES bss, which the file size does not. Using the file size here
# would under-count and hide the collision we are trying to catch.
KERNEL_SIZE=$(python3 -c "
import struct;d=open('$ART/Image','rb').read(32)
to,isz=struct.unpack_from('<QQ',d,8)
assert d[56:60]==b'RSC\x05' if len(d)>=60 else True
print(isz)")
TEXT_OFFSET=$(python3 -c "
import struct;print(struct.unpack_from('<Q',open('$ART/Image','rb').read(32),8)[0])")
KERNEL_END=$((KERNEL_LOAD + KERNEL_SIZE))

# --- stamp linux,initrd-end from the REAL rootfs size --------------------------
printf -v INITRD_END_HEX '0x%08x' "$INITRD_END"
printf -v INITRD_LOAD_HEX '0x%08x' "$INITRD_LOAD"
sed -i -E "s/(linux,initrd-start = <)0x[0-9a-fA-F]+(>;)/\1${INITRD_LOAD_HEX}\2/" "$DTS"
sed -i -E "s/(linux,initrd-end   = <)0x[0-9a-fA-F]+(>;)/\1${INITRD_END_HEX}\2/" "$DTS"

# --- build. -p 4096 pads the DTB: OpenSBI's fdt_reserved_memory_fixup() calls
#     fdt_open_into(fdt, fdt, totalsize + 1024) to append /reserved-memory for its
#     own PMP-protected firmware, i.e. it GROWS the blob in place. dtc emits a
#     tightly-packed blob (0 bytes slack) by default. -----------------------------
dtc -I dts -O dtb -p 4096 -o "$DTB" "$DTS" 2>&1 | sed 's/^/  dtc: /' || true
[ -f "$DTB" ] || { echo "dtc FAILED"; exit 1; }
DTB_SIZE=$(stat -c%s "$DTB")
DTB_END=$((DTB_LOAD + DTB_SIZE + 1024))   # +1024 = OpenSBI's in-place growth

# --- assert the map ------------------------------------------------------------
h() { printf '0x%08x' "$1"; }
fail=0
chk() { # chk <desc> <cond-result> <detail>
    if [ "$2" -eq 1 ]; then printf '  OK    %-46s %s\n' "$1" "$3"
    else printf '  FAIL  %-46s %s\n' "$1" "$3"; fail=1; fi
}
echo
echo "=============== linux_soc boot memory map ==============="
printf '  %-14s %s .. %s  (%s)\n' "OpenSBI"  "$(h $SBI_LOAD)"    "$(h $((SBI_LOAD+SBI_SIZE)))" "$(numfmt --to=iec $SBI_SIZE)"
printf '  %-14s %s .. %s  (%s, incl. bss)\n' "kernel"   "$(h $KERNEL_LOAD)" "$(h $KERNEL_END)"  "$(numfmt --to=iec $KERNEL_SIZE)"
printf '  %-14s %s .. %s  (%s + 1K sbi growth)\n' "DTB" "$(h $DTB_LOAD)"    "$(h $DTB_END)"     "$(numfmt --to=iec $DTB_SIZE)"
printf '  %-14s %s .. %s  (%s)\n' "initramfs" "$(h $INITRD_LOAD)" "$(h $INITRD_END)"  "$(numfmt --to=iec $INITRD_SIZE)"
printf '  %-14s %s .. %s\n'       "DDR"       "$(h $DDR_BASE)"    "$(h $DDR_END)"
echo "--------------------------------------------------------"
chk "OpenSBI fits below the kernel"      "$([ $((SBI_LOAD+SBI_SIZE)) -le $KERNEL_LOAD ] && echo 1 || echo 0)" "$(numfmt --to=iec $((KERNEL_LOAD-SBI_LOAD-SBI_SIZE))) spare"
chk "kernel 4 MiB-aligned (Sv32 superpage)" "$([ $((KERNEL_LOAD % 0x400000)) -eq 0 ] && echo 1 || echo 0)" "$(h $KERNEL_LOAD)"
chk "kernel load == DDR_BASE + text_offset" "$([ $KERNEL_LOAD -eq $((DDR_BASE+TEXT_OFFSET)) ] && echo 1 || echo 0)" "text_offset=$(h $TEXT_OFFSET)"
chk "kernel (incl bss) does not eat the DTB" "$([ $KERNEL_END -le $DTB_LOAD ] && echo 1 || echo 0)" "$(numfmt --to=iec $((DTB_LOAD-KERNEL_END))) margin  <-- THE TIGHT ONE"
chk "DTB does not overlap the initramfs"  "$([ $DTB_END -le $INITRD_LOAD ] && echo 1 || echo 0)" "$(numfmt --to=iec $((INITRD_LOAD-DTB_END))) margin"
chk "initramfs fits in DDR"               "$([ $INITRD_END -le $DDR_END ] && echo 1 || echo 0)" "$(numfmt --to=iec $((DDR_END-INITRD_END))) spare"
chk "DTB has slack for OpenSBI's fixup"   "$([ $DTB_SIZE -ge 1024 ] && echo 1 || echo 0)" "$DTB_SIZE bytes"
echo "--------------------------------------------------------"

# --- the DTB must agree with what we just asserted ------------------------------
# NB: fdtget prints a u32 cell as a SIGNED int32, so 0x84000000 comes back negative.
# Mask back to unsigned before comparing (bash arithmetic is 64-bit signed).
got_start=$(( $(fdtget "$DTB" /chosen linux,initrd-start) & 0xffffffff ))
got_end=$((   $(fdtget "$DTB" /chosen linux,initrd-end)   & 0xffffffff ))
chk "DTB chosen/linux,initrd-start" "$([ "$got_start" -eq "$INITRD_LOAD" ] && echo 1 || echo 0)" "$(h $got_start)"
chk "DTB chosen/linux,initrd-end"   "$([ "$got_end" -eq "$INITRD_END" ] && echo 1 || echo 0)"   "$(h $got_end)"
echo

if [ $fail -ne 0 ]; then
    echo "BOOT MAP IS BROKEN -- refusing to install the DTB."
    exit 1
fi

cp "$DTB" "$ART/$DTB"
cp "$DTS" "$ART/$DTS"

# --- RESCUE DTB (2026-07-16 no-login-prompt board finding) ----------------------
# Identical blob, ONE change: bootargs += " init=/bin/sh". Rationale: busybox getty
# CANNOT print its prompt without a working CLOCKEVENT -- it runs alarm(5)+tcdrain
# (msleep loop) and usleep(100ms) BEFORE the first prompt byte (loginutils/getty.c),
# so a dead/erratic Sstc stimecmp interrupt wedges it in nanosleep, silently.
# /bin/sh as PID1 performs NO pre-prompt sleep and its "# " prompt fits the 16-byte
# uartlite TX FIFO, so it comes up even if BOTH the timer irq and the external irq
# path are dead. It is the decisive one-boot discriminator for the board window --
# see hw/build_dbg/LOGIN_FIX_README.md for the decision tree it feeds.
# Derived from the just-built blob with fdtput so it can never drift from the DTS.
RESCUE=mbv_soc_rescue.dtb
cp "$DTB" "$RESCUE"
BOOTARGS=$(fdtget "$RESCUE" /chosen bootargs)
case "$BOOTARGS" in
    *init=/bin/sh*) : ;;   # already rescue-flavoured (re-run)
    *) fdtput -t s "$RESCUE" /chosen bootargs "$BOOTARGS init=/bin/sh" ;;
esac
got_rescue=$(fdtget "$RESCUE" /chosen bootargs)
chk "rescue DTB bootargs carry init=/bin/sh" \
    "$(case "$got_rescue" in *init=/bin/sh*) echo 1;; *) echo 0;; esac)" "$got_rescue"
# fdtput REPACKS the blob to minimal size, stripping the -p 4096 slack that
# OpenSBI's fdt_reserved_memory_fixup() needs to grow the FDT in place (caught by
# the slack assert below on first run). Re-pad through dtc.
dtc -I dtb -O dtb -p 4096 -o "$RESCUE.tmp" "$RESCUE" 2>/dev/null && mv "$RESCUE.tmp" "$RESCUE"
chk "rescue DTB has slack for OpenSBI's fixup" \
    "$([ "$(stat -c%s "$RESCUE")" -ge $((DTB_SIZE - 512)) ] && echo 1 || echo 0)" "$(stat -c%s "$RESCUE") bytes"
# same size envelope as the primary blob (fdtput edits in place; the -p 4096 pad
# absorbs the few extra bytes) -- re-assert the initramfs boundary anyway.
RESCUE_SIZE=$(stat -c%s "$RESCUE")
chk "rescue DTB does not overlap the initramfs" \
    "$([ $((DTB_LOAD + RESCUE_SIZE + 1024)) -le $INITRD_LOAD ] && echo 1 || echo 0)" \
    "$RESCUE_SIZE bytes"
[ $fail -ne 0 ] && { echo "RESCUE DTB BROKEN -- refusing to install."; exit 1; }
cp "$RESCUE" "$ART/$RESCUE"

# The 2026-07-16 workaround artifacts (Image_nowfi wfi-coma fix + the plan-C
# set from mk_dtb_sbitimer.sh; see hw/build_dbg/WORKAROUND_BOOT_README.md) are
# checksummed too when present, so a rerun of this script never drops them.
( cd "$ART" && sha256sum Image fw_jump.bin fw_jump.elf mbv_soc.dtb mbv_soc_rescue.dtb mbv_soc.dts rootfs.cpio.gz rootfs.ext2 \
    $(for f in Image_nowfi Image_pland Image_irqfix fw_jump_sbitimer.bin mbv_soc_sbitimer.dtb mbv_soc_sbitimer_norxpoll.dtb mbv_soc_sbitimer_rescue.dtb mbv_soc_pland.dtb mbv_soc_pland_irquart.dtb mbv_soc_pland_rescue.dtb rootfs_diag.cpio.gz mbv_soc_pland_diag.dtb mbv_soc_pland_irquart_diag.dtb; do [ -f "$f" ] && echo "$f"; done) > SHA256SUMS )
echo "installed -> $ART/$DTB  ($DTB_SIZE bytes)"
echo "installed -> $ART/$RESCUE  ($RESCUE_SIZE bytes)  [bootargs: $got_rescue]"
echo
cat <<EOF
=============== board load recipe (XSDB) ===============
NOTE: load fw_jump.BIN, never \`elf load\` fw_jump.elf -- the ELF is linked at vaddr
0x0, which is the 128 KiB LMB BRAM, and it is $(numfmt --to=iec $SBI_SIZE). It would overrun the BRAM.

  connect
  targets -set -filter {name =~ "MicroBlaze RISC-V*"}
  rst -processor
  dow -data $ART/fw_jump.bin     $(h $SBI_LOAD)
  dow -data $ART/Image           $(h $KERNEL_LOAD)
  dow -data $ART/mbv_soc.dtb     $(h $DTB_LOAD)
  dow -data $ART/rootfs.cpio.gz  $(h $INITRD_LOAD)
  # OpenSBI takes its OWN fdt pointer from a1 (fw_prev_arg1 returns 0), so a1 MUST
  # be set. a0 = hartid = 0. Garbage in a1 => "fdt_check_header failed" => silent hang.
  rwr a0 0
  rwr a1 $(h $DTB_LOAD)
  rwr pc $(h $SBI_LOAD)
  con
EOF
