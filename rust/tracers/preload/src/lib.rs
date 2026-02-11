use std::cell::Cell;
use std::ffi::{c_char, c_int, c_uint, c_void, CStr};
use std::fs;
use std::os::unix::net::UnixDatagram;
use std::sync::OnceLock;

use libc::{off_t, size_t, ssize_t};

pub mod ipc;
pub use ipc::TraceEvent;

const TRACE_SOCKET_ENV: &str = "ROAR_PRELOAD_TRACE_SOCK";

thread_local! {
    static IN_HOOK: Cell<bool> = const { Cell::new(false) };
}

static TRACE_SOCKET: OnceLock<Option<UnixDatagram>> = OnceLock::new();

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

fn trace_socket() -> Option<&'static UnixDatagram> {
    TRACE_SOCKET
        .get_or_init(|| {
            let socket_path = std::env::var(TRACE_SOCKET_ENV).ok()?;
            let sock = UnixDatagram::unbound().ok()?;
            sock.connect(socket_path).ok()?;
            Some(sock)
        })
        .as_ref()
}

fn send_event(event: &TraceEvent) {
    let Some(sock) = trace_socket() else {
        return;
    };
    let Ok(payload) = rmp_serde::to_vec_named(event) else {
        return;
    };
    let _ = sock.send(&payload);
}

fn current_pid() -> u32 {
    // SAFETY: libc getpid has no preconditions.
    unsafe { libc::getpid() as u32 }
}

fn fd_path(fd: c_int) -> Option<String> {
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

unsafe fn resolve_symbol<T: Copy>(symbol: &[u8]) -> Option<T> {
    let ptr = libc::dlsym(libc::RTLD_NEXT, symbol.as_ptr() as *const c_char);
    if ptr.is_null() {
        return None;
    }
    Some(std::mem::transmute_copy::<*mut c_void, T>(&ptr))
}

fn set_errno(errno: c_int) {
    // SAFETY: __errno_location returns a valid thread-local errno pointer.
    unsafe {
        *libc::__errno_location() = errno;
    }
}

type ReadFn = unsafe extern "C" fn(c_int, *mut c_void, size_t) -> ssize_t;
type WriteFn = unsafe extern "C" fn(c_int, *const c_void, size_t) -> ssize_t;
type PReadFn = unsafe extern "C" fn(c_int, *mut c_void, size_t, off_t) -> ssize_t;
type PWriteFn = unsafe extern "C" fn(c_int, *const c_void, size_t, off_t) -> ssize_t;
type ReadvFn = unsafe extern "C" fn(c_int, *const libc::iovec, c_int) -> ssize_t;
type WritevFn = unsafe extern "C" fn(c_int, *const libc::iovec, c_int) -> ssize_t;
type SendfileFn = unsafe extern "C" fn(c_int, c_int, *mut off_t, size_t) -> ssize_t;
type CopyFileRangeFn =
    unsafe extern "C" fn(c_int, *mut off_t, c_int, *mut off_t, size_t, c_uint) -> ssize_t;
type RenameFn = unsafe extern "C" fn(*const c_char, *const c_char) -> c_int;
type RenameAtFn = unsafe extern "C" fn(c_int, *const c_char, c_int, *const c_char) -> c_int;
type MmapFn = unsafe extern "C" fn(*mut c_void, size_t, c_int, c_int, c_int, off_t) -> *mut c_void;

#[no_mangle]
pub unsafe extern "C" fn read(fd: c_int, buf: *mut c_void, count: size_t) -> ssize_t {
    let Some(real) = resolve_symbol::<ReadFn>(b"read\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, buf, count);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[no_mangle]
pub unsafe extern "C" fn write(fd: c_int, buf: *const c_void, count: size_t) -> ssize_t {
    let Some(real) = resolve_symbol::<WriteFn>(b"write\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, buf, count);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[no_mangle]
pub unsafe extern "C" fn pread(
    fd: c_int,
    buf: *mut c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    let Some(real) = resolve_symbol::<PReadFn>(b"pread\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, buf, count, offset);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[no_mangle]
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

#[no_mangle]
pub unsafe extern "C" fn pwrite(
    fd: c_int,
    buf: *const c_void,
    count: size_t,
    offset: off_t,
) -> ssize_t {
    let Some(real) = resolve_symbol::<PWriteFn>(b"pwrite\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, buf, count, offset);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[no_mangle]
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

#[no_mangle]
pub unsafe extern "C" fn readv(fd: c_int, iov: *const libc::iovec, iovcnt: c_int) -> ssize_t {
    let Some(real) = resolve_symbol::<ReadvFn>(b"readv\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, iov, iovcnt);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_read(fd));
    }
    ret
}

#[no_mangle]
pub unsafe extern "C" fn writev(fd: c_int, iov: *const libc::iovec, iovcnt: c_int) -> ssize_t {
    let Some(real) = resolve_symbol::<WritevFn>(b"writev\0") else {
        set_errno(libc::ENOSYS);
        return -1;
    };
    let ret = real(fd, iov, iovcnt);
    if ret > 0 && !in_hook() {
        with_hook_guard(|| emit_fd_write(fd));
    }
    ret
}

#[no_mangle]
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

#[no_mangle]
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

#[no_mangle]
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

#[no_mangle]
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

#[no_mangle]
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
