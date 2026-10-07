#-----------------------------------------------------------------------------
# qspi_loader_jtag.tcl -- drive firmware/qspi_loader over the LIVE debug path:
# OpenOCD remote_bitbang -> fw jtag_server on 6921 -> SoC-400 SWJ-DP (JTAG).
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license. Copyright (C) 2026, SoC Labs (www.soclabs.org)
#
# WHY THIS EXISTS (2026-09-23)
#   The proven host driver (host/pyverify/pyverify/qspi_loader.py, silicon
#   2026-07-19..21) runs over pyverify.swd.SwdDebugger: SWD on 6920. 6920 is
#   DEAD on every shell since the SWD->JTAG migration (0xCD74B6AE onward). The
#   JTAG cfg (host/openocd/nanosoc_mps3_jtag.cfg) creates the cortex_m target
#   with -defer-examine, so EVERY fresh OpenOCD session must halt-via-apreg and
#   examine before halt/mdw/load_image work -- and SwdDebugger opens a fresh
#   session per op, which would HALT the loader on every mailbox poll. So the
#   Python client cannot be pointed at 6921 by config alone.
#
#   This file is the same mailbox protocol, op for op, in ONE OpenOCD session:
#   examine once, enter the loader, then poll the mailbox with plain memory
#   reads while the M0 runs (AHB-AP reads of IMEM do not need a halt; the
#   AHBSLV=0 caveat in the cfg is about PPB/0xE000xxxx reads only).
#
# STATUS: NOT RUN ON SILICON. Written 2026-09-23 for W1 (2026-09-24). Every
#   constant is copied from qspi_loader.c / pyverify/qspi_loader.py (the
#   silicon-proven pair); the procedure is fpga/rp/nanosoc_upy/README.md "W1".
#   Needs OpenOCD >= 0.12 (read_memory).
#
# USAGE (on the hub; paths are paths ON THE HUB):
#   openocd -c "set TRANSPORT_MODE rbb" -c "set RBB_HOST 192.168.10.101" \
#           -c "set RBB_PORT 6921" \
#           -f <repo>/host/openocd/nanosoc_mps3_jtag.cfg \
#           -f <repo>/firmware/qspi_loader/qspi_loader_jtag.tcl \
#           -c init \
#           -c "ql_enter <stage>/qspi_loader.bin" \
#           -c "ql_crc 0x0 163840" \
#           -c shutdown
#   Program (image pre-split into 48 KiB chunks, see README):
#           -c "ql_enter <stage>/qspi_loader.bin" \
#           -c "ql_program_split <stage>/chunk_ 4 0x0 163840" \
#           -c "ql_crc 0x0 163840" -c shutdown
#-----------------------------------------------------------------------------

# --- mailbox contract: MUST match firmware/qspi_loader/qspi_loader.c --------
set QL_LOAD_ADDR   0x10000000
set QL_MB          0x10010000
set QL_BUFFER      0x10010040
set QL_BUFFER_SIZE 0xC000          ;# 48 KiB
set QL_STACK_TOP   0x10020000      ;# needs the 128 KiB IMEM (rm_nanosoc_upy has it)
set QL_MAGIC       0x51464C44      ;# "QFLD"
# offsets inside the mailbox
set QL_MB_MAGIC  [expr {$QL_MB + 0x00}]
set QL_MB_CMD    [expr {$QL_MB + 0x04}]
set QL_MB_OFFSET [expr {$QL_MB + 0x08}]
set QL_MB_LENGTH [expr {$QL_MB + 0x0C}]
set QL_MB_STATUS [expr {$QL_MB + 0x10}]
set QL_MB_CRC    [expr {$QL_MB + 0x14}]
# commands / status
set QL_CMD_UNLOCK 1
set QL_CMD_ERASE  2
set QL_CMD_PROG   3
set QL_CMD_CRC    4
set QL_ST_BUSY    1
set QL_ST_OK      2
set QL_ST_ERR     0x80000000

proc ql_rd {addr} {
    return [lindex [read_memory $addr 32 1] 0]
}

proc ql_hex {v} { return [format 0x%08X $v] }

# ql_enter <loader.bin> -- halt FIRST, quiesce the previous image's SysTick and
# NVIC (ARMv6-M cannot clear an ACTIVE exception; reset-halt is not reliable on
# this DUT), load the loader at IMEM base, set SP/PC, resume, wait for magic.
proc ql_enter {loader_bin {timeout_ms 30000}} {
    global QL_LOAD_ADDR QL_MB_MAGIC QL_STACK_TOP QL_MAGIC
    # -defer-examine target: halt through raw AP writes, then examine.
    nanosoc_halt_examine
    halt
    mww 0xE000E010 0              ;# SYST_CSR = 0 (SysTick off)
    mww 0xE000E180 0xFFFFFFFF     ;# NVIC_ICER0: disable all
    mww 0xE000E280 0xFFFFFFFF     ;# NVIC_ICPR0: clear all pending
    load_image $loader_bin $QL_LOAD_ADDR bin
    set got [ql_rd $QL_LOAD_ADDR]
    echo "ql_enter: loader word0 @[ql_hex $QL_LOAD_ADDR] = [ql_hex $got] (expect the loader's first word, not 0)"
    mww $QL_MB_MAGIC 0            ;# a stale loader must not look live
    halt
    reg sp $QL_STACK_TOP
    reg pc $QL_LOAD_ADDR
    resume
    set waited 0
    while {$waited < $timeout_ms} {
        if {[ql_rd $QL_MB_MAGIC] == $QL_MAGIC} {
            echo "ql_enter: LOADER LIVE (magic [ql_hex $QL_MAGIC]) after ${waited} ms"
            return
        }
        sleep 200
        incr waited 200
    }
    error "ql_enter: no magic at [ql_hex $QL_MB_MAGIC] within ${timeout_ms} ms -- loader did not enter (SP/PC?) or wedged in a stale handler"
}

# ql_cmd <cmd> <offset> <length> -- offset/length BEFORE cmd (the DUT polls
# cmd and reads the rest once it is non-zero), then poll status.
proc ql_cmd {cmd offset length {timeout_ms 600000}} {
    global QL_MB_CMD QL_MB_OFFSET QL_MB_LENGTH QL_MB_STATUS QL_ST_BUSY QL_ST_OK QL_ST_ERR
    mww $QL_MB_OFFSET $offset
    mww $QL_MB_LENGTH $length
    mww $QL_MB_STATUS $QL_ST_BUSY
    mww $QL_MB_CMD    $cmd
    set waited 0
    while {$waited < $timeout_ms} {
        set st [ql_rd $QL_MB_STATUS]
        if {$st == $QL_ST_OK} { return }
        if {$st & $QL_ST_ERR} {
            error "ql_cmd $cmd off=[ql_hex $offset] len=$length FAILED: status [ql_hex $st] (ERR 1=controller BUSY timeout 2=flash WIP timeout 3=bad length 4=bad command)"
        }
        sleep 100
        incr waited 100
    }
    error "ql_cmd $cmd off=[ql_hex $offset] len=$length: no completion within ${timeout_ms} ms"
}

# ql_crc <offset> <length> -- DUT-side CRC32 of flash. READ-ONLY.
proc ql_crc {offset length} {
    global QL_CMD_CRC QL_MB_CRC
    ql_cmd $QL_CMD_CRC $offset $length
    set crc [ql_rd $QL_MB_CRC]
    echo "ql_crc: flash [ql_hex $offset]+$length CRC32 = [ql_hex $crc]"
    return $crc
}

# ql_program_split <prefix> <nchunks> <base> <total_len>
#   Erase [base, base+total_len), then program chunk files <prefix>00,
#   <prefix>01, ... (made with `split -b 49152 -d -a 2 flash_image.bin <prefix>`)
#   at base + k*48 KiB. Each chunk's size is the file's size. No verify here:
#   run ql_crc afterwards and compare with the image CRC32 on the host.
proc ql_program_split {prefix nchunks base total_len} {
    global QL_CMD_UNLOCK QL_CMD_ERASE QL_CMD_PROG QL_BUFFER QL_BUFFER_SIZE
    ql_cmd $QL_CMD_UNLOCK 0 0
    echo "ql: unlocked (WREN+ULBPR)"
    ql_cmd $QL_CMD_ERASE $base $total_len
    echo "ql: erased [ql_hex $base]+$total_len"
    for {set k 0} {$k < $nchunks} {incr k} {
        set f [format "%s%02d" $prefix $k]
        set n [file size $f]
        if {$n <= 0 || $n > $QL_BUFFER_SIZE} { error "ql: chunk $f has bad size $n" }
        load_image $f $QL_BUFFER bin
        set off [expr {$base + $k * $QL_BUFFER_SIZE}]
        ql_cmd $QL_CMD_PROG $off $n
        echo "ql: programmed chunk $k ($n B) at [ql_hex $off]"
    }
}

echo "qspi_loader_jtag.tcl: procs ql_enter / ql_crc / ql_program_split loaded (UNPROVEN on silicon)"
