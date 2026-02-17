#ifdef __APPLE__
#include <stddef.h>
#include <stdio.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/mman.h>
#include <unistd.h>

extern ssize_t roar_interpose_read(int fd, void *buf, size_t count);
extern ssize_t roar_interpose_write(int fd, const void *buf, size_t count);
extern ssize_t roar_interpose_pread(int fd, void *buf, size_t count, off_t offset);
extern ssize_t roar_interpose_pwrite(int fd, const void *buf, size_t count, off_t offset);
extern ssize_t roar_interpose_readv(int fd, const struct iovec *iov, int iovcnt);
extern ssize_t roar_interpose_writev(int fd, const struct iovec *iov, int iovcnt);
extern void *roar_interpose_mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset);
extern int roar_interpose_rename(const char *old_path, const char *new_path);
extern int roar_interpose_renameat(int old_dirfd, const char *old_path, int new_dirfd, const char *new_path);
extern int roar_interpose_unlink(const char *path);
extern int roar_interpose_unlinkat(int dirfd, const char *path, int flags);
extern int roar_interpose_truncate(const char *path, off_t length);
extern int roar_interpose_ftruncate(int fd, off_t length);

// All hooked functions dispatch to the real implementation via direct syscalls in Rust, which is
// safe during early dyld library loading. fopen/fdopen/freopen are intentionally excluded because
// they have no syscall equivalent and dlsym(RTLD_NEXT) is unreliable during early dyld init.
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
DYLD_INTERPOSE(roar_interpose_mmap, mmap);
DYLD_INTERPOSE(roar_interpose_rename, rename);
DYLD_INTERPOSE(roar_interpose_renameat, renameat);
DYLD_INTERPOSE(roar_interpose_unlink, unlink);
DYLD_INTERPOSE(roar_interpose_unlinkat, unlinkat);
DYLD_INTERPOSE(roar_interpose_truncate, truncate);
DYLD_INTERPOSE(roar_interpose_ftruncate, ftruncate);

// Anchor symbol: referenced from Rust so this TU is pulled in from the static archive.
int roar_preload_interpose_keep(void) { return 0; }

#endif
