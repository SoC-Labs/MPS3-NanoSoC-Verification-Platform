################################################################################
# fpga/board/mps3-hbi0309c/board.tcl  --  THE BOARD PACK for the Arm MPS3
#
# The Arm V2M-MPS3 (HBI0309, rev C) prototyping motherboard: a bare Kintex
# UltraScale XCKU115 in an FLVB1760 package, speed grade -1, commercial, with an
# on-board Motherboard Configuration Controller (MCC) that loads the FPGA from a
# config microSD at power-on.
#
# PCB FACTS ONLY.  Which device is soldered down, what the clock the design is
# closed at runs at, how a loadable image has to be formatted for this family,
# which names this bench addresses the board by.  Every one of them would still
# be true for a COMPLETELY DIFFERENT DESIGN on this board, which is the test.
#
# NOT IN HERE: a pin, a port name, a clock name, a module.  Those are facts
# about a design meeting a board and they live with the rest of the target.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
################################################################################

# The name this bench and this repository call the board. HBI0309 is Arm's
# assembly number; the C is the revision this platform is built and tested
# against, and it is in the name because A/B/C differ at the SD-config level
# (see the oscillator note below) even though no pin differs between them.
board_set board_name mps3-hbi0309c

# XCKU115, FLVB1760, speed grade -1, commercial. Two SLRs - the part pack
# carries that and the Laguna crossing facts that follow from it.
board_set part xcku115-flvb1760-1-c

# No processing system. This is a bare FPGA: no PS7, no PS8, no boot ROM on the
# die, nothing that brings the fabric up but the MCC and the config card.
board_set platform bare

# OSCCLK[1], the 50 MHz board oscillator the monolithic baseline closes on. It
# reaches the design through a BUFG and nothing else - there is no MMCM in that
# clock path.
#
# READ THE OSCILLATOR NOTE BELOW BEFORE TRUSTING THIS FOR ANY OTHER OSCILLATOR.
board_set sys_clk_freq_hz 50000000

# THE MCC LOADS A .bit. THERE IS NO CONVERSION AND THERE IS NO .bin.
#
# Measured across the whole SD path: the card carries the bitstream renamed to
# exactly nanosoc.bit, named by F0FILE: in Nanosoc/nanosoc.txt and reached via
# APPFILE: in MB/HBI0309<rev>/board.txt. No byte swap, no header strip, no .bin
# anywhere. (fpga/mps3_sd/ declares a bitstream_*_bin artefact in one place; it
# is referenced by no action and no such file has ever been produced.)
#
# none is a STATEMENT, not a waiver: it says somebody looked and there is
# nothing to convert. Leaving this key unset would mean nobody chose, and the
# toolkit still refuses that - correctly.
board_set bin_style none

# HBI0309, revision C. A/B/C share every pin; what differs is the config-SD
# bundle the MCC reads, which is why the revision is a board fact here.
board_set board_rev HBI0309C

# Bitstream configuration bank settings, from the two files that descend from
# the hardware-proven legacy pinmap (fpga/monolithic/nanosoc_mps3.xdc and
# fpga/shell/constraints/mps3_harness.xdc). Both say VCCO / 3.3.
#
# DELIBERATE DISAGREEMENT NOTED: fpga/dfx/proof/proof.xdc says GND / 1.8. Two
# files agree against one, and the two that agree are the ones carrying the
# 272-pin map that came up on real hardware. If a build ever fails CFGBVS-1 or
# comes up dead with no message, this is the first line to re-test - do not
# assume it because it is written down here.
board_set cfgbvs VCCO
board_set config_voltage 3.3

# The MCC reads the bitstream off a config microSD. Note this key currently
# selects nothing in the toolkit - it is recorded, not acted on - so the SD
# bundle is still assembled by fpga/mps3_sd/assemble_sd.sh.
board_set deploy_style sd

# Deliberately NOT set: board_part (no Vivado board file is used; the XDC is the
# whole board description) and fpgahub_board / fpgahub_target. Both trigger
# required-key cascades this board does not need, and the fpgahub entry that
# exists for this board today points at a manifest path under a home directory
# that does not exist on this machine. Wiring to it would be wiring to something
# broken.

board_note {
    THE OSCILLATORS ARE MCC-PROGRAMMED, SO sys_clk_freq_hz IS NOT THE WHOLE
    TRUTH ON THIS BOARD.

    OSCCLK 0 to 6 are set by the OSCCLKS section of Nanosoc/nanosoc.txt on the
    config microSD, not by crystals. The SD bundle is therefore part of the
    clock contract, and a design can be constrained correctly and still run at
    the wrong rate because the card in the slot says something else.

    There is a live inconsistency to be aware of. The platform's own template
    stamps OSC0: 25.0 and records that 24.0 was the value that broke Ethernet.
    ethernet-subsystem-ahb's MPS3 target declares create_clock -period 41.667
    (24 MHz) on OSCCLK/AL15 and stamps OSC0: 24.0 in its own MCC config. Each is
    self-consistent; build one and boot it from the other bundle and the
    constraint is wrong by about 4 percent.

    sys_clk_freq_hz above is the 50 MHz OSCCLK 1, which both agree on. OSC0 is
    the one in dispute and no design in this repository closes on it.
}

board_note {
    HOW A BITSTREAM REACHES THIS BOARD, and the four things that have each cost
    somebody real time:

      APPFILE uses a DOS BACKSLASH separator - it is MANDATORY. A forward slash
        silently fails to find the file and leaves the FPGA unprogrammed: dead
        PBON, no UART, no message anywhere.
      The bitstream must be renamed to EXACTLY nanosoc.bit - what F0FILE
        expects.
      MBBIOS mbb_v141.ebf is stock Arm MCC firmware already on the card. It is
        not shipped by this repository and must not be deleted.
      UARTMODE 1 selects which of FPGA lanes 0 and 1 reaches the host through
        the MCC mux. Lanes 2 and 3 are hard-wired and unaffected - and lane 2 is
        the console.
}
