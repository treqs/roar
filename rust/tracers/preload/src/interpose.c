#ifdef __APPLE__
#include <stddef.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <unistd.h>

extern ssize_t roar_interpose_read(int fd, void *buf, size_t count);
extern ssize_t roar_interpose_write(int fd, const void *buf, size_t count);
extern ssize_t roar_interpose_pread(int fd, void *buf, size_t count, off_t offset);
extern ssize_t roar_interpose_pwrite(int fd, const void *buf, size_t count, off_t offset);
extern ssize_t roar_interpose_readv(int fd, const struct iovec *iov, int iovcnt);
extern ssize_t roar_interpose_writev(int fd, const struct iovec *iov, int iovcnt);

// Keep the macOS hook set limited to low-level read/write APIs that dispatch via direct syscalls
// in Rust. Interposing higher-level libc/stdio/mmap symbols has proven unstable on CI.
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

// Anchor symbol: referenced from Rust so this TU is pulled in from the static archive.
int roar_preload_interpose_keep(void) { return 0; }

#endif
