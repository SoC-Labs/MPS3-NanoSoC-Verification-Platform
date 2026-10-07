include $(sort $(wildcard $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/*/*.mk))

# The AMD vendor sysroot ships /etc/ld.so.conf; it ends up in TARGET_DIR and
# Buildroot aborts target-finalize (Makefile:788). Must be a
# TARGET_FINALIZE_HOOK (runs :759), NOT a post-build script (:827 — too
# late). Carried verbatim from the proven linux_soc br2_external
# (../../linux_soc/linux/br2_external/external.mk) so this external tree is
# self-contained.
define MPS3_HARNESS_RM_LD_SO_CONF
	rm -f  $(TARGET_DIR)/etc/ld.so.conf
	rm -rf $(TARGET_DIR)/etc/ld.so.conf.d
endef
TARGET_FINALIZE_HOOKS += MPS3_HARNESS_RM_LD_SO_CONF
