use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum NativeTraceEvent {
    Read {
        pid: u32,
        thread_id: u32,
        path: String,
    },
    Write {
        pid: u32,
        thread_id: u32,
        path: String,
    },
    Fork {
        parent_pid: u32,
        child_pid: u32,
    },
    Exec {
        pid: u32,
        command: Vec<String>,
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
