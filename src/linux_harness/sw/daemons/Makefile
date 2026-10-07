# mps3 harness daemons — plain C99/POSIX, builds with any cc (host x86 for
# the wire-compat tests, the Buildroot cross toolchain for the target image).
CC      ?= cc
CFLAGS  ?= -O2 -g
CFLAGS  += -Wall -Wextra -std=c99

# mps3-swdd is NOT here: see the legacy/ rule at the bottom. Nothing the image
# ships may drive the DUT debug wires with SWD semantics now that the fabric
# carries jtag_bb.
BINS = mps3-ctrld mps3-pushd mps3-configd mps3-xvcd mps3-uartbrd

# The DFX driver's ioctl contract header (swap_worker.c includes it by the
# in-tree relative path). Overridable because the Buildroot local-site rsync
# copies ONLY this directory: the mps3-harnessd package passes the absolute
# path (M6 gate finding #3, 2026-07-17).
ICAP_UAPI ?= ../../drivers/icap/mps3_dfx_uapi.h

all: $(BINS)

mps3-ctrld: mps3_ctrld.c proto.c uio.c spool.c swap_worker.c \
            proto.h uio.h spool.h swap_worker.h \
            $(ICAP_UAPI)
	$(CC) $(CFLAGS) -o $@ mps3_ctrld.c proto.c uio.c spool.c swap_worker.c $(LDFLAGS)

mps3-pushd: mps3_pushd.c proto.c spool.c proto.h spool.h
	$(CC) $(CFLAGS) -o $@ mps3_pushd.c proto.c spool.c $(LDFLAGS)

# mps3-configd (config_agent, M?): the TFTP/UDP :69 twin of mps3-pushd.
# Same spool/swap path (proto.c + spool.c), UDP transport instead of TCP.
# -m mock/host mode for host/QEMU conformance (tests/configd_tftp_test.py).
mps3-configd: mps3_configd.c proto.c spool.c proto.h spool.h
	$(CC) $(CFLAGS) -o $@ mps3_configd.c proto.c spool.c $(LDFLAGS)

# aux-service daemons (M4): UIO backends on the board, -m mock for
# host/QEMU protocol-conformance tests (tests/aux_services_test.py).
mps3-xvcd: mps3_xvcd.c auxhw.c uio.c auxhw.h uio.h
	$(CC) $(CFLAGS) -o $@ mps3_xvcd.c auxhw.c uio.c $(LDFLAGS)

# LEGACY SWD bit-bang server (TCP 6920). NOT in $(BINS), so `all` does not
# build it and the Buildroot package does not install it. Same disposition, and
# the same reasoning, as firmware/platform/Makefile's LEGACY_SWD block in the
# main tree: the SWD->JTAG cutover made the SWDBB block disappear from the
# fabric, so a shipped mps3-swdd is a service that can never be reached -- and
# worse than unreachable here, because jtag_bb sits at the SAME page (0x44A7)
# with the SAME bit positions, so an SWD daemon pointed at it would drive a
# JTAG TAP with SWD sequences and look alive while doing so.
#
# The SOURCE STAYS (moved to legacy/, not deleted): tests/golden_wire/
# test_swd_wire.py and tests/aux_services_test.py link it via this rule and pin
# the OpenOCD remote_bitbang byte encoding that any future mps3-jtagd inherits,
# exactly as firmware/test/test_swd_server.c does for the bare-metal twin.
# `make mps3-swdd` still works; it is simply not part of the image.
#
# legacy/ is also OUT of tests/integration/test_uio_name_parity.py's scan (it
# globs daemons/*.c, non-recursive), which is what lets the DTS node move to
# jtag-bb without a parked daemon's stale "swd-bb" literal failing that gate.
# -I. because the source moved down a directory and still includes "auxhw.h";
# the header did NOT move (auxhw.c/uio.c are live code the shipped daemons use).
mps3-swdd: legacy/mps3_swdd.c auxhw.c uio.c auxhw.h uio.h
	$(CC) $(CFLAGS) -I. -o $@ legacy/mps3_swdd.c auxhw.c uio.c $(LDFLAGS)

mps3-uartbrd: mps3_uartbrd.c auxhw.c uio.c auxhw.h uio.h
	$(CC) $(CFLAGS) -o $@ mps3_uartbrd.c auxhw.c uio.c $(LDFLAGS)

clean:
	rm -f $(BINS) mps3-swdd

.PHONY: all clean
