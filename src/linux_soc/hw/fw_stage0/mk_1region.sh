#!/bin/bash
# mk_1region.sh -- DEPRECATED (2026-09-23). Do not use; it now refuses to run.
#
# The 1-region blob (OpenSBI FW_PAYLOAD + slim nowfi kernel + embedded
# initramfs + DTB, one region at 0x80000000) is built by IMAGE's build.sh, the
# ONE recipe:
#     src/linux_harness/sw/build.sh   ->   artifacts/fw_payload_1region.bin
# then packed for stage0 (a uSD slot or a rescue push are the same bytes):
#     stage0_pack.py --out boot.img --pc 0x80000000 --a1 0 \
#         artifacts/fw_payload_1region.bin@0x80000000
#
# Why this script is retired rather than kept as a second recipe: it rebuilt the
# kernel with a toggled Buildroot config, and both July traps came from that --
# it wiped target/usr/lib/modules without reinstalling them (the July blob's
# initramfs has no /lib/modules) and its re-applied fragments dropped NOWFI (the
# blob carried the wfi kernel). It also hard-coded the -linux worktree.
# build.sh builds one configuration once, and checks both (IMAGE_CONTRACT §1).
echo "mk_1region.sh is DEPRECATED: build the 1-region blob with src/linux_harness/sw/build.sh" >&2
echo "  (-> artifacts/fw_payload_1region.bin), then stage0_pack.py --pc 0x80000000 --a1 0 <blob>@0x80000000" >&2
exit 2
