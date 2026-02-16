#ifdef __APPLE__
#include <stddef.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <sys/uio.h>
#include <stdio.h>
#include <unistd.h>

extern ssize_t roar_interpose_read(int fd, void *buf, size_t count);
extern ssize_t roar_interpose_write(int fd, const void *buf, size_t count);
extern ssize_t roar_interpose_pread(int fd, void *buf, size_t count, off_t offset);
extern ssize_t roar_interpose_pwrite(int fd, const void *buf, size_t count, off_t offset);
extern ssize_t roar_interpose_readv(int fd, const struct iovec *iov, int iovcnt);
extern ssize_t roar_interpose_writev(int fd, const struct iovec *iov, int iovcnt);
extern int roar_interpose_rename(const char *oldpath, const char *newpath);
extern int roar_interpose_renameat(int olddirfd, const char *oldpath, int newdirfd, const char *newpath);
extern void *roar_interpose_mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset);
extern FILE *roar_interpose_fopen(const char *path, const char *mode);
extern FILE *roar_interpose_fdopen(int fd, const char *mode);
extern FILE *roar_interpose_freopen(const char *path, const char *mode, FILE *stream);
extern int roar_interpose_unlink(const char *path);
extern int roar_interpose_unlinkat(int dirfd, const char *path, int flags);
extern int roar_interpose_truncate(const char *path, off_t length);
extern int roar_interpose_ftruncate(int fd, off_t length);

#define DYLD_INTERPOSE(_replacement, _replacee)                                       \
  __attribute__((used)) static struct {                                               \
    const void *replacement;                                                          \
    const void *replacee;                                                             \
  } _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = {         \
      (const void *)(unsigned long)&_replacement,                                     \
      (const void *)(unsigned long)&_replacee                                         \
  }

DYLD_INTERPOSE(roar_interpose_read, read);
DYLD_INTERPOSE(roar_interpose_write, write);
DYLD_INTERPOSE(roar_interpose_pread, pread);
DYLD_INTERPOSE(roar_interpose_pwrite, pwrite);
DYLD_INTERPOSE(roar_interpose_readv, readv);
DYLD_INTERPOSE(roar_interpose_writev, writev);
DYLD_INTERPOSE(roar_interpose_rename, rename);
DYLD_INTERPOSE(roar_interpose_renameat, renameat);
DYLD_INTERPOSE(roar_interpose_unlink, unlink);
DYLD_INTERPOSE(roar_interpose_unlinkat, unlinkat);
DYLD_INTERPOSE(roar_interpose_mmap, mmap);
DYLD_INTERPOSE(roar_interpose_fopen, fopen);
DYLD_INTERPOSE(roar_interpose_fdopen, fdopen);
DYLD_INTERPOSE(roar_interpose_freopen, freopen);
DYLD_INTERPOSE(roar_interpose_truncate, truncate);
DYLD_INTERPOSE(roar_interpose_ftruncate, ftruncate);

// Anchor symbol: referenced from Rust so this TU is pulled in from the static archive.
int roar_preload_interpose_keep(void) { return 0; }

#endif
