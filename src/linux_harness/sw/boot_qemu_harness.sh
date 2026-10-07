#!/bin/bash
# The IMAGE lane's QEMU proof (IMAGE_CONTRACT §10): boots the SHIPPED artefacts
# (artifacts/Image with its embedded rootfs + fw_jump.bin) on qemu-system-riscv32
# -M virt, four times, against a virtio disk made by STAGE0's own
# stage0_mkcard.py (STAGE0_CONTRACT §6: p4 0xDA, p1/p2 0x7F slots, p3 0x83
# blank, boot-select at LBA 1/2):
#
#   boot 1  a blank p3 is NOT formatted: /persist is volatile, the boot is still
#           healthy (persist=degraded). Console (root password): mps3-slot reads
#           the card and sets default B (then STAGE0's checker must agree), and
#           `mps3-persist format --erase` formats p3 — explicitly. mps3-reboot.
#   boot 2  /persist on the card: a per-board host key generated; a TOFU claim
#           written the way harnessd's provider will (file + mps3-keys-sync);
#           SSH in with it and PIN the host key; password SSH refused;
#           /etc/mps3/net.conf edited; the harness process killed and
#           re-spawned by init. mps3-reboot.
#   boot 3  same card: SSH with the PINNED host key (StrictHostKeyChecking=yes)
#           must work — host key, claim and the /etc/mps3 edit persisted.
#   boot 4  NEGATIVE CONTROL: mps3.persist=off: the host key must be new, so
#           the pinned SSH must be REFUSED. A pin check that cannot fail proves
#           nothing.
#
# THE BOARD IDENTITY (lane IDENT, 2026-09-28): boot 1 resolves the image defaults
# (no UIO = no stage0 block) and S41 puts the identity's MAC on eth0 before the
# link comes up (virtio's 52:54:00:12:34:56 -> 02:00:00:4d:50:53); boot 2 sets an
# override with `mps3-identity set`; boot 3 must come up AS that board (label,
# hostname, MAC on eth0, static address, identify's label/mac); boot 4
# (persist=off) must NOT read it (the image defaults again).
#
# What -M virt cannot prove: anything on the MPS3 buses (uartlite, INTC, LAN9220,
# UIO, usd_spi/mmc_spi) — those need the board (B1). The virtio disk stands in
# for the microSD (mps3.persist_disk=/dev/vda), found by the same MBR rule.
#
#   MPS3_QEMU=<qemu-system-riscv32> ./boot_qemu_harness.sh
set -uo pipefail
cd "$(dirname "$0")" || exit 2
SW=$PWD
REPO=$(cd ../../.. && pwd)
ART=${MPS3_ARTIFACTS:-$SW/artifacts}
LOG=$SW/qemu_harness_boot.log
T_BOOT=${T_BOOT:-300}
P_SSH=${P_SSH:-16922}
P_CTRL=${P_CTRL:-16900}
P_ID=${P_ID:-16899}

QEMU=${MPS3_QEMU:-$(command -v qemu-system-riscv32 || true)}
[ -n "$QEMU" ] && [ -x "$QEMU" ] || { echo "no qemu-system-riscv32: set MPS3_QEMU"; exit 2; }
for f in Image fw_jump.bin; do
    [ -f "$ART/$f" ] || { echo "MISSING $ART/$f -- run build.sh first"; exit 2; }
done
PW=${MPS3_ROOT_PASSWD:-$(cat "$ART/ROOT_PASSWD" 2>/dev/null || true)}
[ -n "$PW" ] || { echo "no root password (\$MPS3_ROOT_PASSWD or $ART/ROOT_PASSWD)"; exit 2; }

W=$(mktemp -d)
QPID=""
trap '' PIPE          # a write to a dead VM's console FIFO must not kill the proof
cleanup() { [ -n "$QPID" ] && kill "$QPID" 2>/dev/null; exec 3>&- 2>/dev/null; rm -rf "$W"; }
trap cleanup EXIT
: > "$LOG"
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "  PASS  $1" | tee -a "$LOG"; }
bad() { fail=$((fail + 1)); echo "  FAIL  $1" | tee -a "$LOG"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

echo "== DTS gates" | tee -a "$LOG"
python3 "$REPO/tools/dts_gates.py" >> "$LOG" 2>&1 && ok "tools/dts_gates.py" || bad "tools/dts_gates.py (see $LOG)"

# --- the card, made by STAGE0's own tool ------------------------------------
F0=$SW/../../linux_soc/hw/fw_stage0
DISK=$W/usd.img
head -c 8192 /dev/urandom > "$W/payload"
python3 "$F0/stage0_pack.py" --out "$W/slot.img" --pc 0x80000000 --a1 0 "$W/payload@0x80000000" >/dev/null \
    && python3 "$F0/stage0_mkcard.py" card --slot-a "$W/slot.img" --slot-b same --out-dir "$W/card" \
        --card-img "$DISK" --card-mib 256 >/dev/null \
    || { echo "stage0_mkcard.py could not make the card image"; exit 2; }
ssh-keygen -q -t ed25519 -N "" -C "qemu-proof" -f "$W/id"
PUB=$(cat "$W/id.pub")
KH=$W/known_hosts
SSH_BASE=(-p "$P_SSH" -i "$W/id" -o IdentitiesOnly=yes -o PasswordAuthentication=no
          -o ConnectTimeout=10 -o UserKnownHostsFile="$KH" -o LogLevel=ERROR)

boot() {   # boot <tag> <extra kernel args>
    BL=$W/boot_$1.log; : > "$BL"
    FIFO=$W/fifo_$1; mkfifo "$FIFO"
    "$QEMU" -M virt -m 256M -nographic -no-reboot \
        -bios "$ART/fw_jump.bin" -kernel "$ART/Image" \
        -append "console=ttyS0 $2" \
        -drive "file=$DISK,format=raw,id=hd0,if=none" -device virtio-blk-device,drive=hd0 \
        -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$P_SSH-:22,hostfwd=tcp:127.0.0.1:$P_CTRL-:6900,hostfwd=udp:127.0.0.1:$P_ID-:6899" \
        -device virtio-net-device,netdev=n0 \
        < "$FIFO" > "$BL" 2>&1 &
    QPID=$!
    exec 3> "$FIFO"
}
waitlog() {   # waitlog <regex> <seconds>
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
console() {   # console <cmd> <marker> — runs cmd, then echoes the marker
    printf '%s; echo %s_$((1+1))\n' "$1" "$2" >&3
    waitlog "$2_2" 60
}
halt_vm() {
    kill -0 "$QPID" 2>/dev/null && printf 'reboot -f\n' >&3 2>/dev/null
    local i=0
    while kill -0 "$QPID" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
    kill "$QPID" 2>/dev/null; wait "$QPID" 2>/dev/null; QPID=""
    exec 3>&-
    cat "$BL" >> "$LOG"
}
ssh_vm() {   # ssh_vm [-o OPT]... [REMOTE COMMAND] — options before the host, command after
    local opts=()
    while [ "${1:-}" = -o ]; do opts+=("$1" "$2"); shift 2; done
    ssh "${SSH_BASE[@]}" "${opts[@]}" root@127.0.0.1 "$@"
}
ctrl_line() {   # one 6900 request line -> the reply line (harnessd's control port)
    python3 - "$P_CTRL" "$1" <<'PY'
import socket, sys
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
s.sendall(sys.argv[2].encode() + b"\n")
buf = b""
try:
    while not buf.endswith(b"\n"):
        d = s.recv(1024)
        if not d: break
        buf += d
except socket.timeout:
    pass
print(buf.decode(errors="replace").strip())
PY
}
identify() {    # UDP 6899 identify -> the reply datagram
    python3 - "$P_ID" <<'PY'
import socket, sys
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(3)
for _ in range(4):
    u.sendto(b'{"op":"identify","v":1,"nonce":"0123abcd"}', ("127.0.0.1", int(sys.argv[1])))
    try:
        print(u.recvfrom(1500)[0].decode(errors="replace")); break
    except socket.timeout:
        pass
PY
}

# printk time after /init (B1 2026-09-24): the last kernel timestamp in a boot log
# must be LATER than "Run /init"'s. On silicon b1_v2 every line from riscv-pmu-sbi
# on read 24.431304 (MBV `time` == mcycle, frozen by SBI PMU COUNTER_STOP). QEMU's
# `time` is not mcycle, so this cannot reproduce THAT coupling -- mps3_image.py
# kconfig-gate is the real guard -- but it catches any clock that stops at init.
init_clock_runs() {
    python3 - "$1" <<'PY'
import re, sys
t = open(sys.argv[1], errors="replace").read()
ts = [(float(m.group(1)), m.group(2)) for m in re.finditer(r"\[\s*(\d+\.\d+)\]\s?([^\r\n]*)", t)]
i = next((k for k, (_, s) in enumerate(ts) if "Run /init" in s), None)
sys.exit(0 if i is not None and len(ts) > i + 1 and ts[-1][0] > ts[i][0] else 1)
PY
}
printf '[   24.411915] riscv-pmu-sbi: x\n[   24.431304] Run /init as init process\n[   24.431304] spi_usd: y\n' > "$W/frozen.log"

# ============================== boot 1 ======================================
echo "== boot 1: a harness card with a blank p3" | tee -a "$LOG"
# mps3.wdkick_mem: S00mps3wdkick maps a file as the watchdog page (no fabric here)
boot 1 "mps3.persist_disk=/dev/vda mps3.wdkick_mem=/tmp/wdkick.page"
if console_login; then ok "boot 1 reached a console login (root password)"; else bad "boot 1: no console login"; fi
check "no riscv-pmu-sbi in the booted kernel (MBV: its COUNTER_STOP freezes time)" "! grep -q 'riscv-pmu-sbi' '$BL'"
check "printk time advances after Run /init (and a frozen log FAILS the same test)" \
      "init_clock_runs '$BL' && ! init_clock_runs '$W/frozen.log'"
check "blank p3 NOT formatted automatically" "grep -q '/dev/vda3 is blank' '$BL' && ! grep -qi 'formatted /dev/vda3' '$BL'"
# S41/S99 run in the background (the network no longer delays harnessd's first
# WDOG kick), so the verdict can land after the login: wait for it.
check "healthy=1 with a volatile /persist"  "waitlog 'mps3health: healthy=1 persist=degraded' 60"
check "wdkick bridges from the first rcS script, BEFORE harnessd starts" \
      "grep -q 'wdkick: bridging until harnessd (max 150 s)' '$BL' && [ \$(grep -n -m1 'wdkick: bridging' '$BL' | cut -d: -f1) -lt \$(grep -n -m1 'MPS3 shell services on Linux' '$BL' | cut -d: -f1) ]"
check "wdkick stops once harnessd runs (harnessd owns the watchdog from then on)" \
      "waitlog 'wdkick: harnessd up at [0-9.]* s, stopping' 30 && [ \$(grep -n -m1 'MPS3 shell services on Linux' '$BL' | cut -d: -f1) -lt \$(grep -n -m1 'wdkick: harnessd up' '$BL' | cut -d: -f1) ]"
console "od -An -tx4 -N8 /tmp/wdkick.page | sed 's/^/WDPAGE=/'" WDP
check "wdkick kicked (TWCSR0 = EWDT1|WDS) and never wrote TWCSR1" "grep -Eq 'WDPAGE= *00000006 +a5a5a5a5' '$BL'"
check "harnessd started BEFORE the network settled (first WDOG kick not behind S41)" \
      "[ \$(grep -n -m1 'MPS3 shell services on Linux' '$BL' | cut -d: -f1) -lt \$(grep -n -m1 'mps3health:' '$BL' | cut -d: -f1) ]"
console "cat /run/mps3/identity | sed 's/^/ID1:/'; cat /sys/class/net/eth0/address | sed 's/^/MAC1=/'; grep -h '^mac' /run/mps3/net.state | sed 's/^/NS1:/'" ID1
check "identity resolved before S41 (S13 line first), the image defaults, no stage0 window" \
      "grep -q 'ID1:MPS3_LABEL=MPS3' '$BL' && grep -q 'ID1:MPS3_STAGE0=nowindow' '$BL' && grep -q 'ID1:MPS3_IP=192.168.10.101/24' '$BL' && [ \$(grep -n -m1 'mps3-identity: MPS3 (mps3)' '$BL' | cut -d: -f1) -lt \$(grep -n -m1 'mps3net:' '$BL' | cut -d: -f1) ]"
check "eth0 carries the identity's MAC, set before link up (was virtio's 52:54:00:12:34:56)" \
      "grep -q 'MAC1=02:00:00:4d:50:53' '$BL' && grep -q 'NS1:mac_set=ok' '$BL' && grep -q 'mps3net: eth0 MAC 02:00:00:4d:50:53 (was 52:54:00:12:34:56)' '$BL'"
console "mps3-slot --disk /dev/vda status; mps3-slot --disk /dev/vda default B" SLOT
check "mps3-slot reads the card (slot A S0LB, default A)" "grep -q '^slot A: .*S0LB' '$BL' && grep -q 'default A (seq 1' '$BL'"
check "mps3-slot default B"                 "grep -q 'mps3-slot: default slot B (seq 2' '$BL'"
console "mps3-persist format --erase" FMT
check "explicit mps3-persist format"        "grep -q 'mps3-persist: formatted /dev/vda3' '$BL'"
console "MPS3_REBOOT_WAIT=2 mps3-reboot" RB1
i=0; while kill -0 "$QPID" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
halt_vm
check "STAGE0's checker reads the boot-select Linux wrote (default B)" \
      "python3 '$F0/stage0_mkcard.py' check '$DISK' | grep -q 'default slot B (boot-select seq 2)'"

# ============================== boot 2 ======================================
echo "== boot 2: /persist on the card" | tee -a "$LOG"
boot 2 "mps3.persist_disk=/dev/vda"
if console_login; then ok "boot 2 reached a console login"; else bad "boot 2: no console login"; fi
check "per-board host key generated"        "grep -q \"generated this board's ed25519 host key\" '$BL'"
check "S99 health: healthy=1 persist=card"  "waitlog 'mps3health: healthy=1 persist=card' 60"
console "cat /run/mps3/persist.state /run/mps3/boot-health /run/mps3/net.state; sed -n 's/^/HOSTKEY=/p' /run/mps3/ssh/host_key_sha256" STATE2
check "/persist is the card (backing=card dev=/dev/vda3)" "grep -q 'backing=card dev=/dev/vda3' '$BL'"
check "DHCP lease + 192.168.10.101 secondary" "grep -q '^dhcp=1' '$BL' && grep -q 'static101=added' '$BL'"
FP1=$(sed -n 's/.*HOSTKEY=\(SHA256:[A-Za-z0-9+/]*\).*/\1/p' "$BL" | head -1)
# the TOFU claim, exactly as harnessd's config_agent provider writes it
console "mkdir -p /persist/ssh && echo '$PUB' > /persist/ssh/authorized_keys.tmp && mv /persist/ssh/authorized_keys.tmp /persist/ssh/authorized_keys && /usr/sbin/mps3-keys-sync" CLAIM
check "claim written + keys synced"         "grep -q 'mps3-keys-sync: baked=0 claimed=1' '$BL'"
out=$(ssh_vm -o StrictHostKeyChecking=accept-new 'cat /run/mps3/ssh/host_key_sha256' 2>&1)
check "SSH with the claimed key"            "[ '$out' = '$FP1' ]"
PINNED=$(ssh-keygen -l -E sha256 -f "$KH" 2>/dev/null | awk '{print $2}' | head -1)
clk=$(ssh_vm 'echo MPS3_CLK_A > /dev/kmsg; sleep 2; echo MPS3_CLK_B > /dev/kmsg; dmesg | grep MPS3_CLK_' 2>/dev/null \
      | sed -n 's/^\[ *\([0-9.]*\)\].*MPS3_CLK_\([AB]\).*/\2 \1/p' \
      | awk '$1=="A"{a=$2} $1=="B"{b=$2} END{if (a!="" && b!="") printf "%.3f", b-a}')
check "sched_clock runs under userspace: /dev/kmsg marks 2 s apart read ${clk:-?} s apart" \
      "awk -v d='${clk:-0}' 'BEGIN{exit !(d >= 1.5 && d <= 10)}'"
check "pinned host key == the key the board published ($FP1)" "[ -n '$FP1' ] && [ '$PINNED' = '$FP1' ]"
methods=$(ssh -v -p "$P_SSH" -o PubkeyAuthentication=no -o PreferredAuthentications=password \
               -o NumberOfPasswordPrompts=0 -o BatchMode=yes -o UserKnownHostsFile="$KH" \
               root@127.0.0.1 true 2>&1 | sed -n 's/.*Authentications that can continue: //p' | head -1 | tr -d '\r ')
check "password SSH refused (server offers: ${methods:-?})" "[ '$methods' = publickey ]"
NONCE=$RANDOM$RANDOM
ssh_vm "echo '# QEMU_PERSIST_PROBE=$NONCE' >> /etc/mps3/net.conf" >/dev/null 2>&1
check "/etc/mps3/net.conf edited"           "ssh_vm 'grep -q QEMU_PERSIST_PROBE=$NONCE /etc/mps3/net.conf'"
pidcmd='if [ -x /usr/sbin/mps3-harnessd ]; then pidof mps3-harnessd; else cat /run/mps3/harnessd.pid; fi'
p1=$(ssh_vm "$pidcmd" 2>/dev/null)
ssh_vm "kill $p1" >/dev/null 2>&1
sleep 4
p2=$(ssh_vm "$pidcmd" 2>/dev/null)
check "harness process re-spawned by init after kill ($p1 -> $p2)" \
      "[ -n '$p1' ] && [ -n '$p2' ] && [ '$p1' != '$p2' ] && ssh_vm 'kill -0 $p2'"
check "harnessd came up in NO-HW mode (no UIO under -M virt), not a crash loop" "grep -q 'NO-HW MODE' '$BL'"
ctrl_line '{"op":"ping"}' > "$W/ctrl.txt" 2>&1
check "6900 answers ($(head -c 80 "$W/ctrl.txt"))" "grep -q '\"err\":\"no fabric' '$W/ctrl.txt'"
identify > "$W/ident.txt" 2>&1
check "6899 identify: mode nohw, impl linux, nonce echoed" \
      "grep -q '\"mode\":\"nohw\"' '$W/ident.txt' && grep -q '\"impl\":\"linux\"' '$W/ident.txt' && grep -q '0123abcd' '$W/ident.txt'"
check "identify.ssh = claimed + the board's first-boot host key" \
      "grep -q '\"claimed\":true' '$W/ident.txt' && grep -qF '$FP1' '$W/ident.txt'"
echo "   identify: $(cat "$W/ident.txt")" >> "$LOG"
check "version manifest present (harnessd=$(ssh_vm "sed -n 's/^harnessd_sha256=//p' /etc/mps3/version" 2>/dev/null))" \
      "ssh_vm 'grep -q ^format=1 /etc/mps3/version'"
GBV=$(sed -n 's/^greybox_clear_sha256=//p' "$ART/version")
SIDV=$(sed -n 's/^static_id=//p' "$ART/version")
if [ -n "$GBV" ] && [ "$GBV" != none ]; then
    gbs=$(ssh_vm 'sha256sum /etc/mps3/greybox_clear.bin' 2>/dev/null | cut -d' ' -f1)
    check "the static's greybox clearing is in the booted image (sha256 == version's; for $SIDV)" \
          "[ '$gbs' = '$GBV' ] && grep -qix 'greybox_clear_static_id=$SIDV' '$ART/version'"
else
    echo "   (no greybox clearing in this image: $SIDV)" | tee -a "$LOG"
fi
idset=$(ssh_vm 'mps3-identity set label=QEMU-9 hostname=qemu-nine ip=10.0.2.99/24 mac=02:00:00:00:09:09; echo rc=$?; mps3-identity set mac=01:00:00:00:00:00; echo rc=$?' 2>&1)
echo "   identity set: $idset" >> "$LOG"
check "mps3-identity set writes the override (and refuses a multicast MAC, rc 2)" \
      "echo \"\$idset\" | grep -q 'override written' && echo \"\$idset\" | grep -q '^rc=0' && echo \"\$idset\" | grep -q '^rc=2' && ssh_vm 'grep -q MPS3_LABEL=QEMU-9 /persist/etc/mps3/identity'"
ssh_vm 'MPS3_REBOOT_WAIT=2 mps3-reboot' >/dev/null 2>&1
i=0; while kill -0 "$QPID" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
check "mps3-reboot ended the VM (reboot -f; QEMU -no-reboot)" "! kill -0 $QPID 2>/dev/null"
halt_vm

# ============================== boot 3 ======================================
echo "== boot 3: same card — everything must have persisted" | tee -a "$LOG"
boot 3 "mps3.persist_disk=/dev/vda"
waitlog 'mps3health:' "$T_BOOT"
check "no new host key on the third boot"   "! grep -q \"generated this board's\" '$BL'"
out=$(ssh_vm -o StrictHostKeyChecking=yes 'cat /run/mps3/ssh/host_key_sha256' 2>&1)
check "SSH with the PINNED host key (StrictHostKeyChecking=yes) + the persisted claim" "[ '$out' = '$FP1' ]"
check "the /etc/mps3 edit persisted"        "ssh_vm -o StrictHostKeyChecking=yes 'grep -q QEMU_PERSIST_PROBE=$NONCE /etc/mps3/net.conf'"
check "image-owned version not persisted"   "ssh_vm -o StrictHostKeyChecking=yes '[ ! -e /persist/etc-mps3/upper/version ]'"
id3=$(ssh_vm -o StrictHostKeyChecking=yes 'cat /run/mps3/identity /run/mps3/net.state; echo HOST=$(hostname); echo ETH=$(cat /sys/class/net/eth0/address); ip -4 addr show dev eth0' 2>&1)
echo "$id3" | sed 's/^/   boot3: /' >> "$LOG"
check "boot 3 is the overridden board: label/hostname/ip/mac from the override" \
      "echo \"\$id3\" | grep -q '^MPS3_LABEL_SRC=override' && echo \"\$id3\" | grep -q '^HOST=qemu-nine' && echo \"\$id3\" | grep -q '^ETH=02:00:00:00:09:09'"
check "S41 took the identity's static address (10.0.2.99/24 added, not .101)" \
      "echo \"\$id3\" | grep -q '^static=10.0.2.99/24' && echo \"\$id3\" | grep -q 'inet 10.0.2.99/24' && ! echo \"\$id3\" | grep -q '192.168.10.101'"
identify > "$W/ident3.txt" 2>&1
check "identify reports the label and the MAC (no fabric: nohw)" \
      "grep -q '\"label\":\"QEMU-9\"' '$W/ident3.txt' && grep -q '\"mac\":\"020000000909\"' '$W/ident3.txt'"
halt_vm

# ============================== boot 4 ======================================
echo "== boot 4: NEGATIVE CONTROL — no persistence (mps3.persist=off)" | tee -a "$LOG"
boot 4 "mps3.persist_disk=/dev/vda mps3.persist=off"
waitlog 'mps3health:' "$T_BOOT"
check "tmpfs /persist generates a fresh key" "grep -q \"generated this board's ed25519 host key\" '$BL'"
out=$(ssh_vm -o StrictHostKeyChecking=yes true 2>&1)
check "pinned SSH REFUSED on a different host key [negative control]" \
      "echo \"\$out\" | grep -qi 'host key verification failed\\|REMOTE HOST IDENTIFICATION HAS CHANGED'"
check "persist=off: the card's identity override is NOT read (the image defaults) [negative control]" \
      "grep -q 'mps3-identity: MPS3 (mps3) 192.168.10.101/24 02:00:00:4d:50:53' '$BL' && grep -q 'override none (no persistent /persist)' '$BL'"
halt_vm

echo "================ VERDICT: $pass PASS, $fail FAIL ================" | tee -a "$LOG"
[ $fail = 0 ]
