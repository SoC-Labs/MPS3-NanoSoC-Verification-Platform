#---------------------------------------------------------------------------
# ddr4_pins.xdc  --  Arm MPS3 (xcku115-flvb1760-1-c) DDR4 SODIMM PACKAGE_PIN map
#---------------------------------------------------------------------------
#
# GENERATED FILE -- do not hand-edit.  Regenerate and diff:
#     python3 poc/ddr4_mbv/gen_pins.py
#     python3 poc/ddr4_mbv/gen_pins.py --check   # verify vs source
#
# Source of truth (read-only):
#   <nanosoc_tech>/fpga/targets/arm_mps3/fpga_pinmap.xdc
#   git blob sha1 : 88bb6d2abeba7c8fd16de7716393cca1d03f5d20
#   sha256        : 27dfa000e042b9a07f8901d47085bcc1e1fc620f2c5ba350fcf50f649faf5d48
#   size          : 54512 bytes
#
# The DDR4 pins live there as a commented-out block. This file parses the
# ACTIVE ('# ' single-hash) PACKAGE_PIN lines and rejects the DISABLED
# ('# #' double-hash) 2-rank/x8 socket options.
#
# REPAIR (provenance: inferred, marked below):
#   c0_ddr4_dqs_c[7] = J20
#     c0_ddr4_dqs_c[7] is absent from fpga_pinmap.xdc (its true complement
#     dqs_t[7] is present at K21). J20 is pad IO_L10N_T1U_N7_QBC_AD4N_51, the
#     DIFF_PAIR complement (DIFF_PAIR_PIN) of K21
#     (IO_L10P_T1U_N6_QBC_AD4P_51) on xcku115-flvb1760-1-c, and is otherwise
#     unassigned. Derived from the device pad table, not hand-copied.
#     Corroborated THREE independent ways: (1) Vivado 2024.1 DIFF_PAIR_PIN on
#     the real part; (2) PIN_FUNC query; (3) BSDL boundary-register pad
#     adjacency in xcku115_flvb1760.bsd, where K21=PAD864 and J20=PAD865 -- a
#     rule validated against all 9 known diff pairs in this pinout. Note the
#     seductive wrong answer: K22=PAD871 is the complement of L22=PAD870
#     (=dm_dbi_n[7]), NOT of K21. THIS IS THE ONLY INFERRED PIN IN THE FILE;
#     it is the one thing a board schematic could still overturn.
#
# SCOPE: this file supplies PACKAGE_PIN only. IOSTANDARD / DCI / slew /
# ODT / OUTPUT_IMPEDANCE constraints are expected to come from the DDR4
# IP's generated mig.xdc. The two non-PACKAGE_PIN properties the source
# does carry (for bg[0]) are surfaced verbatim at the end, marked, but
# mig.xdc remains authoritative for the electrical constraint set.
#---------------------------------------------------------------------------

# --- reset_n  (1 pin) ---
set_property PACKAGE_PIN E18 [get_ports {c0_ddr4_reset_n}]

# --- act_n  (1 pin) ---
set_property PACKAGE_PIN D19 [get_ports {c0_ddr4_act_n}]

# --- cs_n  (1 pin) ---
set_property PACKAGE_PIN F17 [get_ports {c0_ddr4_cs_n}]

# --- odt  (1 pin) ---
set_property PACKAGE_PIN E16 [get_ports {c0_ddr4_odt}]

# --- cke  (1 pin) ---
set_property PACKAGE_PIN E20 [get_ports {c0_ddr4_cke}]

# --- ck_t  (1 pin) ---
set_property PACKAGE_PIN P16 [get_ports {c0_ddr4_ck_t}]

# --- ck_c  (1 pin) ---
set_property PACKAGE_PIN N16 [get_ports {c0_ddr4_ck_c}]

# --- bg  (1 pin) ---
set_property PACKAGE_PIN F19 [get_ports {c0_ddr4_bg[0]}]

# --- ba  (2 pins) ---
set_property PACKAGE_PIN G17 [get_ports {c0_ddr4_ba[0]}]
set_property PACKAGE_PIN G19 [get_ports {c0_ddr4_ba[1]}]

# --- adr  (17 pins) ---
set_property PACKAGE_PIN L19 [get_ports {c0_ddr4_adr[0]}]
set_property PACKAGE_PIN J16 [get_ports {c0_ddr4_adr[1]}]
set_property PACKAGE_PIN L18 [get_ports {c0_ddr4_adr[2]}]
set_property PACKAGE_PIN K16 [get_ports {c0_ddr4_adr[3]}]
set_property PACKAGE_PIN K18 [get_ports {c0_ddr4_adr[4]}]
set_property PACKAGE_PIN J18 [get_ports {c0_ddr4_adr[5]}]
set_property PACKAGE_PIN L17 [get_ports {c0_ddr4_adr[6]}]
set_property PACKAGE_PIN M16 [get_ports {c0_ddr4_adr[7]}]
set_property PACKAGE_PIN K17 [get_ports {c0_ddr4_adr[8]}]
set_property PACKAGE_PIN M17 [get_ports {c0_ddr4_adr[9]}]
set_property PACKAGE_PIN M19 [get_ports {c0_ddr4_adr[10]}]
set_property PACKAGE_PIN M15 [get_ports {c0_ddr4_adr[11]}]
set_property PACKAGE_PIN N17 [get_ports {c0_ddr4_adr[12]}]
set_property PACKAGE_PIN N19 [get_ports {c0_ddr4_adr[13]}]
set_property PACKAGE_PIN H16 [get_ports {c0_ddr4_adr[14]}]
set_property PACKAGE_PIN G16 [get_ports {c0_ddr4_adr[15]}]
set_property PACKAGE_PIN H17 [get_ports {c0_ddr4_adr[16]}]

# --- dq  (64 pins) ---
set_property PACKAGE_PIN P11 [get_ports {c0_ddr4_dq[0]}]
set_property PACKAGE_PIN P10 [get_ports {c0_ddr4_dq[1]}]
set_property PACKAGE_PIN L12 [get_ports {c0_ddr4_dq[2]}]
set_property PACKAGE_PIN M12 [get_ports {c0_ddr4_dq[3]}]
set_property PACKAGE_PIN N13 [get_ports {c0_ddr4_dq[4]}]
set_property PACKAGE_PIN N12 [get_ports {c0_ddr4_dq[5]}]
set_property PACKAGE_PIN M10 [get_ports {c0_ddr4_dq[6]}]
set_property PACKAGE_PIN L10 [get_ports {c0_ddr4_dq[7]}]
set_property PACKAGE_PIN J15 [get_ports {c0_ddr4_dq[8]}]
set_property PACKAGE_PIN K15 [get_ports {c0_ddr4_dq[9]}]
set_property PACKAGE_PIN K13 [get_ports {c0_ddr4_dq[10]}]
set_property PACKAGE_PIN J14 [get_ports {c0_ddr4_dq[11]}]
set_property PACKAGE_PIN K11 [get_ports {c0_ddr4_dq[12]}]
set_property PACKAGE_PIN K10 [get_ports {c0_ddr4_dq[13]}]
set_property PACKAGE_PIN J13 [get_ports {c0_ddr4_dq[14]}]
set_property PACKAGE_PIN K12 [get_ports {c0_ddr4_dq[15]}]
set_property PACKAGE_PIN E12 [get_ports {c0_ddr4_dq[16]}]
set_property PACKAGE_PIN F12 [get_ports {c0_ddr4_dq[17]}]
set_property PACKAGE_PIN E15 [get_ports {c0_ddr4_dq[18]}]
set_property PACKAGE_PIN F15 [get_ports {c0_ddr4_dq[19]}]
set_property PACKAGE_PIN H12 [get_ports {c0_ddr4_dq[20]}]
set_property PACKAGE_PIN G12 [get_ports {c0_ddr4_dq[21]}]
set_property PACKAGE_PIN G15 [get_ports {c0_ddr4_dq[22]}]
set_property PACKAGE_PIN G14 [get_ports {c0_ddr4_dq[23]}]
set_property PACKAGE_PIN B14 [get_ports {c0_ddr4_dq[24]}]
set_property PACKAGE_PIN D13 [get_ports {c0_ddr4_dq[25]}]
set_property PACKAGE_PIN A13 [get_ports {c0_ddr4_dq[26]}]
set_property PACKAGE_PIN A14 [get_ports {c0_ddr4_dq[27]}]
set_property PACKAGE_PIN B15 [get_ports {c0_ddr4_dq[28]}]
set_property PACKAGE_PIN C13 [get_ports {c0_ddr4_dq[29]}]
set_property PACKAGE_PIN A15 [get_ports {c0_ddr4_dq[30]}]
set_property PACKAGE_PIN A12 [get_ports {c0_ddr4_dq[31]}]
set_property PACKAGE_PIN C16 [get_ports {c0_ddr4_dq[32]}]
set_property PACKAGE_PIN C19 [get_ports {c0_ddr4_dq[33]}]
set_property PACKAGE_PIN B19 [get_ports {c0_ddr4_dq[34]}]
set_property PACKAGE_PIN A20 [get_ports {c0_ddr4_dq[35]}]
set_property PACKAGE_PIN D16 [get_ports {c0_ddr4_dq[36]}]
set_property PACKAGE_PIN A17 [get_ports {c0_ddr4_dq[37]}]
set_property PACKAGE_PIN A18 [get_ports {c0_ddr4_dq[38]}]
set_property PACKAGE_PIN A19 [get_ports {c0_ddr4_dq[39]}]
set_property PACKAGE_PIN C23 [get_ports {c0_ddr4_dq[40]}]
set_property PACKAGE_PIN C21 [get_ports {c0_ddr4_dq[41]}]
set_property PACKAGE_PIN A24 [get_ports {c0_ddr4_dq[42]}]
set_property PACKAGE_PIN A25 [get_ports {c0_ddr4_dq[43]}]
set_property PACKAGE_PIN C22 [get_ports {c0_ddr4_dq[44]}]
set_property PACKAGE_PIN D21 [get_ports {c0_ddr4_dq[45]}]
set_property PACKAGE_PIN A22 [get_ports {c0_ddr4_dq[46]}]
set_property PACKAGE_PIN A23 [get_ports {c0_ddr4_dq[47]}]
set_property PACKAGE_PIN D25 [get_ports {c0_ddr4_dq[48]}]
set_property PACKAGE_PIN E23 [get_ports {c0_ddr4_dq[49]}]
set_property PACKAGE_PIN G22 [get_ports {c0_ddr4_dq[50]}]
set_property PACKAGE_PIN G21 [get_ports {c0_ddr4_dq[51]}]
set_property PACKAGE_PIN D23 [get_ports {c0_ddr4_dq[52]}]
set_property PACKAGE_PIN D24 [get_ports {c0_ddr4_dq[53]}]
set_property PACKAGE_PIN F24 [get_ports {c0_ddr4_dq[54]}]
set_property PACKAGE_PIN F23 [get_ports {c0_ddr4_dq[55]}]
set_property PACKAGE_PIN H22 [get_ports {c0_ddr4_dq[56]}]
set_property PACKAGE_PIN J23 [get_ports {c0_ddr4_dq[57]}]
set_property PACKAGE_PIN K20 [get_ports {c0_ddr4_dq[58]}]
set_property PACKAGE_PIN L20 [get_ports {c0_ddr4_dq[59]}]
set_property PACKAGE_PIN H21 [get_ports {c0_ddr4_dq[60]}]
set_property PACKAGE_PIN H23 [get_ports {c0_ddr4_dq[61]}]
set_property PACKAGE_PIN K23 [get_ports {c0_ddr4_dq[62]}]
set_property PACKAGE_PIN J21 [get_ports {c0_ddr4_dq[63]}]

# --- dm_dbi_n  (8 pins) ---
set_property PACKAGE_PIN N14 [get_ports {c0_ddr4_dm_n[0]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN L14 [get_ports {c0_ddr4_dm_n[1]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN H14 [get_ports {c0_ddr4_dm_n[2]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN D14 [get_ports {c0_ddr4_dm_n[3]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN C18 [get_ports {c0_ddr4_dm_n[4]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN C24 [get_ports {c0_ddr4_dm_n[5]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN H24 [get_ports {c0_ddr4_dm_n[6]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)
set_property PACKAGE_PIN L22 [get_ports {c0_ddr4_dm_n[7]}]    ;# source name: c0_ddr4_dm_dbi_n (ddr4_rtl bus calls it DM_N)

# --- dqs_t  (8 pins) ---
set_property PACKAGE_PIN N11 [get_ports {c0_ddr4_dqs_t[0]}]
set_property PACKAGE_PIN J11 [get_ports {c0_ddr4_dqs_t[1]}]
set_property PACKAGE_PIN F14 [get_ports {c0_ddr4_dqs_t[2]}]
set_property PACKAGE_PIN C12 [get_ports {c0_ddr4_dqs_t[3]}]
set_property PACKAGE_PIN B17 [get_ports {c0_ddr4_dqs_t[4]}]
set_property PACKAGE_PIN B22 [get_ports {c0_ddr4_dqs_t[5]}]
set_property PACKAGE_PIN E22 [get_ports {c0_ddr4_dqs_t[6]}]
set_property PACKAGE_PIN K21 [get_ports {c0_ddr4_dqs_t[7]}]

# --- dqs_c  (8 pins)  [1 inferred] ---
set_property PACKAGE_PIN M11 [get_ports {c0_ddr4_dqs_c[0]}]
set_property PACKAGE_PIN J10 [get_ports {c0_ddr4_dqs_c[1]}]
set_property PACKAGE_PIN F13 [get_ports {c0_ddr4_dqs_c[2]}]
set_property PACKAGE_PIN B12 [get_ports {c0_ddr4_dqs_c[3]}]
set_property PACKAGE_PIN B16 [get_ports {c0_ddr4_dqs_c[4]}]
set_property PACKAGE_PIN B21 [get_ports {c0_ddr4_dqs_c[5]}]
set_property PACKAGE_PIN E21 [get_ports {c0_ddr4_dqs_c[6]}]
set_property PACKAGE_PIN J20 [get_ports {c0_ddr4_dqs_c[7]}]    ;# INFERRED: DIFF_PAIR complement of K21 (see header)

# --- c0_sys_clk_p  (1 pin) ---
set_property PACKAGE_PIN H19 [get_ports {c0_sys_clk_p}]

# --- c0_sys_clk_n  (1 pin) ---
set_property PACKAGE_PIN H18 [get_ports {c0_sys_clk_n}]

#---------------------------------------------------------------------------
# Non-PACKAGE_PIN properties found in the source (verbatim, MARKED).
# Prefer mig.xdc for the full electrical set; these are surfaced only
# because the source explicitly pins them.
#---------------------------------------------------------------------------
set_property IOSTANDARD SSTL12_DCI [get_ports {c0_ddr4_bg[0]}]    ;# SOURCE (verbatim)
set_property OUTPUT_IMPEDANCE RDRV_40_40 [get_ports {c0_ddr4_bg[0]}]    ;# SOURCE (verbatim)
