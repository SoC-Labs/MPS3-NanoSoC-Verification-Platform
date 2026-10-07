################################################################################
# mps3-legacy-daemons — the v0.7 hand-ported daemons (sources ../../../daemons),
# the LEGACY image variant only (LINUX_HARNESS_PLAN DL6). Same build recipe the
# July image used; the package now also installs its own S-scripts, so the
# default rootfs overlay carries none of them.
################################################################################

MPS3_LEGACY_DAEMONS_VERSION = 0.7
MPS3_LEGACY_DAEMONS_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../daemons
MPS3_LEGACY_DAEMONS_SITE_METHOD = local
MPS3_LEGACY_DAEMONS_LICENSE = Proprietary (SoCLabs project code)

# M6 gate findings (2026-07-17), all LOAD-BEARING:
#  * leading 'clean': the local-site rsync carries host-built binaries left by
#    host test runs; their mtimes satisfy the targets and x86-64 ELFs would
#    land in the rv32 rootfs.
#  * ICAP_UAPI override: the DFX driver's uapi header by an in-tree path.
#  * -I.../daemons inside CFLAGS: a command-line CFLAGS overrides the
#    Makefile's 'CFLAGS +=' additions.
define MPS3_LEGACY_DAEMONS_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) clean
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) \
		CC="$(TARGET_CC)" \
		CFLAGS="$(TARGET_CFLAGS) -Wall -Wextra -std=c99 \
			-I$(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../daemons" \
		LDFLAGS="$(TARGET_LDFLAGS)" \
		ICAP_UAPI="$(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../../drivers/icap/mps3_dfx_uapi.h" \
		all
endef

MPS3_LEGACY_DAEMONS_FILES = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-legacy-daemons/files

define MPS3_LEGACY_DAEMONS_INSTALL_TARGET_CMDS
	for d in mps3-ctrld mps3-pushd mps3-configd mps3-xvcd mps3-uartbrd; do \
		$(INSTALL) -D -m 0755 $(@D)/$$d $(TARGET_DIR)/usr/sbin/$$d || exit 1; \
	done
	$(INSTALL) -D -m 0755 $(MPS3_LEGACY_DAEMONS_FILES)/S90mps3d \
		$(TARGET_DIR)/etc/init.d/S90mps3d
	$(INSTALL) -D -m 0755 $(MPS3_LEGACY_DAEMONS_FILES)/S91mps3aux \
		$(TARGET_DIR)/etc/init.d/S91mps3aux
endef

$(eval $(generic-package))
