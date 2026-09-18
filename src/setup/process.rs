use anyhow::{Context, Result, ensure};
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Pipes are nonblocking so a child cannot stall a reader or allocate unbounded output.
pub fn run(command: &mut Command) -> Result<Vec<u8>> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = command.spawn().context("Cannot start verification tool")?;
    let mut stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    use std::os::fd::AsRawFd;
    for fd in [stdout.as_raw_fd(), stderr.as_raw_fd()] {
        if unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } == -1 {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.wait();
            return Err(std::io::Error::last_os_error().into());
        }
    }
    let result = (|| {
        let deadline = Instant::now() + Duration::from_secs(20);
        let mut output = Vec::new();
        let mut errors = Vec::new();
        let mut stdout_closed = false;
        let mut stderr_closed = false;
        loop {
            for (reader, buffer, closed) in [
                (
                    &mut stdout as &mut dyn Read,
                    &mut output,
                    &mut stdout_closed,
                ),
                (
                    &mut stderr as &mut dyn Read,
                    &mut errors,
                    &mut stderr_closed,
                ),
            ] {
                let mut block = [0u8; 4096];
                match reader.read(&mut block) {
                    Ok(0) => *closed = true,
                    Ok(count) => buffer.extend_from_slice(&block[..count]),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => (),
                    Err(error) => return Err(error.into()),
                }
            }
            ensure!(
                output.len() + errors.len() <= 65536,
                "Verification subprocess output exceeds 64 KiB"
            );
            if let Some(status) = child.try_wait()?
                && stdout_closed
                && stderr_closed
            {
                ensure!(
                    status.success(),
                    "Verification subprocess failed: {}",
                    String::from_utf8_lossy(&errors)
                );
                return Ok(output);
            }
            ensure!(
                Instant::now() < deadline,
                "Verification subprocess exceeded 20 seconds"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    })();
    if result.is_err() {
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
    }
    let _ = child.wait();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_output_and_descendant_pipe_cleanup() {
        assert!(run(Command::new("sh").args(["-c", "yes large-output"])).is_err());
        assert_eq!(
            run(Command::new("sh").args(["-c", "(sleep 0.05; printf done) & exit 0"])).unwrap(),
            b"done"
        );
    }
}
