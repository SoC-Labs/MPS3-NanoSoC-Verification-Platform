################################################################################
# mps3-harnessd — the firmware service modules as one Linux process
# (src/linux_harness/sw/harnessd/, HARNESSD_CONTRACT.md §7).
#
# BUILT IN PLACE, not from an rsync'd copy: the harnessd Makefile compiles the
# firmware modules straight out of <repo>/firmware (never a copy — that is the
# whole point of DL6), so the package points the Makefile at the source tree and
# sends every object to $(@D). Nothing is written into the source tree.
#
#  * RV_CC / CROSS: Buildroot's target compiler + binutils (the AMD rv32 glibc
#    toolchain behind the wrapper). The Makefile still appends the explicit
#    -march=rv32imac_zicsr_zifencei -mabi=ilp32 (the F/D landmine,
#    src/linux_soc/linux/README.md) and its `rv32` target FAILS the build if a
#    single F/D instruction reaches the binary.
#  * BUILD=$(@D)/build is wiped first (the M6 lesson from the legacy package:
#    never let a host-built object satisfy a target build).
#  * mps3-wdkick + S00mps3wdkick (2026-09-26): the bounded watchdog bridge from
#    the first rcS script to harnessd (HARNESSD_CONTRACT.md §5.3). Same sources
#    dir, built by the same `make rv32`.
#  * mps3-identity + S13mps3identity (2026-09-28, lane IDENT): this board's
#    identity (label/hostname/IP/MAC), resolved once per boot into
#    /run/mps3/identity before S41mps3net applies it (identity_core.h).
################################################################################

MPS3_HARNESSD_VERSION = 1.0
MPS3_HARNESSD_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-harnessd
MPS3_HARNESSD_SITE_METHOD = local
MPS3_HARNESSD_LICENSE = Proprietary (SoCLabs project code)

MPS3_HARNESSD_SRC = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../harnessd

define MPS3_HARNESSD_BUILD_CMDS
	rm -rf $(@D)/build
	$(TARGET_MAKE_ENV) $(MAKE) -C $(MPS3_HARNESSD_SRC) rv32 \
		BUILD=$(@D)/build \
		RV_CC="$(TARGET_CC)" \
		CROSS="$(TARGET_CROSS)"
endef

define MPS3_HARNESSD_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/build/rv32/mps3-harnessd \
		$(TARGET_DIR)/usr/sbin/mps3-harnessd
	$(INSTALL) -D -m 0755 $(@D)/build/rv32/mps3-wdkick \
		$(TARGET_DIR)/usr/sbin/mps3-wdkick
	$(INSTALL) -D -m 0755 $(@D)/build/rv32/mps3-lcdmirror \
		$(TARGET_DIR)/usr/sbin/mps3-lcdmirror
	$(INSTALL) -D -m 0755 $(MPS3_HARNESSD_PKGDIR)/S00mps3wdkick \
		$(TARGET_DIR)/etc/init.d/S00mps3wdkick
	$(INSTALL) -D -m 0755 $(@D)/build/rv32/mps3-identity \
		$(TARGET_DIR)/usr/sbin/mps3-identity
	$(INSTALL) -D -m 0755 $(MPS3_HARNESSD_PKGDIR)/S13mps3identity \
		$(TARGET_DIR)/etc/init.d/S13mps3identity
	$(INSTALL) -D -m 0644 $(MPS3_HARNESSD_PKGDIR)/50-mps3-harnessd.inittab \
		$(TARGET_DIR)/usr/share/mps3/inittab.d/50-mps3-harnessd.inittab
endef

$(eval $(generic-package))
