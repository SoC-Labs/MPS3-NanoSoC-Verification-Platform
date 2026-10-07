# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue or pull
request for them.

Email the maintainers at **<security contact: SoC Labs>** with:

- a description of the issue and its impact,
- steps to reproduce (or a proof of concept), and
- any suggested remediation.

We aim to acknowledge reports within a few working days and will coordinate a
fix and disclosure timeline with you.

## Reporting a confidential-IP or credential leak

This repository is built against confidential vendor IP (Arm CoreSight SoC-400,
Cortex-M0, vendor memory compilers) that is deliberately **excluded** from the
public tree (see [NOTICE](NOTICE) and `.gitignore`). If you find any file that
appears to contain confidential third-party IP, private keys, or live
credentials, please report it **privately** to the security contact above rather than
opening a public issue — history may need to be rewritten to remove it.

## Scope

This is a research / verification platform, not a production service. The most
relevant hardening concern is that nothing confidential (vendor RTL, keys,
management-network details, credentials) is ever committed. Contributors should
keep such material in the git-ignored local paths the build already uses, and
must not weaken those `.gitignore` boundaries.
