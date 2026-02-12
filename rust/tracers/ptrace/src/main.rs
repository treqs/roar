mod seccomp;

use nix::sys::ptrace;
use nix::sys::wait::{waitpid, WaitPidFlag, WaitStatus};
use nix::unistd::{fork, ForkResult, Pid};
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs::File;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::process::Command;
use tracer_fd::FdTracker;
use tracer_runtime::{
    build_tracer_report, capture_process_info as capture_proc_info, resolve_path_with_cache,
    timestamp_now,
};
use tracer_schema::ProcessInfo;

// Syscall numbers for x86_64
const SYS_READ: u64 = 0;
const SYS_WRITE: u64 = 1;
const SYS_OPEN: u64 = 2;
const SYS_CLOSE: u64 = 3;
const SYS_MMAP: u64 = 9;
const SYS_PREAD64: u64 = 17; // positional read (used by pyarrow, etc.)
const SYS_PWRITE64: u64 = 18; // positional write
const SYS_READV: u64 = 19; // scatter read
const SYS_WRITEV: u64 = 20; // gather write
const SYS_SENDFILE: u64 = 40; // zero-copy file-to-file/socket
const SYS_CHDIR: u64 = 80;
const SYS_FCHDIR: u64 = 81;
const SYS_RENAME: u64 = 82; // rename(oldpath, newpath)
const SYS_OPENAT: u64 = 257;
const SYS_RENAMEAT: u64 = 264; // renameat(olddirfd, oldpath, newdirfd, newpath)
const SYS_PREADV: u64 = 295; // positional scatter read
const SYS_PWRITEV: u64 = 296; // positional gather write
const SYS_RENAMEAT2: u64 = 316; // renameat2 with flags
const SYS_COPY_FILE_RANGE: u64 = 326; // efficient file copy
const SYS_PREADV2: u64 = 327; // preadv with flags
const SYS_PWRITEV2: u64 = 328; // pwritev with flags

const AUDIT_ARCH_X86_64: u32 = 0xC000_003E;
const TRACKED_SYSCALLS: &[u64] = &[
    SYS_READ,
    SYS_WRITE,
    SYS_OPEN,
    SYS_CLOSE,
    SYS_MMAP,
    SYS_PREAD64,
    SYS_PWRITE64,
    SYS_READV,
    SYS_WRITEV,
    SYS_SENDFILE,
    SYS_CHDIR,
    SYS_FCHDIR,
    SYS_RENAME,
    SYS_OPENAT,
    SYS_RENAMEAT,
    SYS_PREADV,
    SYS_PWRITEV,
    SYS_RENAMEAT2,
    SYS_COPY_FILE_RANGE,
    SYS_PREADV2,
    SYS_PWRITEV2,
];

#[derive(Debug)]
struct TracerState {
    processes: HashMap<i32, ProcessInfo>,
    fd_tracker: FdTracker,
    awaiting_exit: HashSet<i32>, // PIDs waiting for syscall exit stop
    pending_opens: HashMap<i32, (String, u64)>, // pid -> (path, flags)
    pending_writes: HashMap<i32, String>, // pid -> path (write syscalls pending confirmation)
    pending_closes: HashMap<i32, i32>, // pid -> fd (close syscalls pending confirmation)
    pending_chdirs: HashMap<i32, ()>, // pid -> () (chdir pending confirmation)
    pending_fchdirs: HashMap<i32, ()>, // pid -> () (fchdir pending confirmation)
    active_pids: HashSet<i32>,

    // CWD cache per PID
    cwd_cache: HashMap<u32, String>,
}

impl TracerState {
    fn new() -> Self {
        TracerState {
            processes: HashMap::new(),
            fd_tracker: FdTracker::new(None),
            awaiting_exit: HashSet::new(),
            pending_opens: HashMap::new(),
            pending_writes: HashMap::new(),
            pending_closes: HashMap::new(),
            pending_chdirs: HashMap::new(),
            pending_fchdirs: HashMap::new(),
            active_pids: HashSet::new(),
            cwd_cache: HashMap::new(),
        }
    }
}

// =============================================================================
// String reading from tracee memory
// =============================================================================

fn read_string_from_tracee(pid: Pid, addr: u64) -> Option<String> {
    if addr == 0 {
        return None;
    }

    let mut bytes = Vec::new();
    let mut current = addr;

    loop {
        let word = match ptrace::read(pid, current as *mut libc::c_void) {
            Ok(w) => w,
            Err(_) => return None,
        };

        for byte in word.to_ne_bytes() {
            if byte == 0 {
                return String::from_utf8(bytes).ok();
            }
            bytes.push(byte);
            if bytes.len() > 4096 {
                return None; // Safety limit
            }
        }
        current += 8;
    }
}

// =============================================================================
// Process info capture
// =============================================================================

fn capture_process_info(pid: Pid, state: &mut TracerState, parent_pid: Option<u32>) {
    let pid_raw = pid.as_raw();
    if pid_raw <= 0 {
        return;
    }
    if let Some(info) = capture_proc_info(pid_raw as u32, parent_pid) {
        state.processes.insert(pid_raw, info);
    }
}

// =============================================================================
// FD table management
// =============================================================================

fn clone_fd_table(parent_pid: i32, child_pid: i32, state: &mut TracerState) {
    if let (Ok(parent_u32), Ok(child_u32)) = (u32::try_from(parent_pid), u32::try_from(child_pid)) {
        state.fd_tracker.handle_clone(parent_u32, child_u32);
    }

    // Clone parent's CWD cache entry to child
    if let (Ok(parent_u32), Ok(child_u32)) = (u32::try_from(parent_pid), u32::try_from(child_pid)) {
        if let Some(cwd) = state.cwd_cache.get(&parent_u32).cloned() {
            state.cwd_cache.insert(child_u32, cwd);
        }
    }
}

// =============================================================================
// Syscall handling
// =============================================================================

/// Determine if a syscall needs an exit stop to check the return value.
fn needs_exit_stop(syscall_num: u64) -> bool {
    matches!(
        syscall_num,
        SYS_OPEN
            | SYS_OPENAT
            | SYS_CLOSE
            | SYS_WRITE
            | SYS_PWRITE64
            | SYS_WRITEV
            | SYS_PWRITEV
            | SYS_PWRITEV2
            | SYS_SENDFILE
            | SYS_COPY_FILE_RANGE
            | SYS_CHDIR
            | SYS_FCHDIR
    )
}

fn handle_syscall_entry(
    pid: Pid,
    syscall_num: u64,
    regs: &libc::user_regs_struct,
    state: &mut TracerState,
) {
    let pid_raw = pid.as_raw();
    let pid_u32 = u32::try_from(pid_raw).ok();

    match syscall_num {
        SYS_OPEN => {
            let path_ptr = regs.rdi;
            let flags = regs.rsi;
            if let Some(path) = read_string_from_tracee(pid, path_ptr) {
                let abs_path = resolve_path(&path, pid_raw, &mut state.cwd_cache);
                state.pending_opens.insert(pid_raw, (abs_path, flags));
            }
        }
        SYS_OPENAT => {
            let path_ptr = regs.rsi;
            let flags = regs.rdx;
            if let Some(path) = read_string_from_tracee(pid, path_ptr) {
                let abs_path = resolve_path(&path, pid_raw, &mut state.cwd_cache);
                state.pending_opens.insert(pid_raw, (abs_path, flags));
            }
        }
        SYS_CLOSE => {
            // Capture the fd argument on entry so we can clean up fd_table on exit
            let fd = regs.rdi as i32;
            state.pending_closes.insert(pid_raw, fd);
        }
        SYS_READ | SYS_PREAD64 | SYS_READV | SYS_PREADV | SYS_PREADV2 => {
            // All read variants have fd in rdi
            let fd = regs.rdi as i32;
            if let Some(pid_u32) = pid_u32 {
                state.fd_tracker.mark_read(pid_u32, fd);
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2 => {
            // All write variants have fd in rdi
            // Track as pending - only confirm at exit if bytes > 0 were written
            let fd = regs.rdi as i32;
            if let Some(pid_u32) = pid_u32 {
                if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, fd).cloned() {
                    state.pending_writes.insert(pid_raw, path);
                }
            }
        }
        SYS_SENDFILE => {
            // sendfile(out_fd, in_fd, ...) - reads from in_fd (rsi), writes to out_fd (rdi)
            let out_fd = regs.rdi as i32;
            let in_fd = regs.rsi as i32;
            if let Some(pid_u32) = pid_u32 {
                state.fd_tracker.mark_read(pid_u32, in_fd);
                // Track write as pending - confirm at exit if bytes > 0
                if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, out_fd).cloned() {
                    state.pending_writes.insert(pid_raw, path);
                }
            }
        }
        SYS_COPY_FILE_RANGE => {
            // copy_file_range(fd_in, ..., fd_out, ...) - reads from fd_in (rdi), writes to fd_out (r8)
            let in_fd = regs.rdi as i32;
            let out_fd = regs.r8 as i32;
            if let Some(pid_u32) = pid_u32 {
                state.fd_tracker.mark_read(pid_u32, in_fd);
                // Track write as pending - confirm at exit if bytes > 0
                if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, out_fd).cloned() {
                    state.pending_writes.insert(pid_raw, path);
                }
            }
        }
        SYS_MMAP => {
            // mmap(addr, len, prot, flags, fd, offset)
            // Args: rdi=addr, rsi=len, rdx=prot, r10=flags, r8=fd, r9=offset
            let fd = regs.r8 as i64;
            let prot = regs.rdx;
            let flags = regs.r10;

            // Only track if mapping a file (fd >= 0)
            if fd >= 0 {
                let fd_i32 = fd as i32;
                if let Some(pid_u32) = pid_u32 {
                    if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, fd_i32).cloned() {
                        // PROT_READ = 1, PROT_WRITE = 2
                        // MAP_SHARED = 1, MAP_PRIVATE = 2
                        let is_shared = flags & 1 != 0;

                        // Any file-backed mmap is a read
                        if prot & 1 != 0 {
                            state.fd_tracker.mark_path_read(path.clone());
                        }
                        // Only MAP_SHARED + PROT_WRITE is a real write (changes go to disk)
                        // MAP_PRIVATE writes are copy-on-write and don't modify the file
                        if is_shared && (prot & 2 != 0) {
                            state.fd_tracker.mark_path_written(path);
                        }
                    }
                }
            }
        }
        SYS_RENAME => {
            // rename(oldpath, newpath): rdi=oldpath, rsi=newpath
            // The destination (newpath) is effectively written
            if let Some(newpath) = read_string_from_tracee(pid, regs.rsi) {
                let abs_path = resolve_path(&newpath, pid_raw, &mut state.cwd_cache);
                state.fd_tracker.mark_path_written(abs_path);
            }
        }
        SYS_RENAMEAT | SYS_RENAMEAT2 => {
            // renameat(olddirfd, oldpath, newdirfd, newpath): rsi=oldpath, r10=newpath
            // The destination (newpath) is effectively written
            if let Some(newpath) = read_string_from_tracee(pid, regs.r10) {
                let abs_path = resolve_path(&newpath, pid_raw, &mut state.cwd_cache);
                state.fd_tracker.mark_path_written(abs_path);
            }
        }
        SYS_CHDIR => {
            state.pending_chdirs.insert(pid_raw, ());
        }
        SYS_FCHDIR => {
            state.pending_fchdirs.insert(pid_raw, ());
        }
        _ => {}
    }
}

fn handle_syscall_exit(
    pid: Pid,
    syscall_num: u64,
    regs: &libc::user_regs_struct,
    state: &mut TracerState,
) {
    let pid_raw = pid.as_raw();
    let pid_u32 = u32::try_from(pid_raw).ok();
    let ret_val = regs.rax as i64;

    match syscall_num {
        SYS_OPEN | SYS_OPENAT => {
            if ret_val >= 0 {
                if let (Some(pid_u32), Some((path, flags))) =
                    (pid_u32, state.pending_opens.remove(&pid_raw))
                {
                    let fd = ret_val as i32;
                    state.fd_tracker.handle_open(pid_u32, fd, path, flags);
                }
            } else {
                state.pending_opens.remove(&pid_raw);
            }
        }
        SYS_CLOSE => {
            if let Some(fd) = state.pending_closes.remove(&pid_raw) {
                if ret_val == 0 {
                    if let Some(pid_u32) = pid_u32 {
                        state.fd_tracker.handle_close(pid_u32, fd);
                    }
                }
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2 | SYS_SENDFILE
        | SYS_COPY_FILE_RANGE => {
            // Only count as written if bytes were actually written (ret_val > 0)
            if let Some(path) = state.pending_writes.remove(&pid_raw) {
                if ret_val > 0 {
                    state.fd_tracker.mark_path_written(path);
                }
            }
        }
        SYS_CHDIR => {
            if state.pending_chdirs.remove(&pid_raw).is_some() && ret_val == 0 {
                // Invalidate CWD cache on successful chdir
                if let Ok(pid_u32) = u32::try_from(pid_raw) {
                    state.cwd_cache.remove(&pid_u32);
                }
            }
        }
        SYS_FCHDIR => {
            if state.pending_fchdirs.remove(&pid_raw).is_some() && ret_val == 0 {
                // Invalidate CWD cache on successful fchdir
                if let Ok(pid_u32) = u32::try_from(pid_raw) {
                    state.cwd_cache.remove(&pid_u32);
                }
            }
        }
        _ => {}
    }
}

fn resolve_path(path: &str, pid: i32, cwd_cache: &mut HashMap<u32, String>) -> String {
    if pid <= 0 {
        return path.to_string();
    }
    resolve_path_with_cache(path, pid as u32, cwd_cache)
}

// =============================================================================
// Ptrace event handling (fork/clone/exec)
// =============================================================================

fn setup_ptrace(pid: Pid) {
    use nix::sys::ptrace::Options;
    let opts = Options::PTRACE_O_TRACESYSGOOD
        | Options::PTRACE_O_TRACEFORK
        | Options::PTRACE_O_TRACEVFORK
        | Options::PTRACE_O_TRACECLONE
        | Options::PTRACE_O_TRACEEXEC
        | Options::PTRACE_O_TRACESECCOMP;

    if let Err(e) = ptrace::setoptions(pid, opts) {
        eprintln!("Warning: ptrace setoptions failed: {}", e);
    }
}

fn handle_ptrace_event(pid: Pid, event: i32, state: &mut TracerState) {
    match event {
        libc::PTRACE_EVENT_FORK | libc::PTRACE_EVENT_VFORK | libc::PTRACE_EVENT_CLONE => {
            if let Ok(child_pid) = ptrace::getevent(pid) {
                let child_pid_i32 = child_pid as i32;
                state.active_pids.insert(child_pid_i32);
                clone_fd_table(pid.as_raw(), child_pid_i32, state);
                capture_process_info(
                    Pid::from_raw(child_pid_i32),
                    state,
                    u32::try_from(pid.as_raw()).ok(),
                );
            }
        }
        libc::PTRACE_EVENT_EXEC => {
            // Process exec'd - recapture info
            let parent = state
                .processes
                .get(&pid.as_raw())
                .and_then(|p| p.parent_pid);
            capture_process_info(pid, state, parent);
        }
        _ => {}
    }
}

// =============================================================================
// Main tracer loop
// =============================================================================

fn run_tracer(command: Vec<String>, output_file: &str) -> i32 {
    let start_time = timestamp_now();

    let mut state = TracerState::new();

    // Fork and trace
    match unsafe { fork() } {
        Ok(ForkResult::Child) => {
            // Child: request tracing and exec
            ptrace::traceme().expect("ptrace traceme failed");

            // Install seccomp-BPF filter before exec
            // The filter survives exec and is inherited by fork/clone children
            seccomp::install_seccomp_filter(TRACKED_SYSCALLS, AUDIT_ARCH_X86_64);

            let mut cmd = Command::new(&command[0]);
            if command.len() > 1 {
                cmd.args(&command[1..]);
            }

            // This replaces the child process
            let err = cmd.exec();
            eprintln!("exec failed: {}", err);
            std::process::exit(1);
        }
        Ok(ForkResult::Parent { child }) => {
            // Parent: wait for child to stop at exec, then trace
            let child_pid = child.as_raw();
            state.active_pids.insert(child_pid);

            // Wait for initial stop
            match waitpid(child, None) {
                Ok(WaitStatus::Stopped(_, _)) => {
                    setup_ptrace(child);
                    capture_process_info(child, &mut state, None);
                    // Use cont instead of syscall — seccomp events will stop us
                    let _ = ptrace::cont(child, None);
                }
                _ => {
                    eprintln!("Unexpected initial wait status");
                    return 1;
                }
            }

            // Main event loop
            let exit_code = trace_loop(&mut state);

            let end_time = timestamp_now();

            // Collect env vars from the root process
            let env_accessed = state
                .processes
                .values()
                .next()
                .map(|p| p.env.clone())
                .unwrap_or_default();

            // Build output
            let summary = state.fd_tracker.build_summary();

            let output = build_tracer_report(
                "ptrace",
                None,
                state.processes.into_values().collect(),
                summary.files,
                summary.opened_files,
                summary.read_files,
                summary.written_files,
                env_accessed,
                start_time,
                end_time,
                None,
            );

            // Write output (MessagePack)
            if let Ok(mut file) = File::create(output_file) {
                if let Ok(msgpack) = rmp_serde::to_vec_named(&output) {
                    let _ = file.write_all(&msgpack);
                }
            }

            exit_code
        }
        Err(e) => {
            eprintln!("fork failed: {}", e);
            1
        }
    }
}

fn trace_loop(state: &mut TracerState) -> i32 {
    let mut exit_code = 0;

    while !state.active_pids.is_empty() {
        match waitpid(None, Some(WaitPidFlag::__WALL)) {
            Ok(WaitStatus::PtraceEvent(pid, _sig, event)) => {
                if event == libc::PTRACE_EVENT_SECCOMP {
                    // Seccomp event = syscall entry (only for tracked syscalls)
                    let regs = match ptrace::getregs(pid) {
                        Ok(r) => r,
                        Err(_) => {
                            let _ = ptrace::cont(pid, None);
                            continue;
                        }
                    };
                    let syscall_num = regs.orig_rax;
                    handle_syscall_entry(pid, syscall_num, &regs, state);

                    if needs_exit_stop(syscall_num) {
                        // Need to see the exit to check return value
                        state.awaiting_exit.insert(pid.as_raw());
                        let _ = ptrace::syscall(pid, None);
                    } else {
                        // Entry-only syscall — resume without stopping at exit
                        let _ = ptrace::cont(pid, None);
                    }
                } else {
                    // Fork/clone/exec events
                    handle_ptrace_event(pid, event, state);
                    if state.awaiting_exit.contains(&pid.as_raw()) {
                        let _ = ptrace::syscall(pid, None);
                    } else {
                        let _ = ptrace::cont(pid, None);
                    }
                }
            }
            Ok(WaitStatus::PtraceSyscall(pid)) => {
                // Syscall exit stop (only happens for PIDs we resumed with ptrace::syscall)
                let pid_raw = pid.as_raw();
                if state.awaiting_exit.remove(&pid_raw) {
                    let regs = match ptrace::getregs(pid) {
                        Ok(r) => r,
                        Err(_) => {
                            let _ = ptrace::cont(pid, None);
                            continue;
                        }
                    };
                    let syscall_num = regs.orig_rax;
                    handle_syscall_exit(pid, syscall_num, &regs, state);
                }
                // Resume with cont — next stop will be a seccomp event
                let _ = ptrace::cont(pid, None);
            }
            Ok(WaitStatus::Exited(pid, code)) => {
                let pid_raw = pid.as_raw();
                state.active_pids.remove(&pid_raw);
                state.awaiting_exit.remove(&pid_raw);
                if let Ok(pid_u32) = u32::try_from(pid_raw) {
                    state.cwd_cache.remove(&pid_u32);
                }
                state.pending_chdirs.remove(&pid_raw);
                state.pending_fchdirs.remove(&pid_raw);
                // Capture exit code of the root process
                if state
                    .processes
                    .get(&pid_raw)
                    .map(|p| p.parent_pid.is_none())
                    .unwrap_or(false)
                {
                    exit_code = code;
                }
            }
            Ok(WaitStatus::Signaled(pid, sig, _)) => {
                let pid_raw = pid.as_raw();
                state.active_pids.remove(&pid_raw);
                state.awaiting_exit.remove(&pid_raw);
                if let Ok(pid_u32) = u32::try_from(pid_raw) {
                    state.cwd_cache.remove(&pid_u32);
                }
                state.pending_chdirs.remove(&pid_raw);
                state.pending_fchdirs.remove(&pid_raw);
                // If root process was signaled, reflect that
                if state
                    .processes
                    .get(&pid_raw)
                    .map(|p| p.parent_pid.is_none())
                    .unwrap_or(false)
                {
                    exit_code = 128 + sig as i32;
                }
            }
            Ok(WaitStatus::Stopped(pid, sig)) => {
                // Signal delivery — pass through the signal
                if state.awaiting_exit.contains(&pid.as_raw()) {
                    let _ = ptrace::syscall(pid, Some(sig));
                } else {
                    let _ = ptrace::cont(pid, Some(sig));
                }
            }
            Ok(_) => {}
            Err(nix::errno::Errno::ECHILD) => break,
            Err(_) => {}
        }
    }

    exit_code
}

// =============================================================================
// Main
// =============================================================================

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() < 3 {
        eprintln!("Usage: roar-tracer <output-file> <command> [args...]");
        eprintln!("  Traces <command> and writes syscall data to <output-file>");
        std::process::exit(1);
    }

    let output_file = &args[1];
    let command: Vec<String> = args[2..].to_vec();

    let exit_code = run_tracer(command, output_file);
    std::process::exit(exit_code);
}
