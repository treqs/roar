#![cfg_attr(not(feature = "user"), no_std)]

use serde::{Deserialize, Serialize};

/// Maximum path length stored in BPF events.
pub const MAX_PATH_LEN: usize = 256;

// --- Event type discriminants ---

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u16)]
pub enum EventType {
    OpenExit = 0,
    Close = 1,
    Read = 2,
    Write = 3,
    PRead = 4,
    PWrite = 5,
    Lseek = 6,
    Dup = 7,
    MmapRead = 8,
    MmapWrite = 9,
    Sendfile = 10,
    CopyFileRange = 11,
    Rename = 12,
    Clone = 13,
    Exec = 14,
}

// --- BPF event structs (shared between kernel and userspace) ---

/// Small event (~32 bytes) for hot-path syscalls: read, write, close, lseek, dup, sendfile, etc.
#[derive(Debug, Clone, Copy)]
#[repr(C)]
pub struct SmallEvent {
    pub pid: u32,
    pub thread_id: u32,
    pub event_type: u16,
    pub _pad: u16,
    pub ret_val: i64,
    pub arg0: u64, // fd (or in_fd for sendfile)
    pub arg1: u64, // offset (or out_fd for sendfile, new_fd for dup)
}

/// Large event (~288 bytes) for open, rename, exec, clone — includes an inline path.
#[derive(Debug, Clone, Copy)]
#[repr(C)]
pub struct LargeEvent {
    pub pid: u32,
    pub event_type: u16,
    pub _pad: u16,
    pub ret_val: i64,
    pub arg0: u64, // flags (open) or child_pid (clone)
    pub arg1: u64, // dirfd (openat) or unused
    pub path: [u8; MAX_PATH_LEN],
}

/// Pending syscall args stashed between sys_enter and sys_exit.
#[derive(Debug, Clone, Copy)]
#[repr(C)]
pub struct PendingArgs {
    pub syscall_nr: u64,
    pub arg0: u64,
    pub arg1: u64,
    pub arg2: u64,
    pub arg3: u64, // used by mmap for prot
    pub arg4: u64, // used by mmap for flags
    pub path: [u8; MAX_PATH_LEN],
}

// --- Ring buffer event tag (first 4 bytes tell userspace which struct to parse) ---

/// Events are submitted to the ring buffer prefixed with a u32 tag:
///   0 = SmallEvent follows
///   1 = LargeEvent follows
pub const TAG_SMALL: u32 = 0;
pub const TAG_LARGE: u32 = 1;

// --- Syscall numbers (x86_64) ---

pub mod syscall_nr {
    pub const OPEN: u64 = 2;
    pub const CLOSE: u64 = 3;
    pub const READ: u64 = 0;
    pub const WRITE: u64 = 1;
    pub const LSEEK: u64 = 8;
    pub const MMAP: u64 = 9;
    pub const PREAD64: u64 = 17;
    pub const PWRITE64: u64 = 18;
    pub const READV: u64 = 19;
    pub const WRITEV: u64 = 20;
    pub const DUP: u64 = 32;
    pub const DUP2: u64 = 33;
    pub const FORK: u64 = 57;
    pub const VFORK: u64 = 58;
    pub const CLONE: u64 = 56;
    pub const EXECVE: u64 = 59;
    pub const RENAME: u64 = 82;
    pub const SENDFILE: u64 = 40;
    pub const OPENAT: u64 = 257;
    pub const RENAMEAT: u64 = 264;
    pub const DUP3: u64 = 292;
    pub const PREADV: u64 = 295;
    pub const PWRITEV: u64 = 296;
    pub const RENAMEAT2: u64 = 316;
    pub const EXECVEAT: u64 = 322;
    pub const COPY_FILE_RANGE: u64 = 326;
    pub const PREADV2: u64 = 327;
    pub const PWRITEV2: u64 = 328;
    pub const CLONE3: u64 = 435;
}

// Safety: these are plain C-repr structs with no pointers
unsafe impl Send for SmallEvent {}
unsafe impl Sync for SmallEvent {}
unsafe impl Send for LargeEvent {}
unsafe impl Sync for LargeEvent {}
unsafe impl Send for PendingArgs {}
unsafe impl Sync for PendingArgs {}
