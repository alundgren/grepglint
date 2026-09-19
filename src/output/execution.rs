//! Finite command execution with a replayable, bounded capture.
use super::*;
use clap::{Args, ValueEnum};
use std::{
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::process::{CommandExt, ExitStatusExt},
    },
    process::{Child, Command, ExitStatus, Stdio},
    sync::atomic::{AtomicI32, Ordering},
};

#[derive(Clone, Copy, Debug, ValueEnum, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Profile {
    Unchanged,
    Preview16k,
    Preview32k,
}
impl Profile {
    fn threshold(self) -> usize {
        match self {
            Self::Unchanged => 0,
            Self::Preview16k => 16 * 1024,
            Self::Preview32k => 32 * 1024,
        }
    }
}

#[derive(Args)]
#[command(
    after_help = "Runs SHELL -c COMMAND once with inherited permissions and limits. Stdout and stderr share one pipe in kernel write order. Stdin is /dev/null. Only finite, noninteractive commands are supported; no TTY or persistent background services. Capture failure forwards original bytes to the caller, whose executor may truncate them. One bounded JSON accounting record is written to stderr at completion."
)]
pub struct Options {
    /// Shell executable, invoked directly without an outer shell
    #[arg(long)]
    pub shell: PathBuf,
    /// Exact command string passed as one argument to the selected shell
    #[arg(long, allow_hyphen_values = true)]
    pub command: String,
    /// Producer working directory, defaults to the caller's directory
    #[arg(long)]
    pub cwd: Option<PathBuf>,
    /// Set a producer environment variable, repeatable; remaining variables are inherited
    #[arg(long = "env", value_parser = environment)]
    pub environment: Vec<(String, String)>,
    /// Fixed experimental output policy
    #[arg(long, value_enum, default_value = "preview16k")]
    pub profile: Profile,
    /// Caller-selected command deadline; otherwise wait until completion or cancellation
    #[arg(long, value_parser = clap::value_parser!(u32).range(1..))]
    pub timeout_seconds: Option<u32>,
}
fn environment(value: &str) -> std::result::Result<(String, String), String> {
    let (key, value) = value.split_once('=').ok_or("Expected NAME=VALUE")?;
    if key.is_empty() || key.contains('\0') || value.contains('\0') {
        return Err("Environment names must be nonempty and values cannot contain NUL".into());
    }
    Ok((key.into(), value.into()))
}

static SIGNAL: AtomicI32 = AtomicI32::new(0);
extern "C" fn cancel(signal: i32) {
    SIGNAL.store(signal, Ordering::Relaxed);
}
struct Signals(Vec<(i32, libc::sigaction)>);
impl Signals {
    fn install() -> Result<Self> {
        SIGNAL.store(0, Ordering::Relaxed);
        let mut guard = Self(Vec::new());
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
            action.sa_sigaction = cancel as usize;
            unsafe {
                libc::sigemptyset(&mut action.sa_mask);
            }
            let mut old = unsafe { std::mem::zeroed() };
            ensure!(
                unsafe { libc::sigaction(signal, &action, &mut old) } == 0,
                "Cannot install command cancellation handler"
            );
            guard.0.push((signal, old));
        }
        Ok(guard)
    }
    fn allow_storage_write_errors(&mut self) -> Result<()> {
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = libc::SIG_IGN;
        unsafe {
            libc::sigemptyset(&mut action.sa_mask);
        }
        let mut old = unsafe { std::mem::zeroed() };
        ensure!(
            unsafe { libc::sigaction(libc::SIGXFSZ, &action, &mut old) } == 0,
            "Cannot handle output storage file-size errors"
        );
        self.0.push((libc::SIGXFSZ, old));
        Ok(())
    }
}
impl Drop for Signals {
    fn drop(&mut self) {
        for (signal, action) in &self.0 {
            unsafe {
                libc::sigaction(*signal, action, std::ptr::null_mut());
            }
        }
    }
}

#[derive(Clone, Copy)]
struct Control {
    deadline: Option<Instant>,
}
impl Control {
    fn check(self) -> Result<()> {
        ensure!(SIGNAL.load(Ordering::Relaxed) == 0, "Command cancelled");
        ensure!(
            self.deadline.is_none_or(|d| Instant::now() < d),
            "Command deadline reached"
        );
        crate::temporary_rank::consumer_connected(1)
    }
}
fn storage_check(control: Control) -> impl Fn() -> Result<()> + Copy + Send + 'static {
    let deadline = Instant::now() + LOCK_WAIT;
    move || {
        control.check()?;
        ensure!(
            Instant::now() < deadline,
            "Command storage operation timed out"
        );
        Ok(())
    }
}

struct Producer {
    child: Child,
    reaped: bool,
}
impl Producer {
    fn stop(&mut self, signal: i32) -> Result<ExitStatus> {
        unsafe {
            libc::kill(-(self.child.id() as i32), signal);
        }
        // Keep the leader unreaped until the final group signal, so its PID cannot be reused.
        std::thread::sleep(Duration::from_millis(250));
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        let status = self.child.wait()?;
        self.reaped = true;
        Ok(status)
    }
}
impl Drop for Producer {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.stop(libc::SIGTERM);
        }
    }
}
fn pipe() -> Result<(File, File)> {
    let mut fds = [-1; 2];
    ensure!(
        unsafe { libc::pipe(fds.as_mut_ptr()) } == 0,
        "Cannot create command output pipe"
    );
    let read = unsafe { File::from_raw_fd(fds[0]) };
    let write = unsafe { File::from_raw_fd(fds[1]) };
    for fd in fds {
        ensure!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } == 0,
            "Cannot protect command pipe descriptors"
        );
    }
    Ok((read, write))
}

fn deliver(fd: i32, bytes: &[u8], control: Option<Control>, returned: &mut u64) -> Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    ensure!(
        flags >= 0 && unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } >= 0,
        "Cannot bound command output delivery"
    );
    struct Restore(i32, i32);
    impl Drop for Restore {
        fn drop(&mut self) {
            unsafe {
                libc::fcntl(self.0, libc::F_SETFL, self.1);
            }
        }
    }
    let _restore = Restore(fd, flags);
    let mut last = Instant::now();
    let mut remaining = bytes;
    while !remaining.is_empty() {
        if let Some(control) = control {
            control.check()?;
        }
        ensure!(
            last.elapsed() < LOCK_WAIT,
            "Command output consumer stalled"
        );
        let n = unsafe { libc::write(fd, remaining.as_ptr().cast(), remaining.len()) };
        if n > 0 {
            remaining = &remaining[n as usize..];
            *returned = returned.saturating_add(n as u64);
            last = Instant::now();
        } else {
            let error = std::io::Error::last_os_error();
            if !matches!(
                error.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
            ) {
                return Err(error.into());
            }
            let mut poll = libc::pollfd {
                fd,
                events: libc::POLLOUT,
                revents: 0,
            };
            unsafe {
                libc::poll(&mut poll, 1, 50);
            }
        }
    }
    Ok(())
}

struct Spool {
    file: File,
    len: u64,
    store: Store,
    capture: Capture,
    committed: bool,
}
impl Spool {
    fn new(config: &Config, control: Control) -> Result<Self> {
        let mut store = Store::open_checked(config, storage_check(control))?;
        let check = storage_check(control);
        store
            .db
            .progress_handler(1000, Some(move || check().is_err()))?;
        let capture = store.begin()?;
        let file = tempfile::tempfile_in(&store.directory);
        match file {
            Ok(file) => Ok(Self {
                file,
                len: 0,
                store,
                capture,
                committed: false,
            }),
            Err(e) => {
                let _ = store.abort(&capture.handle);
                Err(e.into())
            }
        }
    }
    fn append(&mut self, bytes: &[u8]) -> Result<()> {
        ensure!(self.len + bytes.len() as u64 <= MAX_OUTPUT, "oversized");
        self.store.space()?;
        self.file.write_all(bytes)?;
        self.len += bytes.len() as u64;
        Ok(())
    }
    fn replay(&mut self, control: Control, returned: &mut u64) -> Result<()> {
        self.file.seek(SeekFrom::Start(0))?;
        let mut left = self.len;
        let mut buf = [0; BUFFER];
        while left > 0 {
            control.check()?;
            let count = left.min(BUFFER as u64) as usize;
            self.file.read_exact(&mut buf[..count])?;
            deliver(1, &buf[..count], Some(control), returned)?;
            left -= count as u64;
        }
        Ok(())
    }
    fn retain(&mut self, control: Control, digest: &Sha256) -> Result<()> {
        self.file.seek(SeekFrom::Start(0))?;
        let mut left = self.len;
        let mut buf = [0; BUFFER];
        while left > 0 {
            control.check()?;
            let count = left.min(BUFFER as u64) as usize;
            self.file.read_exact(&mut buf[..count])?;
            let check = storage_check(control);
            self.store
                .db
                .progress_handler(1000, Some(move || check().is_err()))?;
            self.store.append(&mut self.capture, &buf[..count])?;
            left -= count as u64;
        }
        ensure!(
            self.capture.digest.clone().finalize() == digest.clone().finalize(),
            "Spool integrity check failed"
        );
        let check = storage_check(control);
        self.store
            .db
            .progress_handler(1000, Some(move || check().is_err()))?;
        // A command may run longer than the fixed retention lifetime. Never publish an expired handle.
        let created: i64 = self.store.db.query_row(
            "SELECT created FROM outputs WHERE handle=?1",
            [&self.capture.handle],
            |r| r.get(0),
        )?;
        ensure!(
            (now()? as i64) < created + TTL as i64,
            "Capture expired before command completion"
        );
        self.store.commit(&self.capture)?;
        self.committed = true;
        Ok(())
    }
}
impl Drop for Spool {
    fn drop(&mut self) {
        if !self.committed {
            let deadline = Instant::now() + LOCK_WAIT;
            let _ = self
                .store
                .db
                .progress_handler(1000, Some(move || Instant::now() >= deadline));
            let _ = self.store.abort(&self.capture.handle);
            // A failed deletion remains uncommitted; the next request cleans it under this slot's lock.
        }
    }
}

struct CaptureOutput {
    pending: Vec<u8>,
    partial: Vec<u8>,
    head: Vec<u8>,
    tail: Vec<u8>,
    spool: Option<Spool>,
    error: Option<&'static str>,
    bytes: u64,
    returned: u64,
    digest: Sha256,
    profile: Profile,
}
impl CaptureOutput {
    fn new(profile: Profile) -> Self {
        Self {
            pending: Vec::with_capacity(if matches!(profile, Profile::Unchanged) {
                0
            } else {
                profile.threshold() + BUFFER
            }),
            partial: Vec::with_capacity(BUFFER + 3),
            head: Vec::with_capacity(256),
            tail: Vec::with_capacity(256),
            spool: None,
            error: None,
            bytes: 0,
            returned: 0,
            digest: Sha256::new(),
            profile,
        }
    }
    fn fallback(&mut self, reason: &'static str, control: Control) -> Result<()> {
        self.error = Some(reason);
        let diagnostic = format!(
            "grepglint: output capture bypassed ({reason}); forwarding original bytes; the caller may truncate them. Command will not be rerun.\n"
        );
        // A blocked diagnostic destination must not prevent forwarding the producer's bytes.
        let _ = deliver(2, diagnostic.as_bytes(), Some(control), &mut 0);
        if let Some(spool) = &mut self.spool {
            spool.replay(control, &mut self.returned)?;
        }
        self.spool = None;
        deliver(1, &self.pending, Some(control), &mut self.returned)?;
        self.pending.clear();
        Ok(())
    }
    fn accept(&mut self, bytes: &[u8], control: Control) -> Result<()> {
        self.bytes = self.bytes.saturating_add(bytes.len() as u64);
        if matches!(self.profile, Profile::Unchanged) || self.error.is_some() {
            return deliver(1, bytes, Some(control), &mut self.returned);
        }
        self.partial.extend_from_slice(bytes);
        let invalid = match std::str::from_utf8(&self.partial) {
            Ok(_) => {
                self.partial.clear();
                false
            }
            Err(e) if e.error_len().is_none() => {
                self.partial.drain(..e.valid_up_to());
                false
            }
            Err(_) => true,
        };
        let reason = if bytes.contains(&0) {
            Some("nul")
        } else if invalid {
            Some("invalid-utf8")
        } else if self.bytes > MAX_OUTPUT {
            Some("oversized")
        } else {
            None
        };
        if let Some(reason) = reason {
            self.fallback(reason, control)?;
            return deliver(1, bytes, Some(control), &mut self.returned);
        }
        self.digest.update(bytes);
        self.head
            .extend_from_slice(&bytes[..bytes.len().min(256 - self.head.len())]);
        let suffix = &bytes[bytes.len().saturating_sub(256)..];
        let remove = (self.tail.len() + suffix.len()).saturating_sub(256);
        self.tail.drain(..remove);
        self.tail.extend_from_slice(suffix);
        if let Some(spool) = &mut self.spool {
            if spool.append(bytes).is_err() {
                self.fallback("spool-write", control)?;
                deliver(1, bytes, Some(control), &mut self.returned)?;
            }
        } else {
            self.pending.extend_from_slice(bytes);
            if self.pending.len() > self.profile.threshold() {
                match Config::from_env().and_then(|config| Spool::new(&config, control)) {
                    Ok(mut spool) => {
                        if spool.append(&self.pending).is_ok() {
                            self.pending.clear();
                            self.spool = Some(spool);
                        } else {
                            drop(spool);
                            self.fallback("spool-write", control)?;
                        }
                    }
                    Err(_) => self.fallback("storage-unavailable", control)?,
                }
            }
        }
        Ok(())
    }
    fn finish(&mut self, status: ExitStatus, control: Control) -> Result<Option<String>> {
        if self.error.is_some() || matches!(self.profile, Profile::Unchanged) {
            return Ok(None);
        }
        if !self.partial.is_empty() {
            self.fallback("invalid-utf8", control)?;
            return Ok(None);
        }
        if let Some(spool) = &mut self.spool {
            if spool.retain(control, &self.digest).is_err() {
                self.fallback("retention-failed", control)?;
                return Ok(None);
            }
            let handle = spool.capture.handle.clone();
            let preview = format!(
                "Incomplete output preview. Producer status: {}. Original bytes: {}.\nOutput {}\nRetained for at most {} seconds from capture start; may expire or be evicted sooner.\nPage all original bytes: grepglint output page {} --json\nSearch omitted output: grepglint output search {} \"<query>\" --json\nHead: {}\n...\nTail: {}\n",
                status_code(status),
                self.bytes,
                handle,
                TTL,
                handle,
                handle,
                printable(&String::from_utf8_lossy(&self.head)),
                printable(&String::from_utf8_lossy(&self.tail))
            );
            ensure!(preview.len() <= 8192, "Command preview exceeds its budget");
            deliver(1, preview.as_bytes(), Some(control), &mut self.returned)?;
            Ok(Some(handle))
        } else {
            deliver(1, &self.pending, Some(control), &mut self.returned)?;
            self.pending.clear();
            Ok(None)
        }
    }
}
fn status_code(status: ExitStatus) -> i32 {
    status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0))
}

/// Called once per CLI process; signal handling belongs to that process.
pub fn run(options: &Options) -> Result<i32> {
    let mut signals = Signals::install()?;
    let start = Instant::now();
    let control = Control {
        deadline: options
            .timeout_seconds
            .map(|seconds| start + Duration::from_secs(seconds as u64)),
    };
    let (mut reader, writer) = pipe()?;
    let mut command = Command::new(&options.shell);
    command
        .arg("-c")
        .arg(&options.command)
        .stdin(Stdio::null())
        .stdout(writer.try_clone()?)
        .stderr(writer)
        .process_group(0)
        .envs(options.environment.iter().map(|(key, value)| (key, value)));
    if let Some(cwd) = &options.cwd {
        command.current_dir(cwd);
    }
    let child = command
        .spawn()
        .context("Cannot start selected command shell")?;
    drop(command);
    let mut producer = Producer {
        child,
        reaped: false,
    };
    // Change only the wrapper after spawning, preserving the producer's inherited disposition.
    signals.allow_storage_write_errors()?;
    let mut output = CaptureOutput::new(options.profile);
    let mut eof = false;
    let result = (|| -> Result<ExitStatus> {
        loop {
            control.check()?;
            if eof {
                if let Some(status) = producer.child.try_wait()? {
                    producer.reaped = true;
                    return Ok(status);
                }
            } else {
                let mut poll = libc::pollfd {
                    fd: reader.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                };
                let n = unsafe { libc::poll(&mut poll, 1, 50) };
                if n < 0 {
                    let error = std::io::Error::last_os_error();
                    if error.kind() == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err(error.into());
                }
                if poll.revents != 0 {
                    let mut buf = [0; BUFFER];
                    match reader.read(&mut buf) {
                        Ok(0) => eof = true,
                        Ok(n) => output.accept(&buf[..n], control)?,
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => (),
                        Err(e) => return Err(e.into()),
                    }
                }
                continue;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    })();
    let producer_ms = start.elapsed().as_millis();
    let mut handle = None;
    let mut cancelled = false;
    let status = match result {
        Ok(status) => {
            if output.finish(status, control).is_err() {
                cancelled = true;
            } else if let Some(spool) = &output.spool {
                handle = Some(spool.capture.handle.clone());
            }
            status
        }
        Err(_) => {
            cancelled = true;
            let signal = SIGNAL.load(Ordering::Relaxed);
            producer.stop(if signal == 0 { libc::SIGTERM } else { signal })?
        }
    };
    let code = if cancelled {
        let signal = SIGNAL.load(Ordering::Relaxed);
        if signal != 0 {
            128 + signal
        } else if control.deadline.is_some_and(|d| Instant::now() >= d) {
            124
        } else {
            1
        }
    } else {
        status_code(status)
    };
    let metadata = serde_json::json!({
        "type": "grepglint-command", "version": 1, "profile": options.profile,
        "capture": if cancelled { "cancelled" } else if handle.is_some() { "captured" } else { "bypassed" },
        "original_bytes": output.bytes, "returned_bytes": output.returned,
        "producer_status": status_code(status), "producer_signal": status.signal(), "exit_status": code,
        "capture_error": output.error, "handle": handle,
        "execution_error": if !cancelled { None } else if SIGNAL.load(Ordering::Relaxed) != 0 { Some("signal") } else if control.deadline.is_some_and(|d| Instant::now() >= d) { Some("deadline") } else { Some("delivery-or-io") },
        "producer_ms": producer_ms, "elapsed_ms": start.elapsed().as_millis(),
    });
    drop(output);
    let record = format!("{metadata}\n");
    ensure!(record.len() <= 2048, "Command metadata exceeds its budget");
    let _ = deliver(2, record.as_bytes(), None, &mut 0);
    Ok(code)
}
