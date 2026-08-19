use std::ffi::CString;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use anyhow::{Context, Result};
use aya::maps::{HashMap as BpfHashMap, MapData, RingBuf};
use log::{error, info};
use roar_tracer_ebpf::{client, ipc, state};
use serde::Serialize;
use tracer_runtime::timestamp_now;

use state::TracerState;

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

fn tracefs_path() -> Option<&'static str> {
    if std::path::Path::new("/sys/kernel/tracing").exists() {
        return Some("/sys/kernel/tracing");
    }
    if std::path::Path::new("/sys/kernel/debug/tracing").exists() {
        return Some("/sys/kernel/debug/tracing");
    }
    None
}

fn perf_event_paranoid_detail() -> Option<(bool, String)> {
    let value = std::fs::read_to_string("/proc/sys/kernel/perf_event_paranoid")
        .ok()?
        .trim()
        .to_string();
    let parsed = value.parse::<i32>().ok()?;
    Some((parsed <= 1, value))
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
        "eBPF preflight {}: {}",
        if result.ok { "passed" } else { "failed" },
        result.summary
    );
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

fn run_preflight(json_output: bool) -> i32 {
    let mut checks = vec![
        preflight_check(
            "platform",
            cfg!(target_os = "linux"),
            Some(std::env::consts::OS.to_string()),
        ),
        preflight_check(
            "architecture",
            true,
            Some(std::env::consts::ARCH.to_string()),
        ),
    ];

    match tracefs_path() {
        Some(path) => checks.push(preflight_check("tracefs", true, Some(path.to_string()))),
        None => checks.push(preflight_check(
            "tracefs",
            false,
            Some("not found".to_string()),
        )),
    }

    if let Some((ok, value)) = perf_event_paranoid_detail() {
        checks.push(preflight_check("perf_event_paranoid", ok, Some(value)));
    }

    let result = match roar_tracer_ebpf::attach::load_and_attach_bpf() {
        Ok(_bpf) => {
            checks.push(preflight_check(
                "load_and_attach",
                true,
                Some("ok".to_string()),
            ));
            PreflightResult {
                backend: "ebpf",
                ok: true,
                summary: "eBPF preflight succeeded".to_string(),
                command_checked: None,
                warnings: Vec::new(),
                checks,
            }
        }
        Err(e) => {
            let detail = format!("{e:#}");
            checks.push(preflight_check(
                "load_and_attach",
                false,
                Some(detail.clone()),
            ));
            PreflightResult {
                backend: "ebpf",
                ok: false,
                summary: detail,
                command_checked: None,
                warnings: Vec::new(),
                checks,
            }
        }
    };

    emit_preflight(&result, json_output);
    if result.ok {
        0
    } else {
        1
    }
}

/// Try to use the daemon for tracing instead of inline BPF.
///
/// If a daemon is available (or can be spawned), this function:
/// 1. Connects to the daemon
/// 2. Forks the child with SIGSTOP
/// 3. Registers the run
/// 4. Resumes and waits for the child
/// 5. Gets the stub report and writes it to disk
fn try_daemon_mode(output_file: &str, command: &[String]) -> Result<i32> {
    let mut stream = client::connect_or_spawn()?;

    // Generate a random run_id
    let run_id = {
        let mut buf = [0u8; 8];
        let f = std::fs::File::open("/dev/urandom")?;
        std::io::Read::read_exact(&mut std::io::BufReader::new(f), &mut buf)?;
        u64::from_ne_bytes(buf)
    };

    let start_time = timestamp_now();

    // Fork child with SIGSTOP (same pattern as inline BPF path)
    let child_pid = match unsafe { nix::unistd::fork() }.context("fork failed")? {
        nix::unistd::ForkResult::Child => {
            let _ = nix::sys::signal::raise(nix::sys::signal::Signal::SIGSTOP);
            let cmd = CString::new(command[0].as_str()).expect("command contains null byte");
            let args: Vec<CString> = command
                .iter()
                .map(|a| CString::new(a.as_str()).expect("arg contains null byte"))
                .collect();
            let args_ref: Vec<&std::ffi::CStr> = args.iter().map(|a| a.as_c_str()).collect();
            match nix::unistd::execvp(&cmd, &args_ref) {
                Err(err) => {
                    eprintln!("execvp failed: {err}");
                    std::process::exit(127);
                }
                Ok(_) => std::process::exit(127),
            }
        }
        nix::unistd::ForkResult::Parent { child } => child.as_raw() as u32,
    };

    // Register with daemon
    ipc::send_message(
        &mut stream,
        &ipc::ClientMessage::Register {
            run_id,
            root_pid: child_pid,
            root_command: command.to_vec(),
        },
    )?;

    let ack: ipc::DaemonMessage = ipc::recv_message(&mut stream)?;
    match ack {
        ipc::DaemonMessage::Ack { .. } => {}
        ipc::DaemonMessage::Error { message, .. } => {
            anyhow::bail!("daemon rejected registration: {message}");
        }
        _ => anyhow::bail!("unexpected daemon response to Register"),
    }

    // Wait for child to stop, then resume
    nix::sys::wait::waitpid(
        nix::unistd::Pid::from_raw(child_pid as i32),
        Some(nix::sys::wait::WaitPidFlag::WUNTRACED),
    )
    .context("failed to wait for child SIGSTOP")?;

    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(child_pid as i32),
        nix::sys::signal::Signal::SIGCONT,
    )
    .context("failed to send SIGCONT to child")?;

    // Wait for child to exit
    let exit_code = loop {
        match nix::sys::wait::waitpid(
            nix::unistd::Pid::from_raw(child_pid as i32),
            Some(nix::sys::wait::WaitPidFlag::empty()),
        ) {
            Ok(nix::sys::wait::WaitStatus::Exited(_, code)) => break code,
            Ok(nix::sys::wait::WaitStatus::Signaled(_, sig, _)) => break 128 + sig as i32,
            Ok(_) => continue,
            Err(nix::errno::Errno::ECHILD) => break 0,
            Err(e) => anyhow::bail!("waitpid error: {e}"),
        }
    };

    // Deregister
    ipc::send_message(&mut stream, &ipc::ClientMessage::Deregister { run_id })?;
    let _: ipc::DaemonMessage = ipc::recv_message(&mut stream)?;

    // Get report
    ipc::send_message(&mut stream, &ipc::ClientMessage::GetReport { run_id })?;

    let response: ipc::DaemonMessage = ipc::recv_message(&mut stream)?;
    let mut report = match response {
        ipc::DaemonMessage::Report { data, .. } => data,
        ipc::DaemonMessage::Error { message, .. } => {
            anyhow::bail!("daemon error getting report: {message}");
        }
        _ => anyhow::bail!("unexpected daemon response to GetReport"),
    };

    // Patch start_time from our local measurement (more accurate than daemon's)
    report.start_time = start_time;
    report.end_time = timestamp_now();

    // Write MessagePack output
    let msgpack_data = rmp_serde::to_vec_named(&report).context("failed to serialize report")?;
    std::fs::write(output_file, &msgpack_data)
        .context(format!("failed to write output to {output_file}"))?;

    info!(
        "daemon mode: trace complete ({} files, {} events dropped)",
        report.files.len(),
        report.events_dropped.unwrap_or(0)
    );

    Ok(exit_code)
}

fn run_tracer(output_file: &str, command: &[String]) -> Result<i32> {
    env_logger::init();

    // Try daemon mode first
    match try_daemon_mode(output_file, command) {
        Ok(exit_code) => {
            // Daemon mode succeeded — exit immediately
            unsafe { libc::_exit(exit_code) }
        }
        Err(e) => {
            info!("daemon mode unavailable ({e:#}), falling back to inline eBPF");
        }
    }

    // Load the embedded BPF object and attach all tracepoints.
    let mut bpf = roar_tracer_ebpf::attach::load_and_attach_bpf()?;

    // ── Spawn child process ───────────────────────────────────────────────
    //
    // We use raw fork()+execvp() instead of Command::spawn() because the
    // latter creates an internal error-reporting pipe and blocks until exec
    // completes. Since we need the child to SIGSTOP itself before exec (so
    // we can seed the PID filter), Command::spawn() would deadlock.

    let start_time = timestamp_now();

    let child_pid = match unsafe { nix::unistd::fork() }.context("fork failed")? {
        nix::unistd::ForkResult::Child => {
            // Stop ourselves so the parent can seed TRACKED_PIDS before we exec.
            let _ = nix::sys::signal::raise(nix::sys::signal::Signal::SIGSTOP);

            // Parent has resumed us — exec the command.
            let cmd = CString::new(command[0].as_str()).expect("command contains null byte");
            let args: Vec<CString> = command
                .iter()
                .map(|a| CString::new(a.as_str()).expect("arg contains null byte"))
                .collect();
            let args_ref: Vec<&std::ffi::CStr> = args.iter().map(|a| a.as_c_str()).collect();
            match nix::unistd::execvp(&cmd, &args_ref) {
                Err(err) => {
                    eprintln!("execvp failed: {err}");
                    std::process::exit(127);
                }
                Ok(_) => std::process::exit(127),
            }
        }
        nix::unistd::ForkResult::Parent { child } => child.as_raw() as u32,
    };

    info!("child PID: {child_pid}");

    // Seed the tracked_pids BPF map with the child PID (run_id=0 for inline mode)
    {
        let mut tracked_pids: BpfHashMap<_, u32, u64> = BpfHashMap::try_from(
            bpf.map_mut("TRACKED_PIDS")
                .context("TRACKED_PIDS map not found")?,
        )?;
        tracked_pids
            .insert(child_pid, 0, 0)
            .context("failed to insert child PID into tracked_pids")?;
    }

    // ── Set up state ──────────────────────────────────────────────────────

    let mut state = TracerState::new(None); // chunk tracking disabled by default
    state.start_time = start_time;
    state.active_pids.insert(child_pid);

    // Set initial process info from the command args.
    // We can't reliably read /proc/<pid> because the child hasn't exec'd yet
    // (it's SIGSTOP'd), and by the time exec events arrive asynchronously
    // the process may have already exited.
    state.processes.insert(
        child_pid,
        state::ProcessInfo {
            pid: child_pid,
            parent_pid: None,
            command: command.to_vec(),
            env: std::env::vars().collect(),
        },
    );

    // ── Resume the child ──────────────────────────────────────────────────

    // Wait for the child to stop (SIGSTOP raised after fork)
    nix::sys::wait::waitpid(
        nix::unistd::Pid::from_raw(child_pid as i32),
        Some(nix::sys::wait::WaitPidFlag::WUNTRACED),
    )
    .context("failed to wait for child SIGSTOP")?;

    // Resume the child
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(child_pid as i32),
        nix::sys::signal::Signal::SIGCONT,
    )
    .context("failed to send SIGCONT to child")?;

    // ── Event loop ────────────────────────────────────────────────────────

    let running = Arc::new(AtomicBool::new(true));
    let r = running.clone();

    // Monitor child exit in a separate thread
    let child_pid_for_thread = child_pid;
    let monitor = std::thread::spawn(move || -> i32 {
        loop {
            match nix::sys::wait::waitpid(
                nix::unistd::Pid::from_raw(child_pid_for_thread as i32),
                Some(nix::sys::wait::WaitPidFlag::empty()),
            ) {
                Ok(nix::sys::wait::WaitStatus::Exited(_, code)) => {
                    r.store(false, Ordering::SeqCst);
                    return code;
                }
                Ok(nix::sys::wait::WaitStatus::Signaled(_, sig, _)) => {
                    r.store(false, Ordering::SeqCst);
                    return 128 + sig as i32;
                }
                Ok(_) => continue, // other status, keep waiting
                Err(nix::errno::Errno::ECHILD) => {
                    // Child already reaped
                    r.store(false, Ordering::SeqCst);
                    return 0;
                }
                Err(e) => {
                    error!("waitpid error: {e}");
                    r.store(false, Ordering::SeqCst);
                    return 1;
                }
            }
        }
    });

    // Drain ring buffer
    let ring_buf = RingBuf::try_from(bpf.map_mut("EVENTS").context("EVENTS map not found")?)?;
    drain_events(&mut state, ring_buf, &running);

    let exit_code = monitor.join().unwrap_or(1);

    // ── Finalize ──────────────────────────────────────────────────────────

    state.end_time = timestamp_now();
    state.handle_process_exit(child_pid);

    let report = state.build_report();

    // Write MessagePack output (use to_vec_named for named map keys, not positional arrays)
    let msgpack_data = rmp_serde::to_vec_named(&report).context("failed to serialize report")?;
    std::fs::write(output_file, &msgpack_data)
        .context(format!("failed to write output to {output_file}"))?;

    info!(
        "trace complete: {} files tracked, {} events dropped",
        report.files.len(),
        report.events_dropped.unwrap_or(0)
    );

    // Each BPF/perf_event fd close triggers synchronize_rcu() in the kernel,
    // taking ~35-48ms per fd. With 22 tracepoints, that's ~800ms of cleanup.
    // Fix: fork a cleanup child that holds fd copies (bumping refcounts to 2).
    // The parent _exit()s first (refcount 2→1, no release = fast).
    // The child sleeps briefly to ensure the parent exits first, then _exit()s
    // (refcount 1→0, slow release, but nobody is waiting for the child).
    //
    // Close stdio before forking so the cleanup child doesn't hold open any
    // pipes — otherwise callers using pipe-based process management (e.g.
    // subprocess.run with capture_output) would block until the child exits.
    unsafe {
        libc::close(0);
        libc::close(1);
        libc::close(2);
        let pid = libc::fork();
        if pid == 0 {
            // Child: wait for parent to finish exiting, then exit.
            libc::usleep(200_000);
            libc::_exit(0);
        }
        // Parent (or fork error): exit immediately.
        // Child holds fd copies, so refcounts > 0 → no slow BPF cleanup.
        libc::_exit(exit_code);
    }
}

/// Drain events from the ring buffer until the child exits.
fn drain_events(
    state: &mut TracerState,
    mut ring_buf: RingBuf<&mut MapData>,
    running: &Arc<AtomicBool>,
) {
    while running.load(Ordering::SeqCst) {
        while let Some(item) = ring_buf.next() {
            roar_tracer_ebpf::events::process_event(state, &item);
        }
        // Short sleep to avoid busy-spinning
        std::thread::sleep(std::time::Duration::from_millis(1));
    }

    // Final drain after child exit
    while let Some(item) = ring_buf.next() {
        roar_tracer_ebpf::events::process_event(state, &item);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();

    if args.get(1).map(String::as_str) == Some("--preflight") {
        let json_output = args.iter().any(|arg| arg == "--json");
        std::process::exit(run_preflight(json_output));
    }

    if args.len() < 3 {
        eprintln!("usage: roar-tracer-ebpf <output-file> <command> [args...]");
        eprintln!("       roar-tracer-ebpf --preflight [--json]");
        std::process::exit(2);
    }

    let output_file = &args[1];
    let command = &args[2..];

    match run_tracer(output_file, command) {
        Ok(_) => { /* exit handled by run_tracer via std::process::exit */ }
        Err(e) => {
            eprintln!("roar-tracer-ebpf: {e:#}");
            std::process::exit(1);
        }
    }
}
