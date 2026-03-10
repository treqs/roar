use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use tracer_schema::TracerReport;

fn unique_temp_dir() -> PathBuf {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock drift")
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("roar-preload-test-{ts}"));
    fs::create_dir_all(&dir).expect("failed to create temp dir");
    dir
}

fn cargo_bin(name: &str) -> String {
    let underscore = name.replace('-', "_");
    let candidates = [
        format!("CARGO_BIN_EXE_{name}"),
        format!("CARGO_BIN_EXE_{underscore}"),
    ];
    for key in candidates {
        if let Ok(value) = std::env::var(&key) {
            return value;
        }
    }

    for debug_dir in target_debug_dirs() {
        let fallback = debug_dir.join(name);
        if fallback.exists() {
            return fallback.to_string_lossy().into_owned();
        }
    }

    panic!("missing cargo binary env var for {name} and fallback path not found");
}

fn target_debug_dirs() -> Vec<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut dirs = Vec::new();

    if let Ok(target_dir) = std::env::var("CARGO_TARGET_DIR") {
        dirs.push(PathBuf::from(target_dir).join("debug"));
    }

    dirs.push(manifest_dir.join("target").join("debug"));
    dirs.push(manifest_dir.join("rust").join("target").join("debug"));
    dirs.push(manifest_dir.join("..").join("target").join("debug"));
    dirs.push(
        manifest_dir
            .join("..")
            .join("rust")
            .join("target")
            .join("debug"),
    );
    dirs.push(
        manifest_dir
            .join("..")
            .join("..")
            .join("target")
            .join("debug"),
    );
    dirs.push(
        manifest_dir
            .join("..")
            .join("..")
            .join("rust")
            .join("target")
            .join("debug"),
    );
    dirs.push(
        manifest_dir
            .join("..")
            .join("..")
            .join("..")
            .join("target")
            .join("debug"),
    );
    dirs.push(
        manifest_dir
            .join("..")
            .join("..")
            .join("..")
            .join("rust")
            .join("target")
            .join("debug"),
    );

    dirs
}

fn preload_lib() -> String {
    let mut candidates: Vec<PathBuf> = Vec::new();
    for debug_dir in target_debug_dirs() {
        let direct = [
            debug_dir.join("libroar_tracer_preload.dylib"),
            debug_dir.join("libroar-tracer-preload.dylib"),
            debug_dir.join("libroar_tracer_preload.so"),
            debug_dir.join("libroar-tracer-preload.so"),
        ];
        for path in direct {
            if path.exists() {
                candidates.push(path);
            }
        }

        let deps_dir = debug_dir.join("deps");
        if deps_dir.exists() {
            if let Ok(entries) = std::fs::read_dir(&deps_dir) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    let Some(name) = path.file_name().and_then(|s| s.to_str()) else {
                        continue;
                    };
                    let is_match = (name.starts_with("libroar_tracer_preload")
                        || name.starts_with("libroar-tracer-preload"))
                        && (name.ends_with(".dylib") || name.ends_with(".so"));
                    if is_match {
                        candidates.push(path);
                    }
                }
            }
        }
    }

    candidates.sort_by_key(|path| {
        std::fs::metadata(path)
            .and_then(|meta| meta.modified())
            .ok()
    });
    if let Some(path) = candidates.pop() {
        return path.to_string_lossy().into_owned();
    }

    panic!("preload interposer library not found in target/debug");
}

#[test]
fn standalone_preload_tracer_emits_msgpack_report() {
    let tracer_bin = cargo_bin("roar-tracer-preload");
    let fixture_bin = cargo_bin("io_fixture");

    let temp_dir = unique_temp_dir();
    let trace_path = temp_dir.join("trace.msgpack");
    let target_path = temp_dir.join("target.txt");
    let target_path_arg = target_path.to_string_lossy().into_owned();

    let status = Command::new(&tracer_bin)
        .arg(&trace_path)
        .arg(&fixture_bin)
        .arg(&target_path_arg)
        .env("ROAR_PRELOAD_LIB", preload_lib())
        .status()
        .expect("failed to run preload tracer");

    assert!(status.success(), "preload tracer failed: {status}");
    assert!(trace_path.exists(), "expected trace report to exist");

    let bytes = fs::read(&trace_path).expect("failed to read trace report");
    let report: TracerReport = rmp_serde::from_slice(&bytes).expect("invalid msgpack report");

    assert_eq!(report.tracer_mode, "preload");
    assert!(report.end_time >= report.start_time);
    assert!(
        !report.processes.is_empty(),
        "report should include at least one process"
    );

    assert!(
        !report.files.is_empty(),
        "report should include file records"
    );

    // On macOS, F_GETPATH returns canonical paths (/private/var/...) while std::env::temp_dir()
    // returns /var/... — check both the original and canonicalized path.
    let target_canonical = fs::canonicalize(&target_path)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| target_path_arg.clone());
    let target_file = report
        .files
        .iter()
        .find(|f| f.path == target_canonical || f.path == target_path_arg);
    assert!(
        target_file.is_some(),
        "target file path should appear in file records; got: {:?}",
        report.files.iter().map(|f| &f.path).collect::<Vec<_>>()
    );
    let target_file = target_file.unwrap();
    assert!(
        target_file.read,
        "target file should be marked as read"
    );
    assert!(
        target_file.written,
        "target file should be marked as written"
    );

    let _ = fs::remove_file(trace_path);
    let _ = fs::remove_file(target_path);
    let _ = fs::remove_dir(temp_dir);
}
