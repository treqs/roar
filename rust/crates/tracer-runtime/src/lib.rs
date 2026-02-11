use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use tracer_schema::ProcessInfo;

pub fn timestamp_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

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

pub fn resolve_path_with_cache(
    raw_path: &str,
    pid: u32,
    cwd_cache: &mut HashMap<u32, String>,
) -> String {
    if raw_path.starts_with('/') {
        return raw_path.to_string();
    }

    let cwd = cwd_cache
        .entry(pid)
        .or_insert_with(|| {
            let cwd_path = format!("/proc/{pid}/cwd");
            std::fs::read_link(&cwd_path)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default()
        })
        .clone();

    if cwd.is_empty() {
        return raw_path.to_string();
    }

    let mut full_path = std::path::PathBuf::from(&cwd);
    full_path.push(raw_path);
    if let Ok(canonical) = full_path.canonicalize() {
        return canonical.to_string_lossy().to_string();
    }
    full_path.to_string_lossy().to_string()
}

pub fn resolve_path(raw_path: &str, pid: u32) -> String {
    if raw_path.starts_with('/') {
        return raw_path.to_string();
    }

    let cwd = std::fs::read_link(format!("/proc/{pid}/cwd"))
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();

    if cwd.is_empty() {
        return raw_path.to_string();
    }

    let full = format!("{cwd}/{raw_path}");
    std::fs::canonicalize(&full)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or(full)
}
