use nix::sys::ptrace;
use nix::sys::wait::{waitpid, WaitPidFlag, WaitStatus};
use nix::unistd::{fork, ForkResult, Pid};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs::File;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

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
const SYS_RENAME: u64 = 82; // rename(oldpath, newpath)
const SYS_OPENAT: u64 = 257;
const SYS_RENAMEAT: u64 = 264; // renameat(olddirfd, oldpath, newdirfd, newpath)
const SYS_PREADV: u64 = 295; // positional scatter read
const SYS_PWRITEV: u64 = 296; // positional gather write
const SYS_RENAMEAT2: u64 = 316; // renameat2 with flags
const SYS_COPY_FILE_RANGE: u64 = 326; // efficient file copy
const SYS_PREADV2: u64 = 327; // preadv with flags
const SYS_PWRITEV2: u64 = 328; // pwritev with flags

// =============================================================================
// Data Structures - designed to match what roar's Python expects
// =============================================================================

#[derive(Debug, Clone, Serialize)]
struct ProcessInfo {
    pid: i32,
    parent_pid: Option<i32>,
    command: Vec<String>,
    env: HashMap<String, String>,
}

#[derive(Debug, Clone, Serialize)]
struct FileAccess {
    path: String,
    read: bool,
    written: bool,
}

#[derive(Debug, Serialize)]
struct TracerOutput {
    processes: Vec<ProcessInfo>,
    opened_files: Vec<String>,
    read_files: Vec<String>,
    written_files: Vec<String>,
    env_accessed: HashMap<String, String>,
    start_time: f64,
    end_time: f64,
}

#[derive(Debug)]
struct TracerState {
    processes: HashMap<i32, ProcessInfo>,
    fd_table: HashMap<(i32, i32), String>, // (pid, fd) -> path
    in_syscall: HashMap<i32, bool>,
    pending_opens: HashMap<i32, (String, u64)>, // pid -> (path, flags)
    active_pids: HashSet<i32>,

    // Track file access
    opened_files: HashSet<String>,
    read_files: HashSet<String>,
    written_files: HashSet<String>,

    // Track env vars accessed via /proc/*/environ reads
    env_accessed: HashMap<String, String>,
}

impl TracerState {
    fn new() -> Self {
        TracerState {
            processes: HashMap::new(),
            fd_table: HashMap::new(),
            in_syscall: HashMap::new(),
            pending_opens: HashMap::new(),
            active_pids: HashSet::new(),
            opened_files: HashSet::new(),
            read_files: HashSet::new(),
            written_files: HashSet::new(),
            env_accessed: HashMap::new(),
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

fn capture_process_info(pid: Pid, state: &mut TracerState, parent_pid: Option<i32>) {
    let pid_raw = pid.as_raw();

    // Read command line
    let cmdline_path = format!("/proc/{}/cmdline", pid_raw);
    let command = std::fs::read_to_string(&cmdline_path)
        .map(|s| {
            s.split('\0')
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect()
        })
        .unwrap_or_default();

    // Read environment
    let environ_path = format!("/proc/{}/environ", pid_raw);
    let env: HashMap<String, String> = std::fs::read_to_string(&environ_path)
        .map(|s| {
            s.split('\0')
                .filter_map(|entry| {
                    let mut parts = entry.splitn(2, '=');
                    match (parts.next(), parts.next()) {
                        (Some(k), Some(v)) if !k.is_empty() => Some((k.to_string(), v.to_string())),
                        _ => None,
                    }
                })
                .collect()
        })
        .unwrap_or_default();

    state.processes.insert(
        pid_raw,
        ProcessInfo {
            pid: pid_raw,
            parent_pid,
            command,
            env,
        },
    );
}

// =============================================================================
// FD table management
// =============================================================================

fn clone_fd_table(parent_pid: i32, child_pid: i32, state: &mut TracerState) {
    let entries: Vec<_> = state
        .fd_table
        .iter()
        .filter(|((pid, _), _)| *pid == parent_pid)
        .map(|((_, fd), path)| (*fd, path.clone()))
        .collect();

    for (fd, path) in entries {
        state.fd_table.insert((child_pid, fd), path);
    }
}

// =============================================================================
// Syscall handling
// =============================================================================

fn handle_syscall(pid: Pid, state: &mut TracerState) {
    let pid_raw = pid.as_raw();

    let regs = match ptrace::getregs(pid) {
        Ok(r) => r,
        Err(_) => return,
    };

    let syscall_num = regs.orig_rax;
    let is_entry = !state.in_syscall.get(&pid_raw).copied().unwrap_or(false);
    state.in_syscall.insert(pid_raw, is_entry);

    if is_entry {
        handle_syscall_entry(pid, syscall_num, &regs, state);
    } else {
        handle_syscall_exit(pid, syscall_num, &regs, state);
    }
}

fn handle_syscall_entry(
    pid: Pid,
    syscall_num: u64,
    regs: &libc::user_regs_struct,
    state: &mut TracerState,
) {
    let pid_raw = pid.as_raw();

    match syscall_num {
        SYS_OPEN => {
            let path_ptr = regs.rdi;
            let flags = regs.rsi;
            if let Some(path) = read_string_from_tracee(pid, path_ptr) {
                let abs_path = resolve_path(&path, pid_raw);
                state.pending_opens.insert(pid_raw, (abs_path, flags));
            }
        }
        SYS_OPENAT => {
            let path_ptr = regs.rsi;
            let flags = regs.rdx;
            if let Some(path) = read_string_from_tracee(pid, path_ptr) {
                let abs_path = resolve_path(&path, pid_raw);
                state.pending_opens.insert(pid_raw, (abs_path, flags));
            }
        }
        SYS_READ | SYS_PREAD64 | SYS_READV | SYS_PREADV | SYS_PREADV2 => {
            // All read variants have fd in rdi
            let fd = regs.rdi as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, fd)).cloned() {
                state.read_files.insert(path);
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2 => {
            // All write variants have fd in rdi
            let fd = regs.rdi as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, fd)).cloned() {
                state.written_files.insert(path);
            }
        }
        SYS_SENDFILE => {
            // sendfile(out_fd, in_fd, ...) - reads from in_fd (rsi), writes to out_fd (rdi)
            let out_fd = regs.rdi as i32;
            let in_fd = regs.rsi as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, in_fd)).cloned() {
                state.read_files.insert(path);
            }
            if let Some(path) = state.fd_table.get(&(pid_raw, out_fd)).cloned() {
                state.written_files.insert(path);
            }
        }
        SYS_COPY_FILE_RANGE => {
            // copy_file_range(fd_in, ..., fd_out, ...) - reads from fd_in (rdi), writes to fd_out (r8)
            let in_fd = regs.rdi as i32;
            let out_fd = regs.r8 as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, in_fd)).cloned() {
                state.read_files.insert(path);
            }
            if let Some(path) = state.fd_table.get(&(pid_raw, out_fd)).cloned() {
                state.written_files.insert(path);
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
                if let Some(path) = state.fd_table.get(&(pid_raw, fd_i32)).cloned() {
                    // PROT_READ = 1, PROT_WRITE = 2
                    // MAP_SHARED = 1, MAP_PRIVATE = 2
                    let is_shared = flags & 1 != 0;

                    // Any file-backed mmap is a read
                    if prot & 1 != 0 {
                        state.read_files.insert(path.clone());
                    }
                    // Only MAP_SHARED + PROT_WRITE is a real write (changes go to disk)
                    // MAP_PRIVATE writes are copy-on-write and don't modify the file
                    if is_shared && (prot & 2 != 0) {
                        state.written_files.insert(path);
                    }
                }
            }
        }
        SYS_RENAME => {
            // rename(oldpath, newpath): rdi=oldpath, rsi=newpath
            // The destination (newpath) is effectively written
            if let Some(newpath) = read_string_from_tracee(pid, regs.rsi) {
                let abs_path = resolve_path(&newpath, pid_raw);
                state.written_files.insert(abs_path);
            }
        }
        SYS_RENAMEAT | SYS_RENAMEAT2 => {
            // renameat(olddirfd, oldpath, newdirfd, newpath): rsi=oldpath, r10=newpath
            // The destination (newpath) is effectively written
            if let Some(newpath) = read_string_from_tracee(pid, regs.r10) {
                let abs_path = resolve_path(&newpath, pid_raw);
                state.written_files.insert(abs_path);
            }
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
    let ret_val = regs.rax as i64;

    match syscall_num {
        SYS_OPEN | SYS_OPENAT => {
            if ret_val >= 0 {
                if let Some((path, _flags)) = state.pending_opens.remove(&pid_raw) {
                    let fd = ret_val as i32;
                    state.fd_table.insert((pid_raw, fd), path.clone());
                    state.opened_files.insert(path);
                }
            } else {
                state.pending_opens.remove(&pid_raw);
            }
        }
        SYS_CLOSE => {
            if ret_val == 0 {
                // We don't have the fd from entry, so we can't clean up properly
                // This is a known limitation
            }
        }
        _ => {}
    }
}

fn resolve_path(path: &str, pid: i32) -> String {
    if path.starts_with('/') {
        return path.to_string();
    }

    // Try to resolve relative to process CWD
    let cwd_path = format!("/proc/{}/cwd", pid);
    if let Ok(cwd) = std::fs::read_link(&cwd_path) {
        let mut full_path = cwd;
        full_path.push(path);
        if let Ok(canonical) = full_path.canonicalize() {
            return canonical.to_string_lossy().to_string();
        }
        return full_path.to_string_lossy().to_string();
    }

    path.to_string()
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
        | Options::PTRACE_O_TRACEEXEC;

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
                capture_process_info(Pid::from_raw(child_pid_i32), state, Some(pid.as_raw()));
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
    let start_time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time before UNIX epoch")
        .as_secs_f64();

    let mut state = TracerState::new();

    // Fork and trace
    match unsafe { fork() } {
        Ok(ForkResult::Child) => {
            // Child: request tracing and exec
            ptrace::traceme().expect("ptrace traceme failed");

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
                    let _ = ptrace::syscall(child, None);
                }
                _ => {
                    eprintln!("Unexpected initial wait status");
                    return 1;
                }
            }

            // Main event loop
            let exit_code = trace_loop(&mut state);

            let end_time = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system time before UNIX epoch")
                .as_secs_f64();

            // Collect env vars from the root process
            let env_accessed = state
                .processes
                .values()
                .next()
                .map(|p| p.env.clone())
                .unwrap_or_default();

            // Build output
            let output = TracerOutput {
                processes: state.processes.into_values().collect(),
                opened_files: state.opened_files.into_iter().collect(),
                read_files: state.read_files.into_iter().collect(),
                written_files: state.written_files.into_iter().collect(),
                env_accessed,
                start_time,
                end_time,
            };

            // Write output
            if let Ok(mut file) = File::create(output_file) {
                if let Ok(json) = serde_json::to_string_pretty(&output) {
                    let _ = file.write_all(json.as_bytes());
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
            Ok(WaitStatus::PtraceSyscall(pid)) => {
                handle_syscall(pid, state);
                let _ = ptrace::syscall(pid, None);
            }
            Ok(WaitStatus::PtraceEvent(pid, _sig, event)) => {
                handle_ptrace_event(pid, event, state);
                let _ = ptrace::syscall(pid, None);
            }
            Ok(WaitStatus::Exited(pid, code)) => {
                state.active_pids.remove(&pid.as_raw());
                // Capture exit code of the root process
                if state
                    .processes
                    .get(&pid.as_raw())
                    .map(|p| p.parent_pid.is_none())
                    .unwrap_or(false)
                {
                    exit_code = code;
                }
            }
            Ok(WaitStatus::Signaled(pid, sig, _)) => {
                state.active_pids.remove(&pid.as_raw());
                // If root process was signaled, reflect that
                if state
                    .processes
                    .get(&pid.as_raw())
                    .map(|p| p.parent_pid.is_none())
                    .unwrap_or(false)
                {
                    exit_code = 128 + sig as i32;
                }
            }
            Ok(WaitStatus::Stopped(pid, sig)) => {
                // Pass through signals
                let _ = ptrace::syscall(pid, Some(sig));
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
