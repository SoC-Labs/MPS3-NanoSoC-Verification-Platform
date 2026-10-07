# mps3-debug -- the on-board GDB server launcher (mps3_debug.c). Buildroot's
# package (../mps3-debug.mk) passes CC/CFLAGS/LDFLAGS; the host tests
# (../tests/test_mps3_debug.py) build it with the host compiler.
CC      ?= gcc
CFLAGS  ?= -O2 -g
WARN    := -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter
VERSION ?= 1.0.0

all: mps3-debug

mps3-debug: mps3_debug.c
	$(CC) $(CFLAGS) $(WARN) -DMPS3_DEBUG_VERSION='"$(VERSION)"' $(LDFLAGS) -o $@ $<

clean:
	rm -f mps3-debug

.PHONY: all clean
