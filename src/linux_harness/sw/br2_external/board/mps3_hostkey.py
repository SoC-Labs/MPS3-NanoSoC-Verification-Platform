#!/usr/bin/env python3
"""mps3_hostkey.py — dropbear host-key provisioning helper (build host, stdlib only).

The harness bakes ONE persistent ed25519 host key into the rootfs so the board's
SSH identity survives reboots (dropbear otherwise generates a fresh key every boot
into the tmpfs behind /etc/dropbear -> /var/run/dropbear). Used by build.sh (to
emit the matching known_hosts line) and by the Buildroot post-build hook
mps3_provision.sh (to install the key in dropbear's own format).

Accepted input: an UNENCRYPTED ed25519 private key in either
  * OpenSSH format   (`ssh-keygen -t ed25519 -N ""`)  -> converted, or
  * dropbear format  (`dropbearkey -t ed25519`)       -> passed through.
dropbear's server reads ONLY its own format (common-runopts.c readhostkey ->
buf_get_priv_key), which for ed25519 is
    string "ssh-ed25519" ; string (seed[32] || pub[32])
(ed25519.c buf_put_ed25519_priv_key). OpenSSH's private blob stores exactly the
same 64 bytes, so conversion is a re-framing, not a crypto operation.

Private key material is never printed. Subcommands:
  to-dropbear IN OUT        write IN as a dropbear-format key to OUT (mode 0600)
  publine IN                print "ssh-ed25519 AAAA..." (public)
  known-hosts IN HOSTS      print "<HOSTS> ssh-ed25519 AAAA..." (HOSTS comma-separated)
  fingerprint IN            print "SHA256:..." (same form as `ssh-keygen -l`)
"""
import base64
import hashlib
import os
import struct
import sys

KEYTYPE = b"ssh-ed25519"


def _get_string(buf, off):
    (n,) = struct.unpack(">I", buf[off:off + 4])
    if off + 4 + n > len(buf):
        raise ValueError("truncated key blob")
    return buf[off + 4:off + 4 + n], off + 4 + n


def _put_string(b):
    return struct.pack(">I", len(b)) + b


def _parse_openssh(text):
    body = "".join(l.strip() for l in text.splitlines()
                   if l.strip() and not l.startswith("-----"))
    blob = base64.b64decode(body)
    magic = b"openssh-key-v1\0"
    if not blob.startswith(magic):
        raise ValueError("not an openssh-key-v1 private key")
    off = len(magic)
    cipher, off = _get_string(blob, off)
    kdf, off = _get_string(blob, off)
    _kdfopts, off = _get_string(blob, off)
    if cipher != b"none" or kdf != b"none":
        raise ValueError("host key is passphrase-encrypted; decrypt a copy first (ssh-keygen -p)")
    (nkeys,) = struct.unpack(">I", blob[off:off + 4]); off += 4
    if nkeys != 1:
        raise ValueError("expected exactly one key, found %d" % nkeys)
    _pubblob, off = _get_string(blob, off)
    priv, off = _get_string(blob, off)
    c1, c2 = struct.unpack(">II", priv[:8])
    if c1 != c2:
        raise ValueError("openssh private section check-ints differ (corrupt key)")
    p = 8
    ktype, p = _get_string(priv, p)
    if ktype != KEYTYPE:
        raise ValueError("host key must be ed25519, got %r" % ktype.decode(errors="replace"))
    pub, p = _get_string(priv, p)
    sk, p = _get_string(priv, p)
    return pub, sk


def _parse_dropbear(blob):
    ktype, off = _get_string(blob, 0)
    if ktype != KEYTYPE:
        raise ValueError("dropbear key is not ed25519 (%r)" % ktype.decode(errors="replace"))
    sk, off = _get_string(blob, off)
    return sk[32:], sk


def load(path):
    raw = open(path, "rb").read()
    if raw.lstrip().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
        pub, sk = _parse_openssh(raw.decode())
    elif raw.lstrip().startswith(b"-----BEGIN"):
        raise ValueError("PEM key given; ed25519 host keys must be OpenSSH or dropbear format")
    else:
        pub, sk = _parse_dropbear(raw)
    if len(pub) != 32 or len(sk) != 64 or sk[32:] != pub:
        raise ValueError("malformed ed25519 key (seed||pub layout check failed)")
    return pub, sk


def pubblob(pub):
    return _put_string(KEYTYPE) + _put_string(pub)


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    cmd, src = argv[1], argv[2]
    try:
        pub, sk = load(src)
    except (OSError, ValueError, struct.error) as e:
        sys.stderr.write("mps3_hostkey: %s: %s\n" % (src, e))
        return 1
    line = "ssh-ed25519 " + base64.b64encode(pubblob(pub)).decode()
    if cmd == "to-dropbear" and len(argv) == 4:
        out = argv[3]
        fd = os.open(out + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_put_string(KEYTYPE) + _put_string(sk))
        os.chmod(out + ".tmp", 0o600)
        os.replace(out + ".tmp", out)
    elif cmd == "publine":
        print(line)
    elif cmd == "known-hosts" and len(argv) == 4:
        print("%s %s" % (argv[3], line))
    elif cmd == "fingerprint":
        d = hashlib.sha256(pubblob(pub)).digest()
        print("SHA256:" + base64.b64encode(d).decode().rstrip("="))
    else:
        sys.stderr.write(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
