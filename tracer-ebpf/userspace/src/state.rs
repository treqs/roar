use std::collections::{BTreeSet, HashMap, HashSet};

use serde::{Deserialize, Serialize};

/// Per-fd I/O tracking state.
#[derive(Debug, Clone)]
pub struct FdState {
    pub path: String,
    pub cursor: u64,
    pub was_read: bool,
    pub was_written: bool,
    pub chunks_read: BTreeSet<u64>,
    pub chunks_written: BTreeSet<u64>,
}

impl FdState {
    pub fn new(path: String) -> Self {
        Self {
            path,
            cursor: 0,
            was_read: false,
            was_written: false,
            chunks_read: BTreeSet::new(),
            chunks_written: BTreeSet::new(),
        }
    }
}

/// Information about a traced process.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProcessInfo {
    pub pid: u32,
    pub parent_pid: Option<u32>,
    pub command: Vec<String>,
    pub env: HashMap<String, String>,
}

/// Per-path aggregated file record for the final report.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileRecord {
    pub path: String,
    pub read: bool,
    pub written: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunks_read: Option<Vec<u64>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunks_written: Option<Vec<u64>>,
}

/// Final output report.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TracerOutput {
    pub version: u32,
    pub chunk_size: Option<u64>,
    pub processes: Vec<ProcessInfo>,
    pub files: Vec<FileRecord>,
    pub env_accessed: HashMap<String, String>,
    pub start_time: f64,
    pub end_time: f64,
    pub tracer_mode: String,
    pub events_dropped: u64,
}

/// Main tracer state, maintained entirely in userspace.
pub struct TracerState {
    /// (pid, fd) -> file path
    pub fd_table: HashMap<(u32, i32), String>,

    /// (pid, fd) -> I/O state for currently-open fds
    pub fd_state: HashMap<(u32, i32), FdState>,

    /// Completed fd states (from fds that were closed and then reused).
    /// Without this, reusing fd numbers would overwrite previous state.
    pub closed_states: Vec<FdState>,

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
            fd_table: HashMap::new(),
            fd_state: HashMap::new(),
            closed_states: Vec::new(),
            processes: HashMap::new(),
            active_pids: HashSet::new(),
            chunk_size,
            events_dropped: 0,
            start_time: 0.0,
            end_time: 0.0,
        }
    }

    // ── Event handlers ────────────────────────────────────────────────────

    /// Handle a successful open: register the fd → path mapping and create fd state.
    /// If the fd was previously used (common — OS reuses lowest available fd), save
    /// the old state so it's preserved in the final report.
    pub fn handle_open(&mut self, pid: u32, fd: i32, path: String, _flags: u64) {
        if let Some(old) = self.fd_state.remove(&(pid, fd)) {
            self.closed_states.push(old);
        }
        self.fd_table.insert((pid, fd), path.clone());
        self.fd_state.insert((pid, fd), FdState::new(path));
    }

    /// Handle a successful close: remove fd from tracking.
    pub fn handle_close(&mut self, pid: u32, fd: i32) {
        self.fd_table.remove(&(pid, fd));
        // Keep fd_state — we want to preserve the read/write records for the report.
        // The state is aggregated by path at report time.
    }

    /// Handle a sequential read (read, readv).
    pub fn handle_read(&mut self, pid: u32, fd: i32, bytes: u64) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_read = true;
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_read, state.cursor, bytes, chunk_size);
            }
            state.cursor += bytes;
        }
    }

    /// Handle a positional read (pread64, preadv, preadv2).
    pub fn handle_pread(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_read = true;
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_read, offset, bytes, chunk_size);
            }
            // Do NOT advance cursor for positional reads
        }
    }

    /// Handle a sequential write (write, writev).
    pub fn handle_write(&mut self, pid: u32, fd: i32, bytes: u64) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_written = true;
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_written, state.cursor, bytes, chunk_size);
            }
            state.cursor += bytes;
        }
    }

    /// Handle a positional write (pwrite64, pwritev, pwritev2).
    pub fn handle_pwrite(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_written = true;
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_written, offset, bytes, chunk_size);
            }
            // Do NOT advance cursor for positional writes
        }
    }

    /// Handle lseek — update the cursor position.
    pub fn handle_lseek(&mut self, pid: u32, fd: i32, new_offset: u64) {
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.cursor = new_offset;
        }
    }

    /// Handle dup — clone the fd table entry with an independent FdState.
    pub fn handle_dup(&mut self, pid: u32, old_fd: i32, new_fd: i32) {
        if let Some(path) = self.fd_table.get(&(pid, old_fd)).cloned() {
            // Preserve any existing state at the target fd (dup2 atomically closes it)
            if let Some(old) = self.fd_state.remove(&(pid, new_fd)) {
                self.closed_states.push(old);
            }
            self.fd_table.insert((pid, new_fd), path.clone());
            self.fd_state
                .insert((pid, new_fd), FdState::new(path));
        }
    }

    /// Handle clone/fork — copy parent's fd_table to child.
    pub fn handle_clone(&mut self, parent_pid: u32, child_pid: u32) {
        self.active_pids.insert(child_pid);

        // Clone fd_table entries from parent to child
        let parent_entries: Vec<_> = self
            .fd_table
            .iter()
            .filter(|((pid, _), _)| *pid == parent_pid)
            .map(|((_, fd), path)| (*fd, path.clone()))
            .collect();

        for (fd, path) in parent_entries {
            self.fd_table.insert((child_pid, fd), path.clone());
            self.fd_state
                .insert((child_pid, fd), FdState::new(path));
        }
    }

    /// Handle exec — recapture process metadata from /proc.
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

    /// Handle process exit — remove from active set.
    pub fn handle_process_exit(&mut self, pid: u32) {
        self.active_pids.remove(&pid);
        // Don't remove fd_state — we want to keep the records for the report
    }

    // ── Report generation ─────────────────────────────────────────────────

    /// Build the final output report, aggregating per-fd state into per-path records.
    pub fn build_report(&self) -> TracerOutput {
        // Aggregate FdState by path (include both active and closed fd states)
        let mut path_map: HashMap<String, (bool, bool, BTreeSet<u64>, BTreeSet<u64>)> =
            HashMap::new();

        for state in self.fd_state.values().chain(self.closed_states.iter()) {
            if state.path.is_empty() {
                continue; // skip entries with unresolved paths
            }
            let entry = path_map.entry(state.path.clone()).or_insert_with(|| {
                (false, false, BTreeSet::new(), BTreeSet::new())
            });
            entry.0 |= state.was_read;
            entry.1 |= state.was_written;
            entry.2.extend(&state.chunks_read);
            entry.3.extend(&state.chunks_written);
        }

        let mut files: Vec<FileRecord> = path_map
            .into_iter()
            .map(|(path, (read, written, chunks_r, chunks_w))| {
                let chunks_read = if self.chunk_size.is_some() && !chunks_r.is_empty() {
                    Some(chunks_r.into_iter().collect())
                } else {
                    None
                };
                let chunks_written = if self.chunk_size.is_some() && !chunks_w.is_empty() {
                    Some(chunks_w.into_iter().collect())
                } else {
                    None
                };
                FileRecord {
                    path,
                    read,
                    written,
                    chunks_read,
                    chunks_written,
                }
            })
            .collect();

        // Sort files by path for deterministic output
        files.sort_by(|a, b| a.path.cmp(&b.path));

        let processes: Vec<ProcessInfo> = self.processes.values().cloned().collect();

        TracerOutput {
            version: 1,
            chunk_size: self.chunk_size,
            processes,
            files,
            env_accessed: self
                .processes
                .values()
                .next()
                .map(|p| p.env.clone())
                .unwrap_or_default(),
            start_time: self.start_time,
            end_time: self.end_time,
            tracer_mode: "ebpf".to_string(),
            events_dropped: self.events_dropped,
        }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/// Mark chunk indices in a bitset for a byte range [offset, offset+length).
pub fn mark_chunks(chunks: &mut BTreeSet<u64>, offset: u64, length: u64, chunk_size: u64) {
    if length == 0 || chunk_size == 0 {
        return;
    }
    let start_chunk = offset / chunk_size;
    let end_chunk = (offset + length - 1) / chunk_size;
    for chunk in start_chunk..=end_chunk {
        chunks.insert(chunk);
    }
}

/// Read process metadata from /proc.
pub fn capture_process_info(pid: u32, parent_pid: Option<u32>) -> Option<ProcessInfo> {
    let cmdline = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    let command: Vec<String> = cmdline
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect();

    let environ = std::fs::read(format!("/proc/{pid}/environ")).unwrap_or_default();
    let env: HashMap<String, String> = environ
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .filter_map(|s| {
            let s = String::from_utf8_lossy(s);
            let mut parts = s.splitn(2, '=');
            let key = parts.next()?;
            let value = parts.next().unwrap_or("");
            Some((key.to_string(), value.to_string()))
        })
        .collect();

    Some(ProcessInfo {
        pid,
        parent_pid,
        command,
        env,
    })
}

/// Resolve a path from a BPF event. Handles relative paths via /proc/<pid>/cwd.
pub fn resolve_path(pid: u32, raw_path: &str) -> String {
    if raw_path.starts_with('/') {
        return raw_path.to_string();
    }

    // Read CWD from /proc
    let cwd = std::fs::read_link(format!("/proc/{pid}/cwd"))
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();

    if cwd.is_empty() {
        return raw_path.to_string();
    }

    let full = format!("{cwd}/{raw_path}");
    // Try to canonicalize, fall back to the joined path
    std::fs::canonicalize(&full)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or(full)
}

// ── Tests ─────────────────────────────────────────────────────────────────────

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
        // [0, 1024) → chunk 0 only (1023 / 1024 == 0)
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

        assert_eq!(state.fd_table.get(&(1, 3)), Some(&"/tmp/test.txt".to_string()));
        assert!(state.fd_state.contains_key(&(1, 3)));

        state.handle_close(1, 3);
        assert!(!state.fd_table.contains_key(&(1, 3)));
        // fd_state preserved for report
        assert!(state.fd_state.contains_key(&(1, 3)));
    }

    #[test]
    fn test_handle_read_write_sequential() {
        let mut state = TracerState::new(Some(1024));
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        // Read 500 bytes (cursor 0 → 500)
        state.handle_read(1, 3, 500);
        let fd = state.fd_state.get(&(1, 3)).unwrap();
        assert!(fd.was_read);
        assert!(!fd.was_written);
        assert_eq!(fd.cursor, 500);
        assert_eq!(fd.chunks_read, BTreeSet::from([0]));

        // Read another 600 bytes (cursor 500 → 1100, crosses chunk boundary)
        state.handle_read(1, 3, 600);
        let fd = state.fd_state.get(&(1, 3)).unwrap();
        assert_eq!(fd.cursor, 1100);
        assert_eq!(fd.chunks_read, BTreeSet::from([0, 1]));

        // Write 100 bytes (cursor 1100 → 1200)
        state.handle_write(1, 3, 100);
        let fd = state.fd_state.get(&(1, 3)).unwrap();
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
        let fd = state.fd_state.get(&(1, 3)).unwrap();
        assert!(fd.was_read);
        assert_eq!(fd.cursor, 0); // cursor unchanged
        assert_eq!(fd.chunks_read, BTreeSet::from([2]));
    }

    #[test]
    fn test_handle_lseek() {
        let mut state = TracerState::new(Some(1024));
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        state.handle_read(1, 3, 500);
        assert_eq!(state.fd_state.get(&(1, 3)).unwrap().cursor, 500);

        state.handle_lseek(1, 3, 2048);
        assert_eq!(state.fd_state.get(&(1, 3)).unwrap().cursor, 2048);

        state.handle_read(1, 3, 100);
        assert_eq!(state.fd_state.get(&(1, 3)).unwrap().cursor, 2148);
        assert_eq!(
            state.fd_state.get(&(1, 3)).unwrap().chunks_read,
            BTreeSet::from([0, 2])
        );
    }

    #[test]
    fn test_handle_dup() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_read(1, 3, 500); // advance cursor on fd 3

        state.handle_dup(1, 3, 7);
        assert_eq!(state.fd_table.get(&(1, 7)), Some(&"/tmp/test.txt".to_string()));
        // New fd gets independent cursor starting at 0
        assert_eq!(state.fd_state.get(&(1, 7)).unwrap().cursor, 0);
    }

    #[test]
    fn test_handle_clone() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_open(1, 4, "/tmp/other.txt".to_string(), 0);

        state.handle_clone(1, 2);
        assert!(state.active_pids.contains(&2));
        assert_eq!(state.fd_table.get(&(2, 3)), Some(&"/tmp/test.txt".to_string()));
        assert_eq!(state.fd_table.get(&(2, 4)), Some(&"/tmp/other.txt".to_string()));
    }

    #[test]
    fn test_build_report_aggregates_by_path() {
        let mut state = TracerState::new(Some(1024));
        state.start_time = 1000.0;
        state.end_time = 2000.0;

        // Two fds pointing to the same file
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        state.handle_open(1, 4, "/tmp/test.txt".to_string(), 0);

        state.handle_read(1, 3, 500);  // read via fd 3
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

        let fd = state.fd_state.get(&(1, 3)).unwrap();
        assert!(!fd.was_read);
        assert!(!fd.was_written);
        assert_eq!(fd.cursor, 0);
    }

    #[test]
    fn test_unknown_fd_operations_ignored() {
        let mut state = TracerState::new(None);
        // These should not panic — just silently ignore unknown fds
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
}
