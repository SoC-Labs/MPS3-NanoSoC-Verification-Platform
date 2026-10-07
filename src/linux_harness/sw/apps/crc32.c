/* crc32.c — see crc32.h. Table-driven, generated at first use. */
#include "crc32.h"

static uint32_t s_tab[256];
static int s_have_tab;

static void make_tab(void)
{
    for (uint32_t n = 0; n < 256; n++) {
        uint32_t c = n;
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
        s_tab[n] = c;
    }
    s_have_tab = 1;
}

uint32_t mps3_crc32(uint32_t crc, const void *buf, size_t len)
{
    if (!s_have_tab)
        make_tab();
    const uint8_t *p = (const uint8_t *)buf;
    uint32_t c = crc ^ 0xFFFFFFFFu;
    while (len--)
        c = s_tab[(c ^ *p++) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}
