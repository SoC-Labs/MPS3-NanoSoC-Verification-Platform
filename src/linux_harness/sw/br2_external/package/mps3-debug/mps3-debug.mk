################################################################################
# mps3-debug -- the on-board GDB server (MVP, 6 Oct 2026; contract "mps3-debug/1").
#
#   /usr/bin/mps3-debug                 the launcher (src/mps3_debug.c)
#   /usr/share/mps3/openocd/            the target configs (host/openocd, copied at
#                                       build: never a second copy in the tree), the
#                                       design table (designs.conf) and VERSION
#   user `openocd`                      OpenOCD never runs as root
#
# OpenOCD itself is Buildroot's package (selected by Config.in) with the SoC Labs
# drivers from sw/patches/openocd (BR2_GLOBAL_PATCH_DIR) and ONE conf option more:
# remote_bitbang, off by default in 0.12.0 and never passed by Buildroot's
# openocd.mk. The append below is expanded when openocd's configure step RUNS
# (autotools-package reads $(OPENOCD_CONF_OPTS) inside the recipe), so it takes
# effect although this file is parsed after package/openocd/openocd.mk.
#
# harnessd is untouched: OpenOCD dials 127.0.0.1:6921 like any client.
################################################################################

MPS3_DEBUG_VERSION = 1.0.0
MPS3_DEBUG_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-debug/src
MPS3_DEBUG_SITE_METHOD = local
MPS3_DEBUG_LICENSE = Proprietary (SoCLabs project code)
MPS3_DEBUG_DEPENDENCIES = openocd

MPS3_DEBUG_CFG_DIR = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../../../../host/openocd
MPS3_DEBUG_PINS = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../patches/openocd/PINS

ifeq ($(BR2_PACKAGE_MPS3_DEBUG),y)
OPENOCD_CONF_OPTS += --enable-remote-bitbang
endif

# leading clean: a host build left in src/ by the host tests must never reach
# the rv32 rootfs (the mps3-apps M6 lesson)
define MPS3_DEBUG_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) clean
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) CC="$(TARGET_CC)" \
		CFLAGS="$(TARGET_CFLAGS)" LDFLAGS="$(TARGET_LDFLAGS)" \
		VERSION="$(MPS3_DEBUG_VERSION)" all
endef

define MPS3_DEBUG_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/mps3-debug $(TARGET_DIR)/usr/bin/mps3-debug
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/share/mps3/openocd
	$(INSTALL) -m 0644 $(MPS3_DEBUG_CFG_DIR)/*.cfg $(MPS3_DEBUG_CFG_DIR)/*.tcl \
		$(TARGET_DIR)/usr/share/mps3/openocd/
	$(INSTALL) -m 0644 $(MPS3_DEBUG_PKGDIR)/designs.conf \
		$(TARGET_DIR)/usr/share/mps3/openocd/designs.conf
	sed -n 's/^version_line=//p' $(MPS3_DEBUG_PINS) \
		> $(TARGET_DIR)/usr/share/mps3/openocd/VERSION
	test -s $(TARGET_DIR)/usr/share/mps3/openocd/VERSION
endef

# OpenOCD's upstream script tree (/usr/share/openocd: 910 interface/target/board files,
# ~1.3 MB, + contrib) is not used on the board -- mps3-debug passes -s
# /usr/share/mps3/openocd and the nanoSoC cfgs `find` nothing -- and every byte of the
# embedded initramfs comes off the DTB-slot margin (5.8 MB on rc2_v7_clean) and the
# stage0 WDOG budget (the unpack). Dropped at target-finalize.
define MPS3_DEBUG_DROP_UPSTREAM_SCRIPTS
	rm -rf $(TARGET_DIR)/usr/share/openocd
endef
MPS3_DEBUG_TARGET_FINALIZE_HOOKS += MPS3_DEBUG_DROP_UPSTREAM_SCRIPTS

define MPS3_DEBUG_USERS
	openocd -1 openocd -1 * - - - OpenOCD, the on-board GDB server (mps3-debug)
endef

$(eval $(generic-package))
