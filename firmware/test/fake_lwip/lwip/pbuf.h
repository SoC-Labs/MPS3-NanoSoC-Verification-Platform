/*
 * fake_lwip/lwip/pbuf.h — the pbuf, modelled for real.
 *
 * This is the part of the shim that must NOT be a stub. net_if_lwip.c's
 * receive path hands pbufs around with lwIP's exact refcount discipline:
 *
 *     next = p->next;
 *     if (next) pbuf_ref(next);
 *     pbuf_free(p);          // frees only p now that next holds an extra ref
 *
 * That line is only correct because pbuf_free() walks the chain decrementing
 * refs and stops at the first pbuf that still has one. A stub that freed one
 * pbuf and returned would hide a missing pbuf_ref(); a stub that freed the
 * whole chain would hide the opposite bug. So fake_lwip.c implements the real
 * rule, over a table-backed pool that never hands the same block back, which
 * is what makes a double free or a leak an ASSERTABLE fact (see
 * fake_pbuf_live() / fake_pbuf_faults() in fake_lwip_ctl.h).
 */
#ifndef FAKE_LWIP_PBUF_H
#define FAKE_LWIP_PBUF_H

#include "lwip/opt.h"
#include "lwip/arch.h"
#include "lwip/err.h"

typedef enum {
    PBUF_TRANSPORT,
    PBUF_IP,
    PBUF_LINK,
    PBUF_RAW_TX,
    PBUF_RAW
} pbuf_layer;

typedef enum {
    PBUF_RAM,
    PBUF_ROM,
    PBUF_REF,
    PBUF_POOL
} pbuf_type;

struct pbuf {
    struct pbuf *next;
    void        *payload;
    u16_t        tot_len;   /* this pbuf AND every next in the chain */
    u16_t        len;       /* this pbuf only                        */
    u8_t         type_internal;
    u8_t         flags;
    u8_t         ref;
    u8_t         if_idx;
};

struct pbuf *pbuf_alloc(pbuf_layer l, u16_t length, pbuf_type type);
void         pbuf_ref(struct pbuf *p);
u8_t         pbuf_free(struct pbuf *p);
void         pbuf_cat(struct pbuf *head, struct pbuf *tail);
void         pbuf_chain(struct pbuf *head, struct pbuf *tail);
u16_t        pbuf_copy_partial(const struct pbuf *p, void *dataptr, u16_t len, u16_t offset);
err_t        pbuf_take(struct pbuf *buf, const void *dataptr, u16_t len);

#endif /* FAKE_LWIP_PBUF_H */
