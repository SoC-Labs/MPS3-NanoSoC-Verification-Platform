#!/bin/bash
# qemu_mps3_debug_check.sh -- the on-board GDB server on the SHIPPED image, in QEMU
# (-M virt: no fabric, so the product harnessd idles in NO-HW mode). One boot,
# persist=off, no card:
#
#   1. the image: /usr/bin/openocd is 0.12.0 with remote_bitbang + hostio4 adapters and
#      the ahb_qspi flash driver; the `openocd` user (uid != 0, /bin/false);
#      `mps3-debug version --json` names the contract, the OpenOCD and the designs
#   2. the refusals on the real image: `up` (auto) reads identify on 127.0.0.1:6899 --
#      NO-HW reports rm_id 0x00000000 = greybox -> exit 13 no_dap; `up --rm nanosoc`
#      runs OpenOCD as `openocd`, which finds nothing on 6921 (NO-HW serves no JTAG)
#      -> exit 6, log_tail naming the connect
#   3. THE LIVE PATH: the rv32 MOCK harnessd (the same sources, a behavioural fabric
#      with a fake TAP behind JTAGBB, IDCODE 0x6BA00477) pushed over SSH and started on
#      +10000 ports; `mps3-debug up --rm nanosoc` against its 16921/16899: the image's
#      OpenOCD, as `openocd`, reads the IDCODE through harnessd's own jtag_server
#      (tap.idcode 0x6ba00477 in the JSON). The fake TAP has no DAP, so OpenOCD's
#      `dap init` then fails and the session ends "failed" -- expected here; a DAP is
#      the board's to prove.
#   4. `status` / `down` leave nothing running.
#
#   MPS3_ARTIFACTS=<art> MPS3_QEMU=<qemu-system-riscv32> OUT=<dir> \
#   [HD_MOCK=<rv32-mock mps3-harnessd> | CROSS=<riscv32-...-linux-gnu- prefix>] \
#       ./qemu_mps3_debug_check.sh
set -uo pipefail
cd "$(dirname "$0")" || exit 2
SW=$PWD
ART=${MPS3_ARTIFACTS:?}
QEMU=${MPS3_QEMU:?}
OUT=${OUT:?}
P_SSH=${P_SSH:-19822}
T_BOOT=${T_BOOT:-300}
PW=$(cat "$ART/ROOT_PASSWD")
mkdir -p "$OUT"
W=$(mktemp -d -p "$OUT")
QPID=""
trap '' PIPE
cleanup() { [ -n "$QPID" ] && kill "$QPID" 2>/dev/null; exec 3>&- 2>/dev/null; rm -rf "$W"; }
trap cleanup EXIT
BL=$OUT/qemu_mps3_debug_boot.log; : > "$BL"
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "  PASS  $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL  $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
info() { echo "  INFO  $1"; }

# the rv32 MOCK harnessd for step 3
if [ -z "${HD_MOCK:-}" ]; then
    CROSS=${CROSS:-$SW/build/buildroot/output/host/bin/riscv32-amd-linux-gnu-}
    GIT_DIR=. nice -n 19 make -s -C "$SW/harnessd" rv32-mock BUILD="$W/hdb" CROSS="$CROSS" \
        RV_CC="${CROSS}gcc" > "$W/hdmock_build.log" 2>&1 \
        || { cat "$W/hdmock_build.log"; echo "cannot build the rv32-mock harnessd"; exit 2; }
    HD_MOCK=$W/hdb/rv32-mock/mps3-harnessd
fi
[ -x "$HD_MOCK" ] || { echo "no rv32-mock harnessd at $HD_MOCK"; exit 2; }

ssh-keygen -q -t ed25519 -N "" -C "mps3-debug-check" -f "$W/id"
SSH=(ssh -p "$P_SSH" -i "$W/id" -o IdentitiesOnly=yes -o PasswordAuthentication=no
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o LogLevel=ERROR)

FIFO=$W/fifo; mkfifo "$FIFO"
"$QEMU" -M virt -m 256M -nographic -no-reboot \
    -bios "$ART/fw_jump.bin" -kernel "$ART/Image" \
    -append "console=ttyS0 mps3.persist=off mps3.wdkick_mem=/tmp/wdkick.page" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$P_SSH-:22" -device virtio-net-device,netdev=n0 \
    < "$FIFO" > "$BL" 2>&1 &
QPID=$!
exec 3> "$FIFO"
waitlog() { local i=0; while ! grep -qE "$1" "$BL"; do sleep 1; i=$((i + 1));
            [ $i -ge "$2" ] && return 1; kill -0 "$QPID" 2>/dev/null || return 1; done; }
console() { printf '%s; echo %s_$((1+1))\n' "$1" "$2" >&3; waitlog "$2_2" "${3:-120}"; }
jline() {   # the JSON printed after MARK= on the console (a prompt may precede it)
    grep -ao "$1=.*" "$BL" | tail -1 | sed "s/^$1=//" | tr -d '\r'
}
jget() {    # jget <json> <python expr on j>
    python3 -c 'import json,sys; j=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2" 2>/dev/null
}

echo "== boot (persist=off, no card)"
waitlog '(^|[[:cntrl:]])[[:alnum:]_.-]+ login:' "$T_BOOT" && sleep 1 && printf 'root\n' >&3 \
    && waitlog 'Password:' 30 && sleep 1 && printf '%s\n' "$PW" >&3 && sleep 2 \
    && printf 'stty -echo; echo LOGIN_$((6*7))\n' >&3 && waitlog 'LOGIN_42' 30 \
    && ok "console login" || { bad "no console login"; tail -20 "$BL"; exit 1; }
waitlog 'mps3health: healthy=1' 120 && ok "boot healthy=1" || bad "boot not healthy"

echo "== 1. the image"
console 'echo "VER=$(mps3-debug version --json)"' V1
V=$(jline VER)
v_head=$(jget "$V" '(j["schema"], j["openocd"]["adapter"], j["openocd"]["present"])')
v_ver=$(jget "$V" 'j["openocd"]["version"]')
v_bad=$(jget "$V" 'j["designs_bad_lines"]')
v_des=$(jget "$V" '[(d["name"], d["dap"], d["cores"]) for d in j["designs"] if d["name"] in ("nanosoc", "nanosoc_multicore", "led")]')
check "mps3-debug version: schema mps3-debug/1, remote_bitbang, OpenOCD present" \
      '[ "$v_head" = "('"'"'mps3-debug/1'"'"', '"'"'remote_bitbang'"'"', True)" ]'
check "mps3-debug version: the PINS version line, 0 malformed design lines" \
      'case "$v_ver" in "0.12.0+mps3 (soclabs-openocd 39eba58"*) true ;; *) false ;; esac && [ "$v_bad" = 0 ]'
check "mps3-debug version: nanosoc 1 core, nanosoc_multicore 2, led no DAP" \
      '[ "$v_des" = "[('"'"'nanosoc'"'"', True, 1), ('"'"'nanosoc_multicore'"'"', True, 2), ('"'"'led'"'"', False, 0)]" ]'
# 0.12.0 has no `flash list_drivers`: declare a bank on a dummy-adapter `testee` target --
# an unknown driver fails by name ("flash driver '..' not found"; the negative control
# proves the test can tell), a registered one gets past the lookup
O1CMD=$(cat <<'GUEST'
T0="-c 'adapter driver dummy' -c 'transport select jtag' -c 'jtag newtap x cpu -irlen 4' -c 'target create t0 testee -chain-position x.cpu'"; echo "OCDV=$(openocd --version 2>&1 | head -1)"; echo "ADAPTERS=$(openocd -c 'adapter list' -c shutdown 2>&1 | tr '\n' ' ')"; echo "FLASHDRV=$(eval openocd $T0 -c \"'flash bank q ahb_qspi 0x70000000 0x100000 0 0 t0'\" -c shutdown 2>&1 | tr '\n' ' ')"; echo "FLASHNEG=$(eval openocd $T0 -c \"'flash bank q no_such_flash 0x70000000 0x100000 0 0 t0'\" -c shutdown 2>&1 | tr '\n' ' ')"
GUEST
)
console "$O1CMD" O1 180
check "openocd --version is 0.12.0" "grep -ao 'OCDV=.*' '$BL' | grep -q 'Open On-Chip Debugger 0.12.0'"
check "openocd adapters include remote_bitbang and hostio4" \
      "grep -ao 'ADAPTERS=.*' '$BL' | grep -q 'remote_bitbang' && grep -ao 'ADAPTERS=.*' '$BL' | grep -q 'hostio4'"
check "openocd knows the ahb_qspi flash driver (and an unknown one is refused by name)" \
      "! grep -ao 'FLASHDRV=.*' '$BL' | grep -q \"flash driver 'ahb_qspi' not found\" && grep -ao 'FLASHNEG=.*' '$BL' | grep -q \"flash driver 'no_such_flash' not found\""
console 'echo "PWLINE=$(grep ^openocd: /etc/passwd)"' U1
check "the openocd user: uid != 0, shell /bin/false" \
      "grep -ao 'PWLINE=openocd:.*' '$BL' | tr -d '\r' | grep -Eq '^PWLINE=openocd:[^:]*:[1-9][0-9]*:[0-9]+:.*:/bin/false\$'"

echo "== 2. the refusals (NO-HW: identify answers, 6921 is not served)"
console 'o=$(mps3-debug up --json); r=$?; echo "UPAUTO=$o"; echo "RC1=$r"' A1 120
J=$(jline UPAUTO)
j1=$(jget "$J" '(j["error"]["code"], j["design"], j["rm_id"])')
check "up (auto): identify's rm_id 0x00000000 = greybox -> exit 13 no_dap" \
      'grep -aq "RC1=13" "$BL" && [ "$j1" = "('"'"'no_dap'"'"', '"'"'greybox'"'"', '"'"'0x00000000'"'"')" ]'
console 'o=$(mps3-debug up --rm nanosoc --json); r=$?; echo "UPNS=$o"; echo "RC2=$r"' A2 240
J=$(jline UPNS)
j2=$(jget "$J" '(j["state"], j["error"]["code"])')
j2t=$(jget "$J" 'chr(10).join(j["log_tail"])')
check "up --rm nanosoc with no 6921 -> exit 6 failed, openocd_exit" \
      'grep -aq "RC2=6" "$BL" && [ "$j2" = "('"'"'failed'"'"', '"'"'openocd_exit'"'"')" ]'
check "...and its log_tail shows OpenOCD dialling 127.0.0.1:6921" \
      'printf "%s" "$j2t" | grep -q "127.0.0.1:6921"'
info "log_tail: $(printf '%s' "$j2t" | tail -3 | tr '\n' '|' | cut -c1-240)"

echo "== 3. the live path: the rv32 MOCK harnessd's 6921 (a fake TAP)"
console 'mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo "'"$(cat "$W/id.pub")"'" > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys' K1
if "${SSH[@]}" root@127.0.0.1 'cat > /tmp/hdmock && chmod +x /tmp/hdmock' < "$HD_MOCK" 2>"$W/ssh.err"; then
    ok "the rv32-mock harnessd pushed over SSH"
else
    bad "ssh push failed: $(head -2 "$W/ssh.err")"
fi
console '(/tmp/hdmock --mock-fabric /tmp/fab.bin --port-offset 10000 --wdog off --lcdmirror none --run-marker /tmp/hdmock.boot --card none --quiet </dev/null >/tmp/hdmock.log 2>&1 &) ; sleep 8; echo "HDM=$(grep -c . /tmp/hdmock.log)"' H1 60
console 'o=$(MPS3_DEBUG_RBB_PORT=16921 MPS3_DEBUG_IDENTIFY_PORT=16899 mps3-debug up --rm nanosoc --json); r=$?; echo "UPLIVE=$o"; echo "RC3=$r"' A3 300
J=$(jline UPLIVE)
j3=$(jget "$J" 'j["tap"]["idcode"] if j["tap"] else None')
j3d=$(jget "$J" '(j["design"], j["rm_id"])')
j3s=$(jget "$J" 'j["state"]')
j3t=$(jget "$J" 'chr(10).join(j["log_tail"])')
check "the image's OpenOCD, as openocd, read IDCODE 0x6ba00477 through harnessd's jtag_server" \
      '[ "$j3" = 0x6ba00477 ]'
check "...rm_id from the mock's identify (16899), design nanosoc" \
      '[ "$j3d" = "('"'"'nanosoc'"'"', '"'"'0x00000000'"'"')" ]'
info "live up: state $j3s, $(grep -ao 'RC3=[0-9]*' "$BL" | tail -1) (a fake TAP has no DAP: dap init fails, expected)"
info "log_tail: $(printf '%s' "$j3t" | tail -4 | tr '\n' '|' | cut -c1-320)"
console 'echo "DOWN=$(mps3-debug down --json)"; sleep 1; echo "PSOCD=$(ps | grep -v grep | grep -c [o]penocd)"' S1
jd=$(jget "$(jline DOWN)" 'j["state"]')
check "down: state down, no openocd process left" \
      '[ "$jd" = down ] && grep -ao "PSOCD=[0-9]*" "$BL" | tail -1 | grep -qx "PSOCD=0"'

printf 'reboot -f\n' >&3 2>/dev/null
sleep 2
echo "== $pass PASS, $fail FAIL"
[ $fail = 0 ]
