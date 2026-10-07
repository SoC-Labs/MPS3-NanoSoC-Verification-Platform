##############################################################################
# lcd_mirror_ooc.xdc -- standalone (out-of-context) timing for LCDMIR.
#
# ONE clock: s_axi_aclk, the 100 MHz shell clock (clk_wiz_shell CLKOUT1,
# shell_bd.tcl). Every tap input is a registered clcd_kvm_0 output in the SAME
# domain (LCD_MIRROR_FPGA.md §1.2), so the tap is NOT an async boundary: its
# ports get an input delay against the same clock, which is what they are at
# the link (reg -> combinational KVM mux -> this block's t1 register).
# The AXI-Lite ports likewise come from axi_interconnect_0 on the same clock.
# At the BD link these are superseded by the real propagated clock.
##############################################################################
create_clock -name s_axi_aclk -period 10.000 -waveform {0.000 5.000} [get_ports s_axi_aclk]

# Inputs: budget half the period to the (interconnect / KVM mux) upstream logic.
set_input_delay  -clock s_axi_aclk 5.000 [get_ports -quiet {s_axi_aw* s_axi_w* s_axi_b* s_axi_ar* s_axi_r*} -filter {DIRECTION == IN}]
set_input_delay  -clock s_axi_aclk 5.000 [get_ports -quiet {lcd_*}]
set_input_delay  -clock s_axi_aclk 5.000 [get_ports -quiet {s_axi_aresetn}]
# Outputs: the interconnect registers the response channels.
set_output_delay -clock s_axi_aclk 3.000 [get_ports -quiet {s_axi_*} -filter {DIRECTION == OUT}]
