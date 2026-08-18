use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use serde::{de::DeserializeOwned, Deserialize, Serialize};

use crate::state::TracerOutput;

/// Maximum IPC message payload size (16 MB).
const MAX_PAYLOAD_SIZE: u32 = 16 * 1024 * 1024;

// ── Protocol messages ────────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Debug, PartialEq)]
#[serde(tag = "type")]
pub enum ClientMessage {
    Register {
        run_id: u64,
        root_pid: u32,
        /// The command the client was asked to run. Authoritative for the root
        /// process, which /proc reports post-exec. Defaulted so a client and
        /// daemon of different versions still speak to each other -- the wire
        /// format is field-named MessagePack, so the extra key is ignored by an
        /// older daemon and absent-means-empty for an older client.
        #[serde(default)]
        root_command: Vec<String>,
    },
    Deregister {
        run_id: u64,
    },
    GetReport {
        run_id: u64,
    },
    Ping,
}

#[derive(Serialize, Deserialize, Debug)]
#[serde(tag = "type")]
pub enum DaemonMessage {
    Ack { run_id: u64 },
    Report { run_id: u64, data: TracerOutput },
    Error { run_id: u64, message: String },
    Pong,
}

// ── Length-prefixed MessagePack framing ───────────────────────────────────────

/// Send a message as length-prefixed MessagePack over a Unix stream.
///
/// Wire format: `[u32 big-endian: payload length] [payload: MessagePack bytes]`
pub fn send_message<T: Serialize>(stream: &mut UnixStream, msg: &T) -> Result<()> {
    let payload = rmp_serde::to_vec_named(msg).context("failed to serialize IPC message")?;
    let len = payload.len() as u32;
    stream
        .write_all(&len.to_be_bytes())
        .context("failed to write IPC length prefix")?;
    stream
        .write_all(&payload)
        .context("failed to write IPC payload")?;
    Ok(())
}

/// Receive a length-prefixed MessagePack message from a Unix stream.
///
/// Returns an error on EOF or if the payload exceeds `MAX_PAYLOAD_SIZE`.
pub fn recv_message<T: DeserializeOwned>(stream: &mut UnixStream) -> Result<T> {
    let mut len_buf = [0u8; 4];
    stream
        .read_exact(&mut len_buf)
        .context("failed to read IPC length prefix (connection closed?)")?;
    let len = u32::from_be_bytes(len_buf);
    if len > MAX_PAYLOAD_SIZE {
        bail!("IPC payload too large: {len} bytes (max {MAX_PAYLOAD_SIZE})");
    }
    let mut payload = vec![0u8; len as usize];
    stream
        .read_exact(&mut payload)
        .context("failed to read IPC payload")?;
    rmp_serde::from_slice(&payload).context("failed to deserialize IPC message")
}

// ── Socket path ──────────────────────────────────────────────────────────────

/// Return the default daemon socket path.
///
/// Uses `$XDG_RUNTIME_DIR/roard.sock` if set, otherwise `/tmp/roard-<uid>.sock`.
pub fn socket_path() -> PathBuf {
    if let Ok(dir) = std::env::var("XDG_RUNTIME_DIR") {
        return PathBuf::from(dir).join("roard.sock");
    }
    let uid = unsafe { libc::getuid() };
    PathBuf::from(format!("/tmp/roard-{uid}.sock"))
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixStream;

    #[test]
    fn test_round_trip_client_messages() {
        let messages = vec![
            ClientMessage::Register {
                run_id: 42,
                root_pid: 1234,
                root_command: vec!["./train.sh".to_string()],
            },
            ClientMessage::Deregister { run_id: 42 },
            ClientMessage::GetReport { run_id: 42 },
            ClientMessage::Ping,
        ];

        for msg in &messages {
            let payload = rmp_serde::to_vec_named(msg).unwrap();
            let decoded: ClientMessage = rmp_serde::from_slice(&payload).unwrap();
            assert_eq!(&decoded, msg);
        }
    }

    #[test]
    fn test_round_trip_daemon_messages() {
        let report = TracerOutput {
            version: 1,
            chunk_size: None,
            processes: vec![],
            files: vec![],
            opened_files: vec![],
            read_files: vec![],
            written_files: vec![],
            env_accessed: std::collections::HashMap::new(),
            start_time: 1000.0,
            end_time: 2000.0,
            tracer_mode: "ebpf".to_string(),
            events_dropped: Some(0),
        };

        let messages: Vec<DaemonMessage> = vec![
            DaemonMessage::Ack { run_id: 42 },
            DaemonMessage::Report {
                run_id: 42,
                data: report,
            },
            DaemonMessage::Error {
                run_id: 42,
                message: "test error".to_string(),
            },
            DaemonMessage::Pong,
        ];

        for msg in &messages {
            let payload = rmp_serde::to_vec_named(msg).unwrap();
            let _decoded: DaemonMessage = rmp_serde::from_slice(&payload).unwrap();
        }
    }

    #[test]
    fn test_frame_correctness() {
        let msg = ClientMessage::Register {
            run_id: 99,
            root_pid: 5678,
            root_command: vec!["./train.sh".to_string()],
        };
        let payload = rmp_serde::to_vec_named(&msg).unwrap();

        // Build the wire frame manually
        let mut frame = Vec::new();
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(&payload);

        // Verify the length prefix
        let len = u32::from_be_bytes([frame[0], frame[1], frame[2], frame[3]]);
        assert_eq!(len as usize, payload.len());
        assert_eq!(&frame[4..], &payload);
    }

    #[test]
    fn test_send_recv_over_unix_socket() {
        let (mut a, mut b) = UnixStream::pair().unwrap();

        let msg = ClientMessage::Register {
            run_id: 123,
            root_pid: 4567,
            root_command: vec!["python".to_string(), "train.py".to_string()],
        };
        send_message(&mut a, &msg).unwrap();

        let received: ClientMessage = recv_message(&mut b).unwrap();
        assert_eq!(received, msg);
    }

    /// `roard` is long-lived, so a running daemon can predate the client that
    /// connects to it (and vice versa across an upgrade). The wire format is
    /// field-named MessagePack, so a Register that omits root_command must
    /// still decode -- as empty, which every caller treats as "fall back to
    /// /proc" rather than as an empty command line.
    #[test]
    fn a_register_without_root_command_still_decodes() {
        #[derive(Serialize)]
        #[serde(tag = "type")]
        enum LegacyClientMessage {
            Register { run_id: u64, root_pid: u32 },
        }

        let legacy = LegacyClientMessage::Register {
            run_id: 7,
            root_pid: 4242,
        };
        let payload = rmp_serde::to_vec_named(&legacy).unwrap();

        let decoded: ClientMessage = rmp_serde::from_slice(&payload).unwrap();
        assert_eq!(
            decoded,
            ClientMessage::Register {
                run_id: 7,
                root_pid: 4242,
                root_command: vec![],
            }
        );
    }

    #[test]
    fn test_socket_path_format() {
        let path = socket_path();
        let path_str = path.to_string_lossy();
        // Should end with roard.sock or roard-<uid>.sock
        assert!(
            path_str.ends_with("roard.sock") || path_str.contains("roard-"),
            "unexpected socket path: {path_str}"
        );
    }
}
