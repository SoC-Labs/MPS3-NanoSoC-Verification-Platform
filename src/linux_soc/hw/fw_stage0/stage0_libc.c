/*
 * stage0_libc.c -- the four mem* routines gcc may call on its own (structure
 * copies/zeroing) plus the ones smsc911x.c and usd.c call by name. stage0 links
 * no C library (-nostdlib). Built with -fno-builtin and
 * -fno-tree-loop-distribute-patterns so gcc cannot turn these loops back into
 * calls to themselves.
 */
#include <stddef.h>
#include <stdint.h>

void *memcpy(void *d, const void *s, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    const uint8_t *sp = (const uint8_t *)s;
    if ((((uintptr_t)dp | (uintptr_t)sp) & 3u) == 0u) {
        for (; n >= 4u; n -= 4u, dp += 4, sp += 4)
            *(uint32_t *)(void *)dp = *(const uint32_t *)(const void *)sp;
    }
    while (n--)
        *dp++ = *sp++;
    return d;
}

void *memmove(void *d, const void *s, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    const uint8_t *sp = (const uint8_t *)s;
    if (dp <= sp || dp >= sp + n)
        return memcpy(d, s, n);
    while (n--)
        dp[n] = sp[n];
    return d;
}

void *memset(void *d, int c, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    while (n--)
        *dp++ = (uint8_t)c;
    return d;
}

int memcmp(const void *a, const void *b, size_t n)
{
    const uint8_t *x = (const uint8_t *)a, *y = (const uint8_t *)b;
    for (; n; --n, ++x, ++y)
        if (*x != *y)
            return (int)*x - (int)*y;
    return 0;
}
