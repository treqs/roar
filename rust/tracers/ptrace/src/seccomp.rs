#[repr(C)]
struct SockFilter {
    code: u16,
    jt: u8,
    jf: u8,
    k: u32,
}

#[repr(C)]
struct SockFprog {
    len: u16,
    filter: *const SockFilter,
}

fn build_seccomp_filter(tracked_syscalls: &[u64], audit_arch: u32) -> Vec<SockFilter> {
    const BPF_LD: u16 = 0x00;
    const BPF_W: u16 = 0x00;
    const BPF_ABS: u16 = 0x20;
    const BPF_JMP: u16 = 0x05;
    const BPF_JEQ: u16 = 0x10;
    const BPF_RET: u16 = 0x06;
    const BPF_K: u16 = 0x00;

    const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
    const SECCOMP_RET_TRACE: u32 = 0x7ff0_0000;

    const OFFSET_NR: u32 = 0;
    const OFFSET_ARCH: u32 = 4;

    let num_syscalls = tracked_syscalls.len();
    let mut filter = Vec::new();

    filter.push(SockFilter {
        code: BPF_LD | BPF_W | BPF_ABS,
        jt: 0,
        jf: 0,
        k: OFFSET_ARCH,
    });

    let arch_check_idx = filter.len();
    filter.push(SockFilter {
        code: BPF_JMP | BPF_JEQ | BPF_K,
        jt: 0,
        jf: 0,
        k: audit_arch,
    });

    filter.push(SockFilter {
        code: BPF_LD | BPF_W | BPF_ABS,
        jt: 0,
        jf: 0,
        k: OFFSET_NR,
    });

    for (i, &syscall) in tracked_syscalls.iter().enumerate() {
        let remaining = num_syscalls - i - 1;
        filter.push(SockFilter {
            code: BPF_JMP | BPF_JEQ | BPF_K,
            jt: (remaining + 1) as u8,
            jf: 0,
            k: syscall as u32,
        });
    }

    filter.push(SockFilter {
        code: BPF_RET | BPF_K,
        jt: 0,
        jf: 0,
        k: SECCOMP_RET_ALLOW,
    });

    filter.push(SockFilter {
        code: BPF_RET | BPF_K,
        jt: 0,
        jf: 0,
        k: SECCOMP_RET_TRACE,
    });

    filter[arch_check_idx].jf = (num_syscalls + 1) as u8;
    filter
}

pub fn install_seccomp_filter(tracked_syscalls: &[u64], audit_arch: u32) {
    let filter = build_seccomp_filter(tracked_syscalls, audit_arch);
    let prog = SockFprog {
        len: filter.len() as u16,
        filter: filter.as_ptr(),
    };

    let ret = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
    if ret != 0 {
        eprintln!("Warning: prctl(PR_SET_NO_NEW_PRIVS) failed");
        return;
    }

    let ret = unsafe {
        libc::prctl(
            libc::PR_SET_SECCOMP,
            libc::SECCOMP_MODE_FILTER,
            &prog as *const SockFprog as libc::c_ulong,
            0,
            0,
        )
    };
    if ret != 0 {
        eprintln!("Warning: prctl(PR_SET_SECCOMP) failed");
    }
}
