#-----------------------------------------------------------------------------
# eth_ss_probe.mk — read a variable out of an UPSTREAM makefile without running
# any of its rules.
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.
#
# WHY THIS EXISTS. build_eth_ss_bootrom.sh must know the flags a leaf firmware
# makefile *declares* for itself (e.g. firmware/bootloader/makefile's
# `GNU_CC_EXTRA_FLAGS := -Os -flto -mthumb-interwork`) so it can APPEND the
# board defines instead of REPLACING them -- which is the whole bug this
# override exists to route around (see README.md). Re-typing those flags here
# would be a second copy that silently drifts from upstream, so we ask make.
#
# Usage:
#   make -C <dir-to-pretend-we-are-in> -f eth_ss_probe.mk __soclabs_print \
#        INCLUDE_MAKEFILE=<absolute path to the upstream makefile> \
#        VAR=<variable name> [SHIM_ARCH_TECH=<dir>] [VAR2=VALUE ...]
#
# SHIM_ARCH_TECH points SOCLABS_NANOSOC_ARCH_TECH_DIR at a directory holding an
# EMPTY firmware/build/testcode.mk. The leaf makefiles set their variables and
# then `include $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/firmware/build/testcode.mk`;
# with the shim that include is a no-op, so the declared value is observable
# before the shared template gets to append or default anything.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
ifneq ($(SHIM_ARCH_TECH),)
SOCLABS_NANOSOC_ARCH_TECH_DIR := $(SHIM_ARCH_TECH)
endif

include $(INCLUDE_MAKEFILE)

.PHONY: __soclabs_print
__soclabs_print:
	@printf '%s\n' '$($(VAR))'
