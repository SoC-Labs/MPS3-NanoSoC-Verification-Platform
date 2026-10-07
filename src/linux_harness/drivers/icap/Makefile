# MPS3 DFX swap driver — build entry points.
#
#   make module       cross-build mps3_dfx.ko against the Buildroot 6.18.7 tree
#   make check-fpga   compile-check the fpga-mgr/bridge veneer (CONFIG_FPGA is
#                     unset in this kernel; unresolved symbols are expected and
#                     downgraded to warnings — the object is never loaded)
#   make host-tests   run the host unit tests
#   make swaptool     cross-build the guest test tool
#   make clean

# Buildroot output tree to build against. Default = this checkout's
# src/linux_soc/linux build; override BR for another one.
BR      ?= $(abspath $(CURDIR)/../../../linux_soc/linux/buildroot/output)
KDIR    ?= $(BR)/build/linux-6.18.7
CROSS   ?= $(BR)/host/bin/riscv32-amd-linux-gnu-
ARCH    ?= riscv

module:
	$(MAKE) -C $(KDIR) M=$(CURDIR) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS) modules

check-fpga:
	cp mps3_dfx_drv.c mps3_dfx_drv_fpga_check.c
	$(MAKE) -C $(KDIR) M=$(CURDIR) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS) \
		CHECK_FPGA=1 KBUILD_MODPOST_WARN=1 modules
	rm -f mps3_dfx_drv_fpga_check.c

host-tests:
	$(MAKE) -C tests/host run

swaptool:
	$(CROSS)gcc -O2 -Wall -Wextra -std=gnu99 -static \
		-o tests/qemu/swaptool tests/qemu/swaptool.c

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS) clean || true
	$(MAKE) -C tests/host clean
	rm -f tests/qemu/swaptool mps3_dfx_drv_fpga_check.c

.PHONY: module check-fpga host-tests swaptool clean
