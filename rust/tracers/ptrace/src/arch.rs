//! Architecture-specific bits for the ptrace tracer.
//!
//! The tracer reads syscall arguments and return values from the tracee's
//! register state. Both the syscall numbering and the register layout differ
//! between x86_64 and aarch64, so this module exposes a single arch-neutral
//! API the rest of the tracer can use.
//!
//! Syscalls that exist on one arch but not the other (e.g. `open`, `rename`,
//! `link` on aarch64) are given a sentinel value (`u64::MAX`) so that match
//! arms keyed on those constants compile but never match a real syscall.

use nix::unistd::Pid;

#[cfg(target_arch = "x86_64")]
mod imp {

    pub const SYS_READ: u64 = 0;
    pub const SYS_WRITE: u64 = 1;
    pub const SYS_OPEN: u64 = 2;
    pub const SYS_CLOSE: u64 = 3;
    pub const SYS_MMAP: u64 = 9;
    pub const SYS_PREAD64: u64 = 17;
    pub const SYS_PWRITE64: u64 = 18;
    pub const SYS_READV: u64 = 19;
    pub const SYS_WRITEV: u64 = 20;
    pub const SYS_SENDFILE: u64 = 40;
    pub const SYS_CHDIR: u64 = 80;
    pub const SYS_FCHDIR: u64 = 81;
    pub const SYS_RENAME: u64 = 82;
    pub const SYS_LINK: u64 = 86;
    pub const SYS_OPENAT: u64 = 257;
    pub const SYS_RENAMEAT: u64 = 264;
    pub const SYS_LINKAT: u64 = 265;
    pub const SYS_PREADV: u64 = 295;
    pub const SYS_PWRITEV: u64 = 296;
    pub const SYS_RENAMEAT2: u64 = 316;
    pub const SYS_COPY_FILE_RANGE: u64 = 326;
    pub const SYS_PREADV2: u64 = 327;
    pub const SYS_PWRITEV2: u64 = 328;

    pub const AUDIT_ARCH: u32 = 0xC000_003E;
    pub const TRACKED_SYSCALLS: &[u64] = &[
        SYS_READ,
        SYS_WRITE,
        SYS_OPEN,
        SYS_CLOSE,
        SYS_MMAP,
        SYS_PREAD64,
        SYS_PWRITE64,
        SYS_READV,
        SYS_WRITEV,
        SYS_SENDFILE,
        SYS_CHDIR,
        SYS_FCHDIR,
        SYS_RENAME,
        SYS_LINK,
        SYS_OPENAT,
        SYS_RENAMEAT,
        SYS_LINKAT,
        SYS_PREADV,
        SYS_PWRITEV,
        SYS_RENAMEAT2,
        SYS_COPY_FILE_RANGE,
        SYS_PREADV2,
        SYS_PWRITEV2,
    ];

    pub type Regs = libc::user_regs_struct;

    #[inline]
    pub fn syscall_num(regs: &Regs) -> u64 {
        regs.orig_rax
    }
    #[inline]
    pub fn ret_val(regs: &Regs) -> i64 {
        regs.rax as i64
    }
    #[inline]
    pub fn arg0(regs: &Regs) -> u64 {
        regs.rdi
    }
    #[inline]
    pub fn arg1(regs: &Regs) -> u64 {
        regs.rsi
    }
    #[inline]
    pub fn arg2(regs: &Regs) -> u64 {
        regs.rdx
    }
    #[inline]
    pub fn arg3(regs: &Regs) -> u64 {
        regs.r10
    }
    #[inline]
    pub fn arg4(regs: &Regs) -> u64 {
        regs.r8
    }
}

#[cfg(target_arch = "aarch64")]
mod imp {

    // aarch64 uses the asm-generic syscall table (asm-generic/unistd.h).
    // Syscalls that don't exist on aarch64 (open, rename, link) are mapped to
    // distinct out-of-range sentinels so shared match arms keyed on them
    // compile without firing or colliding with one another.
    pub const SYS_OPEN: u64 = u64::MAX;
    pub const SYS_RENAME: u64 = u64::MAX - 1;
    pub const SYS_LINK: u64 = u64::MAX - 2;

    pub const SYS_LINKAT: u64 = 37;
    pub const SYS_RENAMEAT: u64 = 38;
    pub const SYS_CHDIR: u64 = 49;
    pub const SYS_FCHDIR: u64 = 50;
    pub const SYS_OPENAT: u64 = 56;
    pub const SYS_CLOSE: u64 = 57;
    pub const SYS_READ: u64 = 63;
    pub const SYS_WRITE: u64 = 64;
    pub const SYS_READV: u64 = 65;
    pub const SYS_WRITEV: u64 = 66;
    pub const SYS_PREAD64: u64 = 67;
    pub const SYS_PWRITE64: u64 = 68;
    pub const SYS_PREADV: u64 = 69;
    pub const SYS_PWRITEV: u64 = 70;
    pub const SYS_SENDFILE: u64 = 71;
    pub const SYS_MMAP: u64 = 222;
    pub const SYS_RENAMEAT2: u64 = 276;
    pub const SYS_COPY_FILE_RANGE: u64 = 285;
    pub const SYS_PREADV2: u64 = 286;
    pub const SYS_PWRITEV2: u64 = 287;

    pub const AUDIT_ARCH: u32 = 0xC000_00B7;
    pub const TRACKED_SYSCALLS: &[u64] = &[
        SYS_READ,
        SYS_WRITE,
        SYS_CLOSE,
        SYS_MMAP,
        SYS_PREAD64,
        SYS_PWRITE64,
        SYS_READV,
        SYS_WRITEV,
        SYS_SENDFILE,
        SYS_CHDIR,
        SYS_FCHDIR,
        SYS_OPENAT,
        SYS_RENAMEAT,
        SYS_LINKAT,
        SYS_PREADV,
        SYS_PWRITEV,
        SYS_RENAMEAT2,
        SYS_COPY_FILE_RANGE,
        SYS_PREADV2,
        SYS_PWRITEV2,
    ];

    pub type Regs = libc::user_regs_struct;

    // On aarch64 Linux, syscall args are x0..x5, the syscall number is in x8,
    // and the return value comes back in x0.
    #[inline]
    pub fn syscall_num(regs: &Regs) -> u64 {
        regs.regs[8]
    }
    #[inline]
    pub fn ret_val(regs: &Regs) -> i64 {
        regs.regs[0] as i64
    }
    #[inline]
    pub fn arg0(regs: &Regs) -> u64 {
        regs.regs[0]
    }
    #[inline]
    pub fn arg1(regs: &Regs) -> u64 {
        regs.regs[1]
    }
    #[inline]
    pub fn arg2(regs: &Regs) -> u64 {
        regs.regs[2]
    }
    #[inline]
    pub fn arg3(regs: &Regs) -> u64 {
        regs.regs[3]
    }
    #[inline]
    pub fn arg4(regs: &Regs) -> u64 {
        regs.regs[4]
    }
}

pub use imp::*;

/// Wrapper around `nix::sys::ptrace::getregs` that returns our arch-specific
/// `Regs` alias so callers don't have to spell out `libc::user_regs_struct`.
pub fn getregs(pid: Pid) -> nix::Result<Regs> {
    nix::sys::ptrace::getregs(pid)
}
