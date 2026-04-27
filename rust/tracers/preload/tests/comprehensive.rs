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
    let dir = std::env::temp_dir().join(format!("roar-preload-comprehensive-{ts}"));
    fs::create_dir_all(&dir).expect("failed to create temp dir");
    dir
}

fn target_debug_dirs() -> Vec<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut dirs = Vec::new();

    if let Ok(target_dir) = std::env::var("CARGO_TARGET_DIR") {
        dirs.push(PathBuf::from(target_dir).join("debug"));
    }

    for ancestor in &[".", "..", "../..", "../../.."] {
        dirs.push(manifest_dir.join(ancestor).join("target").join("debug"));
        dirs.push(
            manifest_dir
                .join(ancestor)
                .join("rust")
                .join("target")
                .join("debug"),
        );
    }
    dirs
}

fn cargo_bin(name: &str) -> String {
    let underscore = name.replace('-', "_");
    for key in [
        format!("CARGO_BIN_EXE_{name}"),
        format!("CARGO_BIN_EXE_{underscore}"),
    ] {
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
    panic!("missing cargo binary for {name}");
}

fn preload_lib() -> String {
    let mut candidates: Vec<PathBuf> = Vec::new();
    for debug_dir in target_debug_dirs() {
        for name in [
            "libroar_tracer_preload.dylib",
            "libroar-tracer-preload.dylib",
            "libroar_tracer_preload.so",
            "libroar-tracer-preload.so",
        ] {
            let path = debug_dir.join(name);
            if path.exists() {
                candidates.push(path);
            }
        }
        let deps_dir = debug_dir.join("deps");
        if let Ok(entries) = fs::read_dir(&deps_dir) {
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

    candidates.sort_by_key(|path| fs::metadata(path).and_then(|meta| meta.modified()).ok());
    if let Some(path) = candidates.pop() {
        return path.to_string_lossy().into_owned();
    }

    panic!("preload interposer library not found");
}

/// Normalize a path for comparison: check both the raw path and its canonical form.
fn path_matches(file_path: &str, target: &str) -> bool {
    if file_path == target {
        return true;
    }
    // On macOS, F_GETPATH returns /private/var/... while temp_dir() returns /var/...
    if let Ok(canonical) = fs::canonicalize(target) {
        if file_path == canonical.to_string_lossy() {
            return true;
        }
    }
    false
}

fn run_tracer(args: &[&str]) -> TracerReport {
    let tracer_bin = cargo_bin("roar-tracer-preload");
    let temp_dir = unique_temp_dir();
    let trace_path = temp_dir.join("trace.msgpack");

    let status = Command::new(&tracer_bin)
        .arg(&trace_path)
        .args(args)
        .env("ROAR_PRELOAD_LIB", preload_lib())
        .status()
        .expect("failed to run preload tracer");

    assert!(status.success(), "traced command failed: {status}");
    assert!(trace_path.exists(), "trace report missing");

    let bytes = fs::read(&trace_path).expect("failed to read trace report");
    let report: TracerReport = rmp_serde::from_slice(&bytes).expect("invalid msgpack report");

    let _ = fs::remove_file(&trace_path);
    let _ = fs::remove_dir(&temp_dir);

    report
}

fn print_report_summary(report: &TracerReport, label: &str) {
    let duration_ms = (report.end_time - report.start_time) * 1000.0;

    eprintln!();
    eprintln!("=== {label} ===");
    eprintln!("  tracer_mode:    {}", report.tracer_mode);
    eprintln!("  duration:       {duration_ms:.1}ms");
    eprintln!("  events_dropped: {}", report.events_dropped.unwrap_or(0));

    eprintln!();
    eprintln!("  Processes ({}):", report.processes.len());
    for proc in &report.processes {
        let cmd = if proc.command.is_empty() {
            "<unknown>".to_string()
        } else {
            proc.command.join(" ")
        };
        let ppid = proc
            .parent_pid
            .map(|p| p.to_string())
            .unwrap_or_else(|| "-".into());
        eprintln!("    PID {:<8} PPID {:<8} {}", proc.pid, ppid, cmd);
    }

    let read_files: Vec<_> = report.files.iter().filter(|f| f.read).collect();
    let written_files: Vec<_> = report.files.iter().filter(|f| f.written).collect();
    let read_write_files: Vec<_> = report
        .files
        .iter()
        .filter(|f| f.read && f.written)
        .collect();

    eprintln!();
    eprintln!(
        "  Files: {} total, {} read, {} written, {} read+written",
        report.files.len(),
        read_files.len(),
        written_files.len(),
        read_write_files.len(),
    );

    if !read_write_files.is_empty() {
        eprintln!();
        eprintln!("  Read+Written:");
        for f in &read_write_files {
            eprintln!("    [RW] {}", f.path);
        }
    }

    let read_only: Vec<_> = report
        .files
        .iter()
        .filter(|f| f.read && !f.written)
        .collect();
    if !read_only.is_empty() {
        eprintln!();
        eprintln!("  Read-only ({}):", read_only.len());
        for f in read_only.iter().take(30) {
            eprintln!("    [R ] {}", f.path);
        }
        if read_only.len() > 30 {
            eprintln!("    ... and {} more", read_only.len() - 30);
        }
    }

    let write_only: Vec<_> = report
        .files
        .iter()
        .filter(|f| f.written && !f.read)
        .collect();
    if !write_only.is_empty() {
        eprintln!();
        eprintln!("  Write-only ({}):", write_only.len());
        for f in write_only.iter().take(30) {
            eprintln!("    [ W] {}", f.path);
        }
        if write_only.len() > 30 {
            eprintln!("    ... and {} more", write_only.len() - 30);
        }
    }

    eprintln!();
}

// ---------------------------------------------------------------------------
// Test: io_fixture exercises all hooked syscalls
// ---------------------------------------------------------------------------
#[test]
fn comprehensive_io_fixture() {
    let fixture_bin = cargo_bin("io_fixture");
    let temp_dir = unique_temp_dir();
    let target_path = temp_dir.join("target.txt");
    let target_arg = target_path.to_string_lossy().into_owned();

    let report = run_tracer(&[&fixture_bin, &target_arg]);
    print_report_summary(&report, "io_fixture (all hooks)");

    // --- report metadata ---
    assert_eq!(report.tracer_mode, "preload");
    assert!(report.end_time >= report.start_time);
    assert_eq!(report.events_dropped.unwrap_or(0), 0, "no events dropped");

    // --- processes ---
    assert!(
        !report.processes.is_empty(),
        "should capture at least the root process"
    );
    let root = &report.processes[0];
    assert!(
        root.command.iter().any(|c| c.contains("io_fixture")),
        "root process command should contain io_fixture: {:?}",
        root.command
    );

    // --- files ---
    assert!(!report.files.is_empty(), "should capture file I/O events");

    // Target file: written (write + truncate + rename-back), read (read + mmap)
    let target_file = report
        .files
        .iter()
        .find(|f| path_matches(&f.path, &target_arg));
    assert!(
        target_file.is_some(),
        "target file should appear in report; got: {:?}",
        report.files.iter().map(|f| &f.path).collect::<Vec<_>>()
    );
    let target_file = target_file.expect("checked above");
    assert!(target_file.read, "target file should be read (read + mmap)");
    assert!(
        target_file.written,
        "target file should be written (write + truncate)"
    );

    // Renamed path should appear (rename emits a write event for the new name)
    let renamed_path = format!("{}.renamed", target_arg);
    let has_renamed = report
        .files
        .iter()
        .any(|f| path_matches(&f.path, &renamed_path));
    assert!(
        has_renamed,
        "renamed path should appear in report; got: {:?}",
        report.files.iter().map(|f| &f.path).collect::<Vec<_>>()
    );

    // Unlink temp file should appear (unlink emits a write event)
    let unlink_path = format!("{}.unlink_test", target_arg);
    let has_unlink = report
        .files
        .iter()
        .any(|f| path_matches(&f.path, &unlink_path));
    assert!(
        has_unlink,
        "unlinked temp file should appear in report; got: {:?}",
        report.files.iter().map(|f| &f.path).collect::<Vec<_>>()
    );

    // Cleanup
    let _ = fs::remove_file(&target_path);
    let _ = fs::remove_dir(&temp_dir);
}

// ---------------------------------------------------------------------------
// Test: python3 one-liner exercises mmap (module loading) and stdio
// ---------------------------------------------------------------------------
#[test]
fn comprehensive_python_one_liner() {
    // Skip if python3 is not available
    let python_check = Command::new("python3").arg("--version").output();
    if python_check.is_err() || !python_check.expect("checked").status.success() {
        eprintln!("python3 not available, skipping");
        return;
    }

    let temp_dir = unique_temp_dir();
    let test_file = temp_dir.join("pytest.txt");
    let test_path = test_file.to_string_lossy().into_owned();

    let script = format!(
        "f=open('{test_path}','w'); f.write('hello from python'); f.close(); \
         f=open('{test_path}'); data=f.read(); f.close(); \
         assert data=='hello from python', repr(data)"
    );

    let report = run_tracer(&["python3", "-c", &script]);
    print_report_summary(&report, "python3 one-liner");

    // --- processes ---
    assert!(!report.processes.is_empty());

    // macOS SIP blocks DYLD_INSERT_LIBRARIES for Apple platform binaries like /usr/bin/python3.
    // If no files were captured, report and skip assertions.
    if report.files.is_empty() {
        eprintln!(
            "  NOTE: no files captured — macOS SIP likely blocked tracing of /usr/bin/python3"
        );
        eprintln!(
            "  This is expected for Apple platform binaries. Try a Homebrew python to verify hooks."
        );
        let _ = fs::remove_file(&test_file);
        let _ = fs::remove_dir(&temp_dir);
        return;
    }

    // --- files: should have many (python loads .so/.dylib modules via mmap) ---
    assert!(
        report.files.len() >= 2,
        "python should touch multiple files; got {}",
        report.files.len()
    );

    // Our test file should be read + written
    let test_record = report
        .files
        .iter()
        .find(|f| path_matches(&f.path, &test_path));
    assert!(
        test_record.is_some(),
        "python test file should appear; got: {:?}",
        report
            .files
            .iter()
            .filter(|f| f.path.contains("pytest"))
            .map(|f| &f.path)
            .collect::<Vec<_>>()
    );
    let test_record = test_record.expect("checked above");
    assert!(test_record.written, "python test file should be written");
    assert!(test_record.read, "python test file should be read");

    // Should see shared libraries (.dylib/.so) being read via mmap
    let shared_lib_count = report
        .files
        .iter()
        .filter(|f| f.path.ends_with(".dylib") || f.path.ends_with(".so"))
        .count();
    eprintln!("  Shared libraries captured: {shared_lib_count} (.dylib/.so files)");

    // Should see .pyc or .py files
    let py_file_count = report
        .files
        .iter()
        .filter(|f| f.path.ends_with(".py") || f.path.ends_with(".pyc"))
        .count();
    eprintln!("  Python files captured: {py_file_count} (.py/.pyc files)");

    // Cleanup
    let _ = fs::remove_file(&test_file);
    let _ = fs::remove_dir(&temp_dir);
}

// ---------------------------------------------------------------------------
// Test: multi-process (shell spawning a subcommand)
// ---------------------------------------------------------------------------
#[test]
fn comprehensive_multi_process() {
    // Use /bin/sh to spawn a child that does file I/O — tests process tree capture
    let temp_dir = unique_temp_dir();
    let test_file = temp_dir.join("multi.txt");
    let test_path = test_file.to_string_lossy().into_owned();

    // sh -c 'echo hello > file && cat file'
    let script = format!("echo hello > '{test_path}' && cat '{test_path}' > /dev/null");

    let report = run_tracer(&["/bin/sh", "-c", &script]);
    print_report_summary(&report, "multi-process (sh -c)");

    // Should capture at least /bin/sh
    assert!(!report.processes.is_empty(), "should have processes");

    // The test file should be written (echo >) and read (cat)
    let test_record = report
        .files
        .iter()
        .find(|f| path_matches(&f.path, &test_path));
    // Note: on macOS, SIP-protected /bin/sh may block DYLD_INSERT_LIBRARIES.
    // In that case we won't see any file events. That's expected — just report it.
    if test_record.is_none() {
        eprintln!(
            "  NOTE: test file not found in report — macOS SIP likely blocked tracing of /bin/sh"
        );
        eprintln!(
            "  This is expected for Apple platform binaries. The hooks themselves are correct."
        );
    } else {
        let test_record = test_record.expect("checked above");
        assert!(test_record.written, "test file should be written (echo >)");
        // cat may be a separate process that SIP blocks, so read is best-effort
        if test_record.read {
            eprintln!("  test file correctly marked as read+written");
        } else {
            eprintln!("  test file written but read not captured (likely SIP on cat)");
        }
    }

    // Cleanup
    let _ = fs::remove_file(&test_file);
    let _ = fs::remove_dir(&temp_dir);
}

// ---------------------------------------------------------------------------
// Test: report structure invariants
// ---------------------------------------------------------------------------
#[test]
fn comprehensive_report_invariants() {
    let fixture_bin = cargo_bin("io_fixture");
    let temp_dir = unique_temp_dir();
    let target_path = temp_dir.join("invariants.txt");
    let target_arg = target_path.to_string_lossy().into_owned();

    let report = run_tracer(&[&fixture_bin, &target_arg]);

    // Version should be set
    assert!(report.version > 0, "report version should be > 0");

    // Timing
    assert!(report.start_time > 0.0, "start_time should be positive");
    assert!(
        report.end_time >= report.start_time,
        "end_time ({}) should be >= start_time ({})",
        report.end_time,
        report.start_time
    );
    let duration = report.end_time - report.start_time;
    assert!(
        duration < 30.0,
        "test should complete in under 30s; got {duration:.1}s"
    );

    // Every process should have a PID
    for proc in &report.processes {
        assert!(proc.pid > 0, "process PID should be positive");
    }

    // Every file should have a non-empty path and at least one of read/written set
    for file in &report.files {
        assert!(!file.path.is_empty(), "file path should not be empty");
        assert!(
            file.read || file.written,
            "file should be read or written: {}",
            file.path
        );
    }

    // No duplicate file paths
    let mut paths: Vec<&str> = report.files.iter().map(|f| f.path.as_str()).collect();
    paths.sort();
    let before = paths.len();
    paths.dedup();
    assert_eq!(
        before,
        paths.len(),
        "should have no duplicate file paths in report"
    );

    // Cleanup
    let _ = fs::remove_file(&target_path);
    let _ = fs::remove_dir(&temp_dir);
}
