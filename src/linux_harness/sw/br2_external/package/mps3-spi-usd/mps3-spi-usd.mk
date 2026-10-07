################################################################################
# mps3-spi-usd — spi-usd.ko, the user-microSD SPI controller (sources in src/).
# Buildroot kernel-module infrastructure: built against THIS image's kernel,
# modules_install'ed under /lib/modules/<ver>/updates, depmod'ed by Buildroot.
################################################################################

MPS3_SPI_USD_VERSION = 1.0
MPS3_SPI_USD_SITE = $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-spi-usd/src
MPS3_SPI_USD_SITE_METHOD = local
MPS3_SPI_USD_LICENSE = GPL-2.0
MPS3_SPI_USD_LICENSE_FILES = spi-usd.c

# SITE_METHOD=local rsyncs src/ as-is: drop any kbuild output a developer's
# `make KDIR=...` compile-check left there, so the module is always built
# against this image's kernel (the mps3-dfx M6 lesson).
define MPS3_SPI_USD_CLEAN_STALE
	rm -f $(@D)/*.o $(@D)/*.ko $(@D)/*.mod $(@D)/*.mod.c $(@D)/.*.cmd \
	      $(@D)/modules.order $(@D)/Module.symvers
endef
MPS3_SPI_USD_PRE_BUILD_HOOKS += MPS3_SPI_USD_CLEAN_STALE

# S11modules (package/initscripts) modprobes every name listed here.
define MPS3_SPI_USD_INSTALL_MODULES_LOAD
	$(INSTALL) -D -m 0644 $(BR2_EXTERNAL_MPS3_HARNESS_PATH)/package/mps3-spi-usd/mps3-spi-usd.conf \
		$(TARGET_DIR)/etc/modules-load.d/mps3-spi-usd.conf
endef
MPS3_SPI_USD_POST_INSTALL_TARGET_HOOKS += MPS3_SPI_USD_INSTALL_MODULES_LOAD

$(eval $(kernel-module))
$(eval $(generic-package))
