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
    awaiting_exit: HashSet<i32>,           // PIDs waiting for syscall exit stop
    pending_opens: HashMap<i32, (String, u64)>, // pid -> (path, flags)
    pending_writes: HashMap<i32, String>,       // pid -> path (write syscalls pending confirmation)
    pending_closes: HashMap<i32, i32>,          // pid -> fd (close syscalls pending confirmation)
    pending_chdirs: HashMap<i32, ()>,           // pid -> () (chdir pending confirmation)
    pending_fchdirs: HashMap<i32, ()>,          // pid -> () (fchdir pending confirmation)
    active_pids: HashSet<i32>,

    // Track file access
    opened_files: HashSet<String>,
    read_files: HashSet<String>,
    written_files: HashSet<String>,

    // Track env vars accessed via /proc/*/environ reads
    env_accessed: HashMap<String, String>,

    // CWD cache per PID
    cwd_cache: HashMap<i32, String>,
}

impl TracerState {
    fn new() -> Self {
        TracerState {
            processes: HashMap::new(),
            fd_table: HashMap::new(),
            awaiting_exit: HashSet::new(),
            pending_opens: HashMap::new(),
            pending_writes: HashMap::new(),
            pending_closes: HashMap::new(),
            pending_chdirs: HashMap::new(),
            pending_fchdirs: HashMap::new(),
            active_pids: HashSet::new(),
            opened_files: HashSet::new(),
            read_files: HashSet::new(),
            written_files: HashSet::new(),
            env_accessed: HashMap::new(),
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
// Seccomp-BPF filter
// =============================================================================

#[repr(C)]
struct sock_filter {
    code: u16,
    jt: u8,
    jf: u8,
    k: u32,
}

#[repr(C)]
struct sock_fprog {
    len: u16,
    filter: *const sock_filter,
}

fn build_seccomp_filter() -> Vec<sock_filter> {
    // BPF instruction constants
    const BPF_LD: u16 = 0x00;
    const BPF_W: u16 = 0x00;
    const BPF_ABS: u16 = 0x20;
    const BPF_JMP: u16 = 0x05;
    const BPF_JEQ: u16 = 0x10;
    const BPF_RET: u16 = 0x06;
    const BPF_K: u16 = 0x00;

    const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
    const SECCOMP_RET_TRACE: u32 = 0x7ff0_0000;

    // seccomp_data offsets
    const OFFSET_NR: u32 = 0;    // offsetof(seccomp_data, nr)
    const OFFSET_ARCH: u32 = 4;  // offsetof(seccomp_data, arch)

    // All syscalls we want to trace
    let tracked_syscalls: &[u64] = &[
        SYS_READ, SYS_WRITE, SYS_OPEN, SYS_CLOSE, SYS_MMAP,
        SYS_PREAD64, SYS_PWRITE64, SYS_READV, SYS_WRITEV,
        SYS_SENDFILE, SYS_CHDIR, SYS_FCHDIR, SYS_RENAME,
        SYS_OPENAT, SYS_RENAMEAT, SYS_PREADV, SYS_PWRITEV,
        SYS_RENAMEAT2, SYS_COPY_FILE_RANGE, SYS_PREADV2, SYS_PWRITEV2,
    ];

    let num_syscalls = tracked_syscalls.len();
    let mut filter = Vec::new();

    // Load arch: BPF_LD | BPF_W | BPF_ABS, offset=arch
    filter.push(sock_filter {
        code: BPF_LD | BPF_W | BPF_ABS,
        jt: 0,
        jf: 0,
        k: OFFSET_ARCH,
    });

    // Check arch == AUDIT_ARCH_X86_64, if not → ALLOW
    // Jump over 1 instruction (to the load nr) if equal, else jump to the ALLOW at the end
    // We'll fix the jf offset after we know the total filter length
    let arch_check_idx = filter.len();
    filter.push(sock_filter {
        code: BPF_JMP | BPF_JEQ | BPF_K,
        jt: 0, // next instruction
        jf: 0, // placeholder - will be patched
        k: AUDIT_ARCH_X86_64,
    });

    // Load syscall number: BPF_LD | BPF_W | BPF_ABS, offset=nr
    filter.push(sock_filter {
        code: BPF_LD | BPF_W | BPF_ABS,
        jt: 0,
        jf: 0,
        k: OFFSET_NR,
    });

    // Chain of JEQ checks for each tracked syscall
    // Each check: if nr == syscall → jump to TRACE, else fall through
    for (i, &syscall) in tracked_syscalls.iter().enumerate() {
        let remaining = num_syscalls - i - 1;
        // If match, jump over remaining checks + ALLOW to reach TRACE
        // If no match, fall through to next check (jf=0)
        filter.push(sock_filter {
            code: BPF_JMP | BPF_JEQ | BPF_K,
            jt: (remaining + 1) as u8, // jump over remaining JEQs + 1 ALLOW → land on TRACE
            jf: 0,                      // fall through to next JEQ
            k: syscall as u32,
        });
    }

    // Default: ALLOW (for untracked syscalls)
    filter.push(sock_filter {
        code: BPF_RET | BPF_K,
        jt: 0,
        jf: 0,
        k: SECCOMP_RET_ALLOW,
    });

    // TRACE (for tracked syscalls)
    filter.push(sock_filter {
        code: BPF_RET | BPF_K,
        jt: 0,
        jf: 0,
        k: SECCOMP_RET_TRACE,
    });

    // Patch arch check jf: if arch doesn't match, jump to ALLOW
    // From arch_check_idx+1, we need to skip: load_nr(1) + num_syscalls JEQs + reach ALLOW
    // Total instructions after arch check = 1 (load nr) + num_syscalls (JEQs) + 1 (ALLOW) + 1 (TRACE)
    // jf should jump to ALLOW, which is at index (arch_check_idx + 1 + 1 + num_syscalls)
    // Relative from next instruction: 1 (load_nr) + num_syscalls (JEQs) = num_syscalls + 1
    filter[arch_check_idx].jf = (num_syscalls + 1) as u8;

    filter
}

fn install_seccomp_filter() {
    let filter = build_seccomp_filter();
    let prog = sock_fprog {
        len: filter.len() as u16,
        filter: filter.as_ptr(),
    };

    // PR_SET_NO_NEW_PRIVS is required for unprivileged seccomp
    let ret = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
    if ret != 0 {
        eprintln!("Warning: prctl(PR_SET_NO_NEW_PRIVS) failed");
        return;
    }

    // Install the BPF filter
    let ret = unsafe {
        libc::prctl(
            libc::PR_SET_SECCOMP,
            libc::SECCOMP_MODE_FILTER,
            &prog as *const sock_fprog as libc::c_ulong,
            0,
            0,
        )
    };
    if ret != 0 {
        eprintln!("Warning: prctl(PR_SET_SECCOMP) failed");
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

    // Clone parent's CWD cache entry to child
    if let Some(cwd) = state.cwd_cache.get(&parent_pid).cloned() {
        state.cwd_cache.insert(child_pid, cwd);
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
            if let Some(path) = state.fd_table.get(&(pid_raw, fd)).cloned() {
                state.read_files.insert(path);
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2 => {
            // All write variants have fd in rdi
            // Track as pending - only confirm at exit if bytes > 0 were written
            let fd = regs.rdi as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, fd)).cloned() {
                state.pending_writes.insert(pid_raw, path);
            }
        }
        SYS_SENDFILE => {
            // sendfile(out_fd, in_fd, ...) - reads from in_fd (rsi), writes to out_fd (rdi)
            let out_fd = regs.rdi as i32;
            let in_fd = regs.rsi as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, in_fd)).cloned() {
                state.read_files.insert(path);
            }
            // Track write as pending - confirm at exit if bytes > 0
            if let Some(path) = state.fd_table.get(&(pid_raw, out_fd)).cloned() {
                state.pending_writes.insert(pid_raw, path);
            }
        }
        SYS_COPY_FILE_RANGE => {
            // copy_file_range(fd_in, ..., fd_out, ...) - reads from fd_in (rdi), writes to fd_out (r8)
            let in_fd = regs.rdi as i32;
            let out_fd = regs.r8 as i32;
            if let Some(path) = state.fd_table.get(&(pid_raw, in_fd)).cloned() {
                state.read_files.insert(path);
            }
            // Track write as pending - confirm at exit if bytes > 0
            if let Some(path) = state.fd_table.get(&(pid_raw, out_fd)).cloned() {
                state.pending_writes.insert(pid_raw, path);
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
                let abs_path = resolve_path(&newpath, pid_raw, &mut state.cwd_cache);
                state.written_files.insert(abs_path);
            }
        }
        SYS_RENAMEAT | SYS_RENAMEAT2 => {
            // renameat(olddirfd, oldpath, newdirfd, newpath): rsi=oldpath, r10=newpath
            // The destination (newpath) is effectively written
            if let Some(newpath) = read_string_from_tracee(pid, regs.r10) {
                let abs_path = resolve_path(&newpath, pid_raw, &mut state.cwd_cache);
                state.written_files.insert(abs_path);
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
            if let Some(fd) = state.pending_closes.remove(&pid_raw) {
                if ret_val == 0 {
                    state.fd_table.remove(&(pid_raw, fd));
                }
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2
        | SYS_SENDFILE | SYS_COPY_FILE_RANGE => {
            // Only count as written if bytes were actually written (ret_val > 0)
            if let Some(path) = state.pending_writes.remove(&pid_raw) {
                if ret_val > 0 {
                    state.written_files.insert(path);
                }
            }
        }
        SYS_CHDIR => {
            if state.pending_chdirs.remove(&pid_raw).is_some() && ret_val == 0 {
                // Invalidate CWD cache on successful chdir
                state.cwd_cache.remove(&pid_raw);
            }
        }
        SYS_FCHDIR => {
            if state.pending_fchdirs.remove(&pid_raw).is_some() && ret_val == 0 {
                // Invalidate CWD cache on successful fchdir
                state.cwd_cache.remove(&pid_raw);
            }
        }
        _ => {}
    }
}

fn resolve_path(path: &str, pid: i32, cwd_cache: &mut HashMap<i32, String>) -> String {
    if path.starts_with('/') {
        return path.to_string();
    }

    // Try to resolve relative to process CWD, using cache
    let cwd = cwd_cache
        .entry(pid)
        .or_insert_with(|| {
            let cwd_path = format!("/proc/{}/cwd", pid);
            std::fs::read_link(&cwd_path)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default()
        })
        .clone();

    if !cwd.is_empty() {
        let mut full_path = std::path::PathBuf::from(&cwd);
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

            // Install seccomp-BPF filter before exec
            // The filter survives exec and is inherited by fork/clone children
            install_seccomp_filter();

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
                state.cwd_cache.remove(&pid_raw);
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
                state.cwd_cache.remove(&pid_raw);
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
