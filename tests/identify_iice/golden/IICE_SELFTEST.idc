device jtagport soft
device xilinxinsertbufg 0
device skewfree 1
device stop_on_signal_not_found 1
iice new {IICE_SELFTEST} -type regular
iice clock   -iice {IICE_SELFTEST} -edge positive {/tb/u_dut/clk}
iice sampler -iice {IICE_SELFTEST} -depth 64
iice controller -iice {IICE_SELFTEST} statemachine
iice controller -iice {IICE_SELFTEST} -triggerconditions 2 -triggerstates 2
signals add -iice {IICE_SELFTEST} -sample -trigger {/tb/u_dut/haddr}
signals add -iice {IICE_SELFTEST} -sample -trigger {/tb/u_dut/htrans}
signals add -iice {IICE_SELFTEST} -sample {/tb/u_dut/hwrite}
signals add -iice {IICE_SELFTEST} -sample {/tb/u_dut/hrdata}
signals add -iice {IICE_SELFTEST} -sample {/tb/u_dut/hready}
signals add -iice {IICE_SELFTEST} -sample {/tb/u_dut/state}
signals add -iice {IICE_SELFTEST} -sample {/tb/u_dut/lockup}
