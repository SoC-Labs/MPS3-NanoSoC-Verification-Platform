/*
 * mock_blockdev.c — a block device WITH A PAGE CACHE, for the mps3-slot host
 * tests (br2_external/tests/run.sh). mps3-slot.c is compiled with its test seam
 *   -DSLOT_PREAD=mock_pread -DSLOT_PWRITE=mock_pwrite -DSLOT_FSYNC=mock_fsync
 *   -DSLOT_FADVISE=mock_posix_fadvise -include mock_blockdev.h
 * so its own code runs unchanged over this model:
 *
 *   pwrite            -> the sector lands in the cache, dirty
 *   fsync             -> dirty sectors go to the media file and become clean —
 *                        unless MOCK_DROP_WRITES=1: then the "card" loses them
 *                        (they become clean in the cache, the media keeps the
 *                        old bytes: an acknowledged write the card never kept)
 *   pread             -> a cached sector if there is one, else the media
 *   posix_fadvise(DONTNEED) -> clean cached sectors in the range are dropped
 *
 * With MOCK_DROP_WRITES=1 a read-back that goes through the cache PASSES and
 * one that drops the cache first FAILS — exactly the difference the fix makes.
 */
#include "mock_blockdev.h"   /* first: it sets the feature macros */
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#define SEC 512u
#define NENT 8192u

struct ent { int used, dirty, fd; uint64_t sec; uint8_t d[SEC]; };
static struct ent tab[NENT];

static struct ent *find(int fd, uint64_t sec, int make)
{
    uint64_t h = (sec * 2654435761u + (uint64_t)fd) % NENT;
    for (unsigned i = 0; i < NENT; i++) {
        struct ent *e = &tab[(h + i) % NENT];
        if (e->used && e->fd == fd && e->sec == sec)
            return e;
        if (!e->used) {
            if (!make)
                return NULL;
            e->used = 1; e->dirty = 0; e->fd = fd; e->sec = sec;
            if (pread(fd, e->d, SEC, (off_t)(sec * SEC)) != (ssize_t)SEC)
                memset(e->d, 0, SEC);
            return e;
        }
    }
    abort();   /* cache full: the tests stay far below NENT sectors */
}

ssize_t mock_pread(int fd, void *buf, size_t n, off_t off)
{
    uint8_t *b = buf;
    for (size_t done = 0; done < n;) {
        uint64_t pos = (uint64_t)off + done, sec = pos / SEC, in = pos % SEC;
        size_t take = SEC - in < n - done ? SEC - in : n - done;
        struct ent *e = find(fd, sec, 0);
        if (e)
            memcpy(b + done, e->d + in, take);
        else if (pread(fd, b + done, take, (off_t)pos) != (ssize_t)take)
            return done ? (ssize_t)done : -1;
        done += take;
    }
    return (ssize_t)n;
}

ssize_t mock_pwrite(int fd, const void *buf, size_t n, off_t off)
{
    const uint8_t *b = buf;
    for (size_t done = 0; done < n;) {
        uint64_t pos = (uint64_t)off + done, sec = pos / SEC, in = pos % SEC;
        size_t take = SEC - in < n - done ? SEC - in : n - done;
        struct ent *e = find(fd, sec, 1);
        memcpy(e->d + in, b + done, take);
        e->dirty = 1;
        done += take;
    }
    return (ssize_t)n;
}

int mock_fsync(int fd)
{
    const char *drop = getenv("MOCK_DROP_WRITES");
    for (unsigned i = 0; i < NENT; i++) {
        struct ent *e = &tab[i];
        if (!e->used || e->fd != fd || !e->dirty)
            continue;
        if (!(drop && *drop == '1') &&
            pwrite(fd, e->d, SEC, (off_t)(e->sec * SEC)) != (ssize_t)SEC)
            return -1;
        e->dirty = 0;
    }
    return fsync(fd);
}

int mock_posix_fadvise(int fd, off_t off, off_t len, int advice)
{
    if (advice != POSIX_FADV_DONTNEED)
        return 0;
    uint64_t lo = (uint64_t)off / SEC, hi = len ? ((uint64_t)off + (uint64_t)len + SEC - 1) / SEC
                                                : UINT64_MAX;
    for (unsigned i = 0; i < NENT; i++) {
        struct ent *e = &tab[i];
        if (e->used && e->fd == fd && !e->dirty && e->sec >= lo && e->sec < hi)
            e->used = 0;
    }
    /* the table is open-addressed: rebuild it so later probes stay correct */
    static struct ent keep[NENT];
    unsigned k = 0;
    for (unsigned i = 0; i < NENT; i++)
        if (tab[i].used)
            keep[k++] = tab[i];
    memset(tab, 0, sizeof(tab));
    for (unsigned i = 0; i < k; i++) {
        uint64_t h = (keep[i].sec * 2654435761u + (uint64_t)keep[i].fd) % NENT;
        while (tab[h].used)
            h = (h + 1) % NENT;
        tab[h] = keep[i];
    }
    return 0;
}
