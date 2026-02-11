use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use log::info;

use crate::ipc;

/// Try to connect to an already-running daemon.
pub fn connect_to_daemon() -> Result<UnixStream> {
    let path = ipc::socket_path();
    UnixStream::connect(&path)
        .with_context(|| format!("failed to connect to daemon at {}", path.display()))
}

/// Connect to the daemon, auto-spawning it if necessary.
pub fn connect_or_spawn() -> Result<UnixStream> {
    let roard_path = find_roard_binary().context("roard binary not found")?;

    // First, try a direct connect
    if let Ok(stream) = connect_to_daemon() {
        return Ok(stream);
    }

    let sock_path = ipc::socket_path();
    let pid_path = sock_path.with_extension("pid");

    // Check for stale socket
    if sock_path.exists() {
        let is_stale = if pid_path.exists() {
            match std::fs::read_to_string(&pid_path) {
                Ok(pid_str) => match pid_str.trim().parse::<u32>() {
                    Ok(pid) => !PathBuf::from(format!("/proc/{pid}")).exists(),
                    Err(_) => true,
                },
                Err(_) => true,
            }
        } else {
            // No PID file — try connecting to see if it's alive
            UnixStream::connect(&sock_path).is_err()
        };

        if is_stale {
            info!("removing stale socket: {}", sock_path.display());
            let _ = std::fs::remove_file(&sock_path);
            let _ = std::fs::remove_file(&pid_path);
        }
    }

    // Spawn roard as a detached process
    info!("spawning roard: {}", roard_path.display());
    Command::new(&roard_path)
        .args(["--idle-timeout", "60"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("failed to spawn roard")?;

    // Poll for socket (up to 2s, 50ms intervals)
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        std::thread::sleep(Duration::from_millis(50));
        if let Ok(stream) = connect_to_daemon() {
            return Ok(stream);
        }
        if std::time::Instant::now() >= deadline {
            bail!("timed out waiting for roard to start");
        }
    }
}

/// Find the `roard` binary.
///
/// Looks in the same directory as the current executable first,
/// then falls back to searching PATH.
pub fn find_roard_binary() -> Option<PathBuf> {
    // Look next to the current executable
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let candidate = dir.join("roard");
            if candidate.exists() {
                return Some(candidate);
            }
        }
    }

    // Fall back to PATH
    which_in_path("roard")
}

/// Simple PATH lookup (avoids adding a `which` dependency).
fn which_in_path(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var("PATH").ok()?;
    for dir in path_var.split(':') {
        let candidate = PathBuf::from(dir).join(name);
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}
