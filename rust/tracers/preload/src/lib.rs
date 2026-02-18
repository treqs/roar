use std::cell::Cell;
use std::ffi::{c_char, c_int, c_uint, c_void, CStr};
#[cfg(not(target_os = "macos"))]
use std::fs;
use std::sync::OnceLock;

use libc::{off_t, size_t, ssize_t};

pub mod ipc;
pub use ipc::TraceEvent;

const TRACE_SOCK_ENV: &str = "ROAR_PRELOAD_TRACE_SOCK";

thread_local! {
    static IN_HOOK: Cell<bool> = const { Cell::new(false) };
    static TRACE_CONN: Cell<(u32, c_int)> = const { Cell::new((0, -1)) };
}

static TRACE_SOCK_PATH: OnceLock<Option<String>> = OnceLock::new();
#[cfg(not(target_os = "macos"))]
static REAL_WRITE: OnceLock<Option<WriteFn>> = OnceLock::new();

#[cfg(target_os = "macos")]
extern "C" {
    fn roar_preload_interpose_keep() -> c_int;
}

// Ensure the C interpose TU is pulled in from the static archive (it otherwise has only `static`
// data and the linker may drop it).
#[cfg(target_os = "macos")]
#[used]
static _ROAR_PRELOAD_INTERPOSE_KEEP: unsafe extern "C" fn() -> c_int = roar_preload_interpose_keep;

#[cfg(target_os = "macos")]
unsafe fn sys_read(fd: c_int, buf: *mut c_void, count: size_t) -> ssize_t {
    // NOTE: `syscall(2)` is variadic; pass arguments as `long` so the libc va_arg reads are correct.
    const SYS_READ: libc::c_int = 3;
    libc::syscall(
        SYS_READ,
        fd as libc::c_long,
        buf as usize as libc::c_long,
        count as libc::c_long,
    ) as ssize_t
}

#[cfg(target_os = "macos")]
unsafe fn sys_write(fd: c_int, buf: *const c_void, count: size_t) -> ssize_t {
    const SYS_WRITE: libc::c_int = 4;
    libc::syscall(
        SYS_WRITE,
        fd as libc::c_long,
        buf as usize as libc::c_long,
        count as libc::c_long,
    ) as ssize_t
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_read(
    fd: c_int,
    buf: *mut c_void,
    count: size_t,
) -> ssize_t {
    read(fd, buf, count)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_write(
    fd: c_int,
    buf: *const c_void,
    count: size_t,
) -> ssize_t {
    write(fd, buf, count)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_pread(
    fd: c_int,
    buf: *mut c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    pread(fd, buf, count, offset)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_pwrite(
    fd: c_int,
    buf: *const c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    pwrite(fd, buf, count, offset)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_readv(
    fd: c_int,
    iov: *const libc::iovec,
    iovcnt: c_int,
) -> ssize_t {
    readv(fd, iov, iovcnt)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_writev(
    fd: c_int,
    iov: *const libc::iovec,
    iovcnt: c_int,
) -> ssize_t {
    writev(fd, iov, iovcnt)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_rename(
    old_path: *const c_char,
    new_path: *const c_char,
) -> c_int {
    rename(old_path, new_path)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_renameat(
    old_dir_fd: c_int,
    old_path: *const c_char,
    new_dir_fd: c_int,
    new_path: *const c_char,
) -> c_int {
    renameat(old_dir_fd, old_path, new_dir_fd, new_path)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_mmap(
    addr: *mut c_void,
    length: size_t,
    prot: c_int,
    flags: c_int,
    fd: c_int,
    offset: off_t,
) -> *mut c_void {
    mmap(addr, length, prot, flags, fd, offset)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_fopen(
    path: *const c_char,
    mode: *const c_char,
) -> *mut libc::FILE {
    fopen(path, mode)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_fdopen(fd: c_int, mode: *const c_char) -> *mut libc::FILE {
    fdopen(fd, mode)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_freopen(
    path: *const c_char,
    mode: *const c_char,
    stream: *mut libc::FILE,
) -> *mut libc::FILE {
    freopen(path, mode, stream)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_unlink(path: *const c_char) -> c_int {
    unlink(path)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_unlinkat(
    dirfd: c_int,
    path: *const c_char,
    flags: c_int,
) -> c_int {
    unlinkat(dirfd, path, flags)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_truncate(path: *const c_char, length: off_t) -> c_int {
    truncate(path, length)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_ftruncate(fd: c_int, length: off_t) -> c_int {
    ftruncate(fd, length)
}

fn in_hook() -> bool {
    IN_HOOK.with(|flag| flag.get())
}

fn with_hook_guard<F: FnOnce()>(f: F) {
    IN_HOOK.with(|flag| {
        if flag.get() {
            return;
        }
        flag.set(true);
        f();
        flag.set(false);
    });
}

fn trace_sock_path() -> Option<&'static str> {
    TRACE_SOCK_PATH
        .get_or_init(|| std::env::var(TRACE_SOCK_ENV).ok())
        .as_deref()
}

fn connect_unix_stream(path: &str) -> Option<c_int> {
    unsafe {
        let fd = libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0);
        if fd < 0 {
            return None;
        }

        // Set close-on-exec so exec'd processes create their own connections
        libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);

        let mut addr: libc::sockaddr_un = std::mem::zeroed();
        addr.sun_family = libc::AF_UNIX as _;
        let path_bytes = path.as_bytes();
        if path_bytes.len() >= addr.sun_path.len() {
            libc::close(fd);
            return None;
        }
        std::ptr::copy_nonoverlapping(
            path_bytes.as_ptr(),
            addr.sun_path.as_mut_ptr() as *mut u8,
            path_bytes.len(),
        );

        let len = std::mem::size_of::<libc::sa_family_t>() + path_bytes.len() + 1;
        if libc::connect(
            fd,
            &addr as *const _ as *const libc::sockaddr,
            len as libc::socklen_t,
        ) != 0
        {
            libc::close(fd);
            return None;
        }
        Some(fd)
    }
}

fn get_trace_fd() -> Option<c_int> {
    let path = trace_sock_path()?;
    let cur_pid = current_pid();
    TRACE_CONN.with(|cell| {
        let (cached_pid, cached_fd) = cell.get();
        if cached_pid == cur_pid && cached_fd >= 0 {
            return Some(cached_fd); // fast path
        }
        // Close stale fd from before fork (if any)
        if cached_fd >= 0 {
            unsafe { libc::close(cached_fd) };
        }
        let fd = connect_unix_stream(path)?;
        cell.set((cur_pid, fd));
        Some(fd)
    })
}

#[cfg(not(target_os = "macos"))]
fn get_real_write() -> Option<WriteFn> {
    *REAL_WRITE.get_or_init(|| unsafe { resolve_symbol::<WriteFn>(b"write\0") })
}

fn send_event(event: &TraceEvent) {
    let Some(fd) = get_trace_fd() else {
        return;
    };
    let Ok(payload) = rmp_serde::to_vec_named(event) else {
        return;
    };
    let len = payload.len() as u32;
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(&len.to_le_bytes());
    frame.extend_from_slice(&payload);
    unsafe {
        #[cfg(target_os = "macos")]
        {
            let _ = sys_write(fd, frame.as_ptr() as *const c_void, frame.len());
        }
        #[cfg(not(target_os = "macos"))]
        {
            // Write via real libc write to bypass our hook.
            let Some(real_write) = get_real_write() else {
                return;
            };
            real_write(fd, frame.as_ptr() as *const c_void, frame.len());
        }
    }
}

fn current_pid() -> u32 {
    // SAFETY: libc getpid has no preconditions.
    unsafe { libc::getpid() as u32 }
}

fn fd_path(fd: c_int) -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        if fd < 0 {
            return None;
        }

        let mut path_buf = [0 as c_char; libc::PATH_MAX as usize];
        // SAFETY: fcntl(F_GETPATH) writes at most PATH_MAX bytes to the provided buffer.
        let rc = unsafe { libc::fcntl(fd, libc::F_GETPATH, path_buf.as_mut_ptr()) };
        if rc != 0 {
            return None;
        }
        // SAFETY: successful F_GETPATH guarantees a NUL-terminated path.
        let path = unsafe { CStr::from_ptr(path_buf.as_ptr()) };
        let path_str = path.to_string_lossy().into_owned();
        if path_str.is_empty() {
            return None;
        }
        return Some(path_str);
    }

    #[cfg(not(target_os = "macos"))]
    {
        if fd < 0 {
            return None;
        }
        let path = fs::read_link(format!("/proc/self/fd/{fd}")).ok()?;
        let path_str = path.to_string_lossy().into_owned();
        if path_str.starts_with("pipe:")
            || path_str.starts_with("socket:")
            || path_str.starts_with("anon_inode:")
        {
            return None;
        }
        Some(path_str)
    }
}

#[cfg(target_os = "macos")]
fn set_errno(errno: c_int) {
    // SAFETY: __error returns a valid thread-local errno pointer on Darwin.
    unsafe {
        *libc::__error() = errno;
    }
}

#[cfg(not(target_os = "macos"))]
fn set_errno(errno: c_int) {
    // SAFETY: __errno_location returns a valid thread-local errno pointer.
    unsafe {
        *libc::__errno_location() = errno;
    }
}

fn c_str_to_owned(path: *const c_char) -> Option<String> {
    if path.is_null() {
        return None;
    }
    // SAFETY: the caller supplies a C string pointer from libc ABI.
    let value = unsafe { CStr::from_ptr(path) };
    Some(value.to_string_lossy().into_owned())
}

fn emit_fd_read(fd: c_int) {
    let Some(path) = fd_path(fd) else {
        return;
    };
    send_event(&TraceEvent::Read {
        pid: current_pid(),
        path,
    });
}

fn emit_fd_write(fd: c_int) {
    let Some(path) = fd_path(fd) else {
        return;
    };
    send_event(&TraceEvent::Write {
        pid: current_pid(),
        path,
    });
}

fn emit_path_write(path: String) {
    if path.is_empty() {
        return;
    }
    send_event(&TraceEvent::Write {
        pid: current_pid(),
        path,
    });
}

fn mode_implies_read(mode: &str) -> bool {
    mode.contains('r') || mode.contains('+')
}

fn mode_implies_write(mode: &str) -> bool {
    mode.contains('w') || mode.contains('a') || mode.contains('x') || mode.contains('+')
}

fn emit_path_mode(path: String, mode: &str) {
    if path.is_empty() {
        return;
    }
    if mode_implies_read(mode) {
        send_event(&TraceEvent::Read {
            pid: current_pid(),
            path: path.clone(),
        });
    }
    if mode_implies_write(mode) {
        send_event(&TraceEvent::Write {
            pid: current_pid(),
            path,
        });
    }
}

fn resolve_at_path(dirfd: c_int, path: *const c_char) -> Option<String> {
    let path_s = c_str_to_owned(path)?;
    if path_s.is_empty() {
        return None;
    }
    if path_s.starts_with('/') || dirfd == libc::AT_FDCWD {
        return Some(path_s);
    }
    let base = fd_path(dirfd)?;
    if base.is_empty() {
        return Some(path_s);
    }
    Some(format!("{base}/{path_s}"))
}

unsafe fn resolve_symbol<T: Copy>(symbol: &[u8]) -> Option<T> {
    let ptr = libc::dlsym(libc::RTLD_NEXT, symbol.as_ptr() as *const c_char);
    if ptr.is_null() {
        return None;
    }
    Some(std::mem::transmute_copy::<*mut c_void, T>(&ptr))
}

#[cfg(not(target_os = "macos"))]
type ReadFn = unsafe extern "C" fn(c_int, *mut c_void, size_t) -> ssize_t;
#[cfg(not(target_os = "macos"))]
type WriteFn = unsafe extern "C" fn(c_int, *const c_void, size_t) -> ssize_t;
type PReadFn = unsafe extern "C" fn(c_int, *mut c_void, size_t, off_t) -> ssize_t;
type PWriteFn = unsafe extern "C" fn(c_int, *const c_void, size_t, off_t) -> ssize_t;
#[cfg(not(target_os = "macos"))]
type ReadvFn = unsafe extern "C" fn(c_int, *const libc::iovec, c_int) -> ssize_t;
#[cfg(not(target_os = "macos"))]
type WritevFn = unsafe extern "C" fn(c_int, *const libc::iovec, c_int) -> ssize_t;
type SendfileFn = unsafe extern "C" fn(c_int, c_int, *mut off_t, size_t) -> ssize_t;
type CopyFileRangeFn =
    unsafe extern "C" fn(c_int, *mut off_t, c_int, *mut off_t, size_t, c_uint) -> ssize_t;
type RenameFn = unsafe extern "C" fn(*const c_char, *const c_char) -> c_int;
type RenameAtFn = unsafe extern "C" fn(c_int, *const c_char, c_int, *const c_char) -> c_int;
type MmapFn = unsafe extern "C" fn(*mut c_void, size_t, c_int, c_int, c_int, off_t) -> *mut c_void;
type FOpenFn = unsafe extern "C" fn(*const c_char, *const c_char) -> *mut libc::FILE;
type FdOpenFn = unsafe extern "C" fn(c_int, *const c_char) -> *mut libc::FILE;
type FreOpenFn =
    unsafe extern "C" fn(*const c_char, *const c_char, *mut libc::FILE) -> *mut libc::FILE;
type UnlinkFn = unsafe extern "C" fn(*const c_char) -> c_int;
type UnlinkAtFn = unsafe extern "C" fn(c_int, *const c_char, c_int) -> c_int;
type TruncateFn = unsafe extern "C" fn(*const c_char, off_t) -> c_int;
type FTruncateFn = unsafe extern "C" fn(c_int, off_t) -> c_int;

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn read(fd: c_int, buf: *mut c_void, count: size_t) -> ssize_t {
    #[cfg(target_os = "macos")]
    let ret = sys_read(fd, buf, count);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<ReadFn>(b"read\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(fd, buf, count)
    };
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn write(fd: c_int, buf: *const c_void, count: size_t) -> ssize_t {
    #[cfg(target_os = "macos")]
    let ret = sys_write(fd, buf, count);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<WriteFn>(b"write\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(fd, buf, count)
    };
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn unlink(path: *const c_char) -> c_int {
    let Some(real) = resolve_symbol::<UnlinkFn>(b"unlink\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(path);
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(p) = c_str_to_owned(path) {
                emit_path_write(p);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn unlinkat(dirfd: c_int, path: *const c_char, flags: c_int) -> c_int {
    let Some(real) = resolve_symbol::<UnlinkAtFn>(b"unlinkat\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(dirfd, path, flags);
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(p) = resolve_at_path(dirfd, path) {
                emit_path_write(p);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn truncate(path: *const c_char, length: off_t) -> c_int {
    let Some(real) = resolve_symbol::<TruncateFn>(b"truncate\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(path, length);
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(p) = c_str_to_owned(path) {
                emit_path_write(p);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn ftruncate(fd: c_int, length: off_t) -> c_int {
    let Some(real) = resolve_symbol::<FTruncateFn>(b"ftruncate\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, length);
    if ret == 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn pread(
    fd: c_int,
    buf: *mut c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    #[cfg(target_os = "macos")]
    let ret = {
        const SYS_PREAD: libc::c_int = 153;
        libc::syscall(
            SYS_PREAD,
            fd as libc::c_long,
            buf as usize as libc::c_long,
            count as libc::c_long,
            offset as libc::c_long,
        ) as ssize_t
    };
    #[cfg(not(target_os = "macos"))]
    let Some(real) = resolve_symbol::<PReadFn>(b"pread\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    #[cfg(not(target_os = "macos"))]
    let ret = real(fd, buf, count, offset);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn pread64(
    fd: c_int,
    buf: *mut c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    let Some(real) = resolve_symbol::<PReadFn>(b"pread64\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, buf, count, offset);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn pwrite(
    fd: c_int,
    buf: *const c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    #[cfg(target_os = "macos")]
    let ret = {
        const SYS_PWRITE: libc::c_int = 154;
        libc::syscall(
            SYS_PWRITE,
            fd as libc::c_long,
            buf as usize as libc::c_long,
            count as libc::c_long,
            offset as libc::c_long,
        ) as ssize_t
    };
    #[cfg(not(target_os = "macos"))]
    let Some(real) = resolve_symbol::<PWriteFn>(b"pwrite\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    #[cfg(not(target_os = "macos"))]
    let ret = real(fd, buf, count, offset);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn pwrite64(
    fd: c_int,
    buf: *const c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    let Some(real) = resolve_symbol::<PWriteFn>(b"pwrite64\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, buf, count, offset);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn readv(fd: c_int, iov: *const libc::iovec, iovcnt: c_int) -> ssize_t {
    #[cfg(target_os = "macos")]
    let ret = {
        const SYS_READV: libc::c_int = 120;
        libc::syscall(
            SYS_READV,
            fd as libc::c_long,
            iov as usize as libc::c_long,
            iovcnt as libc::c_long,
        ) as ssize_t
    };
    #[cfg(not(target_os = "macos"))]
    let Some(real) = resolve_symbol::<ReadvFn>(b"readv\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    #[cfg(not(target_os = "macos"))]
    let ret = real(fd, iov, iovcnt);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn writev(fd: c_int, iov: *const libc::iovec, iovcnt: c_int) -> ssize_t {
    #[cfg(target_os = "macos")]
    let ret = {
        const SYS_WRITEV: libc::c_int = 121;
        libc::syscall(
            SYS_WRITEV,
            fd as libc::c_long,
            iov as usize as libc::c_long,
            iovcnt as libc::c_long,
        ) as ssize_t
    };
    #[cfg(not(target_os = "macos"))]
    let Some(real) = resolve_symbol::<WritevFn>(b"writev\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    #[cfg(not(target_os = "macos"))]
    let ret = real(fd, iov, iovcnt);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn sendfile(
    out_fd: c_int,
    in_fd: c_int,
    offset: *mut off_t,
    count: size_t,
) -> ssize_t {
    let Some(real) = resolve_symbol::<SendfileFn>(b"sendfile\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(out_fd, in_fd, offset, count);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| {
            emit_fd_read(in_fd);
            emit_fd_write(out_fd);
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn copy_file_range(
    fd_in: c_int,
    off_in: *mut off_t,
    fd_out: c_int,
    off_out: *mut off_t,
    len: size_t,
    flags: c_uint,
) -> ssize_t {
    let Some(real) = resolve_symbol::<CopyFileRangeFn>(b"copy_file_range\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd_in, off_in, fd_out, off_out, len, flags);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| {
            emit_fd_read(fd_in);
            emit_fd_write(fd_out);
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn rename(old_path: *const c_char, new_path: *const c_char) -> c_int {
    let Some(real) = resolve_symbol::<RenameFn>(b"rename\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(old_path, new_path);
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(path) = c_str_to_owned(new_path) {
                emit_path_write(path);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn renameat(
    old_dir_fd: c_int,
    old_path: *const c_char,
    new_dir_fd: c_int,
    new_path: *const c_char,
) -> c_int {
    let Some(real) = resolve_symbol::<RenameAtFn>(b"renameat\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(old_dir_fd, old_path, new_dir_fd, new_path);
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(path) = c_str_to_owned(new_path) {
                emit_path_write(path);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn mmap(
    addr: *mut c_void,
    length: size_t,
    prot: c_int,
    flags: c_int,
    fd: c_int,
    offset: off_t,
) -> *mut c_void {
    let Some(real) = resolve_symbol::<MmapFn>(b"mmap\0") else {
        set_errno(libc::ENOSYS);
        return libc::MAP_FAILED;
    };
    let ret = real(addr, length, prot, flags, fd, offset);
    if ret != libc::MAP_FAILED && fd >= 0 && !in_hook() {
        with_hook_guard(|| {
            if prot & libc::PROT_READ != 0 {
                emit_fd_read(fd);
            }
            if (flags & libc::MAP_SHARED) != 0 && (prot & libc::PROT_WRITE) != 0 {
                emit_fd_write(fd);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn fopen(path: *const c_char, mode: *const c_char) -> *mut libc::FILE {
    let Some(real) = resolve_symbol::<FOpenFn>(b"fopen\0") else {
        set_errno(libc::ENOSYS);
        return std::ptr::null_mut();
    };
    let ret = real(path, mode);
    if !ret.is_null() && !in_hook() {
        with_hook_guard(|| {
            let Some(path_s) = c_str_to_owned(path) else {
                return;
            };
            let Some(mode_s) = c_str_to_owned(mode) else {
                return;
            };
            emit_path_mode(path_s, &mode_s);
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn fdopen(fd: c_int, mode: *const c_char) -> *mut libc::FILE {
    let Some(real) = resolve_symbol::<FdOpenFn>(b"fdopen\0") else {
        set_errno(libc::ENOSYS);
        return std::ptr::null_mut();
    };
    let ret = real(fd, mode);
    if !ret.is_null() && !in_hook() {
        with_hook_guard(|| {
            let Some(path) = fd_path(fd) else {
                return;
            };
            let Some(mode_s) = c_str_to_owned(mode) else {
                return;
            };
            emit_path_mode(path, &mode_s);
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn freopen(
    path: *const c_char,
    mode: *const c_char,
    stream: *mut libc::FILE,
) -> *mut libc::FILE {
    let Some(real) = resolve_symbol::<FreOpenFn>(b"freopen\0") else {
        set_errno(libc::ENOSYS);
        return std::ptr::null_mut();
    };
    let ret = real(path, mode, stream);
    if !ret.is_null() && !in_hook() {
        with_hook_guard(|| {
            let Some(path_s) = c_str_to_owned(path) else {
                return;
            };
            let Some(mode_s) = c_str_to_owned(mode) else {
                return;
            };
            emit_path_mode(path_s, &mode_s);
        });
    }
    ret
}
