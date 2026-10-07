# mps3-slot — built for the target by package/mps3-slot (which passes S0 and HD),
# and for the host by br2_external/tests/run.sh and harnessd's test_slot_e2e.py.
# A THIN CLI over harnessd's slot_card.c (the ONE card layer, shared with
# mps3-harnessd) and STAGE0's stage0_core.c (MBR parse, boot-select pick, S0LB
# loader), both compiled in unmodified -- never copied.
# The warning/include flags live in the RECIPE: a command-line CFLAGS (Buildroot
# passes one) overrides any `CFLAGS +=` here — the mps3-apps M6 lesson.
CC ?= gcc
CFLAGS ?= -O2
S0 ?= ../../../../../../linux_soc/hw/fw_stage0
HD ?= ../../../../harnessd
all: mps3-slot
mps3-slot: mps3-slot.c $(HD)/slot_card.c $(HD)/slot_card.h $(S0)/stage0_core.c $(S0)/stage0_boot.h
	$(CC) $(CFLAGS) -Wall -Wextra -Werror -std=c99 -I$(HD) -I$(S0) $(LDFLAGS) -o $@ \
		mps3-slot.c $(HD)/slot_card.c $(S0)/stage0_core.c
clean:
	rm -f mps3-slot
.PHONY: all clean
