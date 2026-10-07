#!/usr/bin/env python3
"""check_ident_order.py -- stage0.c publishes the board identity at ENTRY (lane
IDENT; STAGE0_CONTRACT §3.3): in main(), the s0_status_publish_identity() call must
come right after s0_status_open() -- before the watchdog WRS clear, the watchdog arm
and the first s0_boot_select() (the DDR gate and every card access are behind that
call). A source-order check, with a NEGATIVE CONTROL: the same check on a copy with
the publish moved after the boot order must fail.

    check_ident_order.py ../stage0.c
"""
import re
import sys


def order_ok(src):
    main = src[src.index("int main(void)"):]
    o = main.find("s0_status_open(")
    p = main.find("s0_status_publish_identity(")
    later = [i for i in (main.find("s0_hw_wdog_clear_wrs("), main.find("s0_hw_wdog_arm("),
                         main.find("s0_boot_select(")) if i >= 0]
    return 0 <= o < p < min(later), (o, p, later)


def main():
    src = open(sys.argv[1]).read()
    ok, pos = order_ok(src)
    if not ok:
        print("FAIL: stage0.c main(): open/publish/boot_select at %s -- the identity must be "
              "published after s0_status_open() and before s0_boot_select()" % (pos,))
        return 1
    # negative control: move the publish call after the boot order
    call = re.search(r"\n[ \t]*s0_status_publish_identity\([^;]*\);", src).group(0)
    bad = src.replace(call, "", 1)
    i = bad.index("int main(void)")
    j = bad.index("s0_boot_select(", i)
    j = bad.index(";", j) + 1
    bad = bad[:j] + call + bad[j:]
    if order_ok(bad)[0]:
        print("NEGATIVE CONTROL FAILED: a publish AFTER s0_boot_select() passed the check")
        return 1
    print("stage0 identity order: published right after s0_status_open, before the WDOG "
          "WRS clear / arm and s0_boot_select "
          "(negative control refused)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
