use std::cell::Cell;
use std::ffi::{c_char, c_int, c_uint, c_void, CStr};
#[cfg(not(target_os = "macos"))]
use std::fs;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::OnceLock;

use libc::{off_t, size_t, ssize_t};

pub mod ipc;
pub use ipc::TraceEvent;

const TRACE_SOCK_ENV: &str = "ROAR_PRELOAD_TRACE_SOCK";
const SOCK_BUF_SIZE: c_int = 65536;

/// Incremented in the child after fork() via pthread_atfork handler.
static FORK_GEN: AtomicU32 = AtomicU32::new(1);

thread_local! {
    static IN_HOOK: Cell<bool> = const { Cell::new(false) };
    /// (fork_generation, fd) — fd is reused while generation matches.
    static TRACE_CONN: Cell<(u32, c_int)> = const { Cell::new((0, -1)) };
}

static TRACE_SOCK_PATH: OnceLock<Option<String>> = OnceLock::new();
#[cfg(not(target_os = "macos"))]
static REAL_WRITE: OnceLock<Option<WriteFn>> = OnceLock::new();

#[cfg(target_os = "macos")]
unsafe extern "C" {
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

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
unsafe fn sys_mmap(
    addr: *mut c_void,
    length: size_t,
    prot: c_int,
    flags: c_int,
    fd: c_int,
    offset: off_t,
) -> *mut c_void {
    // Cannot use libc::syscall() for mmap because it returns `int` on macOS, truncating the
    // 64-bit pointer. Use inline assembly to invoke the syscall directly.
    // XNU arm64 ABI: x16 = syscall number, x0-x5 = args, svc #0x80; on error carry flag set
    // and x0 = errno.
    let ret: usize;
    let err: usize;
    core::arch::asm!(
        "mov x16, #197",
        "svc #0x80",
        "cset {err}, cs",
        inout("x0") addr as usize => ret,
        in("x1") length,
        in("x2") prot as usize,
        in("x3") flags as usize,
        in("x4") fd as usize,
        in("x5") offset as usize,
        err = out(reg) err,
        out("x16") _,
        options(nostack),
    );
    if err != 0 {
        set_errno(ret as c_int);
        return libc::MAP_FAILED;
    }
    ret as *mut c_void
}

#[cfg(all(target_os = "macos", target_arch = "x86_64"))]
unsafe fn sys_mmap(
    addr: *mut c_void,
    length: size_t,
    prot: c_int,
    flags: c_int,
    fd: c_int,
    offset: off_t,
) -> *mut c_void {
    // Cannot use libc::syscall() for mmap because it returns `int` on macOS, truncating the
    // 64-bit pointer. Use inline assembly to invoke the syscall directly.
    // XNU x86_64 ABI: rax = syscall number (BSD class 0x2000000 + 197), args in
    // rdi/rsi/rdx/r10/r8/r9, syscall instruction; on error carry flag set and rax = errno.
    let ret: usize;
    let err: u8;
    core::arch::asm!(
        "mov rax, 0x20000C5",
        "syscall",
        "setc {err_byte}",
        inout("rdi") addr as usize => _,
        inout("rsi") length => _,
        inout("rdx") prot as usize => _,
        inout("r10") flags as usize => _,
        inout("r8") fd as usize => _,
        inout("r9") offset as usize => _,
        inout("rax") 0usize => ret,
        err_byte = out(reg_byte) err,
        out("rcx") _,
        out("r11") _,
        options(nostack),
    );
    if err != 0 {
        set_errno(ret as c_int);
        return libc::MAP_FAILED;
    }
    ret as *mut c_void
}

#[cfg(target_os = "macos")]
unsafe fn sys_rename(old_path: *const c_char, new_path: *const c_char) -> c_int {
    const SYS_RENAME: libc::c_int = 128;
    libc::syscall(
        SYS_RENAME,
        old_path as usize as libc::c_long,
        new_path as usize as libc::c_long,
    ) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_renameat(
    old_dirfd: c_int,
    old_path: *const c_char,
    new_dirfd: c_int,
    new_path: *const c_char,
) -> c_int {
    const SYS_RENAMEAT: libc::c_int = 465;
    libc::syscall(
        SYS_RENAMEAT,
        old_dirfd as libc::c_long,
        old_path as usize as libc::c_long,
        new_dirfd as libc::c_long,
        new_path as usize as libc::c_long,
    ) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_link(old_path: *const c_char, new_path: *const c_char) -> c_int {
    const SYS_LINK: libc::c_int = 9;
    libc::syscall(
        SYS_LINK,
        old_path as usize as libc::c_long,
        new_path as usize as libc::c_long,
    ) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_linkat(
    old_dirfd: c_int,
    old_path: *const c_char,
    new_dirfd: c_int,
    new_path: *const c_char,
    flags: c_int,
) -> c_int {
    const SYS_LINKAT: libc::c_int = 470;
    libc::syscall(
        SYS_LINKAT,
        old_dirfd as libc::c_long,
        old_path as usize as libc::c_long,
        new_dirfd as libc::c_long,
        new_path as usize as libc::c_long,
        flags as libc::c_long,
    ) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_unlink(path: *const c_char) -> c_int {
    const SYS_UNLINK: libc::c_int = 10;
    libc::syscall(SYS_UNLINK, path as usize as libc::c_long) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_unlinkat(dirfd: c_int, path: *const c_char, flags: c_int) -> c_int {
    const SYS_UNLINKAT: libc::c_int = 472;
    libc::syscall(
        SYS_UNLINKAT,
        dirfd as libc::c_long,
        path as usize as libc::c_long,
        flags as libc::c_long,
    ) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_truncate(path: *const c_char, length: off_t) -> c_int {
    const SYS_TRUNCATE: libc::c_int = 200;
    libc::syscall(
        SYS_TRUNCATE,
        path as usize as libc::c_long,
        length as libc::c_long,
    ) as c_int
}

#[cfg(target_os = "macos")]
unsafe fn sys_ftruncate(fd: c_int, length: off_t) -> c_int {
    const SYS_FTRUNCATE: libc::c_int = 201;
    libc::syscall(SYS_FTRUNCATE, fd as libc::c_long, length as libc::c_long) as c_int
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
pub unsafe extern "C" fn roar_interpose_link(
    old_path: *const c_char,
    new_path: *const c_char,
) -> c_int {
    link(old_path, new_path)
}

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn roar_interpose_linkat(
    old_dir_fd: c_int,
    old_path: *const c_char,
    new_dir_fd: c_int,
    new_path: *const c_char,
    flags: c_int,
) -> c_int {
    linkat(old_dir_fd, old_path, new_dir_fd, new_path, flags)
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

unsafe extern "C" fn on_fork_child() {
    FORK_GEN.fetch_add(1, Ordering::Relaxed);
}

fn trace_sock_path() -> Option<&'static str> {
    TRACE_SOCK_PATH
        .get_or_init(|| {
            // Register the fork handler once, alongside the first env-var read.
            unsafe {
                libc::pthread_atfork(None, None, Some(on_fork_child));
            }
            std::env::var(TRACE_SOCK_ENV).ok()
        })
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

        // Enlarge send buffer to reduce writer-side blocking on the 2ms poll interval.
        libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            &SOCK_BUF_SIZE as *const _ as *const c_void,
            std::mem::size_of::<c_int>() as libc::socklen_t,
        );

        // Prevent SIGPIPE when writing to a broken socket (fd closed by child process).
        #[cfg(target_os = "macos")]
        {
            let one: c_int = 1;
            libc::setsockopt(
                fd,
                libc::SOL_SOCKET,
                libc::SO_NOSIGPIPE,
                &one as *const _ as *const c_void,
                std::mem::size_of::<c_int>() as libc::socklen_t,
            );
        }

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
    let gen = FORK_GEN.load(Ordering::Relaxed);
    TRACE_CONN.with(|cell| {
        let (cached_gen, cached_fd) = cell.get();
        if cached_gen == gen && cached_fd >= 0 {
            return Some(cached_fd); // fast path: atomic load + thread-local, no syscall
        }
        // Close stale fd from before fork (if any)
        if cached_fd >= 0 {
            unsafe { libc::close(cached_fd) };
        }
        let fd = connect_unix_stream(path)?;
        cell.set((gen, fd));
        Some(fd)
    })
}

fn invalidate_trace_fd() {
    TRACE_CONN.with(|cell| {
        // Do NOT close — fd is already dead or reused by the app
        cell.set((0, -1));
    });
}

#[cfg(not(target_os = "macos"))]
fn get_real_write() -> Option<WriteFn> {
    *REAL_WRITE.get_or_init(|| unsafe { resolve_symbol::<WriteFn>(b"write\0") })
}

fn write_frame(fd: c_int, frame: &[u8]) -> bool {
    let mut written = 0usize;
    while written < frame.len() {
        let rc: ssize_t;
        unsafe {
            #[cfg(target_os = "macos")]
            {
                rc = sys_write(
                    fd,
                    frame[written..].as_ptr() as *const c_void,
                    frame.len() - written,
                );
            }
            #[cfg(not(target_os = "macos"))]
            {
                let Some(real_write) = get_real_write() else {
                    return false;
                };
                rc = real_write(
                    fd,
                    frame[written..].as_ptr() as *const c_void,
                    frame.len() - written,
                );
            }
        }

        if rc < 0 {
            let err = get_errno();
            if err == libc::EINTR {
                continue;
            }
            return false;
        }
        if rc == 0 {
            return false;
        }
        written += rc as usize;
    }
    true
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

    if !write_frame(fd, &frame) {
        let err = get_errno();
        if err == libc::EBADF || err == libc::EPIPE || err == libc::ENOTCONN {
            invalidate_trace_fd();
            if let Some(new_fd) = get_trace_fd() {
                let _ = write_frame(new_fd, &frame);
            }
        }
    }
}

fn current_pid() -> u32 {
    // SAFETY: libc getpid has no preconditions.
    unsafe { libc::getpid() as u32 }
}

#[cfg(target_os = "macos")]
fn current_thread_id() -> u32 {
    let mut thread_id = 0u64;
    // SAFETY: pthread_threadid_np writes the current thread id into the provided pointer.
    let rc = unsafe { libc::pthread_threadid_np(0, &mut thread_id) };
    if rc == 0 {
        thread_id as u32
    } else {
        current_pid()
    }
}

#[cfg(not(target_os = "macos"))]
fn current_thread_id() -> u32 {
    // SAFETY: syscall(SYS_gettid) has no preconditions on Linux.
    unsafe { libc::syscall(libc::SYS_gettid) as u32 }
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

#[cfg(target_os = "macos")]
fn get_errno() -> c_int {
    unsafe { *libc::__error() }
}

#[cfg(not(target_os = "macos"))]
fn get_errno() -> c_int {
    unsafe { *libc::__errno_location() }
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
        thread_id: current_thread_id(),
        path,
    });
}

fn emit_fd_write(fd: c_int) {
    let Some(path) = fd_path(fd) else {
        return;
    };
    send_event(&TraceEvent::Write {
        pid: current_pid(),
        thread_id: current_thread_id(),
        path,
    });
}

fn emit_path_write(path: String) {
    if path.is_empty() {
        return;
    }
    send_event(&TraceEvent::Write {
        pid: current_pid(),
        thread_id: current_thread_id(),
        path,
    });
}

#[cfg(target_os = "macos")]
fn mode_implies_read(mode: &str) -> bool {
    mode.contains('r') || mode.contains('+')
}

#[cfg(target_os = "macos")]
fn mode_implies_write(mode: &str) -> bool {
    mode.contains('w') || mode.contains('a') || mode.contains('x') || mode.contains('+')
}

#[cfg(target_os = "macos")]
const O_TMPFILE_FLAG: c_int = 0;

#[cfg(not(target_os = "macos"))]
const O_TMPFILE_FLAG: c_int = libc::O_TMPFILE;

fn flags_imply_read(flags: c_int) -> bool {
    let access_mode = flags & libc::O_ACCMODE;
    access_mode == libc::O_RDONLY || access_mode == libc::O_RDWR
}

fn flags_imply_write(flags: c_int) -> bool {
    let access_mode = flags & libc::O_ACCMODE;
    access_mode == libc::O_WRONLY
        || access_mode == libc::O_RDWR
        || (flags & libc::O_CREAT) != 0
        || (flags & libc::O_TRUNC) != 0
        || (flags & libc::O_APPEND) != 0
        || (flags & O_TMPFILE_FLAG) != 0
}

#[cfg(target_os = "macos")]
fn emit_path_mode(path: String, mode: &str) {
    if path.is_empty() {
        return;
    }
    if mode_implies_read(mode) {
        send_event(&TraceEvent::Read {
            pid: current_pid(),
            thread_id: current_thread_id(),
            path: path.clone(),
        });
    }
    if mode_implies_write(mode) {
        send_event(&TraceEvent::Write {
            pid: current_pid(),
            thread_id: current_thread_id(),
            path,
        });
    }
}

fn emit_path_flags(path: String, flags: c_int) {
    if path.is_empty() {
        return;
    }
    if flags_imply_read(flags) {
        send_event(&TraceEvent::Read {
            pid: current_pid(),
            thread_id: current_thread_id(),
            path: path.clone(),
        });
    }
    if flags_imply_write(flags) {
        send_event(&TraceEvent::Write {
            pid: current_pid(),
            thread_id: current_thread_id(),
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

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn roar_preload_emit_path_flags(path: *const c_char, flags: c_int) {
    if in_hook() {
        return;
    }
    with_hook_guard(|| {
        if let Some(path_s) = c_str_to_owned(path) {
            emit_path_flags(path_s, flags);
        }
    });
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn roar_preload_emit_at_path_flags(
    dirfd: c_int,
    path: *const c_char,
    flags: c_int,
) {
    if in_hook() {
        return;
    }
    with_hook_guard(|| {
        if let Some(path_s) = resolve_at_path(dirfd, path) {
            emit_path_flags(path_s, flags);
        }
    });
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
#[cfg(not(target_os = "macos"))]
type RenameFn = unsafe extern "C" fn(*const c_char, *const c_char) -> c_int;
#[cfg(not(target_os = "macos"))]
type RenameAtFn = unsafe extern "C" fn(c_int, *const c_char, c_int, *const c_char) -> c_int;
#[cfg(not(target_os = "macos"))]
type LinkFn = unsafe extern "C" fn(*const c_char, *const c_char) -> c_int;
#[cfg(not(target_os = "macos"))]
type LinkAtFn = unsafe extern "C" fn(c_int, *const c_char, c_int, *const c_char, c_int) -> c_int;
#[cfg(not(target_os = "macos"))]
type MmapFn = unsafe extern "C" fn(*mut c_void, size_t, c_int, c_int, c_int, off_t) -> *mut c_void;
type FOpenFn = unsafe extern "C" fn(*const c_char, *const c_char) -> *mut libc::FILE;
type FReadFn = unsafe extern "C" fn(*mut c_void, size_t, size_t, *mut libc::FILE) -> size_t;
type FWriteFn = unsafe extern "C" fn(*const c_void, size_t, size_t, *mut libc::FILE) -> size_t;
type FdOpenFn = unsafe extern "C" fn(c_int, *const c_char) -> *mut libc::FILE;
type FreOpenFn =
    unsafe extern "C" fn(*const c_char, *const c_char, *mut libc::FILE) -> *mut libc::FILE;
#[cfg(not(target_os = "macos"))]
type UnlinkFn = unsafe extern "C" fn(*const c_char) -> c_int;
#[cfg(not(target_os = "macos"))]
type UnlinkAtFn = unsafe extern "C" fn(c_int, *const c_char, c_int) -> c_int;
#[cfg(not(target_os = "macos"))]
type TruncateFn = unsafe extern "C" fn(*const c_char, off_t) -> c_int;
#[cfg(not(target_os = "macos"))]
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
    #[cfg(target_os = "macos")]
    let ret = sys_unlink(path);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<UnlinkFn>(b"unlink\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(path)
    };
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
    #[cfg(target_os = "macos")]
    let ret = sys_unlinkat(dirfd, path, flags);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<UnlinkAtFn>(b"unlinkat\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(dirfd, path, flags)
    };
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
    #[cfg(target_os = "macos")]
    let ret = sys_truncate(path, length);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<TruncateFn>(b"truncate\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(path, length)
    };
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
    #[cfg(target_os = "macos")]
    let ret = sys_ftruncate(fd, length);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<FTruncateFn>(b"ftruncate\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(fd, length)
    };
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
    #[cfg(target_os = "macos")]
    let ret = sys_rename(old_path, new_path);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<RenameFn>(b"rename\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(old_path, new_path)
    };
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
    #[cfg(target_os = "macos")]
    let ret = sys_renameat(old_dir_fd, old_path, new_dir_fd, new_path);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<RenameAtFn>(b"renameat\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(old_dir_fd, old_path, new_dir_fd, new_path)
    };
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(path) = resolve_at_path(new_dir_fd, new_path) {
                emit_path_write(path);
            }
        });
    }
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn link(old_path: *const c_char, new_path: *const c_char) -> c_int {
    #[cfg(target_os = "macos")]
    let ret = sys_link(old_path, new_path);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<LinkFn>(b"link\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(old_path, new_path)
    };
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
pub unsafe extern "C" fn linkat(
    old_dir_fd: c_int,
    old_path: *const c_char,
    new_dir_fd: c_int,
    new_path: *const c_char,
    flags: c_int,
) -> c_int {
    #[cfg(target_os = "macos")]
    let ret = sys_linkat(old_dir_fd, old_path, new_dir_fd, new_path, flags);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<LinkAtFn>(b"linkat\0") else {
            set_errno(libc::ENOSYS);
            return -1;
        };
        real(old_dir_fd, old_path, new_dir_fd, new_path, flags)
    };
    if ret == 0 && !in_hook() {
        with_hook_guard(|| {
            if let Some(path) = resolve_at_path(new_dir_fd, new_path) {
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
    #[cfg(target_os = "macos")]
    let ret = sys_mmap(addr, length, prot, flags, fd, offset);
    #[cfg(not(target_os = "macos"))]
    let ret = {
        let Some(real) = resolve_symbol::<MmapFn>(b"mmap\0") else {
            set_errno(libc::ENOSYS);
            return libc::MAP_FAILED;
        };
        real(addr, length, prot, flags, fd, offset)
    };
    // Use a process-wide atomic guard instead of thread_local IN_HOOK for mmap. Thread-local
    // storage is not available during early dyld init when mmap is called to map shared libraries.
    // A static AtomicBool is safe because it lives in BSS (zero-initialized by the kernel).
    static MMAP_IN_HOOK: AtomicBool = AtomicBool::new(false);
    if ret != libc::MAP_FAILED && fd >= 0 && !MMAP_IN_HOOK.swap(true, Ordering::Relaxed) {
        if prot & libc::PROT_READ != 0 {
            emit_fd_read(fd);
        }
        if (flags & libc::MAP_SHARED) != 0 && (prot & libc::PROT_WRITE) != 0 {
            emit_fd_write(fd);
        }
        MMAP_IN_HOOK.store(false, Ordering::Relaxed);
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
    // On macOS, emit read/write from fopen since the read/write hooks use direct
    // syscalls and won't capture fopen-based I/O. On Linux, the read/write hooks
    // already cover this via /proc/self/fd path resolution, so skip to avoid
    // duplicate events with mismatched relative/absolute paths.
    #[cfg(target_os = "macos")]
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
    #[cfg(target_os = "macos")]
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
    #[cfg(target_os = "macos")]
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
pub unsafe extern "C" fn fopen64(path: *const c_char, mode: *const c_char) -> *mut libc::FILE {
    let Some(real) = resolve_symbol::<FOpenFn>(b"fopen64\0") else {
        set_errno(libc::ENOSYS);
        return std::ptr::null_mut();
    };
    let ret = real(path, mode);
    #[cfg(target_os = "macos")]
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
pub unsafe extern "C" fn fread(
    ptr: *mut c_void,
    size: size_t,
    nmemb: size_t,
    stream: *mut libc::FILE,
) -> size_t {
    let Some(real) = resolve_symbol::<FReadFn>(b"fread\0") else {
        set_errno(libc::ENOSYS);
        return 0;
    };
    // Set hook guard BEFORE the real call so that nested read() calls
    // from within glibc's fread are suppressed (avoiding double-counting).
    if in_hook() {
        return real(ptr, size, nmemb, stream);
    }
    IN_HOOK.with(|flag| flag.set(true));
    let ret = real(ptr, size, nmemb, stream);
    if ret > 0 {
        let fd = libc::fileno(stream);
        emit_fd_read(fd);
    }
    IN_HOOK.with(|flag| flag.set(false));
    ret
}

#[cfg_attr(not(target_os = "macos"), no_mangle)]
pub unsafe extern "C" fn fwrite(
    ptr: *const c_void,
    size: size_t,
    nmemb: size_t,
    stream: *mut libc::FILE,
) -> size_t {
    let Some(real) = resolve_symbol::<FWriteFn>(b"fwrite\0") else {
        set_errno(libc::ENOSYS);
        return 0;
    };
    // Set hook guard BEFORE the real call so that nested write() calls
    // from within glibc's fwrite are suppressed (avoiding double-counting).
    if in_hook() {
        return real(ptr, size, nmemb, stream);
    }
    IN_HOOK.with(|flag| flag.set(true));
    let ret = real(ptr, size, nmemb, stream);
    if ret > 0 {
        let fd = libc::fileno(stream);
        emit_fd_write(fd);
    }
    IN_HOOK.with(|flag| flag.set(false));
    ret
}
