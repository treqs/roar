use anyhow::{Context, Result};
use aya::programs::TracePoint;
use aya::Ebpf;
use log::{info, warn};
use std::path::Path;

/// Locations tracefs may be mounted under.
const TRACEFS_ROOTS: &[&str] = &["/sys/kernel/tracing", "/sys/kernel/debug/tracing"];

/// Whether `/sys/kernel/{tracing,debug/tracing}/events/<category>/<tp>/id` is
/// readable. Used to skip syscall tracepoints that aren't exposed by the
/// running kernel (e.g. `sys_enter_open` on aarch64, which has no `open()`
/// syscall — only `openat()`).
fn tracepoint_available(category: &str, tp: &str) -> bool {
    TRACEFS_ROOTS
        .iter()
        .any(|root| Path::new(root).join("events").join(category).join(tp).join("id").exists())
}

/// Attach a tracepoint program. Returns `Ok(true)` if attached, `Ok(false)` if
/// the kernel doesn't expose this tracepoint (logged + skipped).
fn attach_tp(bpf: &mut Ebpf, fn_name: &str, category: &str, tp: &str) -> Result<bool> {
    if !tracepoint_available(category, tp) {
        warn!("kernel does not expose {category}/{tp}; skipping {fn_name}");
        return Ok(false);
    }
    let prog: &mut TracePoint = bpf
        .program_mut(fn_name)
        .context(format!("BPF program '{fn_name}' not found"))?
        .try_into()
        .context(format!("'{fn_name}' is not a tracepoint program"))?;
    prog.load().context(format!("failed to load '{fn_name}'"))?;
    prog.attach(category, tp)
        .context(format!("failed to attach '{fn_name}' to {category}/{tp}"))?;
    info!("attached {fn_name} to {category}/{tp}");
    Ok(true)
}

/// Attach all tracepoints used by the eBPF tracer.
fn attach_all_tracepoints(bpf: &mut Ebpf) -> Result<()> {
    // open / openat
    attach_tp(bpf, "sys_enter_openat", "syscalls", "sys_enter_openat")?;
    attach_tp(bpf, "sys_exit_openat", "syscalls", "sys_exit_openat")?;
    attach_tp(bpf, "sys_enter_open", "syscalls", "sys_enter_open")?;
    attach_tp(bpf, "sys_exit_open", "syscalls", "sys_exit_open")?;

    // read / write
    attach_tp(bpf, "sys_enter_read", "syscalls", "sys_enter_read")?;
    attach_tp(bpf, "sys_exit_read", "syscalls", "sys_exit_read")?;
    attach_tp(bpf, "sys_enter_write", "syscalls", "sys_enter_write")?;
    attach_tp(bpf, "sys_exit_write", "syscalls", "sys_exit_write")?;

    // pread64 / pwrite64
    attach_tp(bpf, "sys_enter_pread64", "syscalls", "sys_enter_pread64")?;
    attach_tp(bpf, "sys_exit_pread64", "syscalls", "sys_exit_pread64")?;
    attach_tp(bpf, "sys_enter_pwrite64", "syscalls", "sys_enter_pwrite64")?;
    attach_tp(bpf, "sys_exit_pwrite64", "syscalls", "sys_exit_pwrite64")?;

    // close
    attach_tp(bpf, "sys_enter_close", "syscalls", "sys_enter_close")?;
    attach_tp(bpf, "sys_exit_close", "syscalls", "sys_exit_close")?;

    // mmap
    attach_tp(bpf, "sys_enter_mmap", "syscalls", "sys_enter_mmap")?;
    attach_tp(bpf, "sys_exit_mmap", "syscalls", "sys_exit_mmap")?;

    // copy_file_range
    attach_tp(
        bpf,
        "sys_enter_copy_file_range",
        "syscalls",
        "sys_enter_copy_file_range",
    )?;
    attach_tp(
        bpf,
        "sys_exit_copy_file_range",
        "syscalls",
        "sys_exit_copy_file_range",
    )?;

    // rename / link path publication
    attach_tp(bpf, "sys_enter_rename", "syscalls", "sys_enter_rename")?;
    attach_tp(bpf, "sys_exit_rename", "syscalls", "sys_exit_rename")?;
    attach_tp(bpf, "sys_enter_renameat", "syscalls", "sys_enter_renameat")?;
    attach_tp(bpf, "sys_exit_renameat", "syscalls", "sys_exit_renameat")?;
    attach_tp(
        bpf,
        "sys_enter_renameat2",
        "syscalls",
        "sys_enter_renameat2",
    )?;
    attach_tp(bpf, "sys_exit_renameat2", "syscalls", "sys_exit_renameat2")?;
    attach_tp(bpf, "sys_enter_link", "syscalls", "sys_enter_link")?;
    attach_tp(bpf, "sys_exit_link", "syscalls", "sys_exit_link")?;
    attach_tp(bpf, "sys_enter_linkat", "syscalls", "sys_enter_linkat")?;
    attach_tp(bpf, "sys_exit_linkat", "syscalls", "sys_exit_linkat")?;

    // dup2 / dup3
    attach_tp(bpf, "sys_enter_dup2", "syscalls", "sys_enter_dup2")?;
    attach_tp(bpf, "sys_exit_dup2", "syscalls", "sys_exit_dup2")?;
    attach_tp(bpf, "sys_enter_dup3", "syscalls", "sys_enter_dup3")?;
    attach_tp(bpf, "sys_exit_dup3", "syscalls", "sys_exit_dup3")?;

    // clone / fork / vfork / clone3
    attach_tp(bpf, "sys_exit_clone", "syscalls", "sys_exit_clone")?;
    attach_tp(bpf, "sys_exit_fork", "syscalls", "sys_exit_fork")?;
    attach_tp(bpf, "sys_exit_vfork", "syscalls", "sys_exit_vfork")?;
    attach_tp(bpf, "sys_exit_clone3", "syscalls", "sys_exit_clone3")?;

    // exec
    attach_tp(bpf, "sys_exit_execve", "syscalls", "sys_exit_execve")?;
    attach_tp(bpf, "sys_exit_execveat", "syscalls", "sys_exit_execveat")?;

    Ok(())
}

/// Load BPF object and attach all required tracepoints.
pub fn load_and_attach_bpf() -> Result<Ebpf> {
    let mut bpf = Ebpf::load(aya::include_bytes_aligned!(concat!(
        env!("OUT_DIR"),
        "/roar-tracer-ebpf"
    )))
    .context("failed to load BPF object")?;

    #[cfg(debug_assertions)]
    if let Err(e) = aya_log::EbpfLogger::init(&mut bpf) {
        log::warn!("failed to init aya-log: {e}");
    }

    attach_all_tracepoints(&mut bpf)?;
    Ok(bpf)
}
