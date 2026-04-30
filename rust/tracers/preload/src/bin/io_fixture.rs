use std::env;
use std::ffi::CString;

fn main() {
    let mut args = env::args();
    let _program = args.next();
    let Some(path) = args.next() else {
        eprintln!("usage: io_fixture <path>");
        std::process::exit(2);
    };

    let c_path = CString::new(path).expect("path contains interior NUL");
    let payload = b"roar-preload-test";

    let write_fd = unsafe {
        libc::open(
            c_path.as_ptr(),
            libc::O_CREAT | libc::O_TRUNC | libc::O_WRONLY,
            0o644,
        )
    };
    if write_fd < 0 {
        eprintln!("failed to open file for writing");
        std::process::exit(1);
    }

    let write_rc = unsafe {
        libc::write(
            write_fd,
            payload.as_ptr() as *const libc::c_void,
            payload.len(),
        )
    };
    unsafe {
        libc::close(write_fd);
    }
    if write_rc < 0 || write_rc as usize != payload.len() {
        eprintln!("failed to write payload");
        std::process::exit(1);
    }

    let read_fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDONLY) };
    if read_fd < 0 {
        eprintln!("failed to open file for reading");
        std::process::exit(1);
    }

    let mut buf = vec![0u8; payload.len()];
    let read_rc = unsafe { libc::read(read_fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
    unsafe {
        libc::close(read_fd);
    }
    if read_rc < 0 {
        eprintln!("failed to read payload");
        std::process::exit(1);
    }

    let got = &buf[..read_rc as usize];
    if got != payload {
        eprintln!("unexpected payload: {}", String::from_utf8_lossy(got));
        std::process::exit(1);
    }

    // --- mmap test: read the file via mmap ---
    let mmap_fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDONLY) };
    if mmap_fd < 0 {
        eprintln!("failed to open file for mmap");
        std::process::exit(1);
    }
    let mmap_ptr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            payload.len(),
            libc::PROT_READ,
            libc::MAP_PRIVATE,
            mmap_fd,
            0,
        )
    };
    if mmap_ptr == libc::MAP_FAILED {
        eprintln!("mmap failed");
        unsafe { libc::close(mmap_fd) };
        std::process::exit(1);
    }
    let mmap_slice = unsafe { std::slice::from_raw_parts(mmap_ptr as *const u8, payload.len()) };
    if mmap_slice != payload {
        eprintln!(
            "mmap payload mismatch: {}",
            String::from_utf8_lossy(mmap_slice)
        );
        std::process::exit(1);
    }
    unsafe {
        libc::munmap(mmap_ptr, payload.len());
        libc::close(mmap_fd);
    }

    // --- rename test: rename to .renamed, then rename back ---
    let renamed = format!("{}.renamed", c_path.to_str().unwrap());
    let c_renamed = CString::new(renamed).expect("renamed path contains interior NUL");
    if unsafe { libc::rename(c_path.as_ptr(), c_renamed.as_ptr()) } != 0 {
        eprintln!("rename failed");
        std::process::exit(1);
    }
    if unsafe { libc::rename(c_renamed.as_ptr(), c_path.as_ptr()) } != 0 {
        eprintln!("rename back failed");
        std::process::exit(1);
    }

    // --- hard-link publication test: write temp, link final, unlink temp ---
    let link_tmp = format!("{}.linked#1", c_path.to_str().unwrap());
    let link_final = format!("{}.linked", c_path.to_str().unwrap());
    let c_link_tmp = CString::new(link_tmp).expect("link temp path contains interior NUL");
    let c_link_final = CString::new(link_final).expect("link final path contains interior NUL");
    let link_fd = unsafe {
        libc::open(
            c_link_tmp.as_ptr(),
            libc::O_CREAT | libc::O_TRUNC | libc::O_WRONLY,
            0o644,
        )
    };
    if link_fd < 0 {
        eprintln!("failed to open hard-link temp for writing");
        std::process::exit(1);
    }
    let link_write_rc = unsafe {
        libc::write(
            link_fd,
            payload.as_ptr() as *const libc::c_void,
            payload.len(),
        )
    };
    unsafe {
        libc::close(link_fd);
    }
    if link_write_rc < 0 || link_write_rc as usize != payload.len() {
        eprintln!("failed to write hard-link temp payload");
        std::process::exit(1);
    }
    if unsafe { libc::link(c_link_tmp.as_ptr(), c_link_final.as_ptr()) } != 0 {
        eprintln!("hard link failed");
        std::process::exit(1);
    }
    if unsafe { libc::unlink(c_link_tmp.as_ptr()) } != 0 {
        eprintln!("hard-link temp unlink failed");
        std::process::exit(1);
    }

    // --- truncate test: truncate the file to a smaller size ---
    if unsafe { libc::truncate(c_path.as_ptr(), 5) } != 0 {
        eprintln!("truncate failed");
        std::process::exit(1);
    }

    // --- unlink test: create a temp file then unlink it ---
    let unlink_path = format!("{}.unlink_test", c_path.to_str().unwrap());
    let c_unlink = CString::new(unlink_path).expect("unlink path contains interior NUL");
    let unlink_fd = unsafe { libc::open(c_unlink.as_ptr(), libc::O_CREAT | libc::O_WRONLY, 0o644) };
    if unlink_fd >= 0 {
        unsafe { libc::close(unlink_fd) };
        if unsafe { libc::unlink(c_unlink.as_ptr()) } != 0 {
            eprintln!("unlink failed");
            std::process::exit(1);
        }
    }
}
