################################################################################
# mps3-dfx — the out-of-tree DFX swap driver mps3_dfx.ko (see
#            ../../../drivers/icap/). Buildroot kernel-module infrastructure:
#            builds against THIS image's kernel and `modules_install`s to
#            /lib/modules/$(LINUX_VERSION_PROBED)/extra/mps3_dfx.ko (+ depmod).
################################################################################

MPS3_DFX_VERSION = 1.0
MPS3_DFX_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../../drivers/icap
MPS3_DFX_SITE_METHOD = local
MPS3_DFX_LICENSE = GPL-2.0
MPS3_DFX_LICENSE_FILES = mps3_dfx_drv.c

# The Kbuild here builds ONLY mps3_dfx.ko by default (obj-m := mps3_dfx.o);
# the CHECK_FPGA compile-veneer object stays gated off. Nothing to override.

# LOAD-BEARING clean (mirrors the mps3-harnessd/mps3-apps M6 gate finding):
# the local-site rsync (-au) carries the .o/.ko/.mod*/.cmd left in
# drivers/icap/ by the driver's OWN `make module` (built against the
# linux_soc base kernel, which has CONFIG_FPGA UNSET). This image's kernel
# has CONFIG_FPGA=y, so the fpga-mgr/bridge veneer must be (re)compiled IN;
# reusing the stale objects would either drop the veneer or trip a vermagic
# mismatch at insmod. Wipe every kbuild output before the module build hook
# runs so the package always compiles from source against THIS kernel.
define MPS3_DFX_CLEAN_STALE
	rm -f  $(@D)/*.o $(@D)/*.ko $(@D)/*.mod $(@D)/*.mod.c $(@D)/*.mod.o \
	       $(@D)/.*.o.cmd $(@D)/.*.ko.cmd $(@D)/.*.mod.cmd \
	       $(@D)/.*.mod.o.cmd $(@D)/modules.order $(@D)/Module.symvers
	rm -f  $(@D)/mps3_dfx_drv_fpga_check.c
	rm -rf $(@D)/.tmp_versions
endef
MPS3_DFX_PRE_BUILD_HOOKS += MPS3_DFX_CLEAN_STALE

# Its boot loader + deploy-time seam travel with the package (they used to sit
# in the default rootfs overlay, which would load mps3_dfx.ko on an image that
# no longer has it). LEGACY variant only (LINUX_HARNESS_PLAN DL6).
define MPS3_DFX_INSTALL_INIT
	$(INSTALL) -D -m 0755 $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-dfx/files/S10mps3dfx \
		$(TARGET_DIR)/etc/init.d/S10mps3dfx
	$(INSTALL) -D -m 0644 $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-dfx/files/dfx.conf \
		$(TARGET_DIR)/etc/mps3/dfx.conf
endef
MPS3_DFX_POST_INSTALL_TARGET_HOOKS += MPS3_DFX_INSTALL_INIT

$(eval $(kernel-module))
$(eval $(generic-package))
