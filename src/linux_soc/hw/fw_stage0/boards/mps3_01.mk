# boards/mps3_01.mk -- board 1's baked identity (fpgahub node mps3_01).
# `make stage0.elf S0_BOARD=mps3_01 ...`; the values are validated by
# stage0_board.py and published in the stage0 status block at every entry
# (STAGE0_CONTRACT §3.3). These are also the stage0 defaults.
S0_LABEL := MPS3-01
S0_IP    := 192.168.10.101
S0_MAC   := 02:00:00:4D:50:53
