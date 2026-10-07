device jtagport soft
device xilinxinsertbufg 0
device skewfree 1
device stop_on_signal_not_found 1
iice new {IICE_CPU} -type regular
iice clock   -iice {IICE_CPU} -edge positive {/dut_clk}
iice sampler -iice {IICE_CPU} -depth 1024
iice controller -iice {IICE_CPU} none
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HADDR}
signals add -iice {IICE_CPU} -sample -trigger {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HTRANS}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HPROT}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HREADY}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HWRITE}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HRESP}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HRDATA}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/HWDATA}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/CORE_LOCKUP}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/CORE_SLEEPING}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/CORE_SYSRESETREQ}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/u_cpu_0/SYS_HRESETn}
signals add -iice {IICE_CPU} -sample {/u_rm/u_nanosoc/u_ss_cpu/sys_remap_ctrl}
