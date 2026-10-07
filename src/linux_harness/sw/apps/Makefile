# mps3 harness apps (M5 app port) — plain C99/POSIX, builds with any cc
# (host x86 for the mocked-register tests, the Buildroot cross toolchain for
# the target image via br2_external/package/mps3-apps).
CC      ?= cc
CFLAGS  ?= -O2 -g
CFLAGS  += -Wall -Wextra -std=c99

# The UIO helper is shared with the daemons (single source of truth). The
# br2_external package overrides UIO_SRC with an absolute path because
# Buildroot's local-site rsync only copies this directory.
UIO_SRC ?= ../daemons
CFLAGS  += -I$(UIO_SRC)

BINS = mps3-clcdd mps3-status mps3-ovlstore

CORE_SRC = clcd_core.c hx8347_init.c
CORE_HDR = clcd_core.h clcd_regs.h hx8347_init.h font8x16.h

all: $(BINS)

mps3-clcdd: mps3_clcdd.c status_linux.c $(UIO_SRC)/uio.c \
            $(CORE_SRC) $(CORE_HDR) status_linux.h $(UIO_SRC)/uio.h
	$(CC) $(CFLAGS) -o $@ mps3_clcdd.c status_linux.c $(UIO_SRC)/uio.c \
	    $(CORE_SRC) $(LDFLAGS)

mps3-status: mps3_status.c status_linux.c $(CORE_SRC) $(CORE_HDR) status_linux.h
	$(CC) $(CFLAGS) -o $@ mps3_status.c status_linux.c $(CORE_SRC) $(LDFLAGS)

mps3-ovlstore: mps3_ovlstore.c ovlstore_read.c ovlstore_codec.c crc32.c \
               ovlstore_read.h ovlstore_codec.h crc32.h
	$(CC) $(CFLAGS) -o $@ mps3_ovlstore.c ovlstore_read.c ovlstore_codec.c \
	    crc32.c $(LDFLAGS)

# ---- host tests (mocked registers / faked sysroot) -------------------------
TESTBINS = tests/test_clcd_core tests/test_ovlstore tests/test_status_linux

tests/test_clcd_core: tests/test_clcd_core.c tests/mock_clcd.c \
                      tests/mock_clcd.h $(CORE_SRC) $(CORE_HDR)
	$(CC) $(CFLAGS) -o $@ tests/test_clcd_core.c tests/mock_clcd.c $(CORE_SRC)

tests/test_ovlstore: tests/test_ovlstore.c ovlstore_read.c ovlstore_codec.c \
                     crc32.c ovlstore_read.h ovlstore_codec.h crc32.h
	$(CC) $(CFLAGS) -o $@ tests/test_ovlstore.c ovlstore_read.c \
	    ovlstore_codec.c crc32.c

tests/test_status_linux: tests/test_status_linux.c status_linux.c \
                         $(CORE_SRC) $(CORE_HDR) status_linux.h
	$(CC) $(CFLAGS) -o $@ tests/test_status_linux.c status_linux.c $(CORE_SRC)

testbins: $(TESTBINS)

test: testbins $(BINS)
	sh tests/run_tests.sh

clean:
	rm -f $(BINS) $(TESTBINS)

.PHONY: all test testbins clean
