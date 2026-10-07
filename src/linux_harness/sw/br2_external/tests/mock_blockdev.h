/* mock_blockdev.h — prototypes of the caching mock device (mock_blockdev.c),
 * force-included into mps3-slot.c by br2_external/tests/run.sh. It is read
 * before mps3-slot.c's own lines, so it sets the same feature macros first. */
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <sys/types.h>
ssize_t mock_pread(int fd, void *buf, size_t n, off_t off);
ssize_t mock_pwrite(int fd, const void *buf, size_t n, off_t off);
int mock_fsync(int fd);
int mock_posix_fadvise(int fd, off_t off, off_t len, int advice);
