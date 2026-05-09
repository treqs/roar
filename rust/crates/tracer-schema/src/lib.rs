use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum NativeTraceEvent {
    /// Bytes were observed flowing through a read syscall (or equivalent
    /// libc entry point hooked by the tracer).
    Read {
        pid: u32,
        thread_id: u32,
        path: String,
    },
    /// Bytes were observed flowing through a write syscall, or a path-
    /// publication operation (rename, unlink, link, truncate) succeeded
    /// — i.e. the destination file's content has actually changed.
    Write {
        pid: u32,
        thread_id: u32,
        path: String,
    },
    /// A successful `open(O_RDONLY)` / `fopen("r")` was observed, but
    /// no read bytes have been confirmed yet. Consumers decide whether
    /// to count this as Read or hold it pending a confirmed Read event.
    OpenRead {
        pid: u32,
        thread_id: u32,
        path: String,
    },
    /// A successful `open(O_WRONLY|O_CREAT|...)` / `fopen("w")` was
    /// observed, but no write bytes have been confirmed yet. Used for
    /// the LD_PRELOAD-via-shell-redirect case where the actual write
    /// syscall bypasses our hooks (bash's `echo > x` writes via
    /// libc-internal `__write`); the consumer's policy decides whether
    /// to credit this as a Write.
    OpenWrite {
        pid: u32,
        thread_id: u32,
        path: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProcessInfo {
    pub pid: u32,
    pub parent_pid: Option<u32>,
    pub command: Vec<String>,
    pub env: HashMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileRecord {
    pub path: String,
    pub read: bool,
    pub written: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub read_threads: Option<Vec<u32>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub written_threads: Option<Vec<u32>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunks_read: Option<Vec<u64>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunks_written: Option<Vec<u64>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TracerReport {
    pub version: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunk_size: Option<u64>,
    pub processes: Vec<ProcessInfo>,
    pub files: Vec<FileRecord>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub opened_files: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub read_files: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub written_files: Vec<String>,
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub env_accessed: HashMap<String, String>,
    pub start_time: f64,
    pub end_time: f64,
    pub tracer_mode: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub events_dropped: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The wire-format `kind` discriminant must match the snake_case
    /// rename rule. Hard-coding the expected JSON shape catches accidental
    /// breaking changes to the protocol used between the LD_PRELOAD'd
    /// library (in the tracee) and the parent daemon.
    #[test]
    fn native_trace_event_kind_discriminants_are_stable() {
        let events = [
            (
                NativeTraceEvent::Read {
                    pid: 1,
                    thread_id: 2,
                    path: "/tmp/x".into(),
                },
                "read",
            ),
            (
                NativeTraceEvent::Write {
                    pid: 1,
                    thread_id: 2,
                    path: "/tmp/x".into(),
                },
                "write",
            ),
            (
                NativeTraceEvent::OpenRead {
                    pid: 1,
                    thread_id: 2,
                    path: "/tmp/x".into(),
                },
                "open_read",
            ),
            (
                NativeTraceEvent::OpenWrite {
                    pid: 1,
                    thread_id: 2,
                    path: "/tmp/x".into(),
                },
                "open_write",
            ),
        ];
        for (event, expected_kind) in events {
            let json: serde_json::Value = serde_json::to_value(&event).expect("serialize");
            assert_eq!(
                json["kind"].as_str(),
                Some(expected_kind),
                "expected kind={expected_kind} for {event:?}"
            );
            // Round-trip must preserve the variant.
            let bytes = serde_json::to_vec(&event).expect("serialize");
            let decoded: NativeTraceEvent = serde_json::from_slice(&bytes).expect("decode");
            assert_eq!(decoded, event);
        }
    }
}
