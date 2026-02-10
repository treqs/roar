use roar_ebpf_common::{EventType, LargeEvent, SmallEvent, MAX_PATH_LEN, TAG_LARGE, TAG_SMALL};

use crate::state::{resolve_path, TracerState};

/// Extract a null-terminated string from a fixed-size byte buffer.
pub fn path_from_bytes(buf: &[u8; MAX_PATH_LEN]) -> String {
    let len = buf.iter().position(|b| *b == 0).unwrap_or(MAX_PATH_LEN);
    String::from_utf8_lossy(&buf[..len]).into_owned()
}

/// Parse an event type discriminant from u16.
fn event_type_from_u16(v: u16) -> Option<EventType> {
    match v {
        0 => Some(EventType::OpenExit),
        1 => Some(EventType::Close),
        2 => Some(EventType::Read),
        3 => Some(EventType::Write),
        4 => Some(EventType::PRead),
        5 => Some(EventType::PWrite),
        6 => Some(EventType::Lseek),
        7 => Some(EventType::Dup),
        8 => Some(EventType::MmapRead),
        9 => Some(EventType::MmapWrite),
        10 => Some(EventType::Sendfile),
        11 => Some(EventType::CopyFileRange),
        12 => Some(EventType::Rename),
        13 => Some(EventType::Clone),
        14 => Some(EventType::Exec),
        _ => None,
    }
}

/// Process a raw event from the ring buffer.
///
/// The buffer starts with a u32 tag (TAG_SMALL or TAG_LARGE) followed by the event struct.
pub fn process_event(state: &mut TracerState, data: &[u8]) {
    if data.len() < 4 {
        return;
    }

    let tag = u32::from_ne_bytes([data[0], data[1], data[2], data[3]]);
    let payload = &data[4..];

    match tag {
        TAG_SMALL => {
            if payload.len() < std::mem::size_of::<SmallEvent>() {
                return;
            }
            // Safety: SmallEvent is repr(C), and we've verified the size
            let event: SmallEvent =
                unsafe { std::ptr::read_unaligned(payload.as_ptr() as *const SmallEvent) };
            process_small_event(state, &event);
        }
        TAG_LARGE => {
            if payload.len() < std::mem::size_of::<LargeEvent>() {
                return;
            }
            let event: LargeEvent =
                unsafe { std::ptr::read_unaligned(payload.as_ptr() as *const LargeEvent) };
            process_large_event(state, &event);
        }
        _ => {
            log::warn!("unknown event tag: {tag}");
        }
    }
}

fn process_small_event(state: &mut TracerState, event: &SmallEvent) {
    let pid = event.pid;
    let fd = event.arg0 as i32;

    let Some(etype) = event_type_from_u16(event.event_type) else {
        log::warn!("unknown small event type: {}", event.event_type);
        return;
    };

    match etype {
        EventType::Read => {
            if event.ret_val > 0 {
                state.handle_read(pid, fd, event.ret_val as u64);
            }
        }
        EventType::Write => {
            if event.ret_val > 0 {
                state.handle_write(pid, fd, event.ret_val as u64);
            }
        }
        EventType::PRead => {
            if event.ret_val > 0 {
                state.handle_pread(pid, fd, event.arg1, event.ret_val as u64);
            }
        }
        EventType::PWrite => {
            if event.ret_val > 0 {
                state.handle_pwrite(pid, fd, event.arg1, event.ret_val as u64);
            }
        }
        EventType::Close => {
            if event.ret_val >= 0 {
                state.handle_close(pid, fd);
            }
        }
        EventType::Lseek => {
            if event.ret_val >= 0 {
                state.handle_lseek(pid, fd, event.ret_val as u64);
            }
        }
        EventType::Dup => {
            if event.ret_val >= 0 {
                let new_fd = event.ret_val as i32;
                state.handle_dup(pid, fd, new_fd);
            }
        }
        EventType::MmapRead => {
            let length = event.ret_val as u64;
            let offset = event.arg1;
            state.handle_pread(pid, fd, offset, length);
        }
        EventType::MmapWrite => {
            let length = event.ret_val as u64;
            let offset = event.arg1;
            state.handle_pwrite(pid, fd, offset, length);
        }
        EventType::Sendfile => {
            // arg0 = in_fd, arg1 = out_fd, ret_val = bytes
            if event.ret_val > 0 {
                let in_fd = event.arg0 as i32;
                let out_fd = event.arg1 as i32;
                let bytes = event.ret_val as u64;
                state.handle_read(pid, in_fd, bytes);
                state.handle_write(pid, out_fd, bytes);
            }
        }
        EventType::CopyFileRange => {
            // arg0 = in_fd, arg1 = out_fd, ret_val = bytes
            // offsets are explicit so this is like pread+pwrite, but we don't have
            // the offsets in SmallEvent. For now, treat as sequential.
            if event.ret_val > 0 {
                let in_fd = event.arg0 as i32;
                let out_fd = event.arg1 as i32;
                let bytes = event.ret_val as u64;
                state.handle_read(pid, in_fd, bytes);
                state.handle_write(pid, out_fd, bytes);
            }
        }
        _ => {
            log::warn!("unexpected small event type: {etype:?}");
        }
    }
}

fn process_large_event(state: &mut TracerState, event: &LargeEvent) {
    let pid = event.pid;

    let Some(etype) = event_type_from_u16(event.event_type) else {
        log::warn!("unknown large event type: {}", event.event_type);
        return;
    };

    match etype {
        EventType::OpenExit => {
            let fd = event.ret_val as i32;
            if fd < 0 {
                return;
            }
            let raw_path = path_from_bytes(&event.path);
            let path = resolve_path(pid, &raw_path);
            let flags = event.arg0;
            state.handle_open(pid, fd, path, flags);
        }
        EventType::Rename => {
            // Destination path is "written"
            let raw_path = path_from_bytes(&event.path);
            let path = resolve_path(pid, &raw_path);
            if event.ret_val >= 0 {
                // Create a synthetic file record marking the destination as written
                // We use fd=-1 as a sentinel for rename (no real fd)
                state.handle_open(pid, -1, path.clone(), 0);
                state.handle_write(pid, -1, 1); // mark as written
                state.handle_close(pid, -1);
            }
        }
        EventType::Clone => {
            let child_pid = event.arg0 as u32;
            state.handle_clone(pid, child_pid);

            // Try to capture process info for the child from /proc.
            // If the child has already exited (common with async event processing),
            // create a minimal record with just the PID and parent.
            let info = crate::state::capture_process_info(child_pid, Some(pid))
                .unwrap_or(crate::state::ProcessInfo {
                    pid: child_pid,
                    parent_pid: Some(pid),
                    command: Vec::new(),
                    env: std::collections::HashMap::new(),
                });
            state.processes.insert(child_pid, info);
        }
        EventType::Exec => {
            state.handle_exec(pid);
        }
        _ => {
            log::warn!("unexpected large event type: {etype:?}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_path_from_bytes_null_terminated() {
        let mut buf = [0u8; MAX_PATH_LEN];
        let s = b"/tmp/test.txt";
        buf[..s.len()].copy_from_slice(s);
        assert_eq!(path_from_bytes(&buf), "/tmp/test.txt");
    }

    #[test]
    fn test_path_from_bytes_full_buffer() {
        let buf = [b'a'; MAX_PATH_LEN]; // no null terminator
        assert_eq!(path_from_bytes(&buf).len(), MAX_PATH_LEN);
    }

    #[test]
    fn test_path_from_bytes_empty() {
        let buf = [0u8; MAX_PATH_LEN];
        assert_eq!(path_from_bytes(&buf), "");
    }

    #[test]
    fn test_event_type_from_u16_valid() {
        assert_eq!(event_type_from_u16(0), Some(EventType::OpenExit));
        assert_eq!(event_type_from_u16(2), Some(EventType::Read));
        assert_eq!(event_type_from_u16(14), Some(EventType::Exec));
    }

    #[test]
    fn test_event_type_from_u16_invalid() {
        assert_eq!(event_type_from_u16(99), None);
    }

    #[test]
    fn test_process_small_event_read() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Read as u16,
            _pad: 0,
            ret_val: 100,
            arg0: 3, // fd
            arg1: 0,
        };

        process_small_event(&mut state, &event);

        let fd_state = state.fd_state.get(&(1, 3)).unwrap();
        assert!(fd_state.was_read);
        assert_eq!(fd_state.cursor, 100);
    }

    #[test]
    fn test_process_small_event_write_zero_bytes_ignored() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Write as u16,
            _pad: 0,
            ret_val: 0, // zero bytes
            arg0: 3,
            arg1: 0,
        };

        process_small_event(&mut state, &event);

        let fd_state = state.fd_state.get(&(1, 3)).unwrap();
        assert!(!fd_state.was_written);
    }

    #[test]
    fn test_process_small_event_failed_write_ignored() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Write as u16,
            _pad: 0,
            ret_val: -1, // error
            arg0: 3,
            arg1: 0,
        };

        process_small_event(&mut state, &event);

        let fd_state = state.fd_state.get(&(1, 3)).unwrap();
        assert!(!fd_state.was_written);
    }

    #[test]
    fn test_process_large_event_open() {
        let mut state = TracerState::new(None);

        let mut path = [0u8; MAX_PATH_LEN];
        let p = b"/tmp/test.txt";
        path[..p.len()].copy_from_slice(p);

        let event = LargeEvent {
            pid: 1,
            event_type: EventType::OpenExit as u16,
            _pad: 0,
            ret_val: 3, // fd
            arg0: 0,    // flags
            arg1: 0,
            path,
        };

        process_large_event(&mut state, &event);

        assert_eq!(
            state.fd_table.get(&(1, 3)),
            Some(&"/tmp/test.txt".to_string())
        );
    }

    #[test]
    fn test_process_large_event_failed_open_ignored() {
        let mut state = TracerState::new(None);

        let mut path = [0u8; MAX_PATH_LEN];
        let p = b"/tmp/test.txt";
        path[..p.len()].copy_from_slice(p);

        let event = LargeEvent {
            pid: 1,
            event_type: EventType::OpenExit as u16,
            _pad: 0,
            ret_val: -2, // ENOENT
            arg0: 0,
            arg1: 0,
            path,
        };

        process_large_event(&mut state, &event);

        assert!(state.fd_table.is_empty());
    }

    #[test]
    fn test_process_event_raw_bytes_small() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Read as u16,
            _pad: 0,
            ret_val: 42,
            arg0: 3,
            arg1: 0,
        };

        // Build raw bytes: tag + event
        let mut raw = Vec::new();
        raw.extend_from_slice(&TAG_SMALL.to_ne_bytes());
        raw.extend_from_slice(unsafe {
            std::slice::from_raw_parts(
                &event as *const SmallEvent as *const u8,
                std::mem::size_of::<SmallEvent>(),
            )
        });

        process_event(&mut state, &raw);

        let fd_state = state.fd_state.get(&(1, 3)).unwrap();
        assert!(fd_state.was_read);
        assert_eq!(fd_state.cursor, 42);
    }

    #[test]
    fn test_process_event_truncated_data_ignored() {
        let mut state = TracerState::new(None);
        // Too short to even have a tag
        process_event(&mut state, &[0, 1]);
        // Tag present but payload too short
        process_event(&mut state, &TAG_SMALL.to_ne_bytes());
    }

    #[test]
    fn test_sendfile_event() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/src.txt".to_string(), 0);
        state.handle_open(1, 4, "/tmp/dst.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Sendfile as u16,
            _pad: 0,
            ret_val: 1024,
            arg0: 3, // in_fd
            arg1: 4, // out_fd
        };

        process_small_event(&mut state, &event);

        assert!(state.fd_state.get(&(1, 3)).unwrap().was_read);
        assert!(state.fd_state.get(&(1, 4)).unwrap().was_written);
    }

    #[test]
    fn test_dup_event() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Dup as u16,
            _pad: 0,
            ret_val: 7, // new_fd
            arg0: 3,    // old_fd
            arg1: 0,
        };

        process_small_event(&mut state, &event);

        assert_eq!(
            state.fd_table.get(&(1, 7)),
            Some(&"/tmp/test.txt".to_string())
        );
    }

    #[test]
    fn test_close_event() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Close as u16,
            _pad: 0,
            ret_val: 0,
            arg0: 3, // fd
            arg1: 0,
        };

        process_small_event(&mut state, &event);

        assert!(!state.fd_table.contains_key(&(1, 3)));
    }

    #[test]
    fn test_lseek_event() {
        let mut state = TracerState::new(None);
        state.handle_open(1, 3, "/tmp/test.txt".to_string(), 0);

        let event = SmallEvent {
            pid: 1,
            event_type: EventType::Lseek as u16,
            _pad: 0,
            ret_val: 4096, // new offset
            arg0: 3,       // fd
            arg1: 0,
        };

        process_small_event(&mut state, &event);

        assert_eq!(state.fd_state.get(&(1, 3)).unwrap().cursor, 4096);
    }
}
