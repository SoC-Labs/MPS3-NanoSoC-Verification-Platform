# =============================================================================
# tests/gpio_shield/Makefile
#
# Output-only GPIO passthrough: DUT dut_gpio_o -> REAL board_gpio -> board_pad
# -> §6 IOBUF -> SH0_IO shield pad (docs/CONNECTOR_SURVEY.md §6). Validates the
# RTL path the "route dut_gpio to the shield/Pmod" change would deploy, before
# any board time. Pure-VCS SV, standalone like tests/lan8720_sut.
#
#   source <repo-root>/set_env.sh
#   make -C tests/gpio_shield
# =============================================================================
SIM ?= vcs
ifneq ($(SIM),vcs)
$(error VCS-only pure-SV bench. Got SIM=$(SIM); use SIM=vcs)
endif

THIS_DIR   := $(abspath $(CURDIR))
MPS3_ROOT  := $(abspath $(CURDIR)/../..)
BOARD_GPIO := $(MPS3_ROOT)/fpga/shell/ip/board_gpio/board_gpio.sv
TB         := $(THIS_DIR)/tb_gpio_shield.sv

VCS      := vcs
VCS_ARGS := -full64 -sverilog -timescale=1ns/1ps -notice -licqueue -top tb_gpio_shield

.PHONY: all run result clean

all: run

run:
	@test -f "$(BOARD_GPIO)" || { echo "ERROR: board_gpio.sv not found: $(BOARD_GPIO)"; exit 1; }
	@echo "== build (real board_gpio + §6 IOBUF model) =="
	$(VCS) $(VCS_ARGS) $(BOARD_GPIO) $(TB) -o simv_gpio -l comp.log
	@echo "== run =="
	./simv_gpio -l run.log
	@grep -E 'GPIO_SH:' run.log | grep -vE '^\s*$$' || true
	@grep -q 'VERDICT=PASS' run.log || { echo "FAIL: output-only passthrough not proven"; exit 1; }
	@echo "OK: dut_gpio_o reaches the SH0 shield pad, and OE=0 releases it (hi-Z)."

result: clean run
	@{ echo "gpio_shield -- output-only dut_gpio -> SH0 shield pad passthrough"; \
	   echo "DUT   : fpga/shell/ip/board_gpio/board_gpio.sv (real synthesizable RTL)"; \
	   echo "IOBUF : behavioural per-bit (matches CONNECTOR_SURVEY.md §6 sketch)"; \
	   echo "VCS   : $$(vcs -ID 2>/dev/null | head -1)"; echo; \
	   cat run.log; } > RESULT.txt
	@echo "wrote RESULT.txt"

clean:
	rm -rf csrc simv_gpio simv_gpio.daidir *.log ucli.key vc_hdrs.h .vlogansetup.* \
	       AN.DB novas.* verdi* DVEfiles *.vpd
