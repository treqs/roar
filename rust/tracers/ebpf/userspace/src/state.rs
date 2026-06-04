use std::collections::{HashMap, HashSet};

use tracer_fd::FdTracker;
use tracer_runtime::{
    build_tracer_report, capture_process_info as capture_proc_info, resolve_path_with_cache,
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

    /// Per-PID CWD cache, populated eagerly so relative path resolution
    /// survives the tracee's exit. Without this, fast workloads like
    /// `bash -c 'echo > x'` race against `/proc/<pid>/cwd` becoming
    /// ENOENT after exit and silently fall back to the raw relative path.
    pub cwd_cache: HashMap<u32, String>,

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
            cwd_cache: HashMap::new(),
            chunk_size,
            events_dropped: 0,
            start_time: 0.0,
            end_time: 0.0,
        }
    }

    /// Pre-populate the CWD cache for a PID by reading `/proc/<pid>/cwd`.
    /// Best done while the process is still alive (e.g. at register time
    /// when the BPF probe pattern SIGSTOPs the child before exec).
    pub fn cache_cwd(&mut self, pid: u32) {
        if self.cwd_cache.contains_key(&pid) {
            return;
        }
        if let Ok(cwd) = std::fs::read_link(format!("/proc/{pid}/cwd")) {
            self.cwd_cache
                .insert(pid, cwd.to_string_lossy().into_owned());
        }
    }

    /// Inherit the parent's cached CWD into the child PID. Called when a
    /// Clone event is processed.
    pub fn inherit_cwd(&mut self, parent_pid: u32, child_pid: u32) {
        if let Some(cwd) = self.cwd_cache.get(&parent_pid).cloned() {
            self.cwd_cache.insert(child_pid, cwd);
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

    pub fn handle_read_with_thread(&mut self, pid: u32, fd: i32, bytes: u64, thread_id: u32) {
        self.fd.handle_read_with_thread(pid, fd, bytes, thread_id);
    }

    pub fn handle_pread(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        self.fd.handle_pread(pid, fd, offset, bytes);
    }

    pub fn handle_pread_with_thread(
        &mut self,
        pid: u32,
        fd: i32,
        offset: u64,
        bytes: u64,
        thread_id: u32,
    ) {
        self.fd
            .handle_pread_with_thread(pid, fd, offset, bytes, thread_id);
    }

    pub fn handle_write(&mut self, pid: u32, fd: i32, bytes: u64) {
        self.fd.handle_write(pid, fd, bytes);
    }

    pub fn handle_write_with_thread(&mut self, pid: u32, fd: i32, bytes: u64, thread_id: u32) {
        self.fd.handle_write_with_thread(pid, fd, bytes, thread_id);
    }

    pub fn handle_pwrite(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        self.fd.handle_pwrite(pid, fd, offset, bytes);
    }

    pub fn handle_pwrite_with_thread(
        &mut self,
        pid: u32,
        fd: i32,
        offset: u64,
        bytes: u64,
        thread_id: u32,
    ) {
        self.fd
            .handle_pwrite_with_thread(pid, fd, offset, bytes, thread_id);
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

    pub fn mark_path_written_with_thread(&mut self, path: String, thread_id: u32) {
        self.fd.mark_path_written_with_thread(path, thread_id);
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

/// Resolve a path from a BPF event using a per-PID CWD cache. The cache
/// must be populated *before* the tracee exits (otherwise `/proc/<pid>/cwd`
/// returns ENOENT and the path is left unresolved). The daemon registers
/// the cache entry at register-time when the PID is SIGSTOP'd, and
/// inherits to children on Clone events.
pub fn resolve_path(pid: u32, raw_path: &str, cwd_cache: &mut HashMap<u32, String>) -> String {
    resolve_path_with_cache(raw_path, pid, cwd_cache)
}

/// Resolve an *at syscall path relative to its dirfd when possible.
///
/// `dirfd` of `AT_FDCWD` (or `u64::MAX`, used as a sentinel) routes through
/// the CWD cache. Other valid fds are looked up via `/proc/<pid>/fd/<n>`,
/// which only works while the tracee is alive — relative-to-fd paths
/// captured this way still race against process exit. Eager fd→path
/// caching in [`FdTracker`] is what saves us when the tracee has gone.
pub fn resolve_at_path(
    pid: u32,
    dirfd: u64,
    raw_path: &str,
    cwd_cache: &mut HashMap<u32, String>,
) -> String {
    let dirfd_i32 = dirfd as i32;
    if raw_path.starts_with('/') || dirfd == u64::MAX || dirfd_i32 == libc::AT_FDCWD {
        return resolve_path(pid, raw_path, cwd_cache);
    }

    if dirfd_i32 >= 0 {
        let fd_link = format!("/proc/{pid}/fd/{dirfd_i32}");
        if let Ok(base) = std::fs::read_link(fd_link) {
            let full_path = base.join(raw_path);
            if let Ok(canonical) = full_path.canonicalize() {
                return canonical.to_string_lossy().into_owned();
            }
            return full_path.to_string_lossy().into_owned();
        }
    }

    resolve_path(pid, raw_path, cwd_cache)
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
        state.handle_read(1, 3, 10);

        assert_eq!(
            state.fd.fd_table.get(&(1, 3)),
            Some(&"/tmp/test.txt".to_string())
        );
        assert!(state.fd.fd_state.contains_key(&(1, 3)));

        state.handle_close(1, 3);
        assert!(!state.fd.fd_table.contains_key(&(1, 3)));
        assert!(!state.fd.fd_state.contains_key(&(1, 3)));

        // Closed state is still included in report aggregation.
        let report = state.build_report();
        assert_eq!(report.files.len(), 1);
        assert_eq!(report.files[0].path, "/tmp/test.txt");
        assert!(report.files[0].read);
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
    fn test_handle_dup_untracked_source_closes_target_fd() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/repo.txt".to_string(), 0);
        state.handle_read(1, 3, 10);
        state.handle_close(1, 3);

        state.handle_open(1, 9, "/tmp/other.txt".to_string(), 0);
        state.handle_write(1, 9, 20);

        // old_fd is untracked; dup2/dup3 still closes new_fd.
        state.handle_dup(1, 99, 9);

        assert!(!state.fd.fd_table.contains_key(&(1, 9)));
        assert!(!state.fd.fd_state.contains_key(&(1, 9)));

        let report = state.build_report();
        let written_paths: Vec<&str> = report
            .files
            .iter()
            .filter(|f| f.written)
            .map(|f| f.path.as_str())
            .collect();
        assert!(written_paths.contains(&"/tmp/other.txt"));
        assert!(!written_paths.contains(&"/tmp/repo.txt"));
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
        let mut cache = HashMap::new();
        assert_eq!(
            resolve_path(1, "/absolute/path", &mut cache),
            "/absolute/path"
        );
    }

    #[test]
    fn test_resolve_path_relative_uses_cwd_cache() {
        let mut cache = HashMap::new();
        cache.insert(42u32, "/home/user/project".to_string());
        // canonicalize will fail (path doesn't exist) so we get the
        // joined form back unchanged.
        let resolved = resolve_path(42, "data.csv", &mut cache);
        assert_eq!(resolved, "/home/user/project/data.csv");
    }

    #[test]
    fn test_inherit_cwd_propagates_to_child() {
        let mut state = TracerState::new(None);
        state.cwd_cache.insert(100, "/work".to_string());
        state.inherit_cwd(100, 101);
        assert_eq!(state.cwd_cache.get(&101), Some(&"/work".to_string()));
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
