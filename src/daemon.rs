use crate::{
    index::Index,
    protocol::{MAX_REQUEST, MAX_RESPONSE, Request, Response, WireResponse},
};
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::fd::AsRawFd,
    os::unix::{
        fs::{DirBuilderExt, FileTypeExt, OpenOptionsExt, PermissionsExt},
        net::{UnixListener, UnixStream},
        process::CommandExt,
    },
    path::PathBuf,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[derive(Clone)]
pub struct Config {
    pub directory: PathBuf,
    pub max_bytes: u64,
    pub idle: Duration,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let directory = match std::env::var_os("GREPGLINT_CACHE_DIR") {
            Some(path) => PathBuf::from(path),
            None => {
                let base = std::env::var_os("XDG_CACHE_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| {
                        PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".cache")
                    });
                base.join("grepglint")
            }
        };
        ensure!(
            directory.is_absolute(),
            "Cache directory must be absolute; set HOME or GREPGLINT_CACHE_DIR."
        );
        ensure!(
            directory
                .join("daemon.sock")
                .as_os_str()
                .as_encoded_bytes()
                .len()
                < 100,
            "Cache path is too long for a Unix socket; set GREPGLINT_CACHE_DIR to a shorter absolute path."
        );
        let mb: u64 = std::env::var("GREPGLINT_CACHE_MB")
            .unwrap_or_else(|_| "128".into())
            .parse()
            .context("GREPGLINT_CACHE_MB must be an integer")?;
        ensure!(
            (8..=1024).contains(&mb),
            "GREPGLINT_CACHE_MB must be between 8 and 1024."
        );
        let idle: u64 = std::env::var("GREPGLINT_IDLE_SECONDS")
            .unwrap_or_else(|_| "600".into())
            .parse()
            .context("GREPGLINT_IDLE_SECONDS must be an integer")?;
        ensure!(
            (1..=3600).contains(&idle),
            "GREPGLINT_IDLE_SECONDS must be between 1 and 3600."
        );
        Ok(Self {
            directory,
            max_bytes: mb * 1024 * 1024,
            idle: Duration::from_secs(idle),
        })
    }

    pub fn prepare(&self) -> Result<()> {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.directory)?;
        let metadata = fs::symlink_metadata(&self.directory)?;
        ensure!(
            metadata.is_dir() && !metadata.file_type().is_symlink(),
            "Cache directory must be a real directory."
        );
        fs::set_permissions(&self.directory, fs::Permissions::from_mode(0o700))?;
        Ok(())
    }
    fn socket(&self) -> PathBuf {
        self.directory.join("daemon.sock")
    }
}

fn restrict_process() -> Result<()> {
    // These limits apply only to the detached daemon and its Git children.
    unsafe {
        libc::umask(0o077);
        libc::setpriority(libc::PRIO_PROCESS, 0, 10);
        #[cfg(target_os = "linux")]
        {
            let limit = libc::rlimit {
                rlim_cur: 512 * 1024 * 1024,
                rlim_max: 512 * 1024 * 1024,
            };
            ensure!(
                libc::setrlimit(libc::RLIMIT_AS, &limit) == 0,
                "Cannot apply daemon memory limit"
            );
        }
        let cores = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        ensure!(
            libc::setrlimit(libc::RLIMIT_CORE, &cores) == 0,
            "Cannot disable daemon core dumps"
        );
    }
    Ok(())
}

struct Cleanup {
    directory: PathBuf,
}
impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.directory.join("daemon.sock"));
        let _ = fs::remove_file(self.directory.join("daemon.pid"));
    }
}

pub fn serve(config: &Config) -> Result<()> {
    config.prepare()?;
    restrict_process()?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(config.directory.join("daemon.lock"))?;
    match FileExt::try_lock_exclusive(&lock) {
        Ok(()) => (),
        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(()),
        Err(e) => return Err(e.into()),
    }
    if let Ok(metadata) = fs::symlink_metadata(config.socket()) {
        ensure!(
            metadata.file_type().is_socket(),
            "daemon.sock already exists and is not a socket"
        );
        fs::remove_file(config.socket())?;
    }
    let mut index = Index::open(&config.directory, config.max_bytes)?;
    let listener = UnixListener::bind(config.socket())?;
    let _cleanup = Cleanup {
        directory: config.directory.clone(),
    };
    listener.set_nonblocking(true)?;
    fs::set_permissions(config.socket(), fs::Permissions::from_mode(0o600))?;
    fs::write(
        config.directory.join("daemon.pid"),
        std::process::id().to_string(),
    )?;
    let _ = fs::remove_file(config.directory.join("startup-error.txt"));
    let mut last_request = Instant::now();
    while last_request.elapsed() < config.idle {
        let mut ready = libc::pollfd {
            fd: listener.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let timeout = config
            .idle
            .saturating_sub(last_request.elapsed())
            .as_millis()
            .min(i32::MAX as u128) as i32;
        // poll sleeps until a client arrives or the idle deadline expires.
        let status = unsafe { libc::poll(&mut ready, 1, timeout) };
        if status == 0 {
            break;
        }
        if status < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error.into());
        }
        match listener.accept() {
            Ok((mut stream, _)) => {
                let result = handle(&mut stream, &mut index);
                if let Err(error) = result {
                    let message: String = format!("{error:#}").chars().take(2000).collect();
                    let _ = send(&mut stream, &WireResponse::Error { message });
                }
                last_request = Instant::now();
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => return Err(e.into()),
        }
    }
    drop(listener);
    Ok(())
}

fn handle(stream: &mut UnixStream, index: &mut Index) -> Result<()> {
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    let deadline = Instant::now() + Duration::from_millis(250);
    let mut bytes = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        ensure!(
            !remaining.is_zero(),
            "Client exceeded the 250 ms request deadline."
        );
        stream.set_read_timeout(Some(remaining))?;
        let mut buffer = [0u8; 1024];
        let count = stream.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..count]);
        if let Some(end) = bytes.iter().position(|&b| b == b'\n') {
            bytes.truncate(end + 1);
            break;
        }
        ensure!(bytes.len() <= MAX_REQUEST, "Request exceeds 16 KiB.");
    }
    ensure!(
        bytes.len() <= MAX_REQUEST && bytes.last() == Some(&b'\n'),
        "Request must be a JSON line of at most 16 KiB."
    );
    let request: Request = serde_json::from_slice(&bytes).context("Invalid search request")?;
    let data = index.search(&request)?;
    send(stream, &WireResponse::Ok { data })
}

fn send(stream: &mut UnixStream, response: &WireResponse) -> Result<()> {
    let mut bytes = serde_json::to_vec(response)?;
    ensure!(
        bytes.len() < MAX_RESPONSE as usize,
        "Response exceeds 64 KiB; request fewer results."
    );
    bytes.push(b'\n');
    stream.write_all(&bytes)?;
    Ok(())
}

pub fn search(config: &Config, request: &Request) -> Result<Response> {
    let mut bytes = serde_json::to_vec(request)?;
    bytes.push(b'\n');
    ensure!(bytes.len() <= MAX_REQUEST, "Request exceeds 16 KiB.");
    config.prepare()?;
    let started = Instant::now();
    let mut child = None;
    let mut stream = loop {
        match UnixStream::connect(config.socket()) {
            Ok(stream) => break stream,
            Err(e)
                if e.kind() == std::io::ErrorKind::NotFound
                    || e.kind() == std::io::ErrorKind::ConnectionRefused =>
            {
                if let Some(process) = child.as_mut() {
                    let process: &mut std::process::Child = process;
                    if let Some(status) = process.try_wait()?
                        && !status.success()
                    {
                        let error = fs::read_to_string(config.directory.join("startup-error.txt"))
                            .unwrap_or_else(|_| status.to_string());
                        bail!("Cannot start Grepglint: {error}");
                    }
                } else {
                    let mut command = Command::new(std::env::current_exe()?);
                    command
                        .arg("__daemon")
                        .current_dir("/")
                        .stdin(Stdio::null())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null());
                    // A separate session keeps terminal closure from stopping the daemon.
                    unsafe {
                        command.pre_exec(|| {
                            if libc::setsid() < 0 {
                                return Err(std::io::Error::last_os_error());
                            }
                            Ok(())
                        });
                    }
                    child = Some(command.spawn().context("Cannot spawn Grepglint daemon")?);
                }
                ensure!(
                    started.elapsed() < Duration::from_secs(10),
                    "Daemon did not start within 10 seconds; use rg and inspect the cache directory."
                );
                thread::sleep(Duration::from_millis(25));
            }
            Err(e) => return Err(e.into()),
        }
    };
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    stream.set_read_timeout(Some(Duration::from_secs(120)))?;
    stream.write_all(&bytes)?;
    let mut response = Vec::new();
    BufReader::new(stream.take(MAX_RESPONSE + 1)).read_until(b'\n', &mut response)?;
    ensure!(
        response.len() <= MAX_RESPONSE as usize && response.last() == Some(&b'\n'),
        "Daemon stopped before returning a complete response; retry the query."
    );
    if let Some(mut child) = child {
        let _ = child.try_wait();
    }
    match serde_json::from_slice(&response).context("Invalid daemon response")? {
        WireResponse::Ok { data } => Ok(data),
        WireResponse::Error { message } => bail!("{message}"),
    }
}

pub fn record_startup_error(config: &Config, message: &str) {
    if let Ok(mut file) = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(config.directory.join("startup-error.txt"))
    {
        let _ = file.write_all(message.chars().take(2000).collect::<String>().as_bytes());
    }
}
