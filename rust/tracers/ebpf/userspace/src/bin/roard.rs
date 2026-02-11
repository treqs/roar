use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::Duration;
use std::{fs, process};

fn main() {
    env_logger::init();

    let idle_timeout = parse_idle_timeout();
    let sock = parse_socket_path().unwrap_or_else(roar_tracer_ebpf::ipc::socket_path);

    // Stale socket check: if socket exists, try to connect.
    // If connect succeeds, another instance is running.
    // If connect fails, the socket is stale — remove it.
    if sock.exists() {
        if UnixStream::connect(&sock).is_err() {
            let _ = fs::remove_file(&sock);
            let _ = fs::remove_file(sock.with_extension("pid"));
        } else {
            eprintln!(
                "roard: another instance is already running on {}",
                sock.display()
            );
            process::exit(1);
        }
    }

    roar_tracer_ebpf::daemon::run_daemon(sock, idle_timeout).unwrap_or_else(|e| {
        eprintln!("roard: {e:#}");
        process::exit(1);
    });
}

fn parse_idle_timeout() -> Duration {
    let args: Vec<String> = std::env::args().collect();
    for i in 0..args.len() {
        if args[i] == "--idle-timeout" {
            if let Some(val) = args.get(i + 1) {
                if let Ok(secs) = val.parse::<u64>() {
                    return Duration::from_secs(secs);
                }
            }
        }
    }
    Duration::from_secs(60)
}

fn parse_socket_path() -> Option<PathBuf> {
    let args: Vec<String> = std::env::args().collect();
    for i in 0..args.len() {
        if args[i] == "--socket" {
            if let Some(val) = args.get(i + 1) {
                return Some(PathBuf::from(val));
            }
        }
    }
    None
}
