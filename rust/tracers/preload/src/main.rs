use std::collections::HashMap;
use std::env;
use std::ffi::c_void;
use std::fs;
use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixListener;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use anyhow::{Context, Result};
use serde::Serialize;
use tracer_fd::FdTracker;
use tracer_runtime::{build_tracer_report, capture_process_info, timestamp_now};
use tracer_schema::{ProcessInfo, TracerReport};

use roar_tracer_preload::TraceEvent;

const TRACE_SOCK_ENV: &str = "ROAR_PRELOAD_TRACE_SOCK";
const PRELOAD_LIB_ENV: &str = "ROAR_PRELOAD_LIB";

#[cfg(target_os = "macos")]
const PROCESS_PRELOAD_ENV: &str = "DYLD_INSERT_LIBRARIES";
#[cfg(not(target_os = "macos"))]
const PROCESS_PRELOAD_ENV: &str = "LD_PRELOAD";

#[cfg(target_os = "macos")]
const PRELOAD_LIBRARY_EXT: &str = ".dylib";
#[cfg(not(target_os = "macos"))]
const PRELOAD_LIBRARY_EXT: &str = ".so";

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
        "preload preflight {}: {}",
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

struct CollectorState {
    fd: FdTracker,
    processes: HashMap<u32, ProcessInfo>,
    events_dropped: u64,
    root_pid: u32,
    root_command: Vec<String>,
    root_env: HashMap<String, String>,
    /// First-seen (dev, inode) for each written path, captured while the file
    /// still exists. Used to recover outputs renamed via syscalls the preload
    /// tracer can't observe (see `reconcile_renamed_outputs`).
    seen_inodes: HashMap<String, (u64, u64)>,
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
            seen_inodes: HashMap::new(),
        }
    }

    fn ingest(&mut self, event: TraceEvent) {
        match event {
            TraceEvent::Read {
                pid,
                thread_id,
                path,
            } => self.record_read(pid, thread_id, path),
            TraceEvent::Write {
                pid,
                thread_id,
                path,
            } => self.record_write(pid, thread_id, path),
            TraceEvent::OpenRead {
                pid,
                thread_id,
                path,
            } => {
                // Conflation policy — preload backend: credit the open
                // for-read as a confirmed read. We need this because
                // some libc-internal read paths (similar to write) bypass
                // the PLT and our `read` hook never fires; treating the
                // open as the signal preserves coverage at the cost of
                // recording read access for files that may not have
                // actually been read from. See the corresponding rationale
                // for OpenWrite below.
                self.record_read(pid, thread_id, path);
            }
            TraceEvent::OpenWrite {
                pid,
                thread_id,
                path,
            } => {
                // Conflation policy — preload backend: credit the open
                // with-write-flags as a confirmed write. This is what
                // makes shell-redirect lineage work: bash's `echo > x`
                // emits an OpenWrite via our open hook but never an
                // actual byte-level Write event, because the underlying
                // write goes through libc-internal `__write` which
                // bypasses LD_PRELOAD overrides entirely. Trade-off:
                // a process that opens with O_TRUNC and then exits
                // without writing (rare) is still credited as an output.
                // The ptrace backend, which observes the actual write
                // syscall, keeps the strict byte-write semantic.
                self.record_write(pid, thread_id, path);
            }
        }
    }

    fn record_read(&mut self, pid: u32, thread_id: u32, path: String) {
        if path.is_empty() {
            return;
        }
        self.ensure_process(pid);
        self.fd.mark_path_open(path.clone());
        self.fd.mark_path_read_with_thread(path, thread_id);
    }

    fn record_write(&mut self, pid: u32, thread_id: u32, path: String) {
        if path.is_empty() {
            return;
        }
        self.ensure_process(pid);
        // Capture (dev, inode) on first sight, while the file still exists. This lets
        // build_report() recover an output that is later renamed via a syscall the
        // preload tracer cannot observe (rustix `linux_raw` inline `renameat2`, used by
        // `tempfile` and thus by safetensors/torch checkpoint saves): the temp file is
        // seen written here, then vanishes when atomically renamed to the final name.
        if !self.seen_inodes.contains_key(&path) {
            if let Ok(meta) = fs::metadata(&path) {
                self.seen_inodes
                    .insert(path.clone(), (meta.dev(), meta.ino()));
            }
        }
        self.fd.mark_path_open(path.clone());
        self.fd.mark_path_written_with_thread(path, thread_id);
    }

    /// Recover written outputs renamed via a syscall the preload tracer cannot observe.
    ///
    /// A file recorded as written that no longer exists at report time was almost
    /// certainly atomically renamed (temp -> final) by an untraced syscall — notably
    /// rustix's `linux_raw` inline `renameat2`, which `tempfile` uses and which
    /// safetensors/torch emit when saving checkpoints. `LD_PRELOAD` interposes libc
    /// symbols, so it never sees that rename. Renames preserve the inode, so we look up
    /// the captured (dev, inode) in the vanished file's own parent directory and rewrite
    /// the path to the renamed-to name. Preload-specific: ptrace/eBPF observe the rename
    /// syscall directly and never produce this class of vanished output.
    fn reconcile_renamed_outputs(&self, summary: &mut tracer_fd::FileSummary) {
        let mut remap: HashMap<String, String> = HashMap::new();
        for rec in &summary.files {
            if !rec.written || remap.contains_key(&rec.path) {
                continue;
            }
            let path = Path::new(&rec.path);
            if path.exists() {
                continue; // still present; nothing to recover
            }
            let Some(&(dev, ino)) = self.seen_inodes.get(&rec.path) else {
                continue;
            };
            // The atomic-save pattern renames within the same directory (sibling temp),
            // so the renamed-to file is in the vanished file's own parent.
            let parent = path.parent().unwrap_or_else(|| Path::new("."));
            let Ok(entries) = fs::read_dir(parent) else {
                continue;
            };
            for entry in entries.flatten() {
                let Ok(meta) = entry.metadata() else {
                    continue;
                };
                if meta.is_file() && meta.dev() == dev && meta.ino() == ino {
                    remap.insert(
                        rec.path.clone(),
                        entry.path().to_string_lossy().into_owned(),
                    );
                    break;
                }
            }
        }
        if remap.is_empty() {
            return;
        }
        for rec in &mut summary.files {
            if let Some(new_path) = remap.get(&rec.path) {
                rec.path = new_path.clone();
            }
        }
        // Rebuild the derived path lists from the rewritten records.
        summary.opened_files = summary.files.iter().map(|f| f.path.clone()).collect();
        summary.read_files = summary
            .files
            .iter()
            .filter(|f| f.read)
            .map(|f| f.path.clone())
            .collect();
        summary.written_files = summary
            .files
            .iter()
            .filter(|f| f.written)
            .map(|f| f.path.clone())
            .collect();
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

        let mut summary = self.fd.build_summary();
        self.reconcile_renamed_outputs(&mut summary);
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
    #[cfg(target_os = "macos")]
    {
        // Use proc_pidinfo(PROC_PIDTBSDINFO) to get the parent PID on macOS.
        // struct proc_bsdinfo has pbi_ppid at offset 16 (after flags, status, xstatus, pid).
        extern "C" {
            fn proc_pidinfo(
                pid: libc::c_int,
                flavor: libc::c_int,
                arg: u64,
                buffer: *mut libc::c_void,
                buffersize: libc::c_int,
            ) -> libc::c_int;
        }
        const PROC_PIDTBSDINFO: libc::c_int = 3;
        let mut buf = [0u8; 256]; // struct proc_bsdinfo is ~136 bytes
        let ret = unsafe {
            proc_pidinfo(
                pid as libc::c_int,
                PROC_PIDTBSDINFO,
                0,
                buf.as_mut_ptr() as *mut libc::c_void,
                buf.len() as libc::c_int,
            )
        };
        if ret <= 0 {
            return None;
        }
        // pbi_ppid is at byte offset 16 (4th uint32 field).
        let ppid = u32::from_ne_bytes([buf[16], buf[17], buf[18], buf[19]]);
        if ppid == 0 {
            return None;
        }
        return Some(ppid);
    }

    #[cfg(not(target_os = "macos"))]
    {
        let status_path = format!("/proc/{pid}/status");
        let status = fs::read_to_string(status_path).ok()?;
        for line in status.lines() {
            if let Some(value) = line.strip_prefix("PPid:") {
                return value.trim().parse::<u32>().ok();
            }
        }
        None
    }
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

fn make_socket_path(_output_file: &str) -> PathBuf {
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    // Use /tmp to keep the path short — sun_path is only 104 bytes on macOS.
    Path::new("/tmp").join(format!(".roar-{pid}-{nanos}.sock"))
}

const SOCK_BUF_SIZE: libc::c_int = 65536;

fn set_rcvbuf(stream: &std::os::unix::net::UnixStream) {
    unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_RCVBUF,
            &SOCK_BUF_SIZE as *const _ as *const c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        );
    }
}

#[derive(PartialEq)]
enum DrainResult {
    Ok,
    Eof,
}

fn drain_stream(
    stream: &mut std::os::unix::net::UnixStream,
    buf: &mut Vec<u8>,
    state: &mut CollectorState,
) -> DrainResult {
    let mut tmp = [0u8; 64 * 1024];
    let mut hit_eof = false;
    loop {
        match stream.read(&mut tmp) {
            Ok(0) => {
                hit_eof = true;
                break;
            }
            Ok(n) => buf.extend_from_slice(&tmp[..n]),
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(_) => {
                hit_eof = true;
                break;
            }
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
        buf.drain(..4 + len);
    }

    if hit_eof {
        DrainResult::Eof
    } else {
        DrainResult::Ok
    }
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

fn make_preflight_temp_path(label: &str, suffix: &str) -> PathBuf {
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    env::temp_dir().join(format!("roar-{label}-{pid}-{nanos}{suffix}"))
}

fn run_preflight_probe(path: &Path) -> Result<()> {
    let payload = b"roar-preflight-probe";
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
        preflight_check("platform", true, Some(env::consts::OS.to_string())),
        preflight_check("architecture", true, Some(env::consts::ARCH.to_string())),
    ];

    match resolve_preload_library() {
        Some(path) => {
            checks.push(preflight_check(
                "library",
                true,
                Some(path.display().to_string()),
            ));
        }
        None => {
            let result = PreflightResult {
                backend: "preload",
                ok: false,
                summary: "preload library not found".to_string(),
                command_checked: None,
                warnings: Vec::new(),
                checks: {
                    checks.push(preflight_check(
                        "library",
                        false,
                        Some("preload library not found".to_string()),
                    ));
                    checks
                },
            };
            emit_preflight(&result, json_output);
            return 1;
        }
    };

    let mut command_checked = None;
    if let Some(command_name) = command {
        match resolve_command_path(command_name) {
            Some(path) => {
                let rendered = path.display().to_string();
                command_checked = Some(rendered.clone());
                checks.push(preflight_check("command", true, Some(rendered.clone())));
                let compatible = !is_macos_protected_binary(&path);
                checks.push(preflight_check(
                    "command_compatibility",
                    compatible,
                    Some(if compatible {
                        "compatible".to_string()
                    } else {
                        "macOS protected binary blocks preload injection".to_string()
                    }),
                ));
                if !compatible {
                    let result = PreflightResult {
                        backend: "preload",
                        ok: false,
                        summary: "macOS protected binary blocks preload injection".to_string(),
                        command_checked,
                        warnings: Vec::new(),
                        checks,
                    };
                    emit_preflight(&result, json_output);
                    return 1;
                }
            }
            None => {
                command_checked = Some(command_name.to_string());
                checks.push(preflight_check(
                    "command",
                    false,
                    Some(format!("command not found: {command_name}")),
                ));
                let result = PreflightResult {
                    backend: "preload",
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

    let current_exe = match env::current_exe() {
        Ok(path) => {
            checks.push(preflight_check(
                "launcher",
                true,
                Some(path.display().to_string()),
            ));
            path
        }
        Err(err) => {
            checks.push(preflight_check("launcher", false, Some(err.to_string())));
            let result = PreflightResult {
                backend: "preload",
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

    let report_path = make_preflight_temp_path("preload-report", ".msgpack");
    let probe_path = make_preflight_temp_path("preload-probe", ".txt");
    let probe_command = vec![
        current_exe.display().to_string(),
        "--preflight-probe".to_string(),
        probe_path.display().to_string(),
    ];

    let summary;
    let ok = match run_tracer(
        report_path
            .to_str()
            .expect("temporary report path contains invalid UTF-8"),
        &probe_command,
    ) {
        Ok(exit_code) if exit_code == 0 && report_path.exists() => {
            checks.push(preflight_check(
                "probe_run",
                true,
                Some("report produced".to_string()),
            ));
            summary = "preload preflight succeeded".to_string();
            true
        }
        Ok(exit_code) => {
            checks.push(preflight_check(
                "probe_run",
                false,
                Some(format!("probe exit code {exit_code}")),
            ));
            summary = format!("preload probe failed with exit code {exit_code}");
            false
        }
        Err(err) => {
            checks.push(preflight_check(
                "probe_run",
                false,
                Some(format!("{err:#}")),
            ));
            summary = format!("preload probe failed: {err:#}");
            false
        }
    };

    let _ = fs::remove_file(&probe_path);
    let _ = fs::remove_file(&report_path);
    let result = PreflightResult {
        backend: "preload",
        ok,
        summary,
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

fn run_tracer(output_file: &str, command: &[String]) -> Result<i32> {
    let preload_library = resolve_preload_library()
        .context("preload library not found; set ROAR_PRELOAD_LIB or build roar-tracer-preload")?;

    let socket_path = make_socket_path(output_file);
    // Remove any stale socket file before binding.
    let _ = fs::remove_file(&socket_path);
    let listener = UnixListener::bind(&socket_path).context("failed to bind Unix domain socket")?;
    listener
        .set_nonblocking(true)
        .context("failed to set listener non-blocking")?;

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
    cmd.env(TRACE_SOCK_ENV, &socket_path);

    let mut child = cmd.spawn().context("failed to spawn traced command")?;

    let root_pid = child.id();
    let mut state = CollectorState::new(root_pid, command.to_vec());
    state.ensure_process(root_pid);

    let mut connections: Vec<(std::os::unix::net::UnixStream, Vec<u8>)> = Vec::new();
    let exit_code;
    loop {
        // Accept new connections (non-blocking)
        while let Ok((stream, _)) = listener.accept() {
            let _ = stream.set_nonblocking(true);
            set_rcvbuf(&stream);
            connections.push((stream, Vec::new()));
        }

        // Drain each connection, removing those at EOF
        connections
            .retain_mut(|(stream, buf)| drain_stream(stream, buf, &mut state) != DrainResult::Eof);

        if let Some(status) = child.try_wait()? {
            exit_code = status_to_exit_code(status);
            break;
        }

        thread::sleep(Duration::from_millis(2));
    }

    // Post-exit drain: keep accepting + draining for late-connecting grandchildren
    let drain_deadline = Instant::now() + Duration::from_millis(50);
    while Instant::now() < drain_deadline {
        let mut activity = false;

        while let Ok((stream, _)) = listener.accept() {
            let _ = stream.set_nonblocking(true);
            set_rcvbuf(&stream);
            connections.push((stream, Vec::new()));
            activity = true;
        }

        connections.retain_mut(|(stream, buf)| {
            let result = drain_stream(stream, buf, &mut state);
            if result != DrainResult::Eof {
                activity = true;
            }
            result != DrainResult::Eof
        });

        if !activity && connections.is_empty() {
            break;
        }
        thread::sleep(Duration::from_millis(1));
    }

    drop(listener);
    let _ = fs::remove_file(&socket_path);

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

    if args.get(1).map(String::as_str) == Some("--preflight-probe") {
        let Some(path) = args.get(2) else {
            eprintln!("usage: roar-tracer-preload --preflight-probe <path>");
            std::process::exit(2);
        };
        match run_preflight_probe(Path::new(path)) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("roar-tracer-preload probe: {e:#}");
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
                        eprintln!(
                            "usage: roar-tracer-preload --preflight [--json] [--command <cmd>]"
                        );
                        std::process::exit(2);
                    };
                    command = Some(value.as_str());
                    idx += 2;
                }
                _ => {
                    eprintln!("usage: roar-tracer-preload --preflight [--json] [--command <cmd>]");
                    std::process::exit(2);
                }
            }
        }
        std::process::exit(run_preflight(json_output, command));
    }

    if args.len() < 3 {
        eprintln!("usage: roar-tracer-preload <output-file> <command> [args...]");
        eprintln!("       roar-tracer-preload --preflight [--json] [--command <cmd>]");
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

#[cfg(test)]
mod tests {
    use super::*;
    use tracer_schema::FileRecord;

    fn written_record(path: &str) -> FileRecord {
        FileRecord {
            path: path.to_string(),
            read: false,
            written: true,
            read_threads: None,
            written_threads: None,
            chunks_read: None,
            chunks_written: None,
        }
    }

    /// Regression: an output written then atomically renamed via a syscall the
    /// preload tracer can't observe (rustix inline `renameat2`, as in safetensors
    /// checkpoint saves) is recovered by inode match instead of being dropped.
    #[test]
    fn reconciles_renamed_output_by_inode() {
        let dir = env::temp_dir().join(format!("roar_recon_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let temp = dir.join(".tmpABCDEF");
        let final_path = dir.join("checkpoint.safetensors");

        // Tracer saw the temp written (and captured its inode while it existed)...
        fs::write(&temp, b"weights").unwrap();
        let meta = fs::metadata(&temp).unwrap();
        let temp_str = temp.to_string_lossy().into_owned();
        let final_str = final_path.to_string_lossy().into_owned();

        let mut state = CollectorState::new(1, vec!["test".to_string()]);
        state
            .seen_inodes
            .insert(temp_str.clone(), (meta.dev(), meta.ino()));

        // ...then the (untraced) atomic rename happened.
        fs::rename(&temp, &final_path).unwrap();

        let mut summary = tracer_fd::FileSummary {
            files: vec![written_record(&temp_str)],
            opened_files: vec![temp_str.clone()],
            read_files: vec![],
            written_files: vec![temp_str.clone()],
        };

        state.reconcile_renamed_outputs(&mut summary);

        assert_eq!(summary.files[0].path, final_str, "record rewritten to final name");
        assert!(summary.written_files.contains(&final_str));
        assert!(!summary.written_files.contains(&temp_str));
        let _ = fs::remove_dir_all(&dir);
    }

    /// A genuinely-deleted written file (no rename target with the captured inode)
    /// is left untouched — no false recovery.
    #[test]
    fn leaves_deleted_output_untouched() {
        let dir = env::temp_dir().join(format!("roar_recon_del_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let temp = dir.join(".tmpGONE");
        fs::write(&temp, b"scratch").unwrap();
        let meta = fs::metadata(&temp).unwrap();
        let temp_str = temp.to_string_lossy().into_owned();

        let mut state = CollectorState::new(1, vec!["test".to_string()]);
        state
            .seen_inodes
            .insert(temp_str.clone(), (meta.dev(), meta.ino()));
        fs::remove_file(&temp).unwrap();

        let mut summary = tracer_fd::FileSummary {
            files: vec![written_record(&temp_str)],
            opened_files: vec![temp_str.clone()],
            read_files: vec![],
            written_files: vec![temp_str.clone()],
        };
        state.reconcile_renamed_outputs(&mut summary);

        assert_eq!(summary.files[0].path, temp_str, "deleted file path unchanged");
        let _ = fs::remove_dir_all(&dir);
    }
}
