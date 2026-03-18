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

#ifndef __APPLE__
#define _GNU_SOURCE

#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stddef.h>
#include <sys/types.h>
#include <unistd.h>

extern void roar_preload_emit_path_flags(const char *path, int flags);
extern void roar_preload_emit_at_path_flags(int dirfd, const char *path, int flags);

static int (*resolve_open_symbol(const char *name))(const char *, int, ...) {
  return (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, name);
}

static int (*resolve_openat_symbol(const char *name))(int, const char *, int, ...) {
  return (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, name);
}

int open(const char *path, int flags, ...) {
  static int (*real_open)(const char *, int, ...) = NULL;
  if (real_open == NULL) {
    real_open = resolve_open_symbol("open");
  }
  if (real_open == NULL) {
    return -1;
  }

  mode_t mode = 0;
  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  if (has_mode) {
    va_list args;
    va_start(args, flags);
    mode = va_arg(args, int);
    va_end(args);
  }

  int ret = has_mode ? real_open(path, flags, mode) : real_open(path, flags);
  if (ret >= 0) {
    roar_preload_emit_path_flags(path, flags);
  }
  return ret;
}

int open64(const char *path, int flags, ...) {
  static int (*real_open64)(const char *, int, ...) = NULL;
  if (real_open64 == NULL) {
    real_open64 = resolve_open_symbol("open64");
  }
  if (real_open64 == NULL) {
    return open(path, flags);
  }

  mode_t mode = 0;
  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  if (has_mode) {
    va_list args;
    va_start(args, flags);
    mode = va_arg(args, int);
    va_end(args);
  }

  int ret = has_mode ? real_open64(path, flags, mode) : real_open64(path, flags);
  if (ret >= 0) {
    roar_preload_emit_path_flags(path, flags);
  }
  return ret;
}

int openat(int dirfd, const char *path, int flags, ...) {
  static int (*real_openat)(int, const char *, int, ...) = NULL;
  if (real_openat == NULL) {
    real_openat = resolve_openat_symbol("openat");
  }
  if (real_openat == NULL) {
    return -1;
  }

  mode_t mode = 0;
  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  if (has_mode) {
    va_list args;
    va_start(args, flags);
    mode = va_arg(args, int);
    va_end(args);
  }

  int ret = has_mode ? real_openat(dirfd, path, flags, mode) : real_openat(dirfd, path, flags);
  if (ret >= 0) {
    roar_preload_emit_at_path_flags(dirfd, path, flags);
  }
  return ret;
}

int openat64(int dirfd, const char *path, int flags, ...) {
  static int (*real_openat64)(int, const char *, int, ...) = NULL;
  if (real_openat64 == NULL) {
    real_openat64 = resolve_openat_symbol("openat64");
  }
  if (real_openat64 == NULL) {
    return openat(dirfd, path, flags);
  }

  mode_t mode = 0;
  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  if (has_mode) {
    va_list args;
    va_start(args, flags);
    mode = va_arg(args, int);
    va_end(args);
  }

  int ret = has_mode ? real_openat64(dirfd, path, flags, mode) : real_openat64(dirfd, path, flags);
  if (ret >= 0) {
    roar_preload_emit_at_path_flags(dirfd, path, flags);
  }
  return ret;
}

int creat(const char *path, mode_t mode) {
  static int (*real_creat)(const char *, mode_t) = NULL;
  if (real_creat == NULL) {
    real_creat = (int (*)(const char *, mode_t))dlsym(RTLD_NEXT, "creat");
  }
  if (real_creat == NULL) {
    return -1;
  }

  int ret = real_creat(path, mode);
  if (ret >= 0) {
    roar_preload_emit_path_flags(path, O_WRONLY | O_CREAT | O_TRUNC);
  }
  return ret;
}

/* ── fork/vfork interposition ─────────────────────────────── */

extern void roar_preload_emit_fork(unsigned int parent_pid,
                                   unsigned int child_pid);

pid_t fork(void) {
  static pid_t (*real_fork)(void) = NULL;
  if (real_fork == NULL) {
    real_fork = (pid_t (*)(void))dlsym(RTLD_NEXT, "fork");
  }
  if (real_fork == NULL) {
    return -1;
  }

  pid_t ret = real_fork();
  if (ret > 0) {
    /* Parent side — successful fork. */
    roar_preload_emit_fork((unsigned int)getpid(), (unsigned int)ret);
  }
  return ret;
}

/* ── exec notification via constructor ────────────────────── */

extern void roar_preload_notify_exec(void);

__attribute__((constructor)) static void roar_preload_ctor(void) {
  roar_preload_notify_exec();
}

#endif
