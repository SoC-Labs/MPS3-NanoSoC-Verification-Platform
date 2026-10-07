#!/bin/bash
# rehearse_first_install_qemu.sh — docs/LINUX_HARNESS.md §6.4 "First install on a
# blank card", rehearsed end to end on qemu-system-riscv32 -M virt, so the card
# set-up B2 does on the board is proven before board time
# (docs/planning/LINUX_SPEEDUP_2026-09-24.md #10).
#
# THE DOC IS RUN, NOT RETYPED. §6.3's claim line and every code block of §6.4
# are EXTRACTED from docs/LINUX_HARNESS.md and eval'd line by line as the PC
# operator, with HOME pointed at this run's scratch home (so ~/.ssh/... is this
# run's key and known-hosts file) and two shell functions in front:
#   ssh       = ssh -F <§2.1's stanza, without ProxyJump (as §6.3 says), aimed at
#               the forwarded port>
#   pyverify  = the REAL pyverify CLI; 192.168.10.101 -> 127.0.0.1 plus the port
#               flags slirp's hostfwd needs (--port/--push-port/--control-port/
#               --identify-port)
# A doc edit that breaks a step breaks this script.
#
# WHAT QEMU CANNOT DO — emulated, and every emulation is logged as an EMUL line:
#   E1 stage0 does not run on -M virt. §6.2's RAM rescue boot = QEMU -kernel Image.
#      "stage0 boots slot B" = STAGE0's own card reader (stage0_mkcard.py check)
#      must pick B and slot B's bytes must equal linux_slot.img; QEMU then boots
#      the same Image again (a slot image is an MBV FW_PAYLOAD, not a virt one).
#   E2 no shell fabric: the image's UIO mps3-harnessd runs NO-HW under QEMU (no
#      claim, no slot, no usd). The SAME sources' rv32 MOCK build (harnessd/
#      Makefile rv32-mock) replaces it on the console — init respawns it — with a
#      --mock-fabric seeded the way stage0 leaves the LMB tail (booted_from
#      RESCUE, later B) and --slot-disk/--usd-dev /dev/mmcblk0. The claim (TFTP),
#      slot and usd verbs then run the product code on the real kernel block device.
#   E3 the card is a sparse all-zero virtio disk (a truly blank card) at /dev/vda;
#      /dev/mmcblk0 -> vda makes the doc's device name work verbatim; S12 finds it
#      through mps3.persist_disk=/dev/vda (the board: card detect + /dev/mmcblk0).
#   E4 the MOCK exits instead of reboot(2), so `pyverify reboot --wait` would time
#      a respawn, not a reset: --wait is dropped and the reset is `reboot -f`.
#   E5 the operator's "yes" at ssh's first-connect prompt (§6.3: compare with
#      identify's host_key_sha256) = accept-new, then the recorded key's
#      fingerprint must equal identify's.
#   E6 only if the doc's `pyverify slot push` is cut by its 30 s send timeout
#      (QEMU's emulated ssh + card path is slow): the doc line stays a FAIL, and
#      the same push is re-sent through the same tunnel with pyverify's library
#      and a 900 s timeout, so the rest of the section can still be rehearsed.
# WORKAROUND (not an emulation: a pyverify defect, the doc line stays a FAIL):
#   `pyverify usd format` opens 6900 twice back to back and does not retry the
#   single-client accept-then-EOF refusal; the same request is then sent once on a
#   single connection with slot.py's retry, so the store half is still rehearsed.
#
# Reads only: $MPS3_ARTIFACTS (Image, fw_jump.bin, linux_slot.img, version,
# ROOT_PASSWD). Everything it writes is under a mktemp dir (kept with
# KEEP=1) and ./rehearse_first_install.log.
#
#   MPS3_QEMU=<qemu-system-riscv32> [MPS3_ARTIFACTS=<dir>] \
#   [MPS3_HARNESSD_MOCK=<rv32-mock mps3-harnessd> | MPS3_CROSS=<prefix>] \
#       ./rehearse_first_install_qemu.sh
set -uo pipefail
cd "$(dirname "$0")" || exit 2
SW=$PWD
REPO=$(cd ../../.. && pwd)
DOC=${MPS3_DOC:-$REPO/docs/LINUX_HARNESS.md}
ART=${MPS3_ARTIFACTS:-$SW/artifacts}
F0=$REPO/src/linux_soc/hw/fw_stage0
LOG=${MPS3_REHEARSE_LOG:-$SW/rehearse_first_install.log}
T_BOOT=${T_BOOT:-300}
PB=${MPS3_PORT_BASE:-17000}
P_SSH=$((PB + 22)); P_TFTP=$((PB + 69)); P_ID=$((PB + 899)); P_CTRL=$((PB + 900)); P_PUSH=$((PB + 910))
CARD_MIB=${CARD_MIB:-1024}

QEMU=${MPS3_QEMU:-$(command -v qemu-system-riscv32 || true)}
[ -n "$QEMU" ] && [ -x "$QEMU" ] || { echo "no qemu-system-riscv32: set MPS3_QEMU"; exit 2; }
for f in Image fw_jump.bin linux_slot.img version; do
    [ -f "$ART/$f" ] || { echo "MISSING $ART/$f -- run build.sh first"; exit 2; }
done
[ -f "$DOC" ] || { echo "MISSING $DOC"; exit 2; }
PW=${MPS3_ROOT_PASSWD:-$(cat "$ART/ROOT_PASSWD" 2>/dev/null || true)}
[ -n "$PW" ] || { echo "no root password (\$MPS3_ROOT_PASSWD or $ART/ROOT_PASSWD)"; exit 2; }
SID=$(sed -n 's/^static_id=//p' "$ART/version")
[ -n "$SID" ] || { echo "$ART/version carries no static_id"; exit 2; }

W=$(mktemp -d "${TMPDIR:-/tmp}/rehearse_6_4.XXXXXX")
QPID=""
trap '' PIPE
cleanup() {
    [ -n "$QPID" ] && kill "$QPID" 2>/dev/null
    exec 3>&- 2>/dev/null
    if [ "${KEEP:-0}" = 1 ]; then echo "kept: $W"; else rm -rf "$W"; fi
}
trap cleanup EXIT
: > "$LOG"
pass=0; fail=0
ok()   { pass=$((pass + 1)); echo "  PASS  $1" | tee -a "$LOG"; }
bad()  { fail=$((fail + 1)); echo "  FAIL  $1" | tee -a "$LOG"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
emul() { echo "  EMUL  $1" | tee -a "$LOG"; }
say()  { echo "$1" | tee -a "$LOG"; }

# ---- the rv32 MOCK harnessd (E2): given, or built from THIS tree's sources ----
HDM=${MPS3_HARNESSD_MOCK:-}
if [ -z "$HDM" ]; then
    CROSS=${MPS3_CROSS:-$SW/build/toolchain_wrapper/bin/riscv32-amd-linux-gnu-}
    [ -x "${CROSS}gcc" ] || { echo "no rv32 cross gcc at ${CROSS}gcc: set MPS3_CROSS or MPS3_HARNESSD_MOCK"; exit 2; }
    nice -n 19 make -s -C "$SW/harnessd" rv32-mock BUILD="$W/hd" CROSS="$CROSS" > "$W/hd.log" 2>&1 \
        || { cat "$W/hd.log"; echo "could not build the rv32-mock harnessd"; exit 2; }
    HDM=$W/hd/rv32-mock/mps3-harnessd
fi
[ -f "$HDM" ] || { echo "MISSING $HDM"; exit 2; }

# ---- the PC side: a scratch HOME, the ssh alias, pyverify ---------------------
mkdir -p "$W/home/.ssh" "$W/pc" && chmod 700 "$W/home/.ssh"
ssh-keygen -q -t ed25519 -N "" -C "rehearse-6.4" -f "$W/home/.ssh/id_ed25519"
KH=$W/home/.ssh/known_hosts_mps3_linux
# §2.1's stanza, from pyverify itself, "without its ProxyJump line" (§6.3), aimed
# at the forwarded port; ~ paths made absolute (ssh expands ~ from the passwd
# entry, not $HOME)
PYTHONPATH="$REPO/host/pyverify" python3 -m pyverify.cli ssh --config-stanza \
    | sed -e '/ProxyJump/d' -e 's/HostName .*/HostName 127.0.0.1/' \
          -e "s#~/#$W/home/#" -e "/HostName/a\\    Port $P_SSH" \
          -e '/ServerAlive/d' > "$W/ssh_config"
grep -q "^Host mps3-linux" "$W/ssh_config" && grep -q "HostKeyAlias mps3-linux" "$W/ssh_config" \
    || { echo "pyverify ssh --config-stanza gave no usable mps3-linux stanza"; exit 2; }
export MPS3_SSH="ssh -F $W/ssh_config"      # pyverify slot's tunnel (§6.5) uses the same alias
ssh() { command ssh -F "$W/ssh_config" "$@"; }
pyverify() {
    local verb=$1; shift
    local a=() x
    for x in "$@"; do
        case "$x" in
            192.168.10.101) a+=(127.0.0.1) ;;
            --wait) if [ "$verb" = reboot ]; then
                        emul "E4: 'pyverify reboot --wait': the MOCK exits instead of resetting -- --wait dropped, reset = reboot -f"
                        continue
                    fi; a+=("$x") ;;
            *) a+=("$x") ;;
        esac
    done
    case "$verb" in
        claim)      a+=(--port "$P_TFTP") ;;
        identify)   a+=(--port "$P_ID") ;;
        slot)       # a claimed board's push/commit/rollback ride pyverify's ssh tunnel to the
                    # BOARD's own 127.0.0.1:6900/6910 (--port names the board's port there);
                    # everything else reaches the forwarded ports directly
                    a+=(--identify-port "$P_ID")
                    if ! { case " $* " in *" --no-ssh "*) false ;; *" push "*|*" commit "*|*" rollback "*) true ;; *) false ;; esac \
                           && identify_json | grep -q '"claimed":true'; }; then
                        a+=(--port "$P_CTRL" --push-port "$P_PUSH")
                    fi ;;
        reboot|usd) a+=(--control-port "$P_CTRL") ;;
    esac
    PYTHONPATH="$REPO/host/pyverify" python3 -m pyverify.cli "$verb" "${a[@]}"
}
ln -s "$REPO/src" "$W/pc/src"
ln -s "$(cd "$ART" && pwd)/linux_slot.img" "$W/pc/linux_slot.img"
ln -s "$(cd "$ART" && pwd)/version" "$W/pc/version"     # pyverify slot push: the provenance beside the image

# doc_block SECTION N -> the Nth fenced block of "### SECTION" in the doc
doc_block() {
    python3 - "$DOC" "$1" "$2" <<'PYDOC'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
sec, n = sys.argv[2], int(sys.argv[3])
m = re.search(r"^### " + re.escape(sec) + r" [^\n]*\n(.*?)(?=^#{2,3} |\Z)", text, re.M | re.S)
if not m:
    sys.exit("no section %s in %s" % (sec, sys.argv[1]))
blocks = re.findall(r"^```[^\n]*\n(.*?)^```", m.group(1), re.M | re.S)
if len(blocks) < n:
    sys.exit("section %s has %d code block(s); wanted #%d" % (sec, len(blocks), n))
sys.stdout.write(blocks[n - 1])
PYDOC
}
# run_doc TAG LINE -> rc; output in $W/out_TAG. One doc line, verbatim, as the operator.
run_doc() {
    local out=$W/out_$1
    echo "  \$ $2" | tee -a "$LOG"
    ( export HOME="$W/home"; cd "$W/pc" && eval "$2" ) < /dev/null > "$out" 2>&1
    local rc=$?
    sed 's/^/      | /' "$out" >> "$LOG"
    return $rc
}
# run_block SECTION N TAG -> every line of that block must exit 0
run_block() {
    local blk i=0 line rc=0
    blk=$(doc_block "$1" "$2") || { bad "§$1 block $2: $blk"; return 1; }
    while IFS= read -r line; do
        [ -n "${line// }" ] || continue
        i=$((i + 1))
        if run_doc "$3_$i" "$line"; then ok "§$1 block $2 line $i exits 0"; else bad "§$1 block $2 line $i exited non-zero"; rc=1; fi
    done <<< "$blk"
    return $rc
}
lastjson() { grep '^{' "$W/out_$1" | tail -1; }
jq_py() {   # jq_py FILE EXPR -> python expression over the JSON object j
    python3 -c "import json,sys; j=json.loads(open(sys.argv[1]).read() or '{}'); print($2)" "$1" 2>/dev/null
}

# ---- the card (E3) and the stand-in's disk (E2) --------------------------------
DISK=$W/usd.img
truncate -s "${CARD_MIB}M" "$DISK"
IMG_CRC=$(python3 -c "import struct,sys; print('0x%08x' % struct.unpack_from('<I', open(sys.argv[1],'rb').read(32), 28)[0])" "$ART/linux_slot.img")
mkdir -p "$W/tools"
cp "$HDM" "$W/tools/mps3-harnessd.mock"
cat > "$W/tools/harnessd-standin" <<'STANDIN'
#!/bin/sh
# rehearse_first_install_qemu.sh E2: the rv32 MOCK mps3-harnessd (same sources as
# the image's UIO build), on the card the doc names. init respawns this.
exec /tmp/qr/mps3-harnessd.mock --mock-fabric /tmp/qr/fabric \
    --slot-disk /dev/mmcblk0 --usd-dev /dev/mmcblk0 "$@"
STANDIN
python3 - "$SW/harnessd/tests" "$W/tools" "$SID" "$IMG_CRC" <<'PYFAB'
import sys
sys.path.insert(0, sys.argv[1])
from harnessd_mock import Fabric
out, sid, crc = sys.argv[2], int(sys.argv[3], 0), int(sys.argv[4], 0)
# stage0's LMB-tail status block as it leaves it (STAGE0_CONTRACT §3; stage0_status.h)
for name, frm, dflt in (("rescue", 3, 1), ("B", 2, 2)):
    f = Fabric(out + "/fabric_" + name)
    f.s0_write_block(sid)
    f.wr(0x1F000, 0xE00 + 0x20, frm)      # booted_from: S0_FROM_RESCUE / S0_FROM_B
    f.wr(0x1F000, 0xE00 + 0x44, frm)      # att_from
    f.wr(0x1F000, 0xE00 + 0x3C, dflt)     # default_slot
    f.wr(0x1F000, 0xE00 + 0x90, crc)      # image_hdr_crc: the image handed off
    f.close()
PYFAB
tar -cf "$W/tools.tar" -C "$W/tools" . || exit 2
truncate -s 16M "$W/tools.tar"

regions() {   # the card's regions (STAGE0_CONTRACT §6) -> "name=sha" lines
    python3 - "$DISK" <<'PYREG'
import hashlib, sys
R = {"mbr": (0, 1), "bootsel": (1, 2), "p4": (2048, 65536), "slotA": (67584, 131072),
     "slotB": (198656, 131072), "p3": (329728, 8192)}
with open(sys.argv[1], "rb") as f:
    for k, (lba, n) in R.items():
        f.seek(lba * 512)
        print("%s=%s" % (k, hashlib.sha256(f.read(n * 512)).hexdigest()[:16]))
PYREG
}
moved() { diff <(echo "$1") <(echo "$2") | sed -n 's/^> \([a-zA-Z0-9]*\)=.*/\1/p' | tr '\n' ' ' | sed 's/ $//'; }

boot() {   # boot <tag> <fabric>
    BL=$W/boot_$1.log; : > "$BL"
    FIFO=$W/fifo_$1; mkfifo "$FIFO"
    nice -n 19 "$QEMU" -M virt -m 256M -nographic -no-reboot \
        -bios "$ART/fw_jump.bin" -kernel "$ART/Image" \
        -append "console=ttyS0 mps3.persist_disk=/dev/vda" \
        -drive "file=$DISK,format=raw,id=hd0,if=none" -device virtio-blk-device,drive=hd0 \
        -drive "file=$W/tools.tar,format=raw,id=hd1,if=none,readonly=on" -device virtio-blk-device,drive=hd1 \
        -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$P_SSH-:22,hostfwd=tcp:127.0.0.1:$P_CTRL-:6900,hostfwd=tcp:127.0.0.1:$P_PUSH-:6910,hostfwd=udp:127.0.0.1:$P_ID-:6899,hostfwd=udp:127.0.0.1:$P_TFTP-:69" \
        -device virtio-net-device,netdev=n0 \
        < "$FIFO" > "$BL" 2>&1 &
    QPID=$!
    exec 3> "$FIFO"
    FABRIC=$2
}
waitlog() {
    local i=0
    while ! grep -qE "$1" "$BL"; do
        sleep 1; i=$((i + 1))
        [ $i -ge "$2" ] && return 1
        kill -0 "$QPID" 2>/dev/null || return 1
    done
}
console_login() {
    waitlog '(^|[[:cntrl:]])[[:alnum:]_.-]+ login:' "$T_BOOT" || return 1
    sleep 1; printf 'root\n' >&3
    waitlog 'Password:' 20 || return 1
    sleep 1; printf '%s\n' "$PW" >&3
    sleep 2; printf 'echo LOGIN_$((6*7))\n' >&3
    waitlog 'LOGIN_42' 30
}
console() { printf '%s; echo %s_$((1+1))\n' "$1" "$2" >&3; waitlog "$2_2" "${3:-60}"; }
halt_vm() {
    kill -0 "$QPID" 2>/dev/null && printf 'reboot -f\n' >&3 2>/dev/null
    local i=0
    while kill -0 "$QPID" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
    kill "$QPID" 2>/dev/null; wait "$QPID" 2>/dev/null; QPID=""
    exec 3>&-
    { echo "---- console, boot $1 ----"; cat "$BL"; } >> "$LOG"
}
identify_json() {   # -> the identify reply, one line (raw UDP: pyverify identify prints a summary)
    python3 - "$P_ID" <<'PYID'
import socket, sys
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(2)
for _ in range(5):
    u.sendto(b'{"op":"identify","v":1,"nonce":"6a4b0c1d"}', ("127.0.0.1", int(sys.argv[1])))
    try:
        print(u.recvfrom(1500)[0].decode(errors="replace")); break
    except socket.timeout:
        pass
PYID
}
standin() {   # E2 on the console: the MOCK harnessd in place of the NO-HW UIO one
    emul "E2: rv32 MOCK mps3-harnessd in place of the UIO build (fabric: stage0 left booted_from=$FABRIC)"
    emul "E3: /dev/mmcblk0 -> $CARDDEV (the doc's device name)"
    console "mkdir -p /tmp/qr && tar -xf \$(cat /tmp/tools_is) -C /tmp/qr && cp /tmp/qr/fabric_$FABRIC /tmp/qr/fabric && ln -sf \$(cat /tmp/card_is) /dev/mmcblk0 && echo STANDIN_READY" TOOLS 60
    console "old=\$(pidof mps3-harnessd); cp /tmp/qr/harnessd-standin /usr/sbin/.hd && chmod 755 /usr/sbin/.hd && mv /usr/sbin/.hd /usr/sbin/mps3-harnessd && kill \$old" SWAP
    local i=0
    while [ $i -lt 30 ]; do
        identify_json > "$W/ident.json" 2>/dev/null
        grep -q '"mode":"run"' "$W/ident.json" && return 0
        sleep 1; i=$((i + 1))
    done
    return 1
}
first_connect() {   # E5: the operator's "yes" at ssh's first-connect prompt, then compare
    local fp
    ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes mps3-linux true > "$W/fc.out" 2>&1
    fp=$(ssh-keygen -l -E sha256 -F mps3-linux -f "$KH" 2>/dev/null | awk '/SHA256:/{print $3}' | head -1)
    emul "E5: first connect accepted (operator's yes); recorded ${fp:-nothing}, identify says $1"
    [ -n "$fp" ] && [ "$fp" = "$1" ]
}

# Which virtio disk is the card? boot() creates the card first, which this
# QEMU names vda (the first run had it the other way round: the stand-in's tar
# became S12's "card"). The card is the one with CARD_MIB's size, the stand-in's
# disk the other. Asked on every boot: the answer must be /dev/vda, where the
# cmdline points S12.
find_card() {
    console "for d in vda vdb; do if [ \"\$(cat /sys/block/\$d/size)\" = $((CARD_MIB * 2048)) ]; then echo /dev/\$d > /tmp/card_is; else echo /dev/\$d > /tmp/tools_is; fi; done; echo CARD=\$(cat /tmp/card_is)" CARDQ
    CARDDEV=$(tr -d '\r' < "$BL" | sed -n 's/^CARD=\(\/dev\/vd[ab]\)$/\1/p' | tail -1)
    [ "$CARDDEV" = /dev/vda ]
}

say "== §6.4 rehearsal: $(date -u +%FT%TZ), doc $DOC"
say "   artefacts $ART (static_id $SID, slot image table CRC $IMG_CRC), card ${CARD_MIB} MiB all-zero"
say "   repo $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "not a git tree"), mock $HDM"

# ============================ boot 1: §6.2 + §6.3 + §6.4 =========================
say "== boot 1: rescue boot into RAM on a BLANK card (§6.2), claim (§6.3), lay the card out (§6.4)"
emul "E1: §6.2 'stage0_push.py 192.168.10.101 linux_slot.img' = QEMU -kernel Image (stage0 does not run on -M virt)"
boot 1 rescue
if console_login; then ok "boot 1 reached a console login"; else bad "boot 1: no console login"; fi
check "the all-zero card is /dev/vda, the disk S12 was pointed at" "find_card"
console "cat /run/mps3/persist.state" PST1
check "blank card: /persist is tmpfs (§6.3 'on tmpfs until the next reboot')" "grep -q 'backing=tmpfs' '$BL'"
identify_json > "$W/ident0.json"
check "the image's own harnessd is NO-HW under QEMU (why E2 exists)" "grep -q '\"mode\":\"nohw\"' '$W/ident0.json'"
if standin; then ok "stand-in harnessd up: identify mode run"; else bad "stand-in harnessd never answered identify with mode run"; fi
if grep -q '^greybox_clear_sha256=[0-9a-f]\{64\}$' "$ART/version"; then
    check "harnessd loaded the image's greybox clearing (no 'first swap ... will fail closed')" \
          "tr -d '\r' < '$BL' | grep -q '^greybox: [0-9]* B, crc32 0x' && ! grep -q 'will fail closed' '$BL'"
fi
cp "$W/ident.json" "$W/ident1.json"
FP1=$(jq_py "$W/ident1.json" "j['ssh']['host_key_sha256']")
check "identify: unclaimed, impl linux, a host key ($FP1)" \
      "[ \"\$(jq_py '$W/ident1.json' \"j['ssh']['claimed']\")\" = False ] && grep -q '\"impl\":\"linux\"' '$W/ident1.json' && [ -n '$FP1' ]"

# §6.3 — claim it, and ssh in
CLAIM=$(doc_block 6.3 1 | grep -m1 'pyverify claim') || true
[ -n "$CLAIM" ] || bad "§6.3 has no 'pyverify claim' line"
run_doc claim1 "$CLAIM"; rc=$?
check "§6.3 claim accepted (exit 0)" "[ $rc = 0 ]"
identify_json > "$W/ident1c.json"
check "identify: now claimed" "grep -q '\"claimed\":true' '$W/ident1c.json'"
run_doc claim1b "$CLAIM"; rc=$?
check "a second claim is refused, exit 1 (§6.3) [negative control]" "[ $rc = 1 ]"
check "first ssh: the fingerprint ssh shows == identify's host_key_sha256" "first_connect '$FP1'"
check "ssh mps3-linux works with the claimed key (key-only)" "ssh -o BatchMode=yes mps3-linux true"

# §6.4 block 1 — the layout, written through ssh
BEFORE=$(regions)
run_block 6.4 1 mk
AFTER=$(regions)
check "card/mbr.bin is on LBA 0 and card/bootsel.bin on LBA 1-2" \
      "cmp -s -n 512 '$W/pc/card/mbr.bin' '$DISK' && cmp -s -n 1024 -i 0:512 '$W/pc/card/bootsel.bin' '$DISK'"
check "p3 now holds ext4 (magic 0xEF53 at byte 1080)" \
      "[ \"\$(dd if='$DISK' bs=1 skip=\$((329728 * 512 + 1080)) count=2 2>/dev/null | od -An -tx1 | tr -d ' \\n')\" = 53ef ]"
check "only the MBR, boot-select and p3 moved (moved: $(moved "$BEFORE" "$AFTER"))" \
      "[ '$(moved "$BEFORE" "$AFTER")' = 'mbr bootsel p3' ]"
run_doc persist1 "ssh mps3-linux 'cat /run/mps3/persist.state; mountpoint /persist; df -h /persist | tail -1'"
check "the running system is untouched: /persist still tmpfs" "grep -q 'backing=tmpfs' '$W/out_persist1'"
python3 "$F0/stage0_mkcard.py" check "$DISK" > "$W/s0check1" 2>&1; rc=$?
sed 's/^/      | /' "$W/s0check1" >> "$LOG"
check "STAGE0's reader: default A, nothing bootable yet -> rescue (exit 1)" \
      "[ $rc = 1 ] && grep -q 'default slot A' '$W/s0check1'"

# §6.4 block 2 — push, commit, reboot
run_doc st1 "pyverify slot status --host 192.168.10.101"
check "slot status before the push: running rescue, default A, target B" \
      "[ \"\$(jq_py '$W/out_st1' \"(j.get('running'), j.get('default'), j.get('target'))\")\" = \"('rescue', 'A', 'B')\" ]"
blk=$(doc_block 6.4 2) || bad "§6.4 has no second code block (push/commit/reboot)"
PUSH=$(grep -m1 'slot push' <<< "$blk"); COMMIT=$(grep -m1 'slot commit' <<< "$blk"); REBOOT=$(grep -m1 'pyverify reboot' <<< "$blk")
t0=$(date +%s)
run_doc push "$PUSH"; rc=$?
t1=$(date +%s)
check "§6.4 push exits 0 (claimed -> ssh tunnel by itself; $((t1 - t0)) s)" "[ $rc = 0 ] && grep -q 'ssh tunnel via mps3-linux' '$W/out_push'"
lastjson push > "$W/push.json"
if [ $rc != 0 ] && grep -q 'timed out' "$W/out_push"; then
    # pyverify's push_slot_image() hands its 30 s default to tcp_send(), and since
    # Python 3.5 a socket timeout bounds the WHOLE sendall(): the image must reach
    # the board at >= size/30 s end to end, or the push is cut.
    say "   NOTE  the doc's push was cut after $((t1 - t0)) s: pyverify slot push's 30 s socket timeout bounds the whole send (needs >= $(( $(stat -c %s "$ART/linux_slot.img") / 30 / 1024 )) KiB/s)"
    emul "E6: QEMU's emulated ssh + card path is slower than that -- the SAME push through the SAME tunnel with a 900 s send timeout (pyverify's library), to carry on"
    ( export HOME="$W/home"; PYTHONPATH="$REPO/host/pyverify" python3 - "$ART/linux_slot.img" "$SID" > "$W/push.json" 2> "$W/out_push6" <<'PYPUSH'
import json, sys, time
from pyverify.slot import SshTunnel, push_slot_image, slot_status, wait_job
img = open(sys.argv[1], "rb").read()
with SshTunnel("mps3-linux", (6900, 6910)) as t:
    c, p = t.local(6900), t.local(6910)
    deadline = time.monotonic() + 300          # let the cut push's job settle first
    while slot_status("127.0.0.1", port=c).get("job", {}).get("state") in ("writing", "verifying") \
            and time.monotonic() < deadline:
        time.sleep(1)
    t0 = time.monotonic()
    push_slot_image(img, "127.0.0.1", static_id=int(sys.argv[2], 0), port=p, timeout_s=900)
    t1 = time.monotonic()
    st = wait_job("127.0.0.1", port=c, timeout_s=900)
    t2 = time.monotonic()
    print("E6 push: send %.0f s (%.0f KiB/s), card read-back %.0f s" % (t1 - t0, len(img) / 1024 / (t1 - t0), t2 - t1),
          file=sys.stderr)
    print(json.dumps(st))
PYPUSH
    ) ; sed 's/^/      | /' "$W/out_push6" >> "$LOG"; sed -n 's/^\(E6 push: .*\)/   \1/p' "$W/out_push6" | tee -a "$LOG"
fi
check "push: staged B, slot B verified by read-back" \
      "[ \"\$(jq_py '$W/push.json' \"(j.get('staged'), j['b'].get('verified'))\")\" = \"('B', 'readback')\" ]"
run_doc commitneg "pyverify slot commit --host 192.168.10.101 --no-ssh"; rc=$?
check "commit from the network (--no-ssh) on a claimed board is refused [negative control]" \
      "[ $rc = 1 ] && grep -q 'slot locked' '$W/out_commitneg'"
run_doc commit "$COMMIT"; rc=$?
lastjson commit > "$W/commit.json"
check "§6.4 commit exits 0: default B" "[ $rc = 0 ] && [ \"\$(jq_py '$W/commit.json' \"j.get('default')\")\" = B ]"
PRE_REBOOT=$(regions)
python3 "$F0/stage0_mkcard.py" check "$DISK" > "$W/s0check2" 2>&1; rc=$?
sed 's/^/      | /' "$W/s0check2" >> "$LOG"
emul "E1: stage0's boot decision = stage0_mkcard.py check (STAGE0's reader) on the card"
check "STAGE0's reader: default B (seq 2), stage0 would boot slot B" \
      "[ $rc = 0 ] && grep -q 'default slot B (boot-select seq 2)' '$W/s0check2' && grep -q 'stage0 would boot slot B' '$W/s0check2'"
check "slot B holds linux_slot.img byte for byte; slot A is still empty" \
      "cmp -s -n \$(stat -c %s '$ART/linux_slot.img') -i 0:\$((198656 * 512)) '$ART/linux_slot.img' '$DISK' && grep -q 'slot A @LBA 67584: NOT bootable' '$W/s0check2'"
run_doc reboot1 "$REBOOT"; rc=$?
check "§6.4 reboot verb accepted (exit 0)" "[ $rc = 0 ]"
emul "E4: the reset: reboot -f (QEMU -no-reboot)"
halt_vm 1

# ============================ boot 2: stage0 boots slot B =======================
say "== boot 2: stage0 boots slot B (E1); /persist on the card; claim again; the store"
emul "E1: stage0 hands off slot B = QEMU boots the same Image (fabric: booted_from=B, image_hdr_crc $IMG_CRC)"
boot 2 B
if console_login; then ok "boot 2 reached a console login"; else bad "boot 2: no console login"; fi
check "a host key generated on this boot (the one the board keeps)" "grep -q \"generated this board's ed25519 host key\" '$BL'"
console "cat /run/mps3/persist.state" PST2
check "/persist is the card now (backing=card dev=/dev/vda3)" "grep -q 'backing=card dev=/dev/vda3' '$BL'"
find_card || bad "boot 2: the card is not /dev/vda"
if standin; then ok "stand-in harnessd up (boot 2)"; else bad "stand-in harnessd down (boot 2)"; fi
cp "$W/ident.json" "$W/ident2.json"
FP2=$(jq_py "$W/ident2.json" "j['ssh']['host_key_sha256']")
check "identify: unclaimed (the RAM boot's claim was tmpfs), a NEW host key ($FP2)" \
      "grep -q '\"claimed\":false' '$W/ident2.json' && [ -n '$FP2' ] && [ '$FP2' != '$FP1' ]"
run_doc st2 "pyverify slot status --host 192.168.10.101"
check "slot status: running B, default B, B verified by the boot, next target A" \
      "[ \"\$(jq_py '$W/out_st2' \"(j.get('running'), j.get('default'), j['b'].get('verified'), j.get('target'))\")\" = \"('B', 'B', 'boot', 'A')\" ]"
blk3=$(doc_block 6.4 3) || bad "§6.4 has no third code block (claim again, known_hosts, usd format)"
CLAIM2=$(grep -m1 'pyverify claim' <<< "$blk3"); KGEN=$(grep -m1 'ssh-keygen' <<< "$blk3"); USDF=$(grep -m1 'usd format' <<< "$blk3")
run_doc claim2 "$CLAIM2"; rc=$?
check "§6.4 claim again: accepted (exit 0)" "[ $rc = 0 ]"
run_doc sshold "ssh -o BatchMode=yes mps3-linux true"; rc=$?
check "ssh REFUSES the new host key before the known_hosts fix (§6.4) [negative control]" \
      "[ $rc != 0 ] && grep -qi 'host key verification failed\\|REMOTE HOST IDENTIFICATION HAS CHANGED' '$W/out_sshold'"
run_doc kgen "$KGEN"; rc=$?
check "§6.4 ssh-keygen -R mps3-linux exits 0" "[ $rc = 0 ]"
check "first ssh after it: fingerprint == identify's new host_key_sha256" "first_connect '$FP2'"
run_doc slot2 "ssh mps3-linux mps3-slot status"
check "on the board, mps3-slot agrees: default B" "grep -q -- '-> default B' '$W/out_slot2'"
run_doc usd0 "pyverify usd status --host 192.168.10.101"
check "usd before the format: the MBR's 0xDA partition, never formatted -> foreign" \
      "[ \"\$(jq_py '$W/out_usd0' \"j.get('state')\")\" = foreign ]"
PRE_USD=$(regions)
run_doc usdf "$USDF"; rc=$?
check "§6.4 usd format exits 0" "[ $rc = 0 ]"
if [ $rc != 0 ] && grep -q 'closed by peer' "$W/out_usdf"; then
    # `pyverify usd format` reads the state on one 6900 connection and sends the
    # format on a SECOND one straight after. 6900 is single-client: it answers a new
    # connection accept-then-EOF until it has noticed the previous one close, and the
    # usd path (ShellClient) does not retry that refusal the way slot.py's _one_line
    # does -- so the doc line loses the race (a re-run just loses it again).
    say "   NOTE  6900 refused the format's second connection (single-client accept-then-EOF; the usd CLI does not retry it)"
    say "   WORKAROUND the same request, {\"op\":\"usd\",\"action\":\"format\",\"confirm\":\"erase\"}, on one connection with slot.py's refusal retry"
    PYTHONPATH="$REPO/host/pyverify" python3 - "$P_CTRL" > "$W/out_usdf2" 2>&1 <<'PYUSD'
import json, sys
from pyverify.client import USD_CONFIRM_FORMAT
from pyverify.slot import _one_line
print(json.dumps(_one_line("127.0.0.1", int(sys.argv[1]),
                           {"op": "usd", "action": "format", "confirm": USD_CONFIRM_FORMAT}, 30.0)))
PYUSD
    sed 's/^/      | /' "$W/out_usdf2" >> "$LOG"
    check "the format request itself is accepted when sent on one connection" \
          "grep -q '\"ok\": true' '$W/out_usdf2'"
fi
i=0
while [ $i -lt 30 ]; do
    run_doc usd1 "pyverify usd status --host 192.168.10.101" >/dev/null
    st=$(jq_py "$W/out_usd1" "j.get('state')")
    [ "$st" = init ] || break
    sleep 1; i=$((i + 1))
done
check "usd after the format: empty (a store, no default yet)" "[ '$st' = empty ]"
POST_USD=$(regions)
check "the format moved p4 and nothing else of the layout (moved: $(moved "$PRE_USD" "$POST_USD"))" \
      "[ '$(moved "$PRE_USD" "$POST_USD" | sed 's/ *p3//')' = p4 ]"
check "boot 2 never touched the MBR, boot-select or either slot" \
      "[ -z '$(moved "$PRE_REBOOT" "$POST_USD" | sed -e 's/p3//' -e 's/p4//' | tr -d ' ')' ]"
halt_vm 2

# ============================ boot 3: everything stays =========================
say "== boot 3: same card, no operator action -- the claim, host key and store must have stayed"
boot 3 B
if console_login; then ok "boot 3 reached a console login"; else bad "boot 3: no console login"; fi
check "no new host key on boot 3" "! grep -q \"generated this board's\" '$BL'"
find_card || bad "boot 3: the card is not /dev/vda"
if standin; then ok "stand-in harnessd up (boot 3)"; else bad "stand-in harnessd down (boot 3)"; fi
cp "$W/ident.json" "$W/ident3.json"
check "identify: still claimed, same host key" \
      "grep -q '\"claimed\":true' '$W/ident3.json' && [ \"\$(jq_py '$W/ident3.json' \"j['ssh']['host_key_sha256']\")\" = '$FP2' ]"
run_doc ssh3 "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes mps3-linux cat /run/mps3/persist.state"
check "ssh with the pinned key, no re-claim; /persist on the card" "grep -q 'backing=card' '$W/out_ssh3'"
run_doc usd3 "pyverify usd status --host 192.168.10.101"
check "the store stayed formatted: empty" "[ \"\$(jq_py '$W/out_usd3' \"j.get('state')\")\" = empty ]"
halt_vm 3

say "================ VERDICT: $pass PASS, $fail FAIL ================"
[ $fail = 0 ]
