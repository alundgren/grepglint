use serde_json::Value;
use sha2::{Digest, Sha256};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};
use tempfile::TempDir;

struct Fixture {
    temp: TempDir,
    root: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::Builder::new()
            .prefix("gg-s-")
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap();
        let root = temp.path().canonicalize().unwrap();
        fs::copy(env!("CARGO_BIN_EXE_grepglint"), root.join("source")).unwrap();
        fs::set_permissions(root.join("source"), fs::Permissions::from_mode(0o700)).unwrap();
        Self { temp, root }
    }
    fn state(&self) -> PathBuf {
        self.root.join("state")
    }
    fn destination(&self) -> PathBuf {
        self.root.join("bin/grepglint")
    }
    fn command(&self, action: &str) -> Command {
        let mut cmd = Command::new(self.root.join("source"));
        cmd.args(["setup", action, "--state-dir"])
            .arg(self.state())
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", &self.root)
            .env("GREPGLINT_CACHE_DIR", self.root.join("cache"))
            .env("GREPGLINT_CACHE_MB", "8")
            .env("GREPGLINT_IDLE_SECONDS", "2")
            .env_remove("CARGO_HOME");
        cmd
    }
    fn install(&self) -> Output {
        self.command("install")
            .arg("--destination")
            .arg(self.destination())
            .args([
                "--release",
                "v0.1.0",
                "--commit",
                &"a".repeat(40),
                "--digest",
                &digest(Path::new(env!("CARGO_BIN_EXE_grepglint"))),
            ])
            .output()
            .unwrap()
    }
    fn record(&self) -> Value {
        serde_json::from_slice(&fs::read(self.state().join("record.json")).unwrap()).unwrap()
    }
}
fn digest(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}
fn success(output: Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
fn failure(output: Output, text: &str) {
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains(text),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn fresh_install_repeat_offline_and_modified_copy() {
    let f = Fixture::new();
    success(f.install());
    assert_eq!(f.record()["phase"], "complete");
    assert!(!f.root.join("cache/daemon.sock").exists());
    let metadata = fs::metadata(f.destination()).unwrap();
    success(f.install());
    assert_eq!(
        metadata.modified().unwrap(),
        fs::metadata(f.destination()).unwrap().modified().unwrap()
    );
    success(f.command("verify").output().unwrap());
    let bootstrap = Path::new(env!("CARGO_MANIFEST_DIR")).join("scripts/bootstrap.py");
    success(
        Command::new("python3")
            .arg(&bootstrap)
            .args(["verify", "--state-dir"])
            .arg(f.state())
            .env("PATH", "/usr/bin:/bin")
            .output()
            .unwrap(),
    );
    let marker = f.root.join("unverified-execution");
    fs::write(
        f.state().join("maintenance"),
        format!("#!/bin/sh\ntouch '{}'\n", marker.display()),
    )
    .unwrap();
    failure(
        Command::new("python3")
            .arg(bootstrap)
            .args(["verify", "--state-dir"])
            .arg(f.state())
            .output()
            .unwrap(),
        "Maintenance copy changed",
    );
    assert!(!marker.exists());
    assert!(f.temp.path().exists());
}

#[test]
fn unsafe_objects_and_unknown_destinations_preserved() {
    for object in ["symlink", "directory", "fifo", "file"] {
        let f = Fixture::new();
        fs::create_dir(f.root.join("bin")).unwrap();
        match object {
            "symlink" => symlink("/dev/null", f.destination()).unwrap(),
            "directory" => fs::create_dir(f.destination()).unwrap(),
            "fifo" => {
                let c =
                    std::ffi::CString::new(f.destination().as_os_str().as_encoded_bytes()).unwrap();
                assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
            }
            _ => fs::write(f.destination(), "unrelated").unwrap(),
        }
        assert!(!f.install().status.success(), "{object}");
        assert!(fs::symlink_metadata(f.destination()).is_ok());
        assert!(!f.state().join("record.json").exists());
    }
}

#[test]
fn recovery_at_every_durable_phase_and_partial_copy() {
    for phase in ["prepared", "retained", "installed"] {
        let f = Fixture::new();
        success(f.install());
        let mut record = f.record();
        record["phase"] = phase.into();
        fs::write(
            f.state().join("record.json"),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        if phase != "installed" {
            fs::remove_file(f.destination()).unwrap();
        }
        if phase == "prepared" {
            fs::remove_file(f.state().join("maintenance")).unwrap();
            let bytes = fs::read(env!("CARGO_BIN_EXE_grepglint")).unwrap();
            fs::write(
                f.state().join("maintenance.grepglint-pending"),
                &bytes[..1000],
            )
            .unwrap();
        }
        if phase == "prepared" {
            fs::set_permissions(
                f.state().join("maintenance.grepglint-pending"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        success(f.install());
        assert_eq!(f.record()["phase"], "complete");
        assert_eq!(digest(&f.destination()), record["digest"].as_str().unwrap());
    }
}

#[test]
fn unknown_record_and_modified_destination_are_not_repaired() {
    let f = Fixture::new();
    success(f.install());
    let mut record = f.record();
    record["schema_version"] = 9.into();
    fs::write(
        f.state().join("record.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    failure(
        f.command("verify").output().unwrap(),
        "Unsupported installation record",
    );
    record["schema_version"] = 1.into();
    fs::write(
        f.state().join("record.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    fs::write(f.destination(), "edited").unwrap();
    failure(f.install(), "Installed executable modified");
    assert_eq!(fs::read(f.destination()).unwrap(), b"edited");
}

#[test]
fn migration_requires_consent_and_preserves_registry() {
    let f = Fixture::new();
    let cargo = f.root.join(".cargo");
    fs::create_dir_all(cargo.join("bin")).unwrap();
    fs::set_permissions(&cargo, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(cargo.join("bin"), fs::Permissions::from_mode(0o700)).unwrap();
    let registry =
        br#"{"installs":{"grepglint 0.1.0 (path+file:///source)":{"bins":["grepglint"]}}}"#;
    fs::write(cargo.join(".crates2.json"), registry).unwrap();
    fs::set_permissions(
        cargo.join(".crates2.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::copy(env!("CARGO_BIN_EXE_grepglint"), cargo.join("bin/grepglint")).unwrap();
    fs::set_permissions(
        cargo.join("bin/grepglint"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    let mut cmd = f.command("install");
    cmd.arg("--destination")
        .arg(cargo.join("bin/grepglint"))
        .args([
            "--release",
            "v0.1.0",
            "--commit",
            &"a".repeat(40),
            "--digest",
            &digest(Path::new(env!("CARGO_BIN_EXE_grepglint"))),
        ]);
    failure(cmd.output().unwrap(), "explicit --migrate-cargo");
    success(cmd.arg("--migrate-cargo").output().unwrap());
    assert_eq!(fs::read(cargo.join(".crates2.json")).unwrap(), registry);
}

#[test]
fn operation_lock_and_unsafe_cache_are_refused() {
    use fs2::FileExt;
    let f = Fixture::new();
    success(f.install());
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(f.state().join("operation.lock"))
        .unwrap();
    lock.lock_exclusive().unwrap();
    failure(
        f.command("verify").output().unwrap(),
        "Another installer operation",
    );
    drop(lock);
    fs::set_permissions(f.root.join("cache"), fs::Permissions::from_mode(0o500)).unwrap();
    failure(
        f.command("verify").output().unwrap(),
        "not readable and writable",
    );
    fs::set_permissions(f.root.join("cache"), fs::Permissions::from_mode(0o700)).unwrap();
}

#[test]
fn live_daemon_noop_keeps_identity_and_mismatched_settings_fail() {
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    success(f.install());
    for mb in [8, 16] {
        let mut child = Command::new(f.destination())
            .arg("__daemon")
            .env("GREPGLINT_CACHE_DIR", f.root.join("cache"))
            .env("GREPGLINT_CACHE_MB", mb.to_string())
            .env("GREPGLINT_IDLE_SECONDS", "60")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let config = grepglint::daemon::Config {
            directory: f.root.join("cache"),
            max_bytes: mb * 1024 * 1024,
            idle: Duration::from_secs(60),
        };
        let deadline = Instant::now() + Duration::from_secs(10);
        let health = loop {
            if let Ok(Some(value)) = grepglint::maintenance::status(&config) {
                break value;
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        };
        // The initial record used idle=2. Matching it permits the no-op path.
        let mut record = f.record();
        record["idle_seconds"] = 60.into();
        fs::write(
            f.state().join("record.json"),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        if mb == 8 {
            success(f.install());
            fs::set_permissions(
                f.root.join("cache/daemon.sock"),
                fs::Permissions::from_mode(0o666),
            )
            .unwrap();
            assert!(!f.command("verify").output().unwrap().status.success());
            fs::set_permissions(
                f.root.join("cache/daemon.sock"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
            assert_eq!(
                grepglint::maintenance::status(&config)
                    .unwrap()
                    .unwrap()
                    .instance,
                health.instance
            );
        } else {
            failure(f.command("verify").output().unwrap(), "settings differ");
        }
        grepglint::maintenance::shutdown(&config, None).unwrap();
        child.wait().unwrap();
    }
}

#[test]
fn failed_fixture_restores_prior_cargo_bytes() {
    let f = Fixture::new();
    let cargo = f.root.join(".cargo");
    fs::create_dir_all(cargo.join("bin")).unwrap();
    fs::set_permissions(&cargo, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(cargo.join("bin"), fs::Permissions::from_mode(0o700)).unwrap();
    let registry = cargo.join(".crates2.json");
    fs::write(
        &registry,
        br#"{"installs":{"grepglint 0.0.1 (path+file:///source)":{"bins":["grepglint"]}}}"#,
    )
    .unwrap();
    fs::set_permissions(&registry, fs::Permissions::from_mode(0o600)).unwrap();
    let prior = cargo.join("bin/grepglint");
    fs::write(&prior, b"old trusted Cargo binary").unwrap();
    fs::set_permissions(&prior, fs::Permissions::from_mode(0o700)).unwrap();
    let out = f
        .command("install")
        .arg("--destination")
        .arg(&prior)
        .args([
            "--release",
            "v0.1.0",
            "--commit",
            &"a".repeat(40),
            "--digest",
            &digest(&f.root.join("source")),
            "--migrate-cargo",
        ])
        .env("PATH", f.root.join("empty-path"))
        .output()
        .unwrap();
    failure(out, "rolled back");
    assert_eq!(fs::read(prior).unwrap(), b"old trusted Cargo binary");
    assert_eq!(f.record()["phase"], "retained");
}

#[test]
fn interrupted_copy_resumes_and_path_conflicts_require_consent() {
    use std::os::unix::process::CommandExt;
    let f = Fixture::new();
    let mut command = f.command("install");
    command.arg("--destination").arg(f.destination()).args([
        "--release",
        "v0.1.0",
        "--commit",
        &"a".repeat(40),
        "--digest",
        &digest(&f.root.join("source")),
    ]);
    unsafe {
        command.pre_exec(|| {
            let cap = libc::rlimit {
                rlim_cur: 4096,
                rlim_max: 4096,
            };
            if libc::setrlimit(libc::RLIMIT_FSIZE, &cap) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    assert!(!command.output().unwrap().status.success());
    assert_eq!(f.record()["phase"], "prepared");
    assert!(!f.destination().exists());
    success(f.install());

    let f = Fixture::new();
    let conflict = f.root.join("other");
    fs::create_dir(&conflict).unwrap();
    fs::write(conflict.join("grepglint"), "unknown").unwrap();
    let out = f
        .command("install")
        .arg("--destination")
        .arg(f.destination())
        .args([
            "--release",
            "v0.1.0",
            "--commit",
            &"a".repeat(40),
            "--digest",
            &digest(&f.root.join("source")),
        ])
        .env("PATH", conflict)
        .output()
        .unwrap();
    failure(out, "Unknown PATH executable");
    assert!(!f.state().join("record.json").exists());
}

#[test]
fn offline_entrypoint_recovers_missing_destination_and_rejects_downgrade() {
    let f = Fixture::new();
    success(f.install());
    let mut record = f.record();
    record["phase"] = "retained".into();
    fs::write(
        f.state().join("record.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    fs::remove_file(f.destination()).unwrap();
    let tools = f.root.join("tools");
    fs::create_dir(&tools).unwrap();
    let marker = f.root.join("gh-was-called");
    fs::write(
        tools.join("gh"),
        format!("#!/bin/sh\ntouch '{}'\nexit 99\n", marker.display()),
    )
    .unwrap();
    fs::set_permissions(tools.join("gh"), fs::Permissions::from_mode(0o700)).unwrap();
    let bootstrap = Path::new(env!("CARGO_MANIFEST_DIR")).join("scripts/bootstrap.py");
    let mut cmd = Command::new("python3");
    cmd.arg(&bootstrap)
        .args(["install", "--state-dir"])
        .arg(f.state())
        .env("PATH", format!("{}:/usr/bin:/bin", tools.display()));
    success(cmd.output().unwrap());
    assert!(!marker.exists());
    assert!(f.destination().exists());
    failure(
        cmd.args(["--release", "v0.0.1"]).output().unwrap(),
        "Upgrade or downgrade",
    );
}

#[test]
fn preexisting_cache_is_not_adopted() {
    let f = Fixture::new();
    let cache = f.root.join("cache");
    fs::create_dir(&cache).unwrap();
    fs::set_permissions(&cache, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(cache.join("unrelated"), b"keep").unwrap();
    success(f.install());
    assert_eq!(f.record()["cache_owned"], false);
    assert_eq!(fs::read(cache.join("unrelated")).unwrap(), b"keep");
}

#[test]
fn wrong_running_binary_is_preserved_and_refused() {
    use std::io::Write;
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    success(f.install());
    fs::OpenOptions::new()
        .append(true)
        .open(f.root.join("source"))
        .unwrap()
        .write_all(b"different build bytes")
        .unwrap();
    let mut child = Command::new(f.root.join("source"))
        .arg("__daemon")
        .env("GREPGLINT_CACHE_DIR", f.root.join("cache"))
        .env("GREPGLINT_CACHE_MB", "8")
        .env("GREPGLINT_IDLE_SECONDS", "60")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let config = grepglint::daemon::Config {
        directory: f.root.join("cache"),
        max_bytes: 8 * 1024 * 1024,
        idle: Duration::from_secs(60),
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if matches!(grepglint::maintenance::status(&config), Ok(Some(_))) {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    failure(f.command("verify").output().unwrap(), "daemon differs");
    assert!(child.try_wait().unwrap().is_none());
    grepglint::maintenance::shutdown(&config, None).unwrap();
    child.wait().unwrap();
}

#[test]
fn separate_cargo_path_migration_stops_only_identified_prior_daemon() {
    use std::io::Write;
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    let cargo = f.root.join(".cargo");
    fs::create_dir_all(cargo.join("bin")).unwrap();
    for directory in [&cargo, &cargo.join("bin")] {
        fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let registry = cargo.join(".crates2.json");
    fs::write(
        &registry,
        br#"{"installs":{"grepglint 0.1.0 (path+file:///source)":{"bins":["grepglint"]}}}"#,
    )
    .unwrap();
    fs::set_permissions(&registry, fs::Permissions::from_mode(0o600)).unwrap();
    let prior = cargo.join("bin/grepglint");
    fs::copy(f.root.join("source"), &prior).unwrap();
    fs::OpenOptions::new()
        .append(true)
        .open(&prior)
        .unwrap()
        .write_all(b"Cargo build")
        .unwrap();
    let old_hash = digest(&prior);
    let mut child = Command::new(&prior)
        .arg("__daemon")
        .env("GREPGLINT_CACHE_DIR", f.root.join("cache"))
        .env("GREPGLINT_CACHE_MB", "8")
        .env("GREPGLINT_IDLE_SECONDS", "60")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let config = grepglint::daemon::Config {
        directory: f.root.join("cache"),
        max_bytes: 8 * 1024 * 1024,
        idle: Duration::from_secs(60),
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if matches!(grepglint::maintenance::status(&config), Ok(Some(_))) {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let mut cmd = f.command("install");
    cmd.arg("--destination")
        .arg(f.destination())
        .args([
            "--release",
            "v0.1.0",
            "--commit",
            &"a".repeat(40),
            "--digest",
            &digest(&f.root.join("source")),
        ])
        .env(
            "PATH",
            format!("{}:/usr/bin:/bin", cargo.join("bin").display()),
        );
    failure(cmd.output().unwrap(), "explicit --migrate-cargo");
    assert!(child.try_wait().unwrap().is_none());
    success(cmd.arg("--migrate-cargo").output().unwrap());
    child.wait().unwrap();
    assert_eq!(digest(&prior), old_hash);
    assert_eq!(f.record()["cargo_digest"], old_hash);
    assert!(!f.root.join("cache/daemon.sock").exists());
}
