use std::collections::HashMap;
use std::env;
use std::fs;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use tracer_fd::FdTracker;
use tracer_runtime::{build_tracer_report, capture_process_info, timestamp_now};
use tracer_schema::{ProcessInfo, TracerReport};

use roar_tracer_preload::TraceEvent;

const TRACE_FD_ENV: &str = "ROAR_PRELOAD_TRACE_FD";
const PRELOAD_LIB_ENV: &str = "ROAR_PRELOAD_LIB";

#[cfg(target_os = "macos")]
const PROCESS_PRELOAD_ENV: &str = "DYLD_INSERT_LIBRARIES";
#[cfg(not(target_os = "macos"))]
const PROCESS_PRELOAD_ENV: &str = "LD_PRELOAD";

#[cfg(target_os = "macos")]
const PRELOAD_LIBRARY_EXT: &str = ".dylib";
#[cfg(not(target_os = "macos"))]
const PRELOAD_LIBRARY_EXT: &str = ".so";

struct CollectorState {
    fd: FdTracker,
    processes: HashMap<u32, ProcessInfo>,
    events_dropped: u64,
    root_pid: u32,
    root_command: Vec<String>,
    root_env: HashMap<String, String>,
}

impl CollectorState {
    fn new(root_pid: u32, root_command: Vec<String>) -> Self {
        Self {
            fd: FdTracker::new(None),
            processes: HashMap::new(),
            events_dropped: 0,
            root_pid,
            root_command,
            root_env: env::vars().collect(),
        }
    }

    fn ingest(&mut self, event: TraceEvent) {
        match event {
            TraceEvent::Read { pid, path } => {
                if path.is_empty() {
                    return;
                }
                self.ensure_process(pid);
                self.fd.mark_path_open(path.clone());
                self.fd.mark_path_read(path);
            }
            TraceEvent::Write { pid, path } => {
                if path.is_empty() {
                    return;
                }
                self.ensure_process(pid);
                self.fd.mark_path_open(path.clone());
                self.fd.mark_path_written(path);
            }
        }
    }

    fn ensure_process(&mut self, pid: u32) {
        if self.processes.contains_key(&pid) {
            return;
        }

        let parent_pid = parent_pid_from_proc(pid);
        let fallback_parent = if pid == self.root_pid {
            None
        } else {
            parent_pid
        };
        let info = capture_process_info(pid, fallback_parent).unwrap_or_else(|| ProcessInfo {
            pid,
            parent_pid: fallback_parent,
            command: if pid == self.root_pid {
                self.root_command.clone()
            } else {
                Vec::new()
            },
            env: if pid == self.root_pid {
                self.root_env.clone()
            } else {
                HashMap::new()
            },
        });

        self.processes.insert(pid, info);
    }

    fn build_report(&mut self, start_time: f64, end_time: f64) -> TracerReport {
        self.ensure_process(self.root_pid);

        let summary = self.fd.build_summary();
        let processes: Vec<ProcessInfo> = self.processes.values().cloned().collect();

        let env_accessed = self
            .processes
            .get(&self.root_pid)
            .map(|p| p.env.clone())
            .unwrap_or_else(|| self.root_env.clone());

        build_tracer_report(
            "preload",
            None,
            processes,
            summary.files,
            summary.opened_files,
            summary.read_files,
            summary.written_files,
            env_accessed,
            start_time,
            end_time,
            Some(self.events_dropped),
        )
    }
}

fn parent_pid_from_proc(pid: u32) -> Option<u32> {
    let status_path = format!("/proc/{pid}/status");
    let status = fs::read_to_string(status_path).ok()?;
    for line in status.lines() {
        if let Some(value) = line.strip_prefix("PPid:") {
            return value.trim().parse::<u32>().ok();
        }
    }
    None
}

fn resolve_preload_library() -> Option<PathBuf> {
    if let Ok(explicit) = env::var(PRELOAD_LIB_ENV) {
        let explicit_path = PathBuf::from(explicit);
        if explicit_path.exists() {
            return Some(explicit_path);
        }
    }

    let exe = env::current_exe().ok()?;
    let exe_dir = exe.parent()?;

    let direct_candidates = [
        exe_dir.join(format!("libroar_tracer_preload{PRELOAD_LIBRARY_EXT}")),
        exe_dir.join(format!("libroar-tracer-preload{PRELOAD_LIBRARY_EXT}")),
    ];
    for candidate in direct_candidates {
        if candidate.exists() {
            return Some(candidate);
        }
    }

    if let Ok(entries) = fs::read_dir(exe_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            let Some(name) = path.file_name().and_then(|s| s.to_str()) else {
                continue;
            };
            let is_match = (name.starts_with("libroar_tracer_preload")
                || name.starts_with("libroar-tracer-preload"))
                && name.ends_with(PRELOAD_LIBRARY_EXT);
            if is_match && path.exists() {
                return Some(path);
            }
        }
    }

    None
}

fn drain_events(read_fd: i32, state: &mut CollectorState, buf: &mut Vec<u8>) -> usize {
    let mut processed = 0usize;
    let mut tmp = [0u8; 64 * 1024];

    loop {
        let n = unsafe { libc::read(read_fd, tmp.as_mut_ptr() as *mut libc::c_void, tmp.len()) };
        if n > 0 {
            buf.extend_from_slice(&tmp[..n as usize]);
        } else {
            break;
        }
    }

    while buf.len() >= 4 {
        let len = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
        if buf.len() < 4 + len {
            break;
        }
        match rmp_serde::from_slice::<TraceEvent>(&buf[4..4 + len]) {
            Ok(event) => state.ingest(event),
            Err(_) => state.events_dropped += 1,
        }
        processed += 1;
        buf.drain(..4 + len);
    }

    processed
}

fn status_to_exit_code(status: std::process::ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    let signal = status.signal().unwrap_or(1);
    128 + signal
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

#[cfg(target_os = "macos")]
fn is_macos_protected_binary(path: &Path) -> bool {
    path.starts_with("/System/")
        || path.starts_with("/usr/bin/")
        || path.starts_with("/bin/")
        || path.starts_with("/sbin/")
        || path.starts_with("/usr/sbin/")
}

#[cfg(not(target_os = "macos"))]
fn is_macos_protected_binary(_path: &Path) -> bool {
    false
}

fn run_tracer(output_file: &str, command: &[String]) -> Result<i32> {
    let preload_library = resolve_preload_library()
        .context("preload library not found; set ROAR_PRELOAD_LIB or build roar-tracer-preload")?;

    let mut pipe_fds = [0i32; 2];
    if unsafe { libc::pipe(pipe_fds.as_mut_ptr()) } != 0 {
        anyhow::bail!("failed to create pipe");
    }
    let read_fd = pipe_fds[0];
    let write_fd = pipe_fds[1];

    // Read end: non-blocking (for polling) + close-on-exec (child doesn't need it)
    unsafe {
        libc::fcntl(read_fd, libc::F_SETFL, libc::O_NONBLOCK);
        libc::fcntl(read_fd, libc::F_SETFD, libc::FD_CLOEXEC);
    }

    let start_time = timestamp_now();

    let mut cmd = Command::new(&command[0]);
    if command.len() > 1 {
        cmd.args(&command[1..]);
    }

    let preload_library_str = preload_library.to_string_lossy().to_string();
    let existing_preload = env::var(PROCESS_PRELOAD_ENV).unwrap_or_default();
    let combined_preload = if existing_preload.is_empty() {
        preload_library_str.clone()
    } else {
        format!("{preload_library_str}:{existing_preload}")
    };

    cmd.env(PROCESS_PRELOAD_ENV, combined_preload);
    cmd.env(PRELOAD_LIB_ENV, preload_library_str);
    cmd.env(TRACE_FD_ENV, write_fd.to_string());

    let mut child = cmd.spawn().context("failed to spawn traced command")?;

    // Close write end in parent so read_fd gets EOF when child exits
    unsafe {
        libc::close(write_fd);
    }

    let root_pid = child.id();
    let mut state = CollectorState::new(root_pid, command.to_vec());
    state.ensure_process(root_pid);

    let mut read_buf = Vec::new();
    let exit_code;
    loop {
        let _ = drain_events(read_fd, &mut state, &mut read_buf);

        if let Some(status) = child.try_wait()? {
            exit_code = status_to_exit_code(status);
            break;
        }

        thread::sleep(Duration::from_millis(2));
    }

    let drain_deadline = Instant::now() + Duration::from_millis(50);
    while Instant::now() < drain_deadline {
        if drain_events(read_fd, &mut state, &mut read_buf) == 0 {
            break;
        }
    }

    unsafe {
        libc::close(read_fd);
    }

    let end_time = timestamp_now();
    let report = state.build_report(start_time, end_time);
    if cfg!(target_os = "macos") && report.files.is_empty() {
        let target_path = resolve_command_path(&command[0]);
        if let Some(path) = target_path.as_deref() {
            if is_macos_protected_binary(path) {
                eprintln!(
                    "roar-tracer-preload warning: no file I/O events captured. \
macOS may ignore DYLD_INSERT_LIBRARIES for Apple platform binaries: {}",
                    path.display()
                );
            }
        }
    }
    let msgpack = rmp_serde::to_vec_named(&report).context("failed to serialize report")?;
    fs::write(output_file, &msgpack)
        .with_context(|| format!("failed to write report to {output_file}"))?;

    Ok(exit_code)
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: roar-tracer-preload <output-file> <command> [args...]");
        std::process::exit(2);
    }

    let output_file = &args[1];
    let command = &args[2..];

    match run_tracer(output_file, command) {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("roar-tracer-preload: {e:#}");
            std::process::exit(1);
        }
    }
}
