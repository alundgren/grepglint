use fs2::FileExt;
use grepglint::{daemon::Config, maintenance::Maintenance, protocol::Health};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{OpenOptionsExt, PermissionsExt, symlink},
        net::{UnixListener, UnixStream},
    },
    path::PathBuf,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use tempfile::TempDir;

struct Fixture {
    _temp: TempDir,
    root: PathBuf,
    cache: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("repo");
        fs::create_dir(&root).unwrap();
        assert!(
            Command::new("git")
                .args(["init", "-q"])
                .arg(&root)
                .status()
                .unwrap()
                .success()
        );
        fs::write(root.join("source.txt"), "maintenancequartz\n").unwrap();
        let cache = temp.path().join("cache");
        Self {
            _temp: temp,
            root,
            cache,
        }
    }
    fn config(&self) -> Config {
        Config {
            directory: self.cache.clone(),
            max_bytes: 16 * 1024 * 1024,
            idle: Duration::from_secs(60),
        }
    }
    fn command(&self) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_grepglint"));
        cmd.current_dir(&self.root)
            .env("GREPGLINT_CACHE_DIR", &self.cache)
            .env("GREPGLINT_CACHE_MB", "16")
            .env("GREPGLINT_IDLE_SECONDS", "60");
        cmd
    }
    fn start(&self) -> Health {
        let output = self
            .command()
            .args(["search", "--json", "maintenancequartz"])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{} {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        self.health()
    }
    fn health(&self) -> Health {
        grepglint::maintenance::status(&self.config())
            .unwrap()
            .unwrap()
    }
    fn raw(&self, request: &str) -> serde_json::Value {
        let mut stream = UnixStream::connect(self.cache.join("daemon.sock")).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(4)))
            .unwrap();
        stream.write_all(request.as_bytes()).unwrap();
        stream.write_all(b"\n").unwrap();
        let mut response = String::new();
        BufReader::new(stream).read_line(&mut response).unwrap();
        serde_json::from_str(&response).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = grepglint::maintenance::shutdown(&self.config(), None);
    }
}

#[test]
fn missing_daemon_help_and_catalog_do_not_start_or_create_cache() {
    let f = Fixture::new();
    for args in [
        vec!["status", "--json"],
        vec!["shutdown"],
        vec!["--help"],
        vec!["tools", "--json"],
    ] {
        let started = Instant::now();
        let output = f.command().args(&args).output().unwrap();
        assert!(output.status.success());
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(!f.cache.exists());
        if args[0] == "status" {
            assert_eq!(output.stdout, b"null\n");
        }
        if args[0] == "tools" {
            let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(value["tools"].as_array().unwrap().len(), 1);
        }
    }
}

#[test]
fn health_reports_running_build_settings_and_restarts_after_shutdown() {
    use sha2::{Digest, Sha256};
    let f = Fixture::new();
    let health = f.start();
    assert_eq!(health.build_version, env!("CARGO_PKG_VERSION"));
    assert_eq!(health.protocol_version, 1);
    assert_eq!(health.database_bytes, 16 * 1024 * 1024);
    assert_eq!(health.idle_seconds, 60);
    assert_eq!(
        health.cache_directory,
        fs::canonicalize(&f.cache).unwrap().to_str().unwrap()
    );
    assert_eq!(
        health.executable_sha256,
        format!(
            "{:x}",
            Sha256::digest(fs::read(env!("CARGO_BIN_EXE_grepglint")).unwrap())
        )
    );
    assert_eq!(health.instance.len(), 32);
    assert_eq!(health.request_bytes, 16 * 1024);
    assert_eq!(health.response_bytes, 64 * 1024);
    let started = Instant::now();
    assert!(grepglint::maintenance::shutdown(&f.config(), Some(&health.instance)).unwrap());
    assert!(started.elapsed() < Duration::from_secs(3));
    assert!(!grepglint::maintenance::shutdown(&f.config(), None).unwrap());
    assert!(
        grepglint::maintenance::status(&f.config())
            .unwrap()
            .is_none()
    );
    assert_ne!(f.start().instance, health.instance);
}

#[test]
fn wrong_instance_and_unrelated_pid_never_stop_a_process() {
    let f = Fixture::new();
    let health = f.start();
    let mut unrelated = Command::new("sleep").arg("20").spawn().unwrap();
    fs::write(f.cache.join("daemon.pid"), unrelated.id().to_string()).unwrap();
    assert!(grepglint::maintenance::shutdown(&f.config(), Some("old-instance")).is_err());
    assert_eq!(f.health().instance, health.instance);
    assert!(unrelated.try_wait().unwrap().is_none());
    assert!(grepglint::maintenance::shutdown(&f.config(), Some(&health.instance)).unwrap());
    assert!(unrelated.try_wait().unwrap().is_none());
    assert_eq!(
        fs::read_to_string(f.cache.join("daemon.pid")).unwrap(),
        unrelated.id().to_string()
    );
    unrelated.kill().unwrap();
    unrelated.wait().unwrap();
}

#[test]
fn exclusion_rejects_searches_and_startup_then_releases_on_drop_and_crash() {
    let f = Fixture::new();
    let config = f.config();
    let guard = Maintenance::acquire(&config).unwrap();
    let output = f
        .command()
        .args(["search", "--json", "maintenancequartz"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("maintenance"));
    assert!(!f.cache.join("daemon.sock").exists());
    drop(guard);
    let health = f.start();
    let mut guard = Maintenance::acquire(&config).unwrap();
    let response = f.raw(r#"{"version":1,"cwd":"/","query":"anything","limit":5}"#);
    assert_eq!(response["status"], "error");
    assert!(
        response["message"]
            .as_str()
            .unwrap()
            .contains("maintenance")
    );
    assert_eq!(guard.status().unwrap().unwrap().instance, health.instance);
    assert!(guard.shutdown(&health.instance).unwrap());
    drop(guard);
    // A child holds the same OS lock and then dies, without cleanup code.
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "lock_holder", "--ignored", "--nocapture"])
        .env("GREPGLINT_TEST_LOCK", f.cache.join("maintenance.lock"))
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    loop {
        line.clear();
        assert!(output.read_line(&mut line).unwrap() > 0);
        if line.contains("LOCKED") {
            break;
        }
    }
    assert!(
        !f.command()
            .args(["search", "--json", "maintenancequartz"])
            .output()
            .unwrap()
            .status
            .success()
    );
    child.kill().unwrap();
    child.wait().unwrap();
    assert_ne!(f.start().instance, health.instance);
}

#[test]
#[ignore]
fn lock_holder() {
    let path = std::env::var_os("GREPGLINT_TEST_LOCK").unwrap();
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    FileExt::lock_exclusive(&lock).unwrap();
    println!("LOCKED");
    std::io::stdout().flush().unwrap();
    thread::sleep(Duration::from_secs(60));
}

#[test]
fn two_simultaneous_shutdowns_are_idempotent() {
    let f = Fixture::new();
    f.start();
    let a = f
        .command()
        .arg("shutdown")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let b = f
        .command()
        .arg("shutdown")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    for output in [a.wait_with_output().unwrap(), b.wait_with_output().unwrap()] {
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    assert!(
        grepglint::maintenance::status(&f.config())
            .unwrap()
            .is_none()
    );
}

#[test]
fn stale_socket_and_unexpected_files_are_preserved_and_refused() {
    let f = Fixture::new();
    f.config().prepare().unwrap();
    let socket = f.cache.join("daemon.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    drop(listener);
    assert!(grepglint::maintenance::shutdown(&f.config(), None).is_err());
    assert!(socket.exists());
    fs::remove_file(&socket).unwrap();
    fs::write(&socket, "unrelated").unwrap();
    assert!(grepglint::maintenance::status(&f.config()).is_err());
    assert_eq!(fs::read_to_string(&socket).unwrap(), "unrelated");
    fs::remove_file(&socket).unwrap();
    let target = f.root.join("source.txt");
    symlink(&target, &socket).unwrap();
    assert!(grepglint::maintenance::shutdown(&f.config(), None).is_err());
    assert_eq!(fs::read_to_string(target).unwrap(), "maintenancequartz\n");
}

#[test]
fn replaced_socket_is_not_authority_and_daemon_cleanup_preserves_it() {
    let f = Fixture::new();
    let health = f.start();
    let socket = f.cache.join("daemon.sock");
    let moved = f.cache.join("original.sock");
    fs::rename(&socket, &moved).unwrap();
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    let mut stream = UnixStream::connect(&moved).unwrap();
    stream
        .write_all(
            format!(
                "{{\"command\":\"shutdown\",\"version\":1,\"instance\":\"{}\"}}\n",
                health.instance
            )
            .as_bytes(),
        )
        .unwrap();
    let mut response = String::new();
    BufReader::new(stream).read_line(&mut response).unwrap();
    assert!(response.contains("replaced"));
    drop(listener);
    fs::remove_file(&socket).unwrap();
    fs::rename(&moved, &socket).unwrap();
    assert_eq!(f.health().instance, health.instance);
}

#[test]
fn legacy_unknown_and_slow_control_peers_get_bounded_actionable_refusal() {
    for reply in [
        Some("{\"status\":\"error\",\"message\":\"Invalid search request\"}\n"),
        Some("{\"status\":\"future\"}\n"),
        None,
    ] {
        let f = Fixture::new();
        f.config().prepare().unwrap();
        let socket = f.cache.join("daemon.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut bytes = [0; 512];
            let _ = stream.read(&mut bytes);
            if let Some(reply) = reply {
                stream.write_all(reply.as_bytes()).unwrap();
            } else {
                thread::sleep(Duration::from_millis(3300));
            }
        });
        let started = Instant::now();
        let error = grepglint::maintenance::shutdown(&f.config(), None).unwrap_err();
        let message = format!("{error:#}");
        assert!(
            message.contains("idle exit") && message.contains("rg"),
            "{message}"
        );
        assert!(started.elapsed() < Duration::from_secs(5));
        server.join().unwrap();
        fs::remove_file(socket).unwrap();
    }
}

#[test]
fn slow_request_does_not_hold_shutdown_and_unknown_versions_keep_rg_fallback() {
    let f = Fixture::new();
    f.start();
    let mut slow = UnixStream::connect(f.cache.join("daemon.sock")).unwrap();
    slow.write_all(b"{").unwrap();
    let started = Instant::now();
    assert!(grepglint::maintenance::shutdown(&f.config(), None).unwrap());
    assert!(started.elapsed() < Duration::from_secs(3));
    f.start();
    let response = f.raw(r#"{"version":999,"cwd":"/","query":"secret-query","limit":5}"#);
    assert!(response["message"].as_str().unwrap().contains("rg"));
    assert!(!response.to_string().contains("secret-query"));
}

#[test]
fn maintenance_waits_for_active_work_lock_and_has_a_deadline() {
    let f = Fixture::new();
    f.start();
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .mode(0o600)
        .open(f.cache.join("maintenance.lock"))
        .unwrap();
    FileExt::lock_shared(&lock).unwrap();
    let config = f.config();
    let worker = thread::spawn(move || {
        let started = Instant::now();
        let mut guard = Maintenance::acquire(&config).unwrap();
        assert!(started.elapsed() >= Duration::from_millis(200));
        let health = guard.status().unwrap().unwrap();
        guard.shutdown(&health.instance).unwrap();
    });
    thread::sleep(Duration::from_millis(300));
    drop(lock);
    worker.join().unwrap();
}

#[test]
fn shutdown_waits_for_an_active_search_to_complete() {
    let f = Fixture::new();
    let wrapper = f.root.join("bin");
    fs::create_dir(&wrapper).unwrap();
    let real_git = Command::new("sh")
        .args(["-c", "command -v git"])
        .output()
        .unwrap();
    let real_git = String::from_utf8(real_git.stdout).unwrap();
    let marker = f.root.parent().unwrap().join("git-started");
    fs::write(
        wrapper.join("git"),
        format!(
            "#!/bin/sh\nprintf started > '{}'\nsleep 0.2\nexec '{}' \"$@\"\n",
            marker.display(),
            real_git.trim()
        ),
    )
    .unwrap();
    fs::set_permissions(wrapper.join("git"), fs::Permissions::from_mode(0o700)).unwrap();
    let mut search = f
        .command()
        .args(["search", "--json", "maintenancequartz"])
        .env(
            "PATH",
            format!("{}:{}", wrapper.display(), std::env::var("PATH").unwrap()),
        )
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !marker.exists() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
    let started = Instant::now();
    assert!(grepglint::maintenance::shutdown(&f.config(), None).unwrap());
    assert!(started.elapsed() >= Duration::from_millis(100));
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(search.wait().unwrap().success());
    assert!(
        grepglint::maintenance::status(&f.config())
            .unwrap()
            .is_none()
    );
}

#[test]
fn maintenance_lock_acquisition_expires_without_stopping_daemon() {
    let f = Fixture::new();
    let before = f.start();
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(f.cache.join("maintenance.lock"))
        .unwrap();
    FileExt::lock_shared(&lock).unwrap();
    let started = Instant::now();
    let output = f.command().arg("shutdown").output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("deadline"));
    assert!(started.elapsed() >= Duration::from_secs(35));
    assert!(started.elapsed() < Duration::from_secs(40));
    assert_eq!(f.health().instance, before.instance);
    drop(lock);
}

#[test]
fn shutdown_binds_to_socket_inspected_under_the_guard() {
    let f = Fixture::new();
    f.start();
    let config = f.config();
    let mut guard = Maintenance::acquire(&config).unwrap();
    let health = guard.status().unwrap().unwrap();
    let socket = f.cache.join("daemon.sock");
    let old = f.cache.join("old.sock");
    fs::rename(&socket, &old).unwrap();
    let replacement = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(
        guard
            .shutdown(&health.instance)
            .unwrap_err()
            .to_string()
            .contains("socket changed")
    );
    drop(replacement);
    fs::remove_file(&socket).unwrap();
    fs::rename(old, socket).unwrap();
    assert!(guard.shutdown(&health.instance).unwrap());
}

#[test]
fn idle_cleanup_preserves_a_replacement_socket() {
    let f = Fixture::new();
    assert!(
        f.command()
            .args(["search", "--json", "maintenancequartz"])
            .env("GREPGLINT_IDLE_SECONDS", "1")
            .output()
            .unwrap()
            .status
            .success()
    );
    let socket = f.cache.join("daemon.sock");
    fs::remove_file(&socket).unwrap();
    let replacement = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(f.cache.join("daemon.lock"))
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while FileExt::try_lock_exclusive(&lock).is_err() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(20));
    }
    assert!(socket.exists());
    drop(replacement);
    fs::remove_file(socket).unwrap();
}

#[test]
fn non_private_resources_and_shutdown_without_exclusion_are_refused() {
    let f = Fixture::new();
    let health = f.start();
    let response = f.raw(&format!(
        "{{\"command\":\"shutdown\",\"version\":1,\"instance\":\"{}\"}}",
        health.instance
    ));
    assert!(response["message"].as_str().unwrap().contains("exclusion"));
    let socket = f.cache.join("daemon.sock");
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o666)).unwrap();
    assert!(grepglint::maintenance::status(&f.config()).is_err());
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(f.health().instance, health.instance);
}

#[test]
fn shutdown_preserves_fifo_symlink_and_oversized_pid_replacements() {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};
    for replacement in ["fifo", "symlink", "oversized"] {
        let f = Fixture::new();
        f.start();
        let pid = f.cache.join("daemon.pid");
        if replacement != "oversized" {
            fs::remove_file(&pid).unwrap();
        }
        match replacement {
            "fifo" => {
                let path = CString::new(pid.as_os_str().as_bytes()).unwrap();
                assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
            }
            "symlink" => symlink(f.root.join("source.txt"), &pid).unwrap(),
            _ => {
                // Sparse length tests the bound without allocating or writing source-sized data.
                let file = fs::File::create(&pid).unwrap();
                file.set_len(1024 * 1024 * 1024).unwrap();
            }
        }
        let started = Instant::now();
        assert!(grepglint::maintenance::shutdown(&f.config(), None).unwrap());
        assert!(started.elapsed() < Duration::from_secs(3), "{replacement}");
        assert!(fs::symlink_metadata(&pid).is_ok(), "{replacement}");
        assert!(
            grepglint::maintenance::status(&f.config())
                .unwrap()
                .is_none()
        );
        assert_eq!(
            fs::read_to_string(f.root.join("source.txt")).unwrap(),
            "maintenancequartz\n"
        );
        fs::remove_file(&pid).unwrap();
        f.start();
    }
}

#[test]
fn accepted_connection_can_send_health_after_a_short_delay() {
    let f = Fixture::new();
    f.start();
    let mut client = UnixStream::connect(f.cache.join("daemon.sock")).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    thread::sleep(Duration::from_millis(40));
    client
        .write_all(b"{\"command\":\"health\",\"version\":1}\n")
        .unwrap();
    let mut response = String::new();
    BufReader::new(client).read_line(&mut response).unwrap();
    let response: serde_json::Value = serde_json::from_str(&response).unwrap();
    assert_eq!(response["status"], "healthy", "{response}");
}

#[test]
fn health_refusal_detail_is_bounded_and_printable() {
    let f = Fixture::new();
    f.config().prepare().unwrap();
    let socket = f.cache.join("daemon.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = String::new();
        BufReader::new(&mut stream).read_line(&mut request).unwrap();
        let response = serde_json::json!({"status": "error", "message": "detail\n\0".repeat(3000)});
        writeln!(stream, "{response}").unwrap();
    });
    let error = grepglint::maintenance::status(&f.config())
        .unwrap_err()
        .to_string();
    assert!(error.contains("Daemon refused health"), "{error}");
    assert!(error.len() < 2500);
    assert!(!error.chars().any(char::is_control));
    server.join().unwrap();
    fs::remove_file(socket).unwrap();
}
