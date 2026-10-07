/*
 * status_linux.h — gather the CLCD status snapshot / boot-status surface from
 * Linux sources. Every source is a FILE under a configurable root so the whole
 * collector is testable off-board against a faked sysroot (the same mocked-
 * inputs discipline as the firmware's MPS3_HAL_MOCK harness):
 *
 *   /proc/uptime                                  uptime
 *   /proc/sys/kernel/osrelease                    kernel identity (row 7)
 *   /sys/class/net/<if>/operstate|speed|duplex|address
 *   /sys/class/net/<if>/statistics/rx_dropped|tx_errors
 *   /etc/mps3/static_id                           shell static_id (provisioning
 *                                                 file; same file mps3-ctrld -i
 *                                                 reads; 0 = not provisioned)
 *   /sys/class/misc/mps3dfx/state|rm_id|icap_bytes  the DFX driver's sysfs
 *                                                 (drivers/icap/mps3_dfx_drv.c)
 *
 * The IP address is read from <root>/run/mps3/ip when present (written by the
 * boot scripts / tests), else queried live via getifaddrs() — the latter only
 * when root == "" (a faked sysroot must never fall through to the real one).
 *
 * ABSENCE IS DATA: no mps3dfx sysfs -> dfx_present=0 (the glass says "NO DFX
 * DRIVER"); no netdev -> link_up=-1. The collector never fabricates.
 */
#ifndef MPS3_APPS_STATUS_LINUX_H
#define MPS3_APPS_STATUS_LINUX_H

#include "clcd_core.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char *root;        /* "" = live system; tests point at a fake tree  */
    const char *netdev;      /* default "eth0"                                */
    const char *static_id_file; /* default "/etc/mps3/static_id" (under root) */
} mps3_status_cfg_t;

void mps3_status_cfg_default(mps3_status_cfg_t *cfg);

/* Fill *st from the configured sources. Returns 0 (always usable — missing
 * sources degrade to their honest "unknown" encodings, never to errors). */
int mps3_status_collect(const mps3_status_cfg_t *cfg, clcd_status_t *st);

/* Render the boot-status surface as one flat JSON object into buf (NUL-
 * terminated, truncated if needed; returns strlen). This is a LOCAL surface
 * (file/CLI), NOT the frozen :6900 wire contract — the telemetry/diag wire
 * shapes belong to mps3-ctrld alone. Keys:
 *   board, static_id, uptime_ms, kernel,
 *   net{ip,link,speed_mbps,full_duplex,mac,rx_dropped,tx_errors},
 *   dfx{present,swap_state,swap_state_name,rm_id,rm_name,icap_bytes},
 *   power_sensor:false (what bare-metal TELEM showed: there is NO sensor,
 *                       by construction — four independent dead-ends)
 */
unsigned mps3_status_json(const clcd_status_t *st, char *buf, unsigned cap);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_APPS_STATUS_LINUX_H */
