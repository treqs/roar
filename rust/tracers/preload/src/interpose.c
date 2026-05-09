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
extern int roar_interpose_link(const char *old_path, const char *new_path);
extern int roar_interpose_linkat(int old_dirfd, const char *old_path, int new_dirfd, const char *new_path, int flags);
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
DYLD_INTERPOSE(roar_interpose_link, link);
DYLD_INTERPOSE(roar_interpose_linkat, linkat);
DYLD_INTERPOSE(roar_interpose_unlink, unlink);
DYLD_INTERPOSE(roar_interpose_unlinkat, unlinkat);
DYLD_INTERPOSE(roar_interpose_truncate, truncate);
DYLD_INTERPOSE(roar_interpose_ftruncate, ftruncate);

#endif // __APPLE__

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

// These are the Linux LD_PRELOAD interposers for `open` / `openat` /
// `creat`. Rust's `cdylib` crate type emits a version-script that hides
// every symbol not declared `#[no_mangle] pub extern "C"` from a Rust
// source file — so even though the C symbol `open` is GLOBAL in this
// translation unit, the final .so internalizes it and LD_PRELOAD fails
// to override libc.
//
// The fix: keep the open/dispatch logic here in C (so we get real
// variadic support and dlsym(RTLD_NEXT)), but rename the entry points
// to internal `roar_libc_*_impl` symbols. The exported `open` / `openat`
// / etc. names live as `#[no_mangle]` Rust shims in `lib.rs` that
// forward to these. That way the symbols rustc considers exportable
// happen to *be* `open`, `openat`, etc.
//
// The Rust shims are non-variadic with a fixed `mode_t` third argument.
// AAPCS64 / SysV-x86_64 ABIs both pass `(path, flags, mode)` in the
// same registers regardless of whether the callee is declared variadic
// or fixed, so a 2-arg `open(path, flags)` call still works (the
// `mode` register holds garbage, which we only read when O_CREAT or
// O_TMPFILE is set in flags — exactly per the open(2) man page).

static int (*resolve_open_symbol(const char *name))(const char *, int, ...) {
  return (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, name);
}

static int (*resolve_openat_symbol(const char *name))(int, const char *, int, ...) {
  return (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, name);
}

int roar_libc_open_impl(const char *path, int flags, mode_t mode) {
  static int (*real_open)(const char *, int, ...) = NULL;
  if (real_open == NULL) {
    real_open = resolve_open_symbol("open");
  }
  if (real_open == NULL) {
    return -1;
  }

  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  int ret = has_mode ? real_open(path, flags, mode) : real_open(path, flags);
  if (ret >= 0) {
    roar_preload_emit_path_flags(path, flags);
  }
  return ret;
}

int roar_libc_open64_impl(const char *path, int flags, mode_t mode) {
  static int (*real_open64)(const char *, int, ...) = NULL;
  if (real_open64 == NULL) {
    real_open64 = resolve_open_symbol("open64");
  }
  if (real_open64 == NULL) {
    return roar_libc_open_impl(path, flags, mode);
  }

  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  int ret = has_mode ? real_open64(path, flags, mode) : real_open64(path, flags);
  if (ret >= 0) {
    roar_preload_emit_path_flags(path, flags);
  }
  return ret;
}

int roar_libc_openat_impl(int dirfd, const char *path, int flags, mode_t mode) {
  static int (*real_openat)(int, const char *, int, ...) = NULL;
  if (real_openat == NULL) {
    real_openat = resolve_openat_symbol("openat");
  }
  if (real_openat == NULL) {
    return -1;
  }

  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  int ret = has_mode ? real_openat(dirfd, path, flags, mode) : real_openat(dirfd, path, flags);
  if (ret >= 0) {
    roar_preload_emit_at_path_flags(dirfd, path, flags);
  }
  return ret;
}

int roar_libc_openat64_impl(int dirfd, const char *path, int flags, mode_t mode) {
  static int (*real_openat64)(int, const char *, int, ...) = NULL;
  if (real_openat64 == NULL) {
    real_openat64 = resolve_openat_symbol("openat64");
  }
  if (real_openat64 == NULL) {
    return roar_libc_openat_impl(dirfd, path, flags, mode);
  }

  int has_mode = (flags & O_CREAT) || (flags & O_TMPFILE);
  int ret = has_mode ? real_openat64(dirfd, path, flags, mode) : real_openat64(dirfd, path, flags);
  if (ret >= 0) {
    roar_preload_emit_at_path_flags(dirfd, path, flags);
  }
  return ret;
}

int roar_libc_creat_impl(const char *path, mode_t mode) {
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

#endif // !__APPLE__

// Anchor symbol: referenced from Rust so this TU is pulled in from the
// static archive. Provides the mac DYLD_INTERPOSE table on macOS and
// the roar_libc_*_impl helpers on Linux.
int roar_preload_interpose_keep(void) { return 0; }
