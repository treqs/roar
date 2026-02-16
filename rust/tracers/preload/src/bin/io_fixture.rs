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
}
