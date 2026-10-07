# patches/diag — diagnostic kernel patches, NOT applied

`BR2_GLOBAL_PATCH_DIR` applies `patches/<package>/*.patch`; `diag/` is not a
package name, so nothing here reaches an image by default.

| Patch | What | Why it is parked |
|---|---|---|
| `linux/0005-DIAG-smsc911x-enable-USE_DEBUG-tracing.patch` | raises smsc911x `USE_DEBUG` to 2 so `smsc911x.debug=16` prints | its own header: "DIAGNOSTIC ONLY — drop once eth0 bring-up is understood". It is (the patch-0007 delay fix). With the tracing on, the console flood starves the uartlite shell. |

To use one for a debug build, copy it into `patches/linux/` and rebuild the
kernel (`make linux-reconfigure`), then remove it again.
