/* uio.c — see uio.h. */
#define _POSIX_C_SOURCE 200809L
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "uio.h"

static int read_sys_str(const char *path, char *out, size_t out_sz)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    if (!fgets(out, (int)out_sz, f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    out[strcspn(out, "\n")] = '\0';
    return 0;
}

int mps3_uio_open(const char *name, mps3_uio_t *u)
{
    DIR *d = opendir("/sys/class/uio");
    if (!d)
        return -1;
    struct dirent *e;
    int found = -1;
    char nbuf[128];
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, "uio", 3) != 0)
            continue;
        char p[256];
        snprintf(p, sizeof(p), "/sys/class/uio/%s/name", e->d_name);
        if (read_sys_str(p, nbuf, sizeof(nbuf)) != 0)
            continue;
        if (strcmp(nbuf, name) == 0) {
            found = atoi(e->d_name + 3);
            break;
        }
    }
    closedir(d);
    if (found < 0)
        return -1;

    char p[256], sz[64];
    snprintf(p, sizeof(p), "/sys/class/uio/uio%d/maps/map0/size", found);
    if (read_sys_str(p, sz, sizeof(sz)) != 0)
        return -1;
    u->map_size = (size_t)strtoul(sz, NULL, 0);

    snprintf(u->dev, sizeof(u->dev), "/dev/uio%d", found);
    u->fd = open(u->dev, O_RDWR | O_SYNC);
    if (u->fd < 0)
        return -1;
    void *m = mmap(NULL, u->map_size, PROT_READ | PROT_WRITE, MAP_SHARED,
                   u->fd, 0);
    if (m == MAP_FAILED) {
        close(u->fd);
        u->fd = -1;
        return -1;
    }
    u->regs = (volatile uint32_t *)m;
    return 0;
}

void mps3_uio_close(mps3_uio_t *u)
{
    if (u->regs) {
        munmap((void *)u->regs, u->map_size);
        u->regs = NULL;
    }
    if (u->fd >= 0) {
        close(u->fd);
        u->fd = -1;
    }
}
