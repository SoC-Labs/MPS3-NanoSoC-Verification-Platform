# Out-of-tree build of spi-usd.ko. Buildroot's kernel-module infrastructure
# (package/mps3-spi-usd/mps3-spi-usd.mk) drives Kbuild directly; this Makefile
# is for a developer compile-check against any configured kernel tree:
#     make KDIR=<linux build dir> ARCH=riscv CROSS_COMPILE=<prefix>
KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
