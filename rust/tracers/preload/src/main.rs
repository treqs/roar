use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::ErrorKind;
use std::os::unix::net::UnixDatagram;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use tracer_fd::FdTracker;
use tracer_runtime::{build_tracer_report, capture_process_info, timestamp_now};
use tracer_schema::{ProcessInfo, TracerReport};

use roar_tracer_preload::TraceEvent;

const TRACE_SOCKET_ENV: &str = "ROAR_PRELOAD_TRACE_SOCK";
const PRELOAD_LIB_ENV: &str = "ROAR_PRELOAD_LIB";

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
        exe_dir.join("libroar_tracer_preload.so"),
        exe_dir.join("libroar-tracer-preload.so"),
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
                && name.ends_with(".so");
            if is_match && path.exists() {
                return Some(path);
            }
        }
    }

    None
}

fn make_socket_path(output_file: &str) -> PathBuf {
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let parent = Path::new(output_file)
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    parent.join(format!(".roar-preload-{pid}-{nanos}.sock"))
}

fn drain_events(socket: &UnixDatagram, state: &mut CollectorState) -> usize {
    let mut processed = 0usize;
    let mut buf = [0u8; 16 * 1024];

    loop {
        match socket.recv(&mut buf) {
            Ok(size) => {
                processed += 1;
                match rmp_serde::from_slice::<TraceEvent>(&buf[..size]) {
                    Ok(event) => state.ingest(event),
                    Err(_) => state.events_dropped += 1,
                }
            }
            Err(e) if e.kind() == ErrorKind::WouldBlock || e.kind() == ErrorKind::TimedOut => {
                break;
            }
            Err(_) => {
                state.events_dropped += 1;
                break;
            }
        }
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

fn run_tracer(output_file: &str, command: &[String]) -> Result<i32> {
    let preload_library = resolve_preload_library()
        .context("preload library not found; set ROAR_PRELOAD_LIB or build roar-tracer-preload")?;

    let socket_path = make_socket_path(output_file);
    let socket = UnixDatagram::bind(&socket_path)
        .with_context(|| format!("failed to bind socket {}", socket_path.display()))?;
    socket.set_nonblocking(true)?;

    let start_time = timestamp_now();

    let mut cmd = Command::new(&command[0]);
    if command.len() > 1 {
        cmd.args(&command[1..]);
    }

    let preload_library_str = preload_library.to_string_lossy().to_string();
    let existing_preload = env::var("LD_PRELOAD").unwrap_or_default();
    let combined_preload = if existing_preload.is_empty() {
        preload_library_str.clone()
    } else {
        format!("{preload_library_str}:{existing_preload}")
    };

    cmd.env("LD_PRELOAD", combined_preload);
    cmd.env(PRELOAD_LIB_ENV, preload_library_str);
    cmd.env(TRACE_SOCKET_ENV, &socket_path);

    let mut child = cmd.spawn().context("failed to spawn traced command")?;
    let root_pid = child.id();
    let mut state = CollectorState::new(root_pid, command.to_vec());
    state.ensure_process(root_pid);

    let exit_code;
    loop {
        let _ = drain_events(&socket, &mut state);

        if let Some(status) = child.try_wait()? {
            exit_code = status_to_exit_code(status);
            break;
        }

        thread::sleep(Duration::from_millis(2));
    }

    let drain_deadline = Instant::now() + Duration::from_millis(50);
    while Instant::now() < drain_deadline {
        if drain_events(&socket, &mut state) == 0 {
            break;
        }
    }

    let end_time = timestamp_now();
    let report = state.build_report(start_time, end_time);
    let msgpack = rmp_serde::to_vec_named(&report).context("failed to serialize report")?;
    fs::write(output_file, &msgpack)
        .with_context(|| format!("failed to write report to {output_file}"))?;

    let _ = fs::remove_file(&socket_path);
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
