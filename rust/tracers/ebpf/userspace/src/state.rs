use std::collections::{HashMap, HashSet};

use tracer_fd::FdTracker;
use tracer_runtime::{
    build_tracer_report, capture_process_info as capture_proc_info,
    resolve_path as resolve_runtime_path,
};
pub use tracer_schema::ProcessInfo;
use tracer_schema::TracerReport;

#[cfg(test)]
use std::collections::BTreeSet;
#[cfg(test)]
use tracer_fd::{mark_chunks, FdState};

pub type TracerOutput = TracerReport;

/// Main tracer state, maintained entirely in userspace.
pub struct TracerState {
    /// Shared fd/path aggregation state.
    pub fd: FdTracker,

    pub processes: HashMap<u32, ProcessInfo>,
    pub active_pids: HashSet<u32>,

    /// Chunk size for I/O tracking. None = whole-file granularity (no chunk indices).
    pub chunk_size: Option<u64>,

    /// Count of events dropped by the ring buffer.
    pub events_dropped: u64,

    /// Timestamps
    pub start_time: f64,
    pub end_time: f64,
}

impl TracerState {
    pub fn new(chunk_size: Option<u64>) -> Self {
        Self {
            fd: FdTracker::new(chunk_size),
            processes: HashMap::new(),
            active_pids: HashSet::new(),
            chunk_size,
            events_dropped: 0,
            start_time: 0.0,
            end_time: 0.0,
        }
    }

    // -- Event handlers ---------------------------------------------------

    pub fn handle_open(&mut self, pid: u32, fd: i32, path: String, flags: u64) {
        self.fd.handle_open(pid, fd, path, flags);
    }

    pub fn handle_close(&mut self, pid: u32, fd: i32) {
        self.fd.handle_close(pid, fd);
    }

    pub fn handle_read(&mut self, pid: u32, fd: i32, bytes: u64) {
        self.fd.handle_read(pid, fd, bytes);
    }

    pub fn handle_pread(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        self.fd.handle_pread(pid, fd, offset, bytes);
    }

    pub fn handle_write(&mut self, pid: u32, fd: i32, bytes: u64) {
        self.fd.handle_write(pid, fd, bytes);
    }

    pub fn handle_pwrite(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        self.fd.handle_pwrite(pid, fd, offset, bytes);
    }

    pub fn handle_lseek(&mut self, pid: u32, fd: i32, new_offset: u64) {
        self.fd.handle_lseek(pid, fd, new_offset);
    }

    pub fn handle_dup(&mut self, pid: u32, old_fd: i32, new_fd: i32) {
        self.fd.handle_dup(pid, old_fd, new_fd);
    }

    pub fn handle_clone(&mut self, parent_pid: u32, child_pid: u32) {
        self.active_pids.insert(child_pid);
        self.fd.handle_clone(parent_pid, child_pid);
    }

    /// Handle exec -- recapture process metadata from /proc.
    /// Only overwrites existing info if the new capture has useful data,
    /// since with async event processing the process may have already exited.
    pub fn handle_exec(&mut self, pid: u32) {
        let parent = self.processes.get(&pid).and_then(|p| p.parent_pid);
        if let Some(info) = capture_process_info(pid, parent) {
            if !info.command.is_empty() {
                self.processes.insert(pid, info);
            }
        }
    }

    /// Handle process exit -- remove from active set.
    pub fn handle_process_exit(&mut self, pid: u32) {
        self.active_pids.remove(&pid);
    }

    /// Mark a path as written even if there is no fd context (e.g. rename).
    pub fn mark_path_written(&mut self, path: String) {
        self.fd.mark_path_written(path);
    }

    // -- Report generation ------------------------------------------------

    pub fn build_report(&self) -> TracerOutput {
        let summary = self.fd.build_summary();

        let processes: Vec<ProcessInfo> = self.processes.values().cloned().collect();
        let env_accessed = self
            .processes
            .values()
            .next()
            .map(|p| p.env.clone())
            .unwrap_or_default();

        build_tracer_report(
            "ebpf",
            self.chunk_size,
            processes,
            summary.files,
            summary.opened_files,
            summary.read_files,
            summary.written_files,
            env_accessed,
            self.start_time,
            self.end_time,
            Some(self.events_dropped),
        )
    }
}

/// Read process metadata from /proc.
pub fn capture_process_info(pid: u32, parent_pid: Option<u32>) -> Option<ProcessInfo> {
    capture_proc_info(pid, parent_pid)
}

/// Resolve a path from a BPF event. Handles relative paths via /proc/<pid>/cwd.
pub fn resolve_path(pid: u32, raw_path: &str) -> String {
    resolve_runtime_path(raw_path, pid)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_mark_chunks_single_chunk() {
        let mut chunks = BTreeSet::new();
        mark_chunks(&mut chunks, 0, 100, 1024);
        assert_eq!(chunks, BTreeSet::from([0]));
    }

    #[test]
    fn test_mark_chunks_spanning() {
        let mut chunks = BTreeSet::new();
        // 8 MB chunk size, reading 20 MB starting at offset 4 MB
        let chunk_size = 8 * 1024 * 1024;
        mark_chunks(&mut chunks, 4 * 1024 * 1024, 20 * 1024 * 1024, chunk_size);
        // Spans chunks 0, 1, 2
        assert_eq!(chunks, BTreeSet::from([0, 1, 2]));
    }

    #[test]
    fn test_mark_chunks_exact_boundary() {
        let mut chunks = BTreeSet::new();
        mark_chunks(&mut chunks, 0, 1024, 1024);
        // [0, 1024) -> chunk 0 only (1023 / 1024 == 0)
        assert_eq!(chunks, BTreeSet::from([0]));
    }

    #[test]
    fn test_mark_chunks_boundary_crossing() {
        let mut chunks = BTreeSet::new();
        mark_chunks(&mut chunks, 1023, 2, 1024);
        // byte 1023 in chunk 0, byte 1024 in chunk 1
        assert_eq!(chunks, BTreeSet::from([0, 1]));
    }

    #[test]
    fn test_mark_chunks_zero_length() {
        let mut chunks = BTreeSet::new();
        mark_chunks(&mut chunks, 0, 0, 1024);
        assert!(chunks.is_empty());
    }

    #[test]
    fn test_handle_open_close() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        assert_eq!(
            state.fd.fd_table.get(&(1, 3)),
            Some(&"/tmp/test.txt".to_string())
        );
        assert!(state.fd.fd_state.contains_key(&(1, 3)));

        state.handle_close(1, 3);
        assert!(!state.fd.fd_table.contains_key(&(1, 3)));
        // fd_state preserved for report
        assert!(state.fd.fd_state.contains_key(&(1, 3)));
    }

    #[test]
    fn test_handle_read_write_sequential() {
        let mut state = TracerState::new(Some(1024));
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        // Read 500 bytes (cursor 0 -> 500)
        state.handle_read(1, 3, 500);
        let fd = state.fd.fd_state.get(&(1, 3)).unwrap();
        assert!(fd.was_read);
        assert!(!fd.was_written);
        assert_eq!(fd.cursor, 500);
        assert_eq!(fd.chunks_read, BTreeSet::from([0]));

        // Read another 600 bytes (cursor 500 -> 1100, crosses chunk boundary)
        state.handle_read(1, 3, 600);
        let fd = state.fd.fd_state.get(&(1, 3)).unwrap();
        assert_eq!(fd.cursor, 1100);
        assert_eq!(fd.chunks_read, BTreeSet::from([0, 1]));

        // Write 100 bytes (cursor 1100 -> 1200)
        state.handle_write(1, 3, 100);
        let fd = state.fd.fd_state.get(&(1, 3)).unwrap();
        assert!(fd.was_written);
        assert_eq!(fd.cursor, 1200);
        assert_eq!(fd.chunks_written, BTreeSet::from([1]));
    }

    #[test]
    fn test_handle_pread_no_cursor_advance() {
        let mut state = TracerState::new(Some(1024));
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        // Positional read at offset 2048, 100 bytes
        state.handle_pread(1, 3, 2048, 100);
        let fd = state.fd.fd_state.get(&(1, 3)).unwrap();
        assert!(fd.was_read);
        assert_eq!(fd.cursor, 0); // cursor unchanged
        assert_eq!(fd.chunks_read, BTreeSet::from([2]));
    }

    #[test]
    fn test_handle_lseek() {
        let mut state = TracerState::new(Some(1024));
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        state.handle_read(1, 3, 500);
        assert_eq!(state.fd.fd_state.get(&(1, 3)).unwrap().cursor, 500);

        state.handle_lseek(1, 3, 2048);
        assert_eq!(state.fd.fd_state.get(&(1, 3)).unwrap().cursor, 2048);

        state.handle_read(1, 3, 100);
        assert_eq!(state.fd.fd_state.get(&(1, 3)).unwrap().cursor, 2148);
        assert_eq!(
            state.fd.fd_state.get(&(1, 3)).unwrap().chunks_read,
            BTreeSet::from([0, 2])
        );
    }

    #[test]
    fn test_handle_dup() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_read(1, 3, 500); // advance cursor on fd 3

        state.handle_dup(1, 3, 7);
        assert_eq!(
            state.fd.fd_table.get(&(1, 7)),
            Some(&"/tmp/test.txt".to_string())
        );
        // New fd gets independent cursor starting at 0
        assert_eq!(state.fd.fd_state.get(&(1, 7)).unwrap().cursor, 0);
    }

    #[test]
    fn test_handle_clone() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_open(1, 4, "/tmp/other.txt".to_string(), 0);

        state.handle_clone(1, 2);
        assert!(state.active_pids.contains(&2));
        assert_eq!(
            state.fd.fd_table.get(&(2, 3)),
            Some(&"/tmp/test.txt".to_string())
        );
        assert_eq!(
            state.fd.fd_table.get(&(2, 4)),
            Some(&"/tmp/other.txt".to_string())
        );
    }

    #[test]
    fn test_build_report_aggregates_by_path() {
        let mut state = TracerState::new(Some(1024));
        state.start_time = 1000.0;
        state.end_time = 2000.0;

        // Two fds pointing to the same file
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_open(1, 4, "/tmp/test.txt".to_string(), 0);

        state.handle_read(1, 3, 500); // read via fd 3
        state.handle_write(1, 4, 100); // write via fd 4

        let report = state.build_report();
        assert_eq!(report.files.len(), 1);
        assert_eq!(report.files[0].path, "/tmp/test.txt");
        assert!(report.files[0].read);
        assert!(report.files[0].written);
    }

    #[test]
    fn test_build_report_no_chunks_when_disabled() {
        let mut state = TracerState::new(None); // chunk tracking disabled

        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_read(1, 3, 500);

        let report = state.build_report();
        assert_eq!(report.files.len(), 1);
        assert!(report.files[0].read);
        assert!(report.files[0].chunks_read.is_none());
    }

    #[test]
    fn test_zero_byte_read_write_ignored() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        state.handle_read(1, 3, 0);
        state.handle_write(1, 3, 0);

        let fd = state.fd.fd_state.get(&(1, 3)).unwrap();
        assert!(!fd.was_read);
        assert!(!fd.was_written);
        assert_eq!(fd.cursor, 0);
    }

    #[test]
    fn test_unknown_fd_operations_ignored() {
        let mut state = TracerState::new(None);
        // These should not panic -- just silently ignore unknown fds
        state.handle_read(1, 99, 100);
        state.handle_write(1, 99, 100);
        state.handle_pread(1, 99, 0, 100);
        state.handle_pwrite(1, 99, 0, 100);
        state.handle_lseek(1, 99, 0);
        state.handle_close(1, 99);
        state.handle_dup(1, 99, 100);
    }

    #[test]
    fn test_resolve_path_absolute() {
        assert_eq!(resolve_path(1, "/absolute/path"), "/absolute/path");
    }

    #[test]
    fn test_rename_path_written_without_fd() {
        let mut state = TracerState::new(None);
        state.mark_path_written("/tmp/renamed.txt".to_string());

        let report = state.build_report();
        assert_eq!(report.files.len(), 1);
        assert_eq!(report.files[0].path, "/tmp/renamed.txt");
        assert!(report.files[0].written);
    }

    #[test]
    fn test_fd_state_type_is_shared() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/shared.txt".to_string(), 0);
        let fd: &FdState = state.fd.fd_state.get(&(1, 3)).unwrap();
        assert_eq!(fd.path, "/tmp/shared.txt");
    }
}
