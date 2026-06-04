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

        // Pre-populate the CWD cache while the tracee is still alive
        // (it's SIGSTOP'd at this point). Without this, openat events
        // arriving after a fast tracee has already exited resolve relative
        // paths against a now-ENOENT /proc/<pid>/cwd and silently keep the
        // raw relative path — which is the dominant cause of the
        // `bash -c 'echo > x'` short-lived-write miss rate.
        tracer.cache_cwd(root_pid);

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

    /// Mark a run as completed. Removes all PIDs for this run from the BPF
    /// map (so the kernel stops emitting new events) but keeps `pid_to_run`
    /// alive: events that the BPF probe emitted before the map removal may
    /// still be sitting in the ring buffer, and dropping `pid_to_run` here
    /// would cause [`process_event`] to drop them at the routing step. The
    /// `pid_to_run` cleanup runs in [`get_report`] after a synchronous
    /// drain.
    ///
    /// Returns the number of remaining active runs.
    pub fn deregister(&mut self, run_id: u64) -> usize {
        if let Some(run) = self.runs.get_mut(&run_id) {
            run.status = RunStatus::Completed;
            run.tracer.end_time = timestamp_now();
            // handle_process_exit only marks active_pids; deferring it to
            // get_report keeps in-flight events routable until after drain.
        }

        // Stop new events for this run by removing the PIDs from the BPF
        // tracked_pids map. The probe is read-only against this map, so a
        // removal takes effect for all future syscalls but does not affect
        // entries already queued in the ring buffer.
        let pids_for_run: Vec<u32> = self
            .pid_to_run
            .iter()
            .filter(|(_, &rid)| rid == run_id)
            .map(|(&pid, _)| pid)
            .collect();

        for pid in &pids_for_run {
            if let Some(ref mut map) = self.tracked_pids {
                let _ = map.remove(pid);
            }
        }

        self.active_run_count()
    }

    /// Process every ring-buffer item currently visible to userspace,
    /// routing each event through [`process_event`]. Callers must already
    /// hold the daemon-state mutex; this method does not re-lock.
    pub fn drain_ring_buffer(&mut self, rb: &mut RingBuf<MapData>) {
        while let Some(item) = rb.next() {
            self.process_event(&item);
        }
    }

    /// Build a real `TracerOutput` for a completed run, drop the RunState,
    /// and clean up the run's `pid_to_run` entries.
    ///
    /// Callers should drain the ring buffer immediately before this so that
    /// events emitted in the tracee's last microseconds are reflected in
    /// the report.
    pub fn get_report(&mut self, run_id: u64) -> Option<TracerOutput> {
        let mut run = self.runs.remove(&run_id)?;
        run.tracer.handle_process_exit(run.root_pid);

        // Late cleanup of pid_to_run. Done after the run is removed from
        // `runs` so any event arriving between `runs.remove` and this loop
        // would `pid_to_run.get(&pid) → run_id` and then `runs.get(&run_id)
        // → None` — i.e. the existing process_event short-circuit catches
        // it harmlessly.
        self.pid_to_run.retain(|_, &mut rid| rid != run_id);

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

        // Note: runs in `Completed` status are deliberately NOT filtered
        // out here — between Deregister and GetReport the run is marked
        // Completed but events that the BPF probe emitted *before* the
        // Deregister may still be queued in the ring buffer. Those
        // late events route correctly to `run.tracer` and end up in the
        // final report. Once GetReport runs, the run is removed from
        // `self.runs`, so the lookup below short-circuits.
        let Some(run) = self.runs.get_mut(&run_id) else {
            return;
        };

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

    // Extract the ring buffer. It's behind an Arc<Mutex<>> so the IPC
    // threads can drain synchronously at GetReport time — without that,
    // events emitted by a tracee in its last few microseconds can still
    // be queued in the ring buffer when we build the report and get
    // dropped on the floor.
    let ring_buf = Arc::new(Mutex::new(RingBuf::try_from(
        bpf.take_map("EVENTS").context("EVENTS map not found")?,
    )?));

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
    let rb_for_thread = Arc::clone(&ring_buf);
    let rb_thread = std::thread::spawn(move || {
        drain_events(rb_for_thread, rb_state, rb_running);
    });

    let mut idle_deadline = Instant::now() + idle_timeout;
    let mut client_threads: Vec<std::thread::JoinHandle<()>> = Vec::new();

    loop {
        match listener.accept() {
            Ok((stream, _addr)) => {
                idle_deadline = Instant::now() + idle_timeout;

                let state = Arc::clone(&state);
                let rb = Arc::clone(&ring_buf);
                let handle = std::thread::spawn(move || {
                    if let Err(e) = handle_client(stream, state, rb) {
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
///
/// Lock ordering: this function takes `ring_buf` first, then `state` per
/// item. The IPC-thread synchronous drain path in [`handle_client`] uses
/// the same order, so the two cannot deadlock against each other.
fn drain_events(
    ring_buf: Arc<Mutex<RingBuf<MapData>>>,
    state: Arc<Mutex<DaemonState>>,
    running: Arc<AtomicBool>,
) {
    while running.load(Ordering::SeqCst) {
        let mut got_event = false;
        {
            let mut rb = ring_buf.lock().unwrap();
            while let Some(item) = rb.next() {
                state.lock().unwrap().process_event(&item);
                got_event = true;
            }
        }
        if !got_event {
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    // Final drain
    let mut rb = ring_buf.lock().unwrap();
    while let Some(item) = rb.next() {
        state.lock().unwrap().process_event(&item);
    }
}

/// Handle a single client connection.
///
/// Lock ordering: when both are taken, `ring_buf` is taken before `state`,
/// matching [`drain_events`].
fn handle_client(
    mut stream: UnixStream,
    state: Arc<Mutex<DaemonState>>,
    ring_buf: Arc<Mutex<RingBuf<MapData>>>,
) -> Result<()> {
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
                // Synchronously drain any events still in flight for the
                // (now-deregistered) run before building the report. The
                // lock ordering matches drain_events: rb before state.
                let report = {
                    let mut rb = ring_buf.lock().unwrap();
                    let mut s = state.lock().unwrap();
                    s.drain_ring_buffer(&mut rb);
                    s.get_report(run_id)
                };
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
    fn test_deregister_marks_completed_and_keeps_pid_to_run() {
        let mut state = DaemonState::new();
        state.register(1, 100);
        state.register(2, 200);

        let remaining = state.deregister(1);
        assert_eq!(remaining, 1);
        assert_eq!(state.runs[&1].status, RunStatus::Completed);
        assert_eq!(state.runs[&2].status, RunStatus::Active);
        // pid_to_run is intentionally retained past deregister so that
        // ring-buffer events that landed before the BPF map removal still
        // route to their run (until get_report drains and cleans up).
        assert_eq!(state.pid_to_run.get(&100), Some(&1));
        assert_eq!(state.pid_to_run.get(&200), Some(&2));
    }

    #[test]
    fn test_get_report_clears_pid_to_run_for_run() {
        let mut state = DaemonState::new();
        state.register(1, 100);
        state.register(2, 200);

        state.deregister(1);
        // Still routable until get_report.
        assert_eq!(state.pid_to_run.get(&100), Some(&1));

        let _ = state.get_report(1);
        // Now run 1's pid is cleared but run 2's is untouched.
        assert!(!state.pid_to_run.contains_key(&100));
        assert_eq!(state.pid_to_run.get(&200), Some(&2));
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

    /// Build a synthetic SmallEvent payload with a TAG_SMALL prefix,
    /// matching the on-the-wire format the BPF probe emits. We construct
    /// the SmallEvent directly so the implicit alignment padding before
    /// `ret_val` (i64) is included.
    fn small_event_bytes(
        pid: u32,
        thread_id: u32,
        event_type: roar_ebpf_common::EventType,
        ret_val: i64,
        arg0: u64,
        arg1: u64,
    ) -> Vec<u8> {
        let event = roar_ebpf_common::SmallEvent {
            pid,
            thread_id,
            event_type: event_type as u16,
            _pad: 0,
            ret_val,
            arg0,
            arg1,
        };
        let mut buf = Vec::with_capacity(4 + std::mem::size_of::<roar_ebpf_common::SmallEvent>());
        buf.extend_from_slice(&roar_ebpf_common::TAG_SMALL.to_ne_bytes());
        let event_bytes = unsafe {
            std::slice::from_raw_parts(
                &event as *const _ as *const u8,
                std::mem::size_of::<roar_ebpf_common::SmallEvent>(),
            )
        };
        buf.extend_from_slice(event_bytes);
        buf
    }

    /// Regression test for the deregister race: an event that lands in the
    /// ring buffer just before the tracee's Deregister IPC arrives must
    /// still be attributed to the run when later drained, not dropped at
    /// the routing step.
    #[test]
    fn test_late_event_after_deregister_is_still_routed() {
        let mut state = DaemonState::new();
        state.register(1, 100);

        // Register an open so the FD tracker has a path mapping.
        state
            .runs
            .get_mut(&1)
            .unwrap()
            .tracer
            .handle_open(100, 3, "/tmp/late.bin".to_string(), 0);

        // Tracee exits → daemon receives Deregister → marks Completed.
        state.deregister(1);
        assert_eq!(state.runs[&1].status, RunStatus::Completed);

        // Late write event lands in the ring buffer for the same PID.
        // process_event must still find pid_to_run[100] → run 1 and route
        // the event to the run's tracer.
        let event = small_event_bytes(
            100,
            100,
            roar_ebpf_common::EventType::Write,
            16,
            3, // fd
            0,
        );
        state.process_event(&event);

        // get_report drains its own ring buffer first (in the daemon path),
        // here we just call it to verify the late event made it into the
        // tracer state before the report was built.
        let report = state.get_report(1).expect("report should exist");
        let written: Vec<_> = report
            .files
            .iter()
            .filter(|f| f.written)
            .map(|f| f.path.as_str())
            .collect();
        assert!(
            written.contains(&"/tmp/late.bin"),
            "late write should have been attributed to /tmp/late.bin; got: {written:?}"
        );

        // After get_report, pid_to_run is cleaned up.
        assert!(!state.pid_to_run.contains_key(&100));
    }

    /// Late event arriving AFTER get_report (during shutdown drain) must
    /// not panic or corrupt state — it just gets dropped at routing.
    #[test]
    fn test_event_after_get_report_is_dropped_safely() {
        let mut state = DaemonState::new();
        state.register(1, 100);
        state
            .runs
            .get_mut(&1)
            .unwrap()
            .tracer
            .handle_open(100, 3, "/tmp/x.bin".to_string(), 0);
        state.deregister(1);
        let _ = state.get_report(1);

        let event = small_event_bytes(100, 100, roar_ebpf_common::EventType::Write, 16, 3, 0);
        // Should not panic.
        state.process_event(&event);
        assert!(state.runs.is_empty());
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
            thread_id: 100,
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
