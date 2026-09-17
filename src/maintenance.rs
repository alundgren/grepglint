//! Account-local daemon controls and the lock shared with startup and search.
use crate::{
    daemon::Config,
    protocol::{ControlRequest, ControlResponse, Health, MAX_REQUEST, MAX_RESPONSE, VERSION},
};
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, Metadata, OpenOptions},
    io::{Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            fs::{FileTypeExt, MetadataExt, OpenOptionsExt},
            net::UnixStream,
        },
    },
    path::Path,
    thread,
    time::{Duration, Instant},
};

pub const MAINTENANCE_TIMEOUT: Duration = Duration::from_secs(35);
const CONTROL_TIMEOUT: Duration = Duration::from_secs(3);
const REFUSAL: &str = "Cannot identify a compatible daemon. Nothing was signalled. Pause searches and retry after idle exit; use rg meanwhile.";

pub(crate) fn check_owner(metadata: &Metadata) -> Result<()> {
    ensure!(
        metadata.uid() == unsafe { libc::geteuid() },
        "Cache resource belongs to another OS account; use a private cache directory."
    );
    Ok(())
}

fn private_directory(config: &Config) -> Result<bool> {
    let metadata = match fs::symlink_metadata(&config.directory) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    check_owner(&metadata)?;
    ensure!(
        metadata.is_dir() && metadata.mode() & 0o077 == 0,
        "Cache must be a private directory owned by this OS account."
    );
    Ok(true)
}

pub(crate) fn lock_file(config: &Config, name: &str) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(config.directory.join(name))?;
    let metadata = file.metadata()?;
    check_owner(&metadata)?;
    ensure!(
        metadata.is_file() && metadata.nlink() == 1 && metadata.mode() & 0o077 == 0,
        "Lock must be a private regular file; preserve unexpected files and inspect the cache."
    );
    Ok(file)
}

pub(crate) fn shared(config: &Config) -> Result<File> {
    let file = lock_file(config, "maintenance.lock")?;
    FileExt::try_lock_shared(&file)
        .context("Daemon maintenance is in progress; retry afterward or use rg")?;
    Ok(file)
}

pub(crate) fn exclusion_held(config: &Config) -> Result<bool> {
    let file = lock_file(config, "maintenance.lock")?;
    match FileExt::try_lock_shared(&file) {
        Ok(()) => Ok(false),
        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => Ok(true),
        Err(error) => Err(error.into()),
    }
}

/// Holding this guard excludes daemon startup and expensive search work.
/// Keep it through managed filesystem changes, then drop it to allow searches.
/// Never unlink the lock files: other processes may still have them open.
pub struct Maintenance<'a> {
    config: &'a Config,
    _lock: File,
    deadline: Instant,
    observed: Option<(String, SocketIdentity)>,
}

impl<'a> Maintenance<'a> {
    pub fn acquire(config: &'a Config) -> Result<Self> {
        config.prepare()?;
        Self::acquire_existing(config)
    }

    fn acquire_existing(config: &'a Config) -> Result<Self> {
        let deadline = Instant::now() + MAINTENANCE_TIMEOUT;
        let lock = lock_file(config, "maintenance.lock")?;
        wait_for_lock(&lock, deadline)?;
        Ok(Self {
            config,
            _lock: lock,
            deadline,
            observed: None,
        })
    }

    pub fn status(&mut self) -> Result<Option<Health>> {
        self.observed = None;
        let Some((health, socket)) = inspect(self.config, self.deadline)? else {
            return Ok(None);
        };
        self.observed = Some((health.instance.clone(), socket));
        Ok(Some(health))
    }

    /// Stop only the instance the caller inspected while holding this guard.
    pub fn shutdown(&self, expected: &str) -> Result<bool> {
        let (instance, observed) = self
            .observed
            .as_ref()
            .context("Read daemon status under maintenance exclusion before shutdown")?;
        ensure!(
            instance == expected,
            "Daemon instance differs from inspected status; nothing was stopped."
        );
        let Some((mut stream, socket)) = connect(self.config)? else {
            return Ok(false);
        };
        ensure!(
            socket == *observed,
            "Daemon socket changed after status. {REFUSAL}"
        );
        let response = exchange(
            &mut stream,
            &ControlRequest::Shutdown {
                version: VERSION,
                instance: expected.to_owned(),
            },
            self.deadline,
        )?;
        ensure!(
            matches!(response, ControlResponse::Stopped { ref instance } if instance == expected),
            "{REFUSAL}"
        );
        // A reply precedes process cleanup. The daemon lock proves cleanup completed.
        let lock = lock_file(self.config, "daemon.lock")?;
        wait_for_lock(&lock, self.deadline)?;
        ensure!(
            !socket.matches(&self.config.socket()),
            "Daemon acknowledged shutdown but its socket remains; inspect the private cache. No PID was signalled."
        );
        Ok(true)
    }
}

fn wait_for_lock(file: &File, deadline: Instant) -> Result<()> {
    loop {
        match FileExt::try_lock_exclusive(file) {
            Ok(()) => return Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                ensure!(
                    Instant::now() < deadline,
                    "Maintenance deadline expired; nothing was signalled. Pause searches and retry after idle exit; use rg meanwhile."
                );
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => return Err(error.into()),
        }
    }
}

pub fn status(config: &Config) -> Result<Option<Health>> {
    Ok(inspect(config, Instant::now() + CONTROL_TIMEOUT)?.map(|(health, _)| health))
}

pub fn shutdown(config: &Config, expected: Option<&str>) -> Result<bool> {
    if !private_directory(config)? {
        return Ok(false);
    }
    let mut guard = Maintenance::acquire_existing(config)?;
    let Some(health) = guard.status()? else {
        return Ok(false);
    };
    if let Some(expected) = expected {
        ensure!(
            expected == health.instance,
            "Daemon instance changed; run status again. No daemon was stopped."
        );
    }
    guard.shutdown(&health.instance)
}

fn inspect(config: &Config, deadline: Instant) -> Result<Option<(Health, SocketIdentity)>> {
    let Some((mut stream, socket)) = connect(config)? else {
        return Ok(None);
    };
    match exchange(
        &mut stream,
        &ControlRequest::Health { version: VERSION },
        deadline,
    )? {
        ControlResponse::Healthy { data } => {
            ensure!(
                data.protocol_version == VERSION && !data.instance.is_empty(),
                "{REFUSAL}"
            );
            ensure!(
                socket.matches(&config.socket()),
                "Daemon socket changed during status. {REFUSAL}"
            );
            Ok(Some((data, socket)))
        }
        _ => bail!("{REFUSAL}"),
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) struct SocketIdentity {
    device: u64,
    inode: u64,
}
impl SocketIdentity {
    pub(crate) fn read(path: &Path) -> Result<Self> {
        let metadata = fs::symlink_metadata(path)?;
        check_owner(&metadata)?;
        ensure!(
            metadata.file_type().is_socket() && metadata.mode() & 0o077 == 0,
            "Refusing an unexpected or non-private daemon socket. Nothing was signalled; use rg."
        );
        Ok(Self {
            device: metadata.dev(),
            inode: metadata.ino(),
        })
    }
    pub(crate) fn matches(&self, path: &Path) -> bool {
        Self::read(path).is_ok_and(|value| value.device == self.device && value.inode == self.inode)
    }
}

fn connect(config: &Config) -> Result<Option<(UnixStream, SocketIdentity)>> {
    if !private_directory(config)? {
        return Ok(None);
    }
    let socket = match SocketIdentity::read(&config.socket()) {
        Ok(socket) => socket,
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            // A live daemon may be starting or its pathname may have been removed.
            let path = config.directory.join("daemon.lock");
            let lock = match OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                .open(path)
            {
                Ok(lock) => lock,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                Err(error) => return Err(error.into()),
            };
            let metadata = lock.metadata()?;
            check_owner(&metadata)?;
            ensure!(
                metadata.is_file() && metadata.mode() & 0o077 == 0,
                "Unexpected daemon lock; preserve it and inspect the cache."
            );
            ensure!(
                FileExt::try_lock_exclusive(&lock).is_ok(),
                "Daemon socket is missing while its lock is held. {REFUSAL}"
            );
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    // Nonblocking connect avoids hanging on a full Unix listen queue.
    let stream = nonblocking_connect(&config.socket()).context(REFUSAL)?;
    check_peer(&stream)?;
    ensure!(
        socket.matches(&config.socket()),
        "Daemon socket changed during connection. {REFUSAL}"
    );
    Ok(Some((stream, socket)))
}

fn nonblocking_connect(path: &Path) -> Result<UnixStream> {
    use std::os::fd::FromRawFd;
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0) };
    ensure!(fd >= 0, "Cannot create control socket");
    let stream = unsafe { UnixStream::from_raw_fd(fd) };
    stream.set_nonblocking(true)?;
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    #[cfg(target_os = "macos")]
    {
        address.sun_len = std::mem::size_of_val(&address) as u8;
    }
    let bytes = path.as_os_str().as_encoded_bytes();
    ensure!(
        bytes.len() < address.sun_path.len(),
        "Cache socket path is too long"
    );
    for (target, source) in address.sun_path.iter_mut().zip(bytes) {
        *target = *source as libc::c_char;
    }
    let result = unsafe {
        libc::connect(
            fd,
            (&address as *const libc::sockaddr_un).cast(),
            std::mem::size_of_val(&address) as libc::socklen_t,
        )
    };
    if result != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    stream.set_nonblocking(false)?;
    Ok(stream)
}

pub(crate) fn check_peer(stream: &UnixStream) -> Result<()> {
    #[cfg(target_os = "linux")]
    let uid = unsafe {
        let mut credentials: libc::ucred = std::mem::zeroed();
        let mut length = std::mem::size_of_val(&credentials) as libc::socklen_t;
        ensure!(
            libc::getsockopt(
                stream.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                (&mut credentials as *mut libc::ucred).cast(),
                &mut length
            ) == 0,
            "Cannot verify daemon socket peer"
        );
        credentials.uid
    };
    #[cfg(not(target_os = "linux"))]
    let uid = unsafe {
        let mut uid = 0;
        let mut gid = 0;
        ensure!(
            libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) == 0,
            "Cannot verify daemon socket peer"
        );
        uid
    };
    ensure!(
        uid == unsafe { libc::geteuid() },
        "Socket peer belongs to another OS account; refusing daemon control."
    );
    Ok(())
}

fn exchange(
    stream: &mut UnixStream,
    request: &ControlRequest,
    deadline: Instant,
) -> Result<ControlResponse> {
    let deadline = deadline.min(Instant::now() + CONTROL_TIMEOUT);
    let mut bytes = serde_json::to_vec(request)?;
    bytes.push(b'\n');
    ensure!(bytes.len() <= MAX_REQUEST, "Control request exceeds 16 KiB");
    stream.set_write_timeout(Some(remaining(deadline)?))?;
    stream.write_all(&bytes).context(REFUSAL)?;
    let mut response = Vec::new();
    loop {
        stream.set_read_timeout(Some(remaining(deadline)?))?;
        let mut buffer = [0u8; 1024];
        let count = stream.read(&mut buffer).context(REFUSAL)?;
        ensure!(count != 0, "{REFUSAL}");
        response.extend_from_slice(&buffer[..count]);
        ensure!(response.len() <= MAX_RESPONSE as usize, "{REFUSAL}");
        if let Some(end) = response.iter().position(|&byte| byte == b'\n') {
            response.truncate(end);
            break;
        }
    }
    serde_json::from_slice(&response).context(REFUSAL)
}

fn remaining(deadline: Instant) -> Result<Duration> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    ensure!(!remaining.is_zero(), "Control deadline expired. {REFUSAL}");
    Ok(remaining)
}

pub(crate) fn build_health(config: &Config) -> Result<Health> {
    let mut random = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut random)?;
    let instance = random.iter().map(|byte| format!("{byte:02x}")).collect();
    #[cfg(target_os = "linux")]
    let executable = File::open("/proc/self/exe")?;
    #[cfg(not(target_os = "linux"))]
    let executable = File::open(std::env::current_exe()?)?;
    let mut digest = Sha256::new();
    let count = std::io::copy(&mut executable.take(128 * 1024 * 1024 + 1), &mut digest)?;
    ensure!(
        count <= 128 * 1024 * 1024,
        "Executable exceeds build identity limit of 128 MiB"
    );
    Ok(Health {
        build_version: env!("CARGO_PKG_VERSION").into(),
        executable_sha256: format!("{:x}", digest.finalize()),
        protocol_version: VERSION,
        instance,
        cache_directory: fs::canonicalize(&config.directory)?
            .to_string_lossy()
            .into_owned(),
        database_bytes: config.max_bytes,
        idle_seconds: config.idle.as_secs(),
        request_bytes: MAX_REQUEST,
        response_bytes: MAX_RESPONSE,
        work_seconds: 30,
        sqlite_heap_bytes: 64 * 1024 * 1024,
        address_space_bytes: if cfg!(target_os = "linux") {
            Some(512 * 1024 * 1024)
        } else {
            None
        },
    })
}
