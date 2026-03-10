use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use tracer_schema::FileRecord;

/// Per-fd I/O tracking state.
#[derive(Debug, Clone)]
pub struct FdState {
    pub path: String,
    pub cursor: u64,
    pub was_read: bool,
    pub was_written: bool,
    pub read_threads: BTreeSet<u32>,
    pub written_threads: BTreeSet<u32>,
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
            read_threads: BTreeSet::new(),
            written_threads: BTreeSet::new(),
            chunks_read: BTreeSet::new(),
            chunks_written: BTreeSet::new(),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct FileSummary {
    pub files: Vec<FileRecord>,
    pub opened_files: Vec<String>,
    pub read_files: Vec<String>,
    pub written_files: Vec<String>,
}

/// Shared fd/path tracking used by ptrace and eBPF tracers.
#[derive(Debug)]
pub struct FdTracker {
    /// (pid, fd) -> file path
    pub fd_table: HashMap<(u32, i32), String>,
    /// (pid, fd) -> I/O state for currently-open fds
    pub fd_state: HashMap<(u32, i32), FdState>,
    /// Completed fd states (from fds that were closed and then reused).
    pub closed_states: Vec<FdState>,
    /// Chunk size for I/O tracking. None = whole-file granularity.
    pub chunk_size: Option<u64>,
    /// Paths tracked without a live fd (e.g. rename destinations in ptrace mode).
    pub extra_opened_paths: HashSet<String>,
    pub extra_read_paths: HashSet<String>,
    pub extra_written_paths: HashSet<String>,
    pub extra_read_threads: HashMap<String, BTreeSet<u32>>,
    pub extra_written_threads: HashMap<String, BTreeSet<u32>>,
}

impl FdTracker {
    pub fn new(chunk_size: Option<u64>) -> Self {
        Self {
            fd_table: HashMap::new(),
            fd_state: HashMap::new(),
            closed_states: Vec::new(),
            chunk_size,
            extra_opened_paths: HashSet::new(),
            extra_read_paths: HashSet::new(),
            extra_written_paths: HashSet::new(),
            extra_read_threads: HashMap::new(),
            extra_written_threads: HashMap::new(),
        }
    }

    pub fn path_for_fd(&self, pid: u32, fd: i32) -> Option<&String> {
        self.fd_table.get(&(pid, fd))
    }

    /// Handle a successful open.
    pub fn handle_open(&mut self, pid: u32, fd: i32, path: String, _flags: u64) {
        if let Some(old) = self.fd_state.remove(&(pid, fd)) {
            self.closed_states.push(old);
        }
        self.fd_table.insert((pid, fd), path.clone());
        self.fd_state.insert((pid, fd), FdState::new(path));
    }

    /// Handle a successful close.
    pub fn handle_close(&mut self, pid: u32, fd: i32) {
        self.fd_table.remove(&(pid, fd));
        if let Some(old) = self.fd_state.remove(&(pid, fd)) {
            self.closed_states.push(old);
        }
    }

    /// Mark that the fd's path was read (without cursor/chunk accounting).
    pub fn mark_read(&mut self, pid: u32, fd: i32) {
        self.mark_read_internal(pid, fd, None);
    }

    pub fn mark_read_with_thread(&mut self, pid: u32, fd: i32, thread_id: u32) {
        self.mark_read_internal(pid, fd, Some(thread_id));
    }

    fn mark_read_internal(&mut self, pid: u32, fd: i32, thread_id: Option<u32>) {
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_read = true;
            self.extra_read_paths.insert(state.path.clone());
            if let Some(thread_id) = thread_id {
                state.read_threads.insert(thread_id);
                self.extra_read_threads
                    .entry(state.path.clone())
                    .or_default()
                    .insert(thread_id);
            }
        }
    }

    /// Mark that the fd's path was written (without cursor/chunk accounting).
    pub fn mark_written(&mut self, pid: u32, fd: i32) {
        self.mark_written_internal(pid, fd, None);
    }

    pub fn mark_written_with_thread(&mut self, pid: u32, fd: i32, thread_id: u32) {
        self.mark_written_internal(pid, fd, Some(thread_id));
    }

    fn mark_written_internal(&mut self, pid: u32, fd: i32, thread_id: Option<u32>) {
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_written = true;
            self.extra_written_paths.insert(state.path.clone());
            if let Some(thread_id) = thread_id {
                state.written_threads.insert(thread_id);
                self.extra_written_threads
                    .entry(state.path.clone())
                    .or_default()
                    .insert(thread_id);
            }
        }
    }

    /// Mark a path as opened/read/written without requiring an fd record.
    pub fn mark_path_open(&mut self, path: String) {
        if !path.is_empty() {
            self.extra_opened_paths.insert(path);
        }
    }

    pub fn mark_path_read(&mut self, path: String) {
        self.mark_path_read_internal(path, None);
    }

    pub fn mark_path_read_with_thread(&mut self, path: String, thread_id: u32) {
        self.mark_path_read_internal(path, Some(thread_id));
    }

    fn mark_path_read_internal(&mut self, path: String, thread_id: Option<u32>) {
        if !path.is_empty() {
            self.extra_read_paths.insert(path.clone());
            if let Some(thread_id) = thread_id {
                self.extra_read_threads.entry(path).or_default().insert(thread_id);
            }
        }
    }

    pub fn mark_path_written(&mut self, path: String) {
        self.mark_path_written_internal(path, None);
    }

    pub fn mark_path_written_with_thread(&mut self, path: String, thread_id: u32) {
        self.mark_path_written_internal(path, Some(thread_id));
    }

    fn mark_path_written_internal(&mut self, path: String, thread_id: Option<u32>) {
        if !path.is_empty() {
            self.extra_written_paths.insert(path.clone());
            if let Some(thread_id) = thread_id {
                self.extra_written_threads
                    .entry(path)
                    .or_default()
                    .insert(thread_id);
            }
        }
    }

    /// Handle sequential read.
    pub fn handle_read(&mut self, pid: u32, fd: i32, bytes: u64) {
        self.handle_read_internal(pid, fd, bytes, None);
    }

    pub fn handle_read_with_thread(
        &mut self,
        pid: u32,
        fd: i32,
        bytes: u64,
        thread_id: u32,
    ) {
        self.handle_read_internal(pid, fd, bytes, Some(thread_id));
    }

    fn handle_read_internal(
        &mut self,
        pid: u32,
        fd: i32,
        bytes: u64,
        thread_id: Option<u32>,
    ) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_read = true;
            self.extra_read_paths.insert(state.path.clone());
            if let Some(thread_id) = thread_id {
                state.read_threads.insert(thread_id);
                self.extra_read_threads
                    .entry(state.path.clone())
                    .or_default()
                    .insert(thread_id);
            }
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_read, state.cursor, bytes, chunk_size);
            }
            state.cursor += bytes;
        }
    }

    /// Handle positional read.
    pub fn handle_pread(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        self.handle_pread_internal(pid, fd, offset, bytes, None);
    }

    pub fn handle_pread_with_thread(
        &mut self,
        pid: u32,
        fd: i32,
        offset: u64,
        bytes: u64,
        thread_id: u32,
    ) {
        self.handle_pread_internal(pid, fd, offset, bytes, Some(thread_id));
    }

    fn handle_pread_internal(
        &mut self,
        pid: u32,
        fd: i32,
        offset: u64,
        bytes: u64,
        thread_id: Option<u32>,
    ) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_read = true;
            self.extra_read_paths.insert(state.path.clone());
            if let Some(thread_id) = thread_id {
                state.read_threads.insert(thread_id);
                self.extra_read_threads
                    .entry(state.path.clone())
                    .or_default()
                    .insert(thread_id);
            }
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_read, offset, bytes, chunk_size);
            }
        }
    }

    /// Handle sequential write.
    pub fn handle_write(&mut self, pid: u32, fd: i32, bytes: u64) {
        self.handle_write_internal(pid, fd, bytes, None);
    }

    pub fn handle_write_with_thread(
        &mut self,
        pid: u32,
        fd: i32,
        bytes: u64,
        thread_id: u32,
    ) {
        self.handle_write_internal(pid, fd, bytes, Some(thread_id));
    }

    fn handle_write_internal(
        &mut self,
        pid: u32,
        fd: i32,
        bytes: u64,
        thread_id: Option<u32>,
    ) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_written = true;
            self.extra_written_paths.insert(state.path.clone());
            if let Some(thread_id) = thread_id {
                state.written_threads.insert(thread_id);
                self.extra_written_threads
                    .entry(state.path.clone())
                    .or_default()
                    .insert(thread_id);
            }
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_written, state.cursor, bytes, chunk_size);
            }
            state.cursor += bytes;
        }
    }

    /// Handle positional write.
    pub fn handle_pwrite(&mut self, pid: u32, fd: i32, offset: u64, bytes: u64) {
        self.handle_pwrite_internal(pid, fd, offset, bytes, None);
    }

    pub fn handle_pwrite_with_thread(
        &mut self,
        pid: u32,
        fd: i32,
        offset: u64,
        bytes: u64,
        thread_id: u32,
    ) {
        self.handle_pwrite_internal(pid, fd, offset, bytes, Some(thread_id));
    }

    fn handle_pwrite_internal(
        &mut self,
        pid: u32,
        fd: i32,
        offset: u64,
        bytes: u64,
        thread_id: Option<u32>,
    ) {
        if bytes == 0 {
            return;
        }
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.was_written = true;
            self.extra_written_paths.insert(state.path.clone());
            if let Some(thread_id) = thread_id {
                state.written_threads.insert(thread_id);
                self.extra_written_threads
                    .entry(state.path.clone())
                    .or_default()
                    .insert(thread_id);
            }
            if let Some(chunk_size) = self.chunk_size {
                mark_chunks(&mut state.chunks_written, offset, bytes, chunk_size);
            }
        }
    }

    /// Handle lseek.
    pub fn handle_lseek(&mut self, pid: u32, fd: i32, new_offset: u64) {
        if let Some(state) = self.fd_state.get_mut(&(pid, fd)) {
            state.cursor = new_offset;
        }
    }

    /// Handle dup.
    pub fn handle_dup(&mut self, pid: u32, old_fd: i32, new_fd: i32) {
        // dup2/dup3 with identical fds is a no-op.
        if old_fd == new_fd {
            return;
        }

        // dup2/dup3 semantics: if new_fd is open, it is closed first.
        self.fd_table.remove(&(pid, new_fd));
        if let Some(old) = self.fd_state.remove(&(pid, new_fd)) {
            self.closed_states.push(old);
        }

        if let Some(path) = self.fd_table.get(&(pid, old_fd)).cloned() {
            self.fd_table.insert((pid, new_fd), path.clone());
            self.fd_state.insert((pid, new_fd), FdState::new(path));
        }
    }

    /// Handle clone/fork by copying the parent's fd map into child.
    pub fn handle_clone(&mut self, parent_pid: u32, child_pid: u32) {
        let parent_entries: Vec<_> = self
            .fd_table
            .iter()
            .filter(|((pid, _), _)| *pid == parent_pid)
            .map(|((_, fd), path)| (*fd, path.clone()))
            .collect();

        for (fd, path) in parent_entries {
            self.fd_table.insert((child_pid, fd), path.clone());
            self.fd_state.insert((child_pid, fd), FdState::new(path));
        }
    }

    /// Aggregate per-fd state into deterministic per-path report data.
    pub fn build_summary(&self) -> FileSummary {
        let mut path_map: BTreeMap<
            String,
            (
                bool,
                bool,
                BTreeSet<u32>,
                BTreeSet<u32>,
                BTreeSet<u64>,
                BTreeSet<u64>,
            ),
        > = BTreeMap::new();

        for state in self.fd_state.values().chain(self.closed_states.iter()) {
            if state.path.is_empty() {
                continue;
            }
            let entry = path_map
                .entry(state.path.clone())
                .or_insert_with(|| {
                    (
                        false,
                        false,
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                    )
                });
            entry.0 |= state.was_read;
            entry.1 |= state.was_written;
            entry.2.extend(&state.read_threads);
            entry.3.extend(&state.written_threads);
            entry.4.extend(&state.chunks_read);
            entry.5.extend(&state.chunks_written);
        }

        for path in &self.extra_opened_paths {
            if !path.is_empty() {
                path_map.entry(path.clone()).or_insert_with(|| {
                    (
                        false,
                        false,
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                    )
                });
            }
        }
        for path in &self.extra_read_paths {
            if path.is_empty() {
                continue;
            }
            let entry = path_map
                .entry(path.clone())
                .or_insert_with(|| {
                    (
                        false,
                        false,
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                    )
                });
            entry.0 = true;
            if let Some(threads) = self.extra_read_threads.get(path) {
                entry.2.extend(threads);
            }
        }
        for path in &self.extra_written_paths {
            if path.is_empty() {
                continue;
            }
            let entry = path_map
                .entry(path.clone())
                .or_insert_with(|| {
                    (
                        false,
                        false,
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                        BTreeSet::new(),
                    )
                });
            entry.1 = true;
            if let Some(threads) = self.extra_written_threads.get(path) {
                entry.3.extend(threads);
            }
        }

        let files: Vec<FileRecord> = path_map
            .into_iter()
            .map(|(path, (read, written, read_threads, written_threads, chunks_r, chunks_w))| {
                let read_threads = if read_threads.is_empty() {
                    None
                } else {
                    Some(read_threads.into_iter().collect())
                };
                let written_threads = if written_threads.is_empty() {
                    None
                } else {
                    Some(written_threads.into_iter().collect())
                };
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
                    read_threads,
                    written_threads,
                    chunks_read,
                    chunks_written,
                }
            })
            .collect();

        let opened_files: Vec<String> = files.iter().map(|f| f.path.clone()).collect();
        let read_files: Vec<String> = files
            .iter()
            .filter(|f| f.read)
            .map(|f| f.path.clone())
            .collect();
        let written_files: Vec<String> = files
            .iter()
            .filter(|f| f.written)
            .map(|f| f.path.clone())
            .collect();

        FileSummary {
            files,
            opened_files,
            read_files,
            written_files,
        }
    }
}

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_mark_chunks_boundary_crossing() {
        let mut chunks = BTreeSet::new();
        mark_chunks(&mut chunks, 1023, 2, 1024);
        assert_eq!(chunks, BTreeSet::from([0, 1]));
    }

    #[test]
    fn test_handle_open_close_and_summary() {
        let mut tracker = FdTracker::new(None);
        tracker.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);
        tracker.handle_read(1, 3, 64);
        tracker.handle_close(1, 3);

        let summary = tracker.build_summary();
        assert_eq!(summary.files.len(), 1);
        assert_eq!(summary.files[0].path, "/tmp/test.txt");
        assert!(summary.files[0].read);
        assert!(!summary.files[0].written);
        assert_eq!(summary.opened_files, vec!["/tmp/test.txt".to_string()]);
        assert!(!tracker.fd_state.contains_key(&(1, 3)));
    }

    #[test]
    fn test_summary_preserves_thread_ids_per_file_direction() {
        let mut tracker = FdTracker::new(None);
        tracker.handle_open(7, 3, "/tmp/threaded.txt".to_string(), 0);
        tracker.handle_read_with_thread(7, 3, 16, 101);
        tracker.handle_write_with_thread(7, 3, 8, 202);
        tracker.handle_close(7, 3);

        let summary = tracker.build_summary();
        assert_eq!(summary.files.len(), 1);
        assert_eq!(summary.files[0].path, "/tmp/threaded.txt");
        assert_eq!(summary.files[0].read_threads, Some(vec![101]));
        assert_eq!(summary.files[0].written_threads, Some(vec![202]));
    }

    #[test]
    fn test_handle_dup_untracked_source_clears_target_fd() {
        let mut tracker = FdTracker::new(None);
        tracker.handle_open(1, 3, "/tmp/repo.txt".to_string(), 0);
        tracker.handle_read(1, 3, 16);
        tracker.handle_close(1, 3);

        tracker.handle_open(1, 9, "/tmp/other.txt".to_string(), 0);
        tracker.handle_write(1, 9, 8);

        // old_fd is untracked; dup2/dup3 should still close new_fd.
        tracker.handle_dup(1, 99, 9);

        assert!(!tracker.fd_table.contains_key(&(1, 9)));
        assert!(!tracker.fd_state.contains_key(&(1, 9)));

        let summary = tracker.build_summary();
        let written: Vec<_> = summary
            .files
            .iter()
            .filter(|f| f.written)
            .map(|f| f.path.as_str())
            .collect();
        assert!(written.contains(&"/tmp/other.txt"));
        assert!(!written.contains(&"/tmp/repo.txt"));
    }

    #[test]
    fn test_handle_dup_same_fd_is_noop() {
        let mut tracker = FdTracker::new(None);
        tracker.handle_open(1, 4, "/tmp/same.txt".to_string(), 0);
        tracker.handle_dup(1, 4, 4);
        assert_eq!(
            tracker.fd_table.get(&(1, 4)),
            Some(&"/tmp/same.txt".to_string())
        );
        assert!(tracker.fd_state.contains_key(&(1, 4)));
    }

    #[test]
    fn test_mark_path_written_without_fd() {
        let mut tracker = FdTracker::new(None);
        tracker.mark_path_written("/tmp/renamed.txt".to_string());
        let summary = tracker.build_summary();
        assert_eq!(summary.files.len(), 1);
        assert_eq!(summary.files[0].path, "/tmp/renamed.txt");
        assert!(summary.files[0].written);
    }
}
