################################################################################
# mps3-slot — /usr/sbin/mps3-slot (sources in src/), STAGE0_CONTRACT §6.
################################################################################

MPS3_SLOT_VERSION = 1.0
MPS3_SLOT_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-slot/src
MPS3_SLOT_SITE_METHOD = local
MPS3_SLOT_LICENSE = Proprietary (SoCLabs project code)

# STAGE0's own rules, compiled in (never copied): src/linux_soc/hw/fw_stage0
MPS3_SLOT_S0 = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../../../linux_soc/hw/fw_stage0
# ...and the ONE card layer mps3-harnessd links too (harnessd/slot_card.[ch])
MPS3_SLOT_HD = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../harnessd

# leading clean: a host build left in src/ by tests/run.sh must never reach the
# rv32 rootfs (the mps3-apps M6 lesson)
define MPS3_SLOT_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) clean
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) CC="$(TARGET_CC)" \
		CFLAGS="$(TARGET_CFLAGS)" LDFLAGS="$(TARGET_LDFLAGS)" \
		S0="$(MPS3_SLOT_S0)" HD="$(MPS3_SLOT_HD)" all
endef

define MPS3_SLOT_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/mps3-slot $(TARGET_DIR)/usr/sbin/mps3-slot
endef

$(eval $(generic-package))
