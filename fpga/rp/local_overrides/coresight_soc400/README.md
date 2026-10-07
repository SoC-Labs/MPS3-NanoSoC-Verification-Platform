# coresight_soc400 — local (git-ignored) IP override slot

Purpose: hold a **build-only, git-ignored** patched copy of an Arm SoC-400
CoreSight file when a fix requires an IP-library change (project IP-override
policy: never edit `$ARM_IP_LIBRARY_PATH`; copy locally + wire the flist).

**LICENSING:** the Arm SoC-400 RTL is confidential (Arm Academic Access). The
patched copy placed here is git-ignored by design (see `.gitignore`) and MUST
NOT be committed/pushed. This directory is intentionally empty in the repo
except for this README and the ignore rule.

Current intended use: the DAP `swclktck` DFX-teardown fix —
`docs/planning/DAP_SWCLKTCK_DFX_FIX.md` (patched `cxdapswjdp.v` making
`swclktck`/`swclktckn` BUFG/clock-resource clocks so they don't glitch during
partial reconfig). Executing that fix (copying + patching + building the Arm
RTL here) is held pending sign-off — see §7 of that doc.
