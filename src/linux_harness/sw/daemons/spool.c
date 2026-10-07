/* spool.c — see spool.h. Plain POSIX; shared by both daemons. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "spool.h"

const char *spool_path(const char *dir, const char *name, const char *suffix,
                       char *out, size_t out_sz)
{
    (void)snprintf(out, out_sz, "%s/%s%s", dir, name, suffix ? suffix : "");
    return out;
}

int spool_mkdir(const char *dir)
{
    if (mkdir(dir, 0755) == 0 || errno == EEXIST)
        return 0;
    return -1;
}

int spool_meta_write(const char *dir, const char *base, const spool_meta_t *m)
{
    char tmp[512], dst[512];
    spool_path(dir, base, ".meta.tmp", tmp, sizeof(tmp));
    spool_path(dir, base, ".meta", dst, sizeof(dst));

    FILE *f = fopen(tmp, "w");
    if (!f)
        return -1;
    int n = fprintf(f,
        "kind=%u rm_slot=%u static_id=0x%08x rm_id=0x%08x len_words=%u crc32=0x%08x\n",
        (unsigned)m->kind, (unsigned)m->rm_slot, m->static_id, m->rm_id,
        (unsigned)m->len_words, m->crc32);
    if (n < 0 || fclose(f) != 0) {
        (void)unlink(tmp);
        return -1;
    }
    if (rename(tmp, dst) != 0) {
        (void)unlink(tmp);
        return -1;
    }
    return 0;
}

int spool_meta_read(const char *dir, const char *base, spool_meta_t *m)
{
    char p[512];
    spool_path(dir, base, ".meta", p, sizeof(p));
    FILE *f = fopen(p, "r");
    if (!f)
        return -1;
    unsigned kind, slot, lw;
    uint32_t sid, rid, crc;
    int rc = fscanf(f, "kind=%u rm_slot=%u static_id=%x rm_id=%x len_words=%u crc32=%x",
                    &kind, &slot, &sid, &rid, &lw, &crc);
    fclose(f);
    if (rc != 6)
        return -1;
    m->kind = kind;
    m->rm_slot = slot;
    m->static_id = sid;
    m->rm_id = rid;
    m->len_words = lw;
    m->crc32 = crc;
    return 0;
}

void spool_unlink_pair(const char *dir, const char *base)
{
    char p[512];
    /* meta first: readers treat the meta as the publication marker, so
     * removing it first means no reader can adopt a half-removed stage. */
    (void)unlink(spool_path(dir, base, ".meta", p, sizeof(p)));
    (void)unlink(spool_path(dir, base, ".bin", p, sizeof(p)));
}

int spool_rename_pair(const char *dir, const char *from, const char *to)
{
    char a[512], b[512];
    /* bin first, meta last (meta is the marker). */
    if (rename(spool_path(dir, from, ".bin", a, sizeof(a)),
               spool_path(dir, to, ".bin", b, sizeof(b))) != 0)
        return -1;
    if (rename(spool_path(dir, from, ".meta", a, sizeof(a)),
               spool_path(dir, to, ".meta", b, sizeof(b))) != 0)
        return -1;
    return 0;
}

int spool_staged(const char *dir, const char *base)
{
    char p[512];
    return access(spool_path(dir, base, ".meta", p, sizeof(p)), R_OK) == 0;
}
