# Host unit tests for the ICAP-SPIKE engine/mock/table.
# Run: make -C tests/host run
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Werror -std=gnu99
SRCDIR   = ../..

TESTS = test_transitions test_pack test_engine_host

all: $(TESTS)

test_transitions: test_transitions.c $(SRCDIR)/mps3_swap_transitions.c
	$(CC) $(CFLAGS) -o $@ $^

test_pack: test_pack.c
	$(CC) $(CFLAGS) -o $@ $^

test_engine_host: test_engine_host.c $(SRCDIR)/mps3_swap_transitions.c \
		$(SRCDIR)/mps3_icap_engine.c $(SRCDIR)/mps3_icap_mock.c
	$(CC) $(CFLAGS) -o $@ $^

run: all
	./test_transitions
	./test_pack
	./test_engine_host
	@echo "HOST TESTS: ALL PASS"

clean:
	rm -f $(TESTS)

.PHONY: all run clean
