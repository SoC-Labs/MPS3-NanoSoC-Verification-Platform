#!/usr/bin/env python3
"""gen_init_rom.py -- render the HX8347-D init table from
`firmware/clcd/hx8347_init.c` into the GENERATED block of `clcd_demo_gen.sv`.

    python3 fpga/rp/clcd_demo/gen_init_rom.py            # rewrite in place
    python3 fpga/rp/clcd_demo/gen_init_rom.py --check    # exit 1 if stale

The RM must re-initialise the panel on every repaint, because every CLCD-KVM
handover HARD-RESETS it (`fpga/shell/ip/clcd_kvm/README.md` §10,
`docs/contracts/dut-display-tunnel.md` §6). The sequence it sends is the
FIRMWARE's -- see `hx8347_table.py`'s header for why this is generated rather
than retyped, and `tests/clcd_demo/test_init_rom_fresh.py` for the gate that
keeps it that way.

The generated block is delimited by BEGIN/END GENERATED markers, the same idiom
`fpga/dfx/pin_check.py` uses for its boundary table. Everything outside the
markers is hand-written and preserved.

Stdlib only. Board-free. No Vivado, no simulator.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = HERE / "clcd_demo_gen.sv"
BEGIN = "  // BEGIN GENERATED[hx8347] -- fpga/rp/clcd_demo/gen_init_rom.py -- DO NOT EDIT BY HAND"
END = "  // END GENERATED[hx8347]"

sys.path.insert(0, str(HERE))
import hx8347_table as fw  # noqa: E402


def render(repo_root: pathlib.Path) -> str:
    table, defines = fw.load(repo_root)
    win = fw.window_writes(table, defines)
    frame_w, frame_h = fw.frame_geometry(table, defines)

    # The GRAM-write command must really be in the firmware table; if the
    # firmware stops issuing it, the RM's repaint would write into whatever
    # register was last selected. Cross-check, do not assume.
    cmds = {e.val for e in table if e.op == defines["HX_CMD"]}
    if fw.GRAM_WRITE_CMD not in cmds:
        raise SystemExit(
            f"firmware/clcd/hx8347_init.c no longer issues the GRAM-write "
            f"command 0x{fw.GRAM_WRITE_CMD:02X}; fpga/rp/clcd_demo takes it "
            f"from the firmware table (hx8347_table.py).")

    lines = [BEGIN]
    lines += [
        "  //",
        "  //   source   firmware/clcd/hx8347_init.c  +  hx8347_init.h",
        "  //   entries  %d   (op[1:0], val[7:0]) packed as {op, val}" % len(table),
        "  //   ops      HX_CMD=%d  HX_DAT=%d  HX_DLY=%d  (values read from the header)"
        % (defines["HX_CMD"], defines["HX_DAT"], defines["HX_DLY"]),
        "  //   MADCTL   reg 0x%02X = 0x%02X  (CLCD_ROTATE_180=%d)"
        % (defines["HX_REG_MADCTL"], defines["HX_MADCTL_VALUE"],
           defines.get("CLCD_ROTATE_180", 1)),
        "  //",
        "  // Provenance for every byte: firmware/clcd/PANEL_PROVENANCE.md.",
        "",
        "  localparam int OP_CMD = %d;" % defines["HX_CMD"],
        "  localparam int OP_DAT = %d;" % defines["HX_DAT"],
        "  localparam int OP_DLY = %d;" % defines["HX_DLY"],
        "",
        "  localparam int INIT_N = %d;" % len(table),
        "  localparam logic [9:0] INIT_ROM [0:INIT_N-1] = '{",
    ]
    for k, e in enumerate(table):
        comma = "," if k != len(table) - 1 else ""
        note = e.raw_val if not e.raw_val.startswith("0x") else ""
        lines.append(
            "    10'h%03X%s   // [%3d] %s 0x%02X%s"
            % ((e.op << 8) | e.val, comma, k, e.op_name, e.val,
               ("  <- %s" % note) if note else ""))
    lines.append("  };")
    lines += [
        "",
        "  // Address-window writes, TAKEN FROM the same firmware table (the",
        "  // register indices are cross-checked against it -- hx8347_table.py",
        "  // raises if the firmware stops writing any of them). The RM re-issues",
        "  // the window before every repaint because the KVM hard-reset wiped it.",
        "  // The END values below are the FIRMWARE's; the RM recomputes them from",
        "  // its own PIX_W/PIX_H so a bench can shrink the frame, and",
        "  // tests/clcd_demo asserts the two agree at the default geometry.",
        "  localparam int WIN_N = %d;" % len(win),
    ]
    lines.append("  localparam logic [7:0] WIN_REG [0:WIN_N-1] = '{"
                 + ", ".join("8'h%02X" % r for r, _ in win) + "};")
    lines.append("  localparam logic [7:0] WIN_FW_VAL [0:WIN_N-1] = '{"
                 + ", ".join("8'h%02X" % v for _, v in win) + "};")
    lines += [
        "  localparam logic [7:0] GRAM_WRITE_CMD = 8'h%02X;" % fw.GRAM_WRITE_CMD,
        "",
        "  // The panel geometry the firmware table's window END registers imply.",
        "  localparam int FW_FRAME_W = %d;" % frame_w,
        "  localparam int FW_FRAME_H = %d;" % frame_h,
        END,
    ]
    return "\n".join(lines) + "\n"


def splice(src: str, block: str) -> str:
    if BEGIN not in src or END not in src:
        raise SystemExit(f"{TARGET}: BEGIN/END GENERATED[hx8347] markers missing")
    head = src[:src.index(BEGIN)]
    tail = src[src.index(END) + len(END):]
    tail = tail.lstrip("\n")
    return head + block + tail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the checked-in file is not a fresh render")
    ap.add_argument("--repo", default=str(ROOT))
    a = ap.parse_args()

    block = render(pathlib.Path(a.repo))
    cur = TARGET.read_text()
    new = splice(cur, block)
    if a.check:
        if cur == new:
            print(f"OK: {TARGET.relative_to(ROOT)} is a fresh render of "
                  f"firmware/clcd/hx8347_init.c")
            return 0
        print(f"STALE: {TARGET.relative_to(ROOT)} disagrees with "
              f"firmware/clcd/hx8347_init.c.\n"
              f"  Re-run: python3 fpga/rp/clcd_demo/gen_init_rom.py")
        return 1
    TARGET.write_text(new)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
