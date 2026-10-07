# The AMD vendor sysroot ships /etc/ld.so.conf; it ends up in TARGET_DIR, and Buildroot
# then aborts target-finalize with "ERROR: we shouldn't have a /etc/ld.so.conf file"
# (Makefile:788). Buildroot refuses on purpose -- it fixes the library search path at
# build time, and a stray ld.so.conf can send the runtime loader hunting paths that do
# not exist on the target.
#
# This must be a TARGET_FINALIZE_HOOK, NOT a BR2_ROOTFS_POST_BUILD_SCRIPT: hooks run at
# Makefile:759, the check is at :788, and post-build scripts only run at :827 -- i.e. a
# post-build script is too late and the build still fails.
define MBV_RM_LD_SO_CONF
	rm -f  $(TARGET_DIR)/etc/ld.so.conf
	rm -rf $(TARGET_DIR)/etc/ld.so.conf.d
endef
TARGET_FINALIZE_HOOKS += MBV_RM_LD_SO_CONF
