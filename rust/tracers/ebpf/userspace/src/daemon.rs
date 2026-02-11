use std::collections::HashMap;
use std::io::ErrorKind;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use aya::maps::{HashMap as BpfHashMap, MapData, RingBuf};
use log::info;
use tracer_runtime::timestamp_now;

use crate::events;
use crate::ipc::{self, ClientMessage, DaemonMessage};
use crate::state::{TracerOutput, TracerState};

// ── State types ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunStatus {
    Active,
    Completed,
}

pub struct RunState {
    pub run_id: u64,
    pub root_pid: u32,
    pub status: RunStatus,
    pub tracer: TracerState,
}

pub struct DaemonState {
    pub runs: HashMap<u64, RunState>,
    pub pid_to_run: HashMap<u32, u64>,
    /// BPF map handle for inserting/removing tracked PIDs.
    pub tracked_pids: Option<BpfHashMap<MapData, u32, u64>>,
}

impl DaemonState {
    pub fn new() -> Self {
        Self {
            runs: HashMap::new(),
            pid_to_run: HashMap::new(),
            tracked_pids: None,
        }
    }

    pub fn register(&mut self, run_id: u64, root_pid: u32) {
        let mut tracer = TracerState::new(None);
        tracer.start_time = timestamp_now();
        tracer.active_pids.insert(root_pid);

        // Capture initial process info from /proc (child is SIGSTOP'd but exists)
        if let Some(info) = crate::state::capture_process_info(root_pid, None) {
            tracer.processes.insert(root_pid, info);
        }

        self.runs.insert(
            run_id,
            RunState {
                run_id,
                root_pid,
                status: RunStatus::Active,
                tracer,
            },
        );
        self.pid_to_run.insert(root_pid, run_id);

        // Add root PID to BPF tracked_pids map with this run_id
        if let Some(ref mut map) = self.tracked_pids {
            if let Err(e) = map.insert(root_pid, run_id, 0) {
                log::warn!("failed to insert PID {root_pid} into tracked_pids: {e}");
            }
        }
    }

    /// Mark a run as completed. Removes all PIDs for this run from the BPF map.
    /// Returns the number of remaining active runs.
    pub fn deregister(&mut self, run_id: u64) -> usize {
        if let Some(run) = self.runs.get_mut(&run_id) {
            run.status = RunStatus::Completed;
            run.tracer.end_time = timestamp_now();
            run.tracer.handle_process_exit(run.root_pid);
        }

        // Remove all PIDs belonging to this run from the BPF map and pid_to_run
        let pids_to_remove: Vec<u32> = self
            .pid_to_run
            .iter()
            .filter(|(_, &rid)| rid == run_id)
            .map(|(&pid, _)| pid)
            .collect();

        for pid in &pids_to_remove {
            self.pid_to_run.remove(pid);
            if let Some(ref mut map) = self.tracked_pids {
                let _ = map.remove(pid);
            }
        }

        self.active_run_count()
    }

    /// Build a real TracerOutput for a completed run and remove the RunState.
    pub fn get_report(&mut self, run_id: u64) -> Option<TracerOutput> {
        let run = self.runs.remove(&run_id)?;
        Some(run.tracer.build_report())
    }

    /// Route a parsed event to the correct run's TracerState.
    pub fn process_event(&mut self, data: &[u8]) {
        // Extract PID from the event (both SmallEvent and LargeEvent have pid at
        // the same offset: right after the 4-byte tag).
        if data.len() < 8 {
            return;
        }
        let pid = u32::from_ne_bytes([data[4], data[5], data[6], data[7]]);

        let Some(&run_id) = self.pid_to_run.get(&pid) else {
            return; // PID not tracked by any run (shouldn't happen with BPF filter)
        };

        let Some(run) = self.runs.get_mut(&run_id) else {
            return;
        };

        if run.status != RunStatus::Active {
            return;
        }

        // For clone events, we need to update pid_to_run with the child PID.
        // The BPF program already inserted the child into tracked_pids with the
        // parent's run_id, but we need the userspace pid_to_run mapping too.
        //
        // Check if this is a clone event by peeking at the tag and event_type.
        // TAG_LARGE=1, and LargeEvent.event_type is at offset 4 (pid) + 2 bytes into event.
        let tag = u32::from_ne_bytes([data[0], data[1], data[2], data[3]]);
        if tag == roar_ebpf_common::TAG_LARGE && data.len() >= 4 + 8 {
            let event_type = u16::from_ne_bytes([data[8], data[9]]);
            if event_type == roar_ebpf_common::EventType::Clone as u16 {
                // arg0 (child_pid) is at offset 4 (tag) + 4 (pid) + 2 (event_type) + 2 (pad) + 8 (ret_val) = 20
                if data.len() >= 28 {
                    let child_pid_bytes = &data[20..28];
                    let child_pid = u64::from_ne_bytes(child_pid_bytes.try_into().unwrap()) as u32;
                    if child_pid > 0 {
                        self.pid_to_run.insert(child_pid, run_id);
                    }
                }
            }
        }

        events::process_event(&mut run.tracer, data);
    }

    pub fn active_run_count(&self) -> usize {
        self.runs
            .values()
            .filter(|r| r.status == RunStatus::Active)
            .count()
    }
}

// ── Server ───────────────────────────────────────────────────────────────────

/// Run the daemon server loop.
///
/// Loads BPF programs, starts the ring buffer consumer thread, then accepts
/// client connections (one thread per client). Exits when idle for longer than
/// `idle_timeout` with no active runs.
pub fn run_daemon(socket_path: PathBuf, idle_timeout: Duration) -> Result<()> {
    // Load and attach BPF programs
    let mut bpf = crate::attach::load_and_attach_bpf()?;

    // Extract the tracked_pids map (owned handle for sharing across threads)
    let tracked_pids_map: BpfHashMap<MapData, u32, u64> = BpfHashMap::try_from(
        bpf.take_map("TRACKED_PIDS")
            .context("TRACKED_PIDS map not found")?,
    )?;

    // Extract the ring buffer
    let ring_buf = RingBuf::try_from(bpf.take_map("EVENTS").context("EVENTS map not found")?)?;

    // Set up shared state
    let mut daemon_state = DaemonState::new();
    daemon_state.tracked_pids = Some(tracked_pids_map);
    let state = Arc::new(Mutex::new(daemon_state));

    // Bind the listener
    let listener = UnixListener::bind(&socket_path).context("failed to bind Unix socket")?;

    // Write PID file
    let pid_path = socket_path.with_extension("pid");
    std::fs::write(&pid_path, std::process::id().to_string())
        .context("failed to write PID file")?;

    // Non-blocking for idle-timeout polling
    listener
        .set_nonblocking(true)
        .context("failed to set listener non-blocking")?;

    info!(
        "roard: listening on {} (pid {})",
        socket_path.display(),
        std::process::id()
    );

    // Start ring buffer consumer thread
    let running = Arc::new(AtomicBool::new(true));
    let rb_state = Arc::clone(&state);
    let rb_running = Arc::clone(&running);
    let rb_thread = std::thread::spawn(move || {
        drain_events(ring_buf, rb_state, rb_running);
    });

    let mut idle_deadline = Instant::now() + idle_timeout;
    let mut client_threads: Vec<std::thread::JoinHandle<()>> = Vec::new();

    loop {
        match listener.accept() {
            Ok((stream, _addr)) => {
                idle_deadline = Instant::now() + idle_timeout;

                let state = Arc::clone(&state);
                let handle = std::thread::spawn(move || {
                    if let Err(e) = handle_client(stream, state) {
                        log::debug!("client disconnected: {e:#}");
                    }
                });
                client_threads.push(handle);
            }
            Err(ref e) if e.kind() == ErrorKind::WouldBlock => {}
            Err(e) => {
                log::warn!("accept error: {e}");
            }
        }

        // Check idle timeout
        let active = state.lock().unwrap().active_run_count();
        if active > 0 {
            idle_deadline = Instant::now() + idle_timeout;
        } else if Instant::now() >= idle_deadline {
            info!("roard: idle timeout reached, shutting down");
            break;
        }

        std::thread::sleep(Duration::from_millis(50));
    }

    // Signal ring buffer thread to stop and wait for it
    running.store(false, Ordering::SeqCst);
    let _ = rb_thread.join();

    // Clean up
    let _ = std::fs::remove_file(&socket_path);
    let _ = std::fs::remove_file(&pid_path);

    for handle in client_threads {
        let _ = handle.join();
    }

    // Keep bpf alive until cleanup is done (dropping it detaches tracepoints)
    drop(bpf);

    Ok(())
}

/// Drain events from the ring buffer, routing each to the correct run.
fn drain_events(
    mut ring_buf: RingBuf<MapData>,
    state: Arc<Mutex<DaemonState>>,
    running: Arc<AtomicBool>,
) {
    while running.load(Ordering::SeqCst) {
        let mut got_event = false;
        while let Some(item) = ring_buf.next() {
            state.lock().unwrap().process_event(&item);
            got_event = true;
        }
        if !got_event {
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    // Final drain
    while let Some(item) = ring_buf.next() {
        state.lock().unwrap().process_event(&item);
    }
}

/// Handle a single client connection.
fn handle_client(mut stream: UnixStream, state: Arc<Mutex<DaemonState>>) -> Result<()> {
    stream.set_nonblocking(false)?;

    loop {
        let msg: ClientMessage = match ipc::recv_message(&mut stream) {
            Ok(msg) => msg,
            Err(_) => return Ok(()),
        };

        match msg {
            ClientMessage::Register { run_id, root_pid } => {
                info!("register: run_id={run_id} pid={root_pid}");
                state.lock().unwrap().register(run_id, root_pid);
                ipc::send_message(&mut stream, &DaemonMessage::Ack { run_id })?;
            }
            ClientMessage::Deregister { run_id } => {
                info!("deregister: run_id={run_id}");
                let remaining = state.lock().unwrap().deregister(run_id);
                info!("  active runs remaining: {remaining}");
                ipc::send_message(&mut stream, &DaemonMessage::Ack { run_id })?;
            }
            ClientMessage::GetReport { run_id } => {
                info!("get_report: run_id={run_id}");
                let report = state.lock().unwrap().get_report(run_id);
                match report {
                    Some(data) => {
                        ipc::send_message(&mut stream, &DaemonMessage::Report { run_id, data })?;
                    }
                    None => {
                        ipc::send_message(
                            &mut stream,
                            &DaemonMessage::Error {
                                run_id,
                                message: format!("unknown run_id {run_id}"),
                            },
                        )?;
                    }
                }
            }
            ClientMessage::Ping => {
                ipc::send_message(&mut stream, &DaemonMessage::Pong)?;
            }
        }
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_register_creates_run_state() {
        let mut state = DaemonState::new();
        state.register(1, 100);

        assert!(state.runs.contains_key(&1));
        let run = &state.runs[&1];
        assert_eq!(run.run_id, 1);
        assert_eq!(run.root_pid, 100);
        assert_eq!(run.status, RunStatus::Active);
        assert_eq!(state.pid_to_run.get(&100), Some(&1));
        assert!(run.tracer.active_pids.contains(&100));
    }

    #[test]
    fn test_deregister_marks_completed() {
        let mut state = DaemonState::new();
        state.register(1, 100);
        state.register(2, 200);

        let remaining = state.deregister(1);
        assert_eq!(remaining, 1);
        assert_eq!(state.runs[&1].status, RunStatus::Completed);
        assert_eq!(state.runs[&2].status, RunStatus::Active);
        // PIDs for run 1 should be removed from pid_to_run
        assert!(!state.pid_to_run.contains_key(&100));
    }

    #[test]
    fn test_get_report_returns_real_data() {
        let mut state = DaemonState::new();
        state.register(1, 100);

        // Simulate some file I/O via the TracerState
        state
            .runs
            .get_mut(&1)
            .unwrap()
            .tracer
            .handle_open(100, 3, "/tmp/test.txt".to_string(), 0);
        state
            .runs
            .get_mut(&1)
            .unwrap()
            .tracer
            .handle_write(100, 3, 1024);

        state.deregister(1);
        let report = state.get_report(1);
        assert!(report.is_some());
        let report = report.unwrap();
        assert_eq!(report.version, 1);
        assert_eq!(report.tracer_mode, "ebpf");
        assert_eq!(report.files.len(), 1);
        assert_eq!(report.files[0].path, "/tmp/test.txt");
        assert!(report.files[0].written);
    }

    #[test]
    fn test_get_report_unknown_run_id() {
        let mut state = DaemonState::new();
        assert!(state.get_report(999).is_none());
    }

    #[test]
    fn test_active_run_count() {
        let mut state = DaemonState::new();
        assert_eq!(state.active_run_count(), 0);

        state.register(1, 100);
        state.register(2, 200);
        assert_eq!(state.active_run_count(), 2);

        state.deregister(1);
        assert_eq!(state.active_run_count(), 1);

        state.deregister(2);
        assert_eq!(state.active_run_count(), 0);
    }

    #[test]
    fn test_multiple_registrations_independent() {
        let mut state = DaemonState::new();
        state.register(1, 100);
        state.register(2, 200);
        state.register(3, 300);

        assert_eq!(state.active_run_count(), 3);
        assert_eq!(state.pid_to_run.get(&100), Some(&1));
        assert_eq!(state.pid_to_run.get(&200), Some(&2));
        assert_eq!(state.pid_to_run.get(&300), Some(&3));

        state.deregister(2);
        assert_eq!(state.runs[&1].status, RunStatus::Active);
        assert_eq!(state.runs[&2].status, RunStatus::Completed);
        assert_eq!(state.runs[&3].status, RunStatus::Active);

        let report = state.get_report(2).unwrap();
        assert_eq!(report.tracer_mode, "ebpf");
        assert!(state.runs.contains_key(&1));
        assert!(state.runs.contains_key(&3));
    }

    #[test]
    fn test_process_event_routes_to_correct_run() {
        let mut state = DaemonState::new();
        state.register(1, 100);
        state.register(2, 200);

        // Open a file on run 1's PID (pid=100)
        state
            .runs
            .get_mut(&1)
            .unwrap()
            .tracer
            .handle_open(100, 3, "/tmp/run1.txt".to_string(), 0);

        // Open a file on run 2's PID (pid=200)
        state
            .runs
            .get_mut(&2)
            .unwrap()
            .tracer
            .handle_open(200, 3, "/tmp/run2.txt".to_string(), 0);

        // Simulate a write event for pid=100 (run 1) using raw bytes
        let event = roar_ebpf_common::SmallEvent {
            pid: 100,
            event_type: roar_ebpf_common::EventType::Write as u16,
            _pad: 0,
            ret_val: 512,
            arg0: 3,
            arg1: 0,
        };
        let mut raw = Vec::new();
        raw.extend_from_slice(&roar_ebpf_common::TAG_SMALL.to_ne_bytes());
        raw.extend_from_slice(unsafe {
            std::slice::from_raw_parts(
                &event as *const roar_ebpf_common::SmallEvent as *const u8,
                std::mem::size_of::<roar_ebpf_common::SmallEvent>(),
            )
        });

        state.process_event(&raw);

        // Run 1 should have the write, run 2 should not
        let fd1 = state.runs[&1].tracer.fd.fd_state.get(&(100, 3)).unwrap();
        assert!(fd1.was_written);

        let fd2 = state.runs[&2].tracer.fd.fd_state.get(&(200, 3)).unwrap();
        assert!(!fd2.was_written);
    }
}
