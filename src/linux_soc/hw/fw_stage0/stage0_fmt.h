/*
 * stage0_fmt.h -- console-line building without printf (stage0 links no libc).
 * Header-only; every module that talks to the console builds a struct s0_line
 * and hands the finished string to its log callback.
 */
#ifndef STAGE0_FMT_H
#define STAGE0_FMT_H

#include <stdint.h>

struct s0_line {
    char     b[112];
    uint32_t n;
};

static inline void s0l_init(struct s0_line *l)
{
    l->n = 0u;
    l->b[0] = '\0';
}

static inline void s0l_str(struct s0_line *l, const char *s)
{
    while (s && *s && l->n < sizeof l->b - 1u)
        l->b[l->n++] = *s++;
    l->b[l->n] = '\0';
}

static inline void s0l_hex(struct s0_line *l, uint32_t v)
{
    static const char hd[] = "0123456789ABCDEF";
    s0l_str(l, "0x");
    for (int i = 28; i >= 0 && l->n < sizeof l->b - 1u; i -= 4)
        l->b[l->n++] = hd[(v >> i) & 0xFu];
    l->b[l->n] = '\0';
}

static inline void s0l_dec(struct s0_line *l, uint32_t v)
{
    char d[10];
    uint32_t k = 0;
    do {
        d[k++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v != 0u && k < sizeof d);
    while (k != 0u && l->n < sizeof l->b - 1u)
        l->b[l->n++] = d[--k];
    l->b[l->n] = '\0';
}

/* dotted quad of a host-order IPv4 address */
static inline void s0l_ip(struct s0_line *l, uint32_t ip)
{
    for (int i = 24; i >= 0; i -= 8) {
        s0l_dec(l, (ip >> i) & 0xFFu);
        if (i)
            s0l_str(l, ".");
    }
}

#endif /* STAGE0_FMT_H */
