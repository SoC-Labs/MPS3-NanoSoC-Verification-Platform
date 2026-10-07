"""``pyverify.identify`` — the UDP 6899 ``identify`` probe (client side).

One datagram out, one datagram back, independent of the single-client 6900
control channel -- so it answers "what is at this address, and is its SSH
claimed yet?" even while somebody else holds 6900 or a swap has parked it.

Request (plan §10, "Harness Manager interface"; the normative spec is the
``identify`` section HARNESSD adds to ``docs/contracts/net-protocol.md``)::

    {"op":"identify","v":1,"nonce":"<8-32 hex>"}

The reply goes to the sender's addr:port, is at most 1200 bytes, echoes the
nonce, and is rate-limited (~10/s). Linux-only keys (``impl``, ``ssh``) come
from providers; a bare-metal image, once it registers the same service module,
simply omits them. This client therefore parses tolerantly: it checks the nonce
echo (so a stale or spoofed reply is not taken for this one) and hands back the
whole object.

FakeShell models the server: ``FakeShell(profile="linux")`` answers on 6899.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = ["IDENTIFY_PORT", "IDENTIFY_MAX_REPLY", "IdentifyReply", "IdentifyError",
           "new_nonce", "identify"]

IDENTIFY_PORT = 6899
IDENTIFY_MAX_REPLY = 1200


class IdentifyError(Exception):
    """No reply, or a reply that does not answer THIS request."""


@dataclass(frozen=True)
class IdentifyReply:
    ok: bool
    nonce: str
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def impl(self) -> str:
        """``"linux"`` or absent -> ``"bare-metal"`` (same rule as ``version``)."""
        v = self.raw.get("impl")
        return v if isinstance(v, str) and v else "bare-metal"

    @property
    def ssh_claimed(self) -> Optional[bool]:
        """True once a first ``authorized_keys`` was accepted (TOFU); None when
        the reply carries no ``ssh`` block (bare metal)."""
        ssh = self.raw.get("ssh")
        if isinstance(ssh, dict) and isinstance(ssh.get("claimed"), bool):
            return ssh["claimed"]
        return None

    @property
    def host_key_sha256(self) -> Optional[str]:
        """The board's SSH host-key fingerprint, to compare against what ssh
        shows on first connect (never trust-on-first-use blindly)."""
        ssh = self.raw.get("ssh")
        if isinstance(ssh, dict) and isinstance(ssh.get("host_key_sha256"), str):
            return ssh["host_key_sha256"]
        return None

    @property
    def key_sha256(self) -> Optional[str]:
        """The CLAIMING key's fingerprint (the claim's first key, OpenSSH
        ``SHA256:<b64>``; 2026-09-26): "" while unclaimed, None when the reply
        carries no ``ssh`` block or predates the key. Compare it with your own
        key's to tell "claimed by me" from "claimed by someone else"."""
        ssh = self.raw.get("ssh")
        if isinstance(ssh, dict) and isinstance(ssh.get("key_sha256"), str):
            return ssh["key_sha256"]
        return None


def new_nonce(nbytes: int = 8) -> str:
    """8..16 random bytes as 16..32 lowercase hex (the request accepts 8-32 hex)."""
    if not 4 <= nbytes <= 16:
        raise ValueError("nonce must be 4..16 bytes (8..32 hex)")
    return os.urandom(nbytes).hex()


def identify(host: str, port: int = IDENTIFY_PORT, *, nonce: Optional[str] = None,
             timeout: float = 2.0, retries: int = 2) -> IdentifyReply:
    """Send one identify probe and wait for its reply (re-sent ``retries``
    times -- it is UDP). Raises :class:`IdentifyError` on silence or on a
    reply whose nonce is not ours."""
    nonce = nonce or new_nonce()
    req = json.dumps({"op": "identify", "v": 1, "nonce": nonce},
                     separators=(",", ":")).encode("ascii")
    last = "no reply"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for _ in range(max(1, retries + 1)):
            sock.sendto(req, (host, port))
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                last = "no reply within %.1fs" % timeout
                continue
            if len(data) > IDENTIFY_MAX_REPLY:
                raise IdentifyError("identify reply is %d B (> %d)" % (len(data), IDENTIFY_MAX_REPLY))
            try:
                obj = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise IdentifyError("identify reply is not JSON: %r" % data[:80]) from exc
            if not isinstance(obj, dict):
                raise IdentifyError("identify reply is not an object: %r" % (obj,))
            if obj.get("nonce") != nonce:
                last = "reply carried nonce %r, not ours" % (obj.get("nonce"),)
                continue
            return IdentifyReply(ok=bool(obj.get("ok")), nonce=nonce, raw=obj)
    raise IdentifyError("identify %s:%d: %s" % (host, port, last))
