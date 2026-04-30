#![no_std]
#![no_main]

use aya_ebpf::{
    helpers::bpf_get_current_pid_tgid,
    macros::{map, tracepoint},
    maps::{HashMap, PerCpuArray, RingBuf},
    programs::TracePointContext,
};
use roar_ebpf_common::{
    EventType, LargeEvent, PendingArgs, SmallEvent, MAX_PATH_LEN, TAG_LARGE, TAG_SMALL,
};

// ── BPF Maps ──────────────────────────────────────────────────────────────────

/// PID filter: only trace events from PIDs in this map.
/// Value is the run_id (used by the daemon to demux events to the correct run).
#[map]
static TRACKED_PIDS: HashMap<u32, u64> = HashMap::with_max_entries(4096, 0);

/// Stash for syscall entry args (keyed by pid_tgid).
/// Must be a regular HashMap (not PerCpu) because a thread can migrate
/// between CPUs between syscall entry and exit tracepoints.
#[map]
static PENDING_ARGS: HashMap<u64, PendingArgs> = HashMap::with_max_entries(4096, 0);

/// Ring buffer for emitting events to userspace.
/// Must be a power-of-two size. 256KB is sufficient for most workloads.
#[map]
static EVENTS: RingBuf = RingBuf::with_byte_size(256 * 1024, 0);

/// Per-CPU scratch space for building PendingArgs without exceeding BPF stack limit.
/// The BPF stack is only 512 bytes; PendingArgs is ~312 bytes.
#[map]
static SCRATCH_ARGS: PerCpuArray<PendingArgs> = PerCpuArray::with_max_entries(1, 0);

// ── Helpers ───────────────────────────────────────────────────────────────────

#[inline(always)]
fn pid_tracked(pid: u32) -> bool {
    unsafe { TRACKED_PIDS.get(&pid).is_some() }
}

#[inline(always)]
fn current_pid() -> u32 {
    (bpf_get_current_pid_tgid() >> 32) as u32
}

#[inline(always)]
fn current_pid_tgid() -> u64 {
    bpf_get_current_pid_tgid()
}

/// Read a sys_enter tracepoint argument by index.
/// sys_enter layout: __syscall_nr at offset 8 (padded), args at offset 16+.
#[inline(always)]
unsafe fn read_sys_enter_arg(ctx: &TracePointContext, index: usize) -> Result<u64, i64> {
    ctx.read_at(16 + index * 8)
}

/// Read the return value from a sys_exit tracepoint.
/// sys_exit layout: id at offset 8, ret at offset 16.
#[inline(always)]
unsafe fn read_sys_exit_ret(ctx: &TracePointContext) -> Result<i64, i64> {
    ctx.read_at(16)
}

/// Get a mutable pointer to the scratch PendingArgs buffer (per-CPU, no stack allocation).
#[inline(always)]
fn get_scratch() -> Option<*mut PendingArgs> {
    SCRATCH_ARGS.get_ptr_mut(0)
}

/// Stash scratch args into the pending map for the current pid_tgid.
#[inline(always)]
fn stash_scratch() {
    let key = current_pid_tgid();
    if let Some(ptr) = get_scratch() {
        let _ = unsafe { PENDING_ARGS.insert(&key, &*ptr, 0) };
    }
}

/// Submit a SmallEvent to the ring buffer. SmallEvent is 32 bytes — safe on BPF stack.
#[inline(always)]
fn emit_small(event: &SmallEvent) {
    const SIZE: usize = 4 + core::mem::size_of::<SmallEvent>();
    if let Some(mut buf) = EVENTS.reserve::<[u8; SIZE]>(0) {
        let ptr = buf.as_mut_ptr() as *mut u8;
        unsafe {
            core::ptr::write_unaligned(ptr as *mut u32, TAG_SMALL);
            core::ptr::write_unaligned(ptr.add(4) as *mut SmallEvent, *event);
        }
        buf.submit(0);
    }
}

/// Write a LargeEvent directly into the ring buffer (no stack allocation).
/// Reads path data from the given source pointer.
#[inline(always)]
fn emit_large_direct(
    pid: u32,
    event_type: EventType,
    ret_val: i64,
    arg0: u64,
    arg1: u64,
    path_src: *const u8,
) {
    const SIZE: usize = 4 + core::mem::size_of::<LargeEvent>();
    if let Some(mut buf) = EVENTS.reserve::<[u8; SIZE]>(0) {
        let base = buf.as_mut_ptr() as *mut u8;
        unsafe {
            core::ptr::write_unaligned(base as *mut u32, TAG_LARGE);
            let ev = base.add(4) as *mut LargeEvent;
            (*ev).pid = pid;
            (*ev).event_type = event_type as u16;
            (*ev)._pad = 0;
            (*ev).ret_val = ret_val;
            (*ev).arg0 = arg0;
            (*ev).arg1 = arg1;
            if !path_src.is_null() {
                core::ptr::copy_nonoverlapping(path_src, (*ev).path.as_mut_ptr(), MAX_PATH_LEN);
            } else {
                core::ptr::write_bytes((*ev).path.as_mut_ptr(), 0, MAX_PATH_LEN);
            }
        }
        buf.submit(0);
    }
}

// ── open / openat ─────────────────────────────────────────────────────────────

#[tracepoint]
pub fn sys_enter_openat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_openat(&ctx);
    0
}

fn try_sys_enter_openat(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let dirfd = unsafe { read_sys_enter_arg(ctx, 0)? };
    let filename_ptr = unsafe { read_sys_enter_arg(ctx, 1)? };
    let flags = unsafe { read_sys_enter_arg(ctx, 2)? };

    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = roar_ebpf_common::syscall_nr::OPENAT;
        (*scratch).arg0 = flags;
        (*scratch).arg1 = dirfd;
        (*scratch).arg2 = 0;
        (*scratch).arg3 = 0;
        (*scratch).arg4 = 0;
        // Zero and read path
        core::ptr::write_bytes((*scratch).path.as_mut_ptr(), 0, MAX_PATH_LEN);
        let _ = aya_ebpf::helpers::bpf_probe_read_user_str_bytes(
            filename_ptr as *const u8,
            &mut (*scratch).path,
        );
    }

    stash_scratch();
    Ok(())
}

#[tracepoint]
pub fn sys_exit_openat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_open(&ctx);
    0
}

fn try_sys_exit_open(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };

    let ret = unsafe { read_sys_exit_ret(ctx)? };
    if ret < 0 {
        let _ = PENDING_ARGS.remove(&key);
        return Ok(());
    }

    emit_large_direct(
        pid,
        EventType::OpenExit,
        ret,
        args.arg0, // flags
        args.arg1, // dirfd
        args.path.as_ptr(),
    );

    let _ = PENDING_ARGS.remove(&key);
    Ok(())
}

// Legacy open(2)
#[tracepoint]
pub fn sys_enter_open(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_open(&ctx);
    0
}

fn try_sys_enter_open(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let filename_ptr = unsafe { read_sys_enter_arg(ctx, 0)? };
    let flags = unsafe { read_sys_enter_arg(ctx, 1)? };

    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = roar_ebpf_common::syscall_nr::OPEN;
        (*scratch).arg0 = flags;
        (*scratch).arg1 = u64::MAX; // AT_FDCWD sentinel
        (*scratch).arg2 = 0;
        (*scratch).arg3 = 0;
        (*scratch).arg4 = 0;
        core::ptr::write_bytes((*scratch).path.as_mut_ptr(), 0, MAX_PATH_LEN);
        let _ = aya_ebpf::helpers::bpf_probe_read_user_str_bytes(
            filename_ptr as *const u8,
            &mut (*scratch).path,
        );
    }

    stash_scratch();
    Ok(())
}

#[tracepoint]
pub fn sys_exit_open(ctx: TracePointContext) -> u32 {
    // Reuses same exit handler as openat
    let _ = try_sys_exit_open(&ctx);
    0
}

// ── Generic entry for small syscalls (read, write, close, lseek) ──────────────

/// Stash a simple pending entry: just syscall_nr and fd (arg0).
#[inline(always)]
fn stash_fd_entry(syscall_nr: u64, fd: u64) -> Result<(), i64> {
    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = syscall_nr;
        (*scratch).arg0 = fd;
        (*scratch).arg1 = 0;
        (*scratch).arg2 = 0;
        (*scratch).arg3 = 0;
        (*scratch).arg4 = 0;
    }
    stash_scratch();
    Ok(())
}

/// Stash a pending entry with two fd args (for dup2, copy_file_range, etc.).
#[inline(always)]
fn stash_two_fd_entry(syscall_nr: u64, fd0: u64, fd1: u64) -> Result<(), i64> {
    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = syscall_nr;
        (*scratch).arg0 = fd0;
        (*scratch).arg1 = fd1;
        (*scratch).arg2 = 0;
        (*scratch).arg3 = 0;
        (*scratch).arg4 = 0;
    }
    stash_scratch();
    Ok(())
}

// ── read ──────────────────────────────────────────────────────────────────────

#[tracepoint]
pub fn sys_enter_read(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_fd(&ctx, roar_ebpf_common::syscall_nr::READ);
    0
}

#[tracepoint]
pub fn sys_exit_read(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_rw(&ctx, EventType::Read);
    0
}

// ── pread64 ──────────────────────────────────────────────────────────────────
// pread64(fd, buf, count, offset) — positional read without moving the cursor.

#[tracepoint]
pub fn sys_enter_pread64(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_pread64(&ctx);
    0
}

fn try_sys_enter_pread64(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }
    let fd = unsafe { read_sys_enter_arg(ctx, 0)? };
    let offset = unsafe { read_sys_enter_arg(ctx, 3)? };

    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = roar_ebpf_common::syscall_nr::PREAD64;
        (*scratch).arg0 = fd;
        (*scratch).arg1 = offset;
        (*scratch).arg2 = 0;
        (*scratch).arg3 = 0;
        (*scratch).arg4 = 0;
    }
    stash_scratch();
    Ok(())
}

#[tracepoint]
pub fn sys_exit_pread64(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_preadwrite(&ctx, EventType::PRead);
    0
}

// ── pwrite64 ─────────────────────────────────────────────────────────────────
// pwrite64(fd, buf, count, offset) — positional write without moving the cursor.

#[tracepoint]
pub fn sys_enter_pwrite64(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_pread64(&ctx); // same arg layout as pread64
    0
}

#[tracepoint]
pub fn sys_exit_pwrite64(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_preadwrite(&ctx, EventType::PWrite);
    0
}

/// Common exit handler for pread64/pwrite64 — emits SmallEvent with fd, offset, and bytes.
fn try_sys_exit_preadwrite(ctx: &TracePointContext, event_type: EventType) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let fd = args.arg0;
    let offset = args.arg1;

    let ret = unsafe { read_sys_exit_ret(ctx)? };

    let _ = PENDING_ARGS.remove(&key);

    if ret <= 0 {
        return Ok(());
    }

    emit_small(&SmallEvent {
        pid,
        thread_id: bpf_get_current_pid_tgid() as u32,
        event_type: event_type as u16,
        _pad: 0,
        ret_val: ret,
        arg0: fd,
        arg1: offset,
    });

    Ok(())
}

// ── write ─────────────────────────────────────────────────────────────────────

#[tracepoint]
pub fn sys_enter_write(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_fd(&ctx, roar_ebpf_common::syscall_nr::WRITE);
    0
}

#[tracepoint]
pub fn sys_exit_write(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_rw(&ctx, EventType::Write);
    0
}

/// Common entry handler for syscalls that just need fd (arg0).
fn try_sys_enter_fd(ctx: &TracePointContext, syscall_nr: u64) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }
    let fd = unsafe { read_sys_enter_arg(ctx, 0)? };
    stash_fd_entry(syscall_nr, fd)
}

/// Common exit handler for read/write family — emits SmallEvent with fd and bytes.
fn try_sys_exit_rw(ctx: &TracePointContext, event_type: EventType) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let fd = args.arg0;

    let ret = unsafe { read_sys_exit_ret(ctx)? };

    let _ = PENDING_ARGS.remove(&key);

    if ret <= 0 {
        return Ok(());
    }

    emit_small(&SmallEvent {
        pid,
        thread_id: bpf_get_current_pid_tgid() as u32,
        event_type: event_type as u16,
        _pad: 0,
        ret_val: ret,
        arg0: fd,
        arg1: 0,
    });

    Ok(())
}

// ── close ─────────────────────────────────────────────────────────────────────

#[tracepoint]
pub fn sys_enter_close(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_fd(&ctx, roar_ebpf_common::syscall_nr::CLOSE);
    0
}

#[tracepoint]
pub fn sys_exit_close(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_close(&ctx);
    0
}

fn try_sys_exit_close(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let fd = args.arg0;

    let ret = unsafe { read_sys_exit_ret(ctx)? };

    let _ = PENDING_ARGS.remove(&key);

    emit_small(&SmallEvent {
        pid,
        thread_id: bpf_get_current_pid_tgid() as u32,
        event_type: EventType::Close as u16,
        _pad: 0,
        ret_val: ret,
        arg0: fd,
        arg1: 0,
    });

    Ok(())
}

// ── mmap ─────────────────────────────────────────────────────────────────────
// mmap(addr, length, prot, flags, fd, offset)
// PyArrow and other libraries use mmap for reading parquet/columnar files.

#[tracepoint]
pub fn sys_enter_mmap(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_mmap(&ctx);
    0
}

fn try_sys_enter_mmap(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }
    let length = unsafe { read_sys_enter_arg(ctx, 1)? }; // arg1 = length
    let prot = unsafe { read_sys_enter_arg(ctx, 2)? }; // arg2 = prot
    let flags = unsafe { read_sys_enter_arg(ctx, 3)? }; // arg3 = flags
    let fd = unsafe { read_sys_enter_arg(ctx, 4)? }; // arg4 = fd
    let offset = unsafe { read_sys_enter_arg(ctx, 5)? }; // arg5 = offset

    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = roar_ebpf_common::syscall_nr::MMAP;
        (*scratch).arg0 = fd;
        (*scratch).arg1 = offset;
        (*scratch).arg2 = length;
        (*scratch).arg3 = prot;
        (*scratch).arg4 = flags;
    }
    stash_scratch();
    Ok(())
}

#[tracepoint]
pub fn sys_exit_mmap(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_mmap(&ctx);
    0
}

fn try_sys_exit_mmap(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let fd = args.arg0;
    let offset = args.arg1;
    let length = args.arg2;
    let prot = args.arg3;
    let flags = args.arg4;

    let ret = unsafe { read_sys_exit_ret(ctx)? };
    let _ = PENDING_ARGS.remove(&key);

    // MAP_FAILED = -1; also skip anonymous mappings (fd < 0 as signed)
    if ret == -1 || (fd as i64) < 0 {
        return Ok(());
    }

    const PROT_READ: u64 = 1;
    const PROT_WRITE: u64 = 2;
    const MAP_SHARED: u64 = 1;

    let thread_id = bpf_get_current_pid_tgid() as u32;

    if prot & PROT_READ != 0 {
        emit_small(&SmallEvent {
            pid,
            thread_id,
            event_type: EventType::MmapRead as u16,
            _pad: 0,
            ret_val: length as i64,
            arg0: fd,
            arg1: offset,
        });
    }

    if (flags & MAP_SHARED != 0) && (prot & PROT_WRITE != 0) {
        emit_small(&SmallEvent {
            pid,
            thread_id,
            event_type: EventType::MmapWrite as u16,
            _pad: 0,
            ret_val: length as i64,
            arg0: fd,
            arg1: offset,
        });
    }

    Ok(())
}

// ── copy_file_range ──────────────────────────────────────────────────────────
// copy_file_range(fd_in, off_in, fd_out, off_out, len, flags)
// Modern cat uses this instead of read/write.

#[tracepoint]
pub fn sys_enter_copy_file_range(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_copy_file_range(&ctx);
    0
}

fn try_sys_enter_copy_file_range(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }
    let fd_in = unsafe { read_sys_enter_arg(ctx, 0)? };
    let fd_out = unsafe { read_sys_enter_arg(ctx, 2)? }; // arg2, not arg1 (arg1 is off_in*)
    stash_two_fd_entry(roar_ebpf_common::syscall_nr::COPY_FILE_RANGE, fd_in, fd_out)
}

#[tracepoint]
pub fn sys_exit_copy_file_range(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_copy_file_range(&ctx);
    0
}

fn try_sys_exit_copy_file_range(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let fd_in = args.arg0;
    let fd_out = args.arg1;

    let ret = unsafe { read_sys_exit_ret(ctx)? };

    let _ = PENDING_ARGS.remove(&key);

    if ret <= 0 {
        return Ok(());
    }

    // Emit CopyFileRange event: arg0=fd_in, arg1=fd_out, ret_val=bytes
    emit_small(&SmallEvent {
        pid,
        thread_id: bpf_get_current_pid_tgid() as u32,
        event_type: EventType::CopyFileRange as u16,
        _pad: 0,
        ret_val: ret,
        arg0: fd_in,
        arg1: fd_out,
    });

    Ok(())
}

// ── rename / link path publication ──────────────────────────────────────────
// File-publication syscalls make their destination path visible as a produced file.

#[inline(always)]
fn stash_path_publish_entry(syscall_nr: u64, dirfd: u64, path_ptr: u64) -> Result<(), i64> {
    let scratch = get_scratch().ok_or(1i64)?;
    unsafe {
        (*scratch).syscall_nr = syscall_nr;
        (*scratch).arg0 = 0;
        (*scratch).arg1 = dirfd;
        (*scratch).arg2 = 0;
        (*scratch).arg3 = 0;
        (*scratch).arg4 = 0;
        core::ptr::write_bytes((*scratch).path.as_mut_ptr(), 0, MAX_PATH_LEN);
        let _ = aya_ebpf::helpers::bpf_probe_read_user_str_bytes(
            path_ptr as *const u8,
            &mut (*scratch).path,
        );
    }
    stash_scratch();
    Ok(())
}

#[inline(always)]
fn try_sys_enter_path_publish(
    ctx: &TracePointContext,
    syscall_nr: u64,
    dirfd_arg_index: i64,
    path_arg_index: usize,
) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let dirfd = if dirfd_arg_index >= 0 {
        unsafe { read_sys_enter_arg(ctx, dirfd_arg_index as usize)? }
    } else {
        u64::MAX
    };
    let path_ptr = unsafe { read_sys_enter_arg(ctx, path_arg_index)? };
    stash_path_publish_entry(syscall_nr, dirfd, path_ptr)
}

#[inline(always)]
fn try_sys_exit_path_publish(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let dirfd = args.arg1;
    let ret = unsafe { read_sys_exit_ret(ctx)? };

    if ret >= 0 {
        emit_large_direct(pid, EventType::Rename, ret, 0, dirfd, args.path.as_ptr());
    }

    let _ = PENDING_ARGS.remove(&key);
    Ok(())
}

#[tracepoint]
pub fn sys_enter_rename(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_path_publish(&ctx, roar_ebpf_common::syscall_nr::RENAME, -1, 1);
    0
}

#[tracepoint]
pub fn sys_exit_rename(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_path_publish(&ctx);
    0
}

#[tracepoint]
pub fn sys_enter_renameat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_path_publish(&ctx, roar_ebpf_common::syscall_nr::RENAMEAT, 2, 3);
    0
}

#[tracepoint]
pub fn sys_exit_renameat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_path_publish(&ctx);
    0
}

#[tracepoint]
pub fn sys_enter_renameat2(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_path_publish(&ctx, roar_ebpf_common::syscall_nr::RENAMEAT2, 2, 3);
    0
}

#[tracepoint]
pub fn sys_exit_renameat2(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_path_publish(&ctx);
    0
}

#[tracepoint]
pub fn sys_enter_link(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_path_publish(&ctx, roar_ebpf_common::syscall_nr::LINK, -1, 1);
    0
}

#[tracepoint]
pub fn sys_exit_link(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_path_publish(&ctx);
    0
}

#[tracepoint]
pub fn sys_enter_linkat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_path_publish(&ctx, roar_ebpf_common::syscall_nr::LINKAT, 2, 3);
    0
}

#[tracepoint]
pub fn sys_exit_linkat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_path_publish(&ctx);
    0
}

// ── dup2 / dup3 ──────────────────────────────────────────────────────────────
// Shell uses dup2 for I/O redirections.

#[tracepoint]
pub fn sys_enter_dup2(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_dup2(&ctx);
    0
}

fn try_sys_enter_dup2(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }
    let oldfd = unsafe { read_sys_enter_arg(ctx, 0)? };
    let newfd = unsafe { read_sys_enter_arg(ctx, 1)? };
    stash_two_fd_entry(roar_ebpf_common::syscall_nr::DUP2, oldfd, newfd)
}

#[tracepoint]
pub fn sys_exit_dup2(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_dup(&ctx);
    0
}

#[tracepoint]
pub fn sys_enter_dup3(ctx: TracePointContext) -> u32 {
    let _ = try_sys_enter_dup2(&ctx); // same arg layout
    0
}

#[tracepoint]
pub fn sys_exit_dup3(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_dup(&ctx);
    0
}

fn try_sys_exit_dup(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let key = current_pid_tgid();
    let args = unsafe { PENDING_ARGS.get(&key).ok_or(1i64)? };
    let oldfd = args.arg0;

    let ret = unsafe { read_sys_exit_ret(ctx)? };

    let _ = PENDING_ARGS.remove(&key);

    if ret < 0 {
        return Ok(());
    }

    // Emit Dup event: arg0=oldfd, ret_val=newfd
    emit_small(&SmallEvent {
        pid,
        thread_id: bpf_get_current_pid_tgid() as u32,
        event_type: EventType::Dup as u16,
        _pad: 0,
        ret_val: ret,
        arg0: oldfd,
        arg1: 0,
    });

    Ok(())
}

// ── clone / fork / vfork ──────────────────────────────────────────────────────
// Critical: add child PIDs to tracked_pids IN-KERNEL to avoid a race.

#[tracepoint]
pub fn sys_exit_clone(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_clone(&ctx);
    0
}

fn try_sys_exit_clone(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    // Read the parent's run_id (also serves as the tracked check)
    let parent_run_id = match unsafe { TRACKED_PIDS.get(&pid) } {
        Some(val) => *val,
        None => return Ok(()),
    };

    let ret = unsafe { read_sys_exit_ret(ctx)? };
    if ret <= 0 {
        return Ok(()); // failed or we're in the child (ret==0)
    }

    let child_pid = ret as u32;

    // Add child to tracked_pids IN-KERNEL with parent's run_id
    let _ = TRACKED_PIDS.insert(&child_pid, &parent_run_id, 0);

    emit_large_direct(
        pid,
        EventType::Clone,
        ret,
        child_pid as u64,
        0,
        core::ptr::null(), // no path
    );

    Ok(())
}

#[tracepoint]
pub fn sys_exit_fork(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_clone(&ctx);
    0
}

#[tracepoint]
pub fn sys_exit_vfork(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_clone(&ctx);
    0
}

#[tracepoint]
pub fn sys_exit_clone3(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_clone(&ctx);
    0
}

// ── execve / execveat ─────────────────────────────────────────────────────────

#[tracepoint]
pub fn sys_exit_execve(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_exec(&ctx);
    0
}

#[tracepoint]
pub fn sys_exit_execveat(ctx: TracePointContext) -> u32 {
    let _ = try_sys_exit_exec(&ctx);
    0
}

fn try_sys_exit_exec(ctx: &TracePointContext) -> Result<(), i64> {
    let pid = current_pid();
    if !pid_tracked(pid) {
        return Ok(());
    }

    let ret = unsafe { read_sys_exit_ret(ctx)? };
    if ret != 0 {
        return Ok(()); // exec failed
    }

    emit_large_direct(
        pid,
        EventType::Exec,
        0,
        0,
        0,
        core::ptr::null(), // no path
    );

    Ok(())
}

// ── Panic handler ─────────────────────────────────────────────────────────────

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    unsafe { core::hint::unreachable_unchecked() }
}
