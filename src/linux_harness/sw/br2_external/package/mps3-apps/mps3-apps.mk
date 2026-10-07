################################################################################
# mps3-apps — the harness application layer (see ../../../apps/)
#   mps3-clcdd / mps3-status / mps3-ovlstore (read-only; D16 embargo)
# LEGACY variant only: harnessd's firmware clcd module owns the CLCD UIO in the
# default image, and one block has one owner (LINUX_HARNESS_PLAN §3).
################################################################################

MPS3_APPS_VERSION = 0.1
MPS3_APPS_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../apps
MPS3_APPS_SITE_METHOD = local
MPS3_APPS_LICENSE = Proprietary (SoCLabs project code; hx8347 init table \
	BSD-3-Clause ST + MIT nopnop2002, provenance in-source)

# uio.[ch] is shared with sw/daemons (single source of truth) — the local
# rsync only copies apps/, so point the Makefile at the daemons dir directly.
#
# The leading 'clean' is LOAD-BEARING (M6 gate finding, 2026-07-17): the
# local-site rsync (-au) carries any HOST-built binaries left in sw/apps/ by
# 'make test', their mtimes satisfy the Makefile targets ("Nothing to be
# done"), and the x86-64 ELFs get installed into the rv32 rootfs — Buildroot's
# arch check then kills the whole image build. Always rebuild from source.
define MPS3_APPS_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) clean
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) \
		CC="$(TARGET_CC)" \
		CFLAGS="$(TARGET_CFLAGS) -Wall -Wextra -std=c99 \
			-I$(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../daemons" \
		LDFLAGS="$(TARGET_LDFLAGS)" \
		UIO_SRC="$(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../daemons" all
# ^ -I repeated inside CFLAGS (M6 gate finding #2): a command-line CFLAGS
#   OVERRIDES the app Makefile's 'CFLAGS += -I$(UIO_SRC)' (GNU make command
#   line beats every makefile assignment), so without it the cross build
#   cannot find uio.h. The M5 off-tree emulation passed UIO_SRC but not a
#   command-line CFLAGS, so this only fires in the real Buildroot package.
endef

define MPS3_APPS_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/mps3-clcdd    $(TARGET_DIR)/usr/sbin/mps3-clcdd
	$(INSTALL) -D -m 0755 $(@D)/mps3-status   $(TARGET_DIR)/usr/bin/mps3-status
	$(INSTALL) -D -m 0755 $(@D)/mps3-ovlstore $(TARGET_DIR)/usr/bin/mps3-ovlstore
	$(INSTALL) -D -m 0755 $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-apps/files/S91mps3clcd \
		$(TARGET_DIR)/etc/init.d/S91mps3clcd
endef

$(eval $(generic-package))
