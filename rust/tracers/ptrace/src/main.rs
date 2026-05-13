mod arch;
mod seccomp;

use anyhow::{Context, Result};
use nix::sys::ptrace;
use nix::sys::wait::{waitpid, WaitPidFlag, WaitStatus};
use nix::unistd::{fork, ForkResult, Pid};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs::{self, File};
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use tracer_fd::FdTracker;
use tracer_runtime::{
    build_tracer_report, capture_process_info as capture_proc_info, resolve_path_with_cache,
    timestamp_now,
};
use tracer_schema::ProcessInfo;

use arch::{
    SYS_CHDIR, SYS_CLOSE, SYS_COPY_FILE_RANGE, SYS_DUP2, SYS_DUP3, SYS_FCHDIR, SYS_LINK,
    SYS_LINKAT, SYS_MMAP, SYS_OPEN, SYS_OPENAT, SYS_PREAD64, SYS_PREADV, SYS_PREADV2,
    SYS_PWRITE64, SYS_PWRITEV, SYS_PWRITEV2, SYS_READ, SYS_READV, SYS_RENAME, SYS_RENAMEAT,
    SYS_RENAMEAT2, SYS_SENDFILE, SYS_WRITE, SYS_WRITEV,
};

#[derive(Serialize)]
struct PreflightCheck {
    name: String,
    ok: bool,
    detail: Option<String>,
}

#[derive(Serialize)]
struct PreflightResult {
    backend: &'static str,
    ok: bool,
    summary: String,
    command_checked: Option<String>,
    warnings: Vec<String>,
    checks: Vec<PreflightCheck>,
}

fn preflight_check(name: &str, ok: bool, detail: impl Into<Option<String>>) -> PreflightCheck {
    PreflightCheck {
        name: name.to_string(),
        ok,
        detail: detail.into(),
    }
}

fn emit_preflight(result: &PreflightResult, json_output: bool) {
    if json_output {
        println!(
            "{}",
            serde_json::to_string(result).expect("failed to serialize preflight JSON")
        );
        return;
    }

    println!(
        "ptrace preflight {}: {}",
        if result.ok { "passed" } else { "failed" },
        result.summary
    );
    if let Some(command_checked) = &result.command_checked {
        println!("  command: {command_checked}");
    }
    for check in &result.checks {
        let detail = check
            .detail
            .as_ref()
            .map(|value| format!(" ({value})"))
            .unwrap_or_default();
        println!(
            "  - {}: {}{}",
            check.name,
            if check.ok { "ok" } else { "fail" },
            detail
        );
    }
}

#[derive(Debug)]
struct TracerState {
    processes: HashMap<i32, ProcessInfo>,
    fd_tracker: FdTracker,
    awaiting_exit: HashSet<i32>, // PIDs waiting for syscall exit stop
    pending_opens: HashMap<i32, (String, u64)>, // pid -> (path, flags)
    pending_writes: HashMap<i32, (String, u32)>, // tid -> (path, thread_id)
    pending_path_writes: HashMap<i32, (String, Option<u32>)>, // pid -> (destination path, thread_id)
    pending_closes: HashMap<i32, i32>, // pid -> fd (close syscalls pending confirmation)
    pending_dups: HashMap<i32, i32>,   // pid -> old_fd captured at dup{2,3} entry
    pending_chdirs: HashMap<i32, ()>,  // pid -> () (chdir pending confirmation)
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
            pending_path_writes: HashMap::new(),
            pending_closes: HashMap::new(),
            pending_dups: HashMap::new(),
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
            | SYS_RENAME
            | SYS_LINK
            | SYS_RENAMEAT
            | SYS_LINKAT
            | SYS_RENAMEAT2
            | SYS_CHDIR
            | SYS_FCHDIR
            | SYS_DUP2
            | SYS_DUP3
    )
}

fn handle_syscall_entry(
    pid: Pid,
    syscall_num: u64,
    regs: &arch::Regs,
    state: &mut TracerState,
) {
    let pid_raw = pid.as_raw();
    let pid_u32 = u32::try_from(pid_raw).ok();

    match syscall_num {
        SYS_OPEN => {
            let path_ptr = arch::arg0(regs);
            let flags = arch::arg1(regs);
            if let Some(path) = read_string_from_tracee(pid, path_ptr) {
                let abs_path = resolve_path(&path, pid_raw, &mut state.cwd_cache);
                state.pending_opens.insert(pid_raw, (abs_path, flags));
            }
        }
        SYS_OPENAT => {
            let path_ptr = arch::arg1(regs);
            let flags = arch::arg2(regs);
            if let Some(path) = read_string_from_tracee(pid, path_ptr) {
                let abs_path = resolve_path(&path, pid_raw, &mut state.cwd_cache);
                state.pending_opens.insert(pid_raw, (abs_path, flags));
            }
        }
        SYS_CLOSE => {
            // Capture the fd argument on entry so we can clean up fd_table on exit
            let fd = arch::arg0(regs) as i32;
            state.pending_closes.insert(pid_raw, fd);
        }
        SYS_DUP2 | SYS_DUP3 => {
            // dup2/dup3(old_fd, new_fd[, flags]): we need old_fd for the
            // exit-side handle_dup. arg0 holds the same value at exit on
            // x86_64 (rdi preserved) but on aarch64 x0 is clobbered with
            // the return value, so capture it now.
            let old_fd = arch::arg0(regs) as i32;
            state.pending_dups.insert(pid_raw, old_fd);
        }
        SYS_READ | SYS_PREAD64 | SYS_READV | SYS_PREADV | SYS_PREADV2 => {
            // All read variants have fd in arg0
            let fd = arch::arg0(regs) as i32;
            if let Some(pid_u32) = pid_u32 {
                state.fd_tracker.mark_read_with_thread(pid_u32, fd, pid_u32);
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2 => {
            // All write variants have fd in arg0
            // Track as pending - only confirm at exit if bytes > 0 were written
            let fd = arch::arg0(regs) as i32;
            if let Some(pid_u32) = pid_u32 {
                if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, fd).cloned() {
                    state.pending_writes.insert(pid_raw, (path, pid_u32));
                }
            }
        }
        SYS_SENDFILE => {
            // sendfile(out_fd, in_fd, ...) - reads from in_fd (arg1), writes to out_fd (arg0)
            let out_fd = arch::arg0(regs) as i32;
            let in_fd = arch::arg1(regs) as i32;
            if let Some(pid_u32) = pid_u32 {
                state
                    .fd_tracker
                    .mark_read_with_thread(pid_u32, in_fd, pid_u32);
                // Track write as pending - confirm at exit if bytes > 0
                if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, out_fd).cloned() {
                    state.pending_writes.insert(pid_raw, (path, pid_u32));
                }
            }
        }
        SYS_COPY_FILE_RANGE => {
            // copy_file_range(fd_in, ..., fd_out, ...) - reads from fd_in (arg0), writes to fd_out (arg4)
            let in_fd = arch::arg0(regs) as i32;
            let out_fd = arch::arg4(regs) as i32;
            if let Some(pid_u32) = pid_u32 {
                state
                    .fd_tracker
                    .mark_read_with_thread(pid_u32, in_fd, pid_u32);
                // Track write as pending - confirm at exit if bytes > 0
                if let Some(path) = state.fd_tracker.path_for_fd(pid_u32, out_fd).cloned() {
                    state.pending_writes.insert(pid_raw, (path, pid_u32));
                }
            }
        }
        SYS_MMAP => {
            // mmap(addr, len, prot, flags, fd, offset)
            // arg0=addr, arg1=len, arg2=prot, arg3=flags, arg4=fd, arg5=offset
            let fd = arch::arg4(regs) as i64;
            let prot = arch::arg2(regs);
            let flags = arch::arg3(regs);

            // Only track if mapping a file (fd >= 0)
            if fd >= 0 {
                let fd_i32 = fd as i32;
                if let Some(pid_u32) = pid_u32 {
                    // Use the fd-keyed mark_* functions (rather than the
                    // path-keyed mark_path_*) so the O_TRUNC suppression
                    // on the underlying fd_state applies — a process
                    // that opens with O_RDWR|O_CREAT|O_TRUNC and then
                    // mmaps the fd with PROT_READ is still semantically
                    // a write-only output for lineage purposes.
                    if state.fd_tracker.path_for_fd(pid_u32, fd_i32).is_some() {
                        // PROT_READ = 1, PROT_WRITE = 2
                        // MAP_SHARED = 1, MAP_PRIVATE = 2
                        let is_shared = flags & 1 != 0;

                        // Any file-backed mmap is a read
                        if prot & 1 != 0 {
                            state.fd_tracker.mark_read_with_thread(pid_u32, fd_i32, pid_u32);
                        }
                        // Only MAP_SHARED + PROT_WRITE is a real write (changes go to disk)
                        // MAP_PRIVATE writes are copy-on-write and don't modify the file
                        if is_shared && (prot & 2 != 0) {
                            state.fd_tracker.mark_written_with_thread(pid_u32, fd_i32, pid_u32);
                        }
                    }
                }
            }
        }
        SYS_RENAME => {
            // rename(oldpath, newpath): arg0=oldpath, arg1=newpath
            // The destination (newpath) is written only if the syscall succeeds.
            if let Some(newpath) = read_string_from_tracee(pid, arch::arg1(regs)) {
                let abs_path = resolve_path(&newpath, pid_raw, &mut state.cwd_cache);
                state
                    .pending_path_writes
                    .insert(pid_raw, (abs_path, pid_u32));
            }
        }
        SYS_LINK => {
            // link(oldpath, newpath): arg0=oldpath, arg1=newpath
            // Do not mark the source as written; the destination is published on success.
            if let Some(newpath) = read_string_from_tracee(pid, arch::arg1(regs)) {
                let abs_path = resolve_path(&newpath, pid_raw, &mut state.cwd_cache);
                state
                    .pending_path_writes
                    .insert(pid_raw, (abs_path, pid_u32));
            }
        }
        SYS_RENAMEAT | SYS_RENAMEAT2 => {
            // renameat(olddirfd, oldpath, newdirfd, newpath): arg2=newdirfd, arg3=newpath
            // renameat2 has the same first four arguments plus flags in arg4.
            if let Some(newpath) = read_string_from_tracee(pid, arch::arg3(regs)) {
                let new_dir_fd = arch::arg2(regs) as i32;
                let abs_path = resolve_at_path(&newpath, new_dir_fd, pid_raw, &mut state.cwd_cache);
                state
                    .pending_path_writes
                    .insert(pid_raw, (abs_path, pid_u32));
            }
        }
        SYS_LINKAT => {
            // linkat(olddirfd, oldpath, newdirfd, newpath, flags): arg2=newdirfd, arg3=newpath
            if let Some(newpath) = read_string_from_tracee(pid, arch::arg3(regs)) {
                let new_dir_fd = arch::arg2(regs) as i32;
                let abs_path = resolve_at_path(&newpath, new_dir_fd, pid_raw, &mut state.cwd_cache);
                state
                    .pending_path_writes
                    .insert(pid_raw, (abs_path, pid_u32));
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
    regs: &arch::Regs,
    state: &mut TracerState,
) {
    let pid_raw = pid.as_raw();
    let pid_u32 = u32::try_from(pid_raw).ok();
    let ret_val = arch::ret_val(regs);

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
        SYS_DUP2 | SYS_DUP3 => {
            // On success the kernel returns the new fd (which may be 0).
            // On failure ret_val is negative; we must NOT call handle_dup
            // in that case or we'd corrupt new_fd's path mapping with
            // old_fd's path even though the dup never happened.
            if let Some(old_fd) = state.pending_dups.remove(&pid_raw) {
                if ret_val >= 0 {
                    if let Some(pid_u32) = pid_u32 {
                        let new_fd = ret_val as i32;
                        state.fd_tracker.handle_dup(pid_u32, old_fd, new_fd);
                    }
                }
            }
        }
        SYS_WRITE | SYS_PWRITE64 | SYS_WRITEV | SYS_PWRITEV | SYS_PWRITEV2 | SYS_SENDFILE
        | SYS_COPY_FILE_RANGE => {
            // Only count as written if bytes were actually written (ret_val > 0)
            if let Some((path, thread_id)) = state.pending_writes.remove(&pid_raw) {
                if ret_val > 0 {
                    state
                        .fd_tracker
                        .mark_path_written_with_thread(path, thread_id);
                }
            }
        }
        SYS_RENAME | SYS_LINK | SYS_RENAMEAT | SYS_LINKAT | SYS_RENAMEAT2 => {
            if let Some((path, thread_id)) = state.pending_path_writes.remove(&pid_raw) {
                if ret_val == 0 {
                    if let Some(thread_id) = thread_id {
                        state
                            .fd_tracker
                            .mark_path_written_with_thread(path, thread_id);
                    } else {
                        state.fd_tracker.mark_path_written(path);
                    }
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

fn resolve_at_path(
    path: &str,
    dirfd: i32,
    pid: i32,
    cwd_cache: &mut HashMap<u32, String>,
) -> String {
    if path.starts_with('/') || dirfd == libc::AT_FDCWD {
        return resolve_path(path, pid, cwd_cache);
    }

    if pid > 0 && dirfd >= 0 {
        let fd_link = format!("/proc/{pid}/fd/{dirfd}");
        if let Ok(base) = fs::read_link(fd_link) {
            let full_path = base.join(path);
            if let Ok(canonical) = full_path.canonicalize() {
                return canonical.to_string_lossy().to_string();
            }
            return full_path.to_string_lossy().to_string();
        }
    }

    resolve_path(path, pid, cwd_cache)
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

fn resolve_command_path(command: &str) -> Option<PathBuf> {
    let command_path = PathBuf::from(command);
    if command_path.is_absolute() || command.contains('/') {
        if command_path.exists() {
            return Some(command_path);
        }
        return None;
    }

    let path_var = env::var_os("PATH")?;
    for segment in env::split_paths(&path_var) {
        let candidate = segment.join(command);
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}

fn make_preflight_temp_path(label: &str, suffix: &str) -> PathBuf {
    let pid = std::process::id();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    env::temp_dir().join(format!("roar-{label}-{pid}-{nanos}{suffix}"))
}

fn run_preflight_probe(path: &Path) -> Result<()> {
    let payload = b"roar-ptrace-preflight";
    fs::write(path, payload).with_context(|| format!("failed to write {}", path.display()))?;
    let got = fs::read(path).with_context(|| format!("failed to read {}", path.display()))?;
    if got != payload {
        anyhow::bail!("probe payload mismatch");
    }

    let renamed = path.with_extension("renamed");
    fs::rename(path, &renamed).with_context(|| {
        format!(
            "failed to rename {} -> {}",
            path.display(),
            renamed.display()
        )
    })?;
    fs::rename(&renamed, path).with_context(|| {
        format!(
            "failed to rename {} -> {}",
            renamed.display(),
            path.display()
        )
    })?;
    Ok(())
}

fn run_preflight(json_output: bool, command: Option<&str>) -> i32 {
    let mut checks = vec![
        preflight_check(
            "platform",
            cfg!(target_os = "linux"),
            Some(env::consts::OS.to_string()),
        ),
        preflight_check(
            "architecture",
            matches!(env::consts::ARCH, "x86_64" | "aarch64"),
            Some(env::consts::ARCH.to_string()),
        ),
    ];

    let mut command_checked = None;
    if let Some(command_name) = command {
        match resolve_command_path(command_name) {
            Some(path) => {
                let rendered = path.display().to_string();
                command_checked = Some(rendered.clone());
                checks.push(preflight_check("command", true, Some(rendered)));
            }
            None => {
                command_checked = Some(command_name.to_string());
                checks.push(preflight_check(
                    "command",
                    false,
                    Some(format!("command not found: {command_name}")),
                ));
                let result = PreflightResult {
                    backend: "ptrace",
                    ok: false,
                    summary: format!("command not found: {command_name}"),
                    command_checked,
                    warnings: Vec::new(),
                    checks,
                };
                emit_preflight(&result, json_output);
                return 1;
            }
        }
    }

    if !cfg!(target_os = "linux") {
        let result = PreflightResult {
            backend: "ptrace",
            ok: false,
            summary: "ptrace tracer only supports Linux hosts".to_string(),
            command_checked,
            warnings: Vec::new(),
            checks,
        };
        emit_preflight(&result, json_output);
        return 1;
    }

    if !matches!(env::consts::ARCH, "x86_64" | "aarch64") {
        let result = PreflightResult {
            backend: "ptrace",
            ok: false,
            summary: format!(
                "ptrace tracer supports x86_64 and aarch64 (got {})",
                env::consts::ARCH
            ),
            command_checked,
            warnings: Vec::new(),
            checks,
        };
        emit_preflight(&result, json_output);
        return 1;
    }

    match env::current_exe() {
        Ok(path) => {
            checks.push(preflight_check(
                "launcher",
                true,
                Some(path.display().to_string()),
            ));
        }
        Err(err) => {
            checks.push(preflight_check("launcher", false, Some(err.to_string())));
            let result = PreflightResult {
                backend: "ptrace",
                ok: false,
                summary: format!("failed to resolve current executable: {err}"),
                command_checked,
                warnings: Vec::new(),
                checks,
            };
            emit_preflight(&result, json_output);
            return 1;
        }
    };

    let report_path = make_preflight_temp_path("ptrace-report", ".msgpack");
    let probe_path = make_preflight_temp_path("ptrace-probe", ".txt");
    let shell_snippet = format!(
        "printf 'roar-ptrace-preflight' > '{}' && test -s '{}'",
        probe_path.display(),
        probe_path.display(),
    );
    let exit_code = run_tracer(
        vec!["/bin/sh".to_string(), "-c".to_string(), shell_snippet],
        report_path
            .to_str()
            .expect("temporary report path contains invalid UTF-8"),
    );

    let ok = exit_code == 0 && report_path.exists();
    checks.push(preflight_check(
        "probe_run",
        ok,
        Some(if ok {
            "report produced".to_string()
        } else {
            format!("probe exit code {exit_code}")
        }),
    ));

    let _ = fs::remove_file(&probe_path);
    let _ = fs::remove_file(&report_path);

    let result = PreflightResult {
        backend: "ptrace",
        ok,
        summary: if ok {
            "ptrace preflight succeeded".to_string()
        } else {
            format!("ptrace probe failed with exit code {exit_code}")
        },
        command_checked,
        warnings: Vec::new(),
        checks,
    };
    emit_preflight(&result, json_output);
    if result.ok {
        0
    } else {
        1
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
            seccomp::install_seccomp_filter(arch::TRACKED_SYSCALLS, arch::AUDIT_ARCH);

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
                    let regs = match arch::getregs(pid) {
                        Ok(r) => r,
                        Err(_) => {
                            let _ = ptrace::cont(pid, None);
                            continue;
                        }
                    };
                    let syscall_num = arch::syscall_num(&regs);
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
                    let regs = match arch::getregs(pid) {
                        Ok(r) => r,
                        Err(_) => {
                            let _ = ptrace::cont(pid, None);
                            continue;
                        }
                    };
                    let syscall_num = arch::syscall_num(&regs);
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
                state.pending_path_writes.remove(&pid_raw);
                state.pending_dups.remove(&pid_raw);
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
                state.pending_path_writes.remove(&pid_raw);
                state.pending_dups.remove(&pid_raw);
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

    if args.get(1).map(String::as_str) == Some("--preflight-probe") {
        let Some(path) = args.get(2) else {
            eprintln!("usage: roar-tracer --preflight-probe <path>");
            std::process::exit(2);
        };
        match run_preflight_probe(Path::new(path)) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("roar-tracer probe: {e:#}");
                std::process::exit(1);
            }
        }
    }

    if args.get(1).map(String::as_str) == Some("--preflight") {
        let mut json_output = false;
        let mut command = None;
        let mut idx = 2;
        while idx < args.len() {
            match args[idx].as_str() {
                "--json" => {
                    json_output = true;
                    idx += 1;
                }
                "--command" => {
                    let Some(value) = args.get(idx + 1) else {
                        eprintln!("usage: roar-tracer --preflight [--json] [--command <cmd>]");
                        std::process::exit(2);
                    };
                    command = Some(value.as_str());
                    idx += 2;
                }
                _ => {
                    eprintln!("usage: roar-tracer --preflight [--json] [--command <cmd>]");
                    std::process::exit(2);
                }
            }
        }
        std::process::exit(run_preflight(json_output, command));
    }

    if args.len() < 3 {
        eprintln!("Usage: roar-tracer <output-file> <command> [args...]");
        eprintln!("       roar-tracer --preflight [--json] [--command <cmd>]");
        eprintln!("  Traces <command> and writes syscall data to <output-file>");
        std::process::exit(1);
    }

    let output_file = &args[1];
    let command: Vec<String> = args[2..].to_vec();

    let exit_code = run_tracer(command, output_file);
    std::process::exit(exit_code);
}
