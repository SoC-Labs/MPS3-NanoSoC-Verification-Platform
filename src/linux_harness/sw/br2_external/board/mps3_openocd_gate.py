#!/usr/bin/env python3
"""mps3_openocd_gate.py -- the image gate for the on-board GDB server (mps3-debug).

    python3 mps3_openocd_gate.py TARGET_DIR OBJDUMP [--max-bytes=N] [--passwd=FILE]

TARGET_DIR is Buildroot's output/target (or an unpacked rootfs), OBJDUMP the target
toolchain's objdump. --passwd names the /etc/passwd to check: Buildroot creates a
package's <PKG>_USERS in the FILESYSTEM step (support/scripts/mkusers under fakeroot),
so output/target/etc/passwd never has them -- build.sh passes the rootfs.cpio's copy
(the image as shipped). Default: TARGET_DIR/etc/passwd. FAILS (exit 1, one line per
problem) unless:

  1. /usr/bin/openocd is there, at most --max-bytes (default 4 MiB: the measured
     stripped rv32 build is ~3.1 MB, and the embedded initramfs has ~5.8 MB of
     DTB-slot margin before it -- build.sh 7/9 checks that margin separately);
  2. it carries the three drivers the MVP needs, by the names OpenOCD registers them
     under: remote_bitbang (the adapter to 127.0.0.1:6921), ahb_qspi (the SoC Labs
     NOR flash driver) and hostio4 (the SoC Labs ADP adapter);
  3. neither it nor /usr/bin/mps3-debug holds an F or D instruction (the F/D
     LANDMINE: the MicroBlaze V has no FPU; QEMU does, so only this catches it);
  4. /usr/share/mps3/openocd has designs.conf, VERSION and every config a recipe
     line names;
  5. /etc/passwd has the unprivileged `openocd` user (uid != 0, no login shell).

br2_external/tests/run.sh drives it against fakes, each rule with a negative control.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DRIVERS = (b"remote_bitbang", b"ahb_qspi", b"hostio4")
# Any F/D mnemonic: the arithmetic/convert/compare/move forms carry a ".s"/".d"/...
# suffix, the loads/stores and the FP-CSR pseudo-ops do not. (The harnessd Makefile's
# pattern, `\s(f(add|..|cvt)\.|flw|fsw|fld|fsd)\s`, needs whitespace right after the
# dot, so it can only ever match the four load/store forms: this one does not copy it.)
FD_RE = re.compile(r"\s(f(add|sub|mul|div|sqrt|min|max|mv|cvt|eq|lt|le|class|sgnj[nx]?|"
                   r"madd|msub|nmadd|nmsub)\.[a-z.]+|flw|fsw|fld|fsd|"
                   r"fr(csr|rm|flags)|fs(csr|rm|flags)(i)?)\s")


def fd_hits(objdump: str, binary: Path) -> list[str]:
    out = subprocess.run([objdump, "-d", str(binary)], capture_output=True, text=True)
    if out.returncode != 0:
        return [f"objdump failed on {binary}: {out.stderr.strip()[:120]}"]
    return [line.strip() for line in out.stdout.splitlines() if FD_RE.search(line)][:3]


def gate(target: Path, objdump: str, max_bytes: int, passwd: Path | None = None) -> list[str]:
    bad: list[str] = []
    ocd = target / "usr/bin/openocd"
    launcher = target / "usr/bin/mps3-debug"
    share = target / "usr/share/mps3/openocd"
    if not ocd.is_file():
        bad.append("no /usr/bin/openocd")
    else:
        data = ocd.read_bytes()
        if not data.startswith(b"\x7fELF"):
            bad.append("/usr/bin/openocd is not an ELF")
        if len(data) > max_bytes:
            bad.append(f"/usr/bin/openocd is {len(data)} B > the {max_bytes} B budget")
        for d in DRIVERS:
            if d not in data:
                bad.append(f"/usr/bin/openocd has no {d.decode()} driver")
        for hit in fd_hits(objdump, ocd):
            bad.append(f"/usr/bin/openocd: F/D instruction: {hit}")
    if not launcher.is_file():
        bad.append("no /usr/bin/mps3-debug")
    else:
        for hit in fd_hits(objdump, launcher):
            bad.append(f"/usr/bin/mps3-debug: F/D instruction: {hit}")
    conf = share / "designs.conf"
    if not conf.is_file():
        bad.append("no /usr/share/mps3/openocd/designs.conf")
    else:
        for line in conf.read_text().splitlines():
            line = line.split("#", 1)[0]
            for tok in line.split():
                if tok.startswith("cfg="):
                    for cfg in tok[4:].split(","):
                        if not (share / cfg).is_file():
                            bad.append(f"designs.conf names {cfg}, which is not installed")
    ver = share / "VERSION"
    if not ver.is_file() or not ver.read_text().strip():
        bad.append("no /usr/share/mps3/openocd/VERSION")
    pw = passwd if passwd is not None else target / "etc/passwd"
    user = None
    if pw.is_file():
        for line in pw.read_text().splitlines():
            f = line.split(":")
            if len(f) >= 7 and f[0] == "openocd":
                user = f
    if user is None:
        bad.append("no `openocd` user in /etc/passwd")
    else:
        if user[2] == "0":
            bad.append("the `openocd` user is uid 0")
        if user[6] not in ("/bin/false", "/sbin/nologin", "/usr/sbin/nologin", "/bin/nologin"):
            bad.append(f"the `openocd` user has a login shell ({user[6]})")
    return bad


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    max_bytes = 4 << 20
    passwd = None
    for a in argv[1:]:
        if a.startswith("--max-bytes="):
            max_bytes = int(a.split("=", 1)[1], 0)
        elif a.startswith("--passwd="):
            passwd = Path(a.split("=", 1)[1])
        elif a.startswith("--"):
            print(f"mps3_openocd_gate: unknown option {a}")
            return 2
    if len(args) != 2:
        print(__doc__)
        return 2
    target, objdump = Path(args[0]), args[1]
    bad = gate(target, objdump, max_bytes, passwd)
    for b in bad:
        print(f"mps3_openocd_gate: FAIL {b}")
    if bad:
        return 1
    size = (target / "usr/bin/openocd").stat().st_size
    print(f"mps3_openocd_gate: OK openocd {size} B (<= {max_bytes}), drivers "
          f"{'/'.join(d.decode() for d in DRIVERS)}, no F/D, mps3-debug, cfgs, `openocd` user")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
