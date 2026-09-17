use serde_json::Value;
use sha2::{Digest, Sha256};
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
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
            success(f.command("repair").output().unwrap());
            success(
                f.command("upgrade")
                    .args([
                        "--release",
                        "v0.1.0",
                        "--commit",
                        &"a".repeat(40),
                        "--digest",
                        &digest(&f.root.join("source")),
                    ])
                    .output()
                    .unwrap(),
            );
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

#[test]
fn readonly_database_fails_verification_without_changes() {
    let f = Fixture::new();
    success(f.install());
    let database = f.root.join("cache/index.sqlite");
    fs::write(&database, b"preserved database bytes").unwrap();
    fs::set_permissions(&database, fs::Permissions::from_mode(0o400)).unwrap();
    failure(
        f.command("verify").output().unwrap(),
        "Cache database is not readable and writable",
    );
    assert_eq!(fs::read(&database).unwrap(), b"preserved database bytes");
    assert_eq!(
        fs::metadata(&database).unwrap().permissions().mode() & 0o777,
        0o400
    );
}

impl Fixture {
    fn candidate(&self) {
        use std::io::Write;
        fs::OpenOptions::new()
            .append(true)
            .open(self.root.join("source"))
            .unwrap()
            .write_all(b"candidate build")
            .unwrap();
    }
    fn upgrade(&self) -> Command {
        let mut command = self.command("upgrade");
        command.args([
            "--release",
            "v0.1.0",
            "--commit",
            &"b".repeat(40),
            "--digest",
            &digest(&self.root.join("source")),
        ]);
        command
    }
    fn write_record(&self, value: &Value) {
        fs::write(
            self.state().join("record.json"),
            serde_json::to_vec_pretty(value).unwrap(),
        )
        .unwrap();
    }
    fn interrupted_upgrade(&self, phase: &str) -> Value {
        let prior = self.record();
        self.candidate();
        let mut record = prior.clone();
        record["schema_version"] = 2.into();
        record["phase"] = phase.into();
        record["commit"] = "b".repeat(40).into();
        record["digest"] = digest(&self.root.join("source")).into();
        record["change"] = serde_json::json!({"prior": prior, "destination_mode": 0o750, "maintenance_mode": 0o500});
        fs::copy(self.root.join("source"), self.state().join("candidate")).unwrap();
        fs::copy(self.destination(), self.state().join("previous")).unwrap();
        fs::set_permissions(
            self.state().join("previous"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        if ["upgrade_installed", "upgrade_copied", "complete"].contains(&phase) {
            fs::copy(self.root.join("source"), self.destination()).unwrap();
        }
        if ["upgrade_copied", "complete"].contains(&phase) {
            fs::copy(self.root.join("source"), self.state().join("maintenance")).unwrap();
        }
        self.write_record(&record);
        record
    }
}

#[test]
fn upgrade_replaces_both_and_cleans_only_recorded_recovery_files() {
    let f = Fixture::new();
    success(f.install());
    f.candidate();
    success(f.upgrade().output().unwrap());
    assert_eq!(digest(&f.destination()), digest(&f.root.join("source")));
    assert_eq!(
        digest(&f.state().join("maintenance")),
        digest(&f.root.join("source"))
    );
    assert_eq!(f.record()["phase"], "complete");
    assert!(f.record().get("change").is_none());
    assert!(!f.state().join("previous").exists());
    assert!(!f.state().join("candidate").exists());
    let before = fs::metadata(f.destination()).unwrap().modified().unwrap();
    success(f.upgrade().output().unwrap());
    success(f.command("repair").output().unwrap());
    assert_eq!(
        before,
        fs::metadata(f.destination()).unwrap().modified().unwrap()
    );
}

#[test]
fn upgrade_recovery_restores_both_files_and_modes_at_every_phase() {
    for phase in [
        "upgrade_prepared",
        "upgrade_retained",
        "upgrade_installed",
        "upgrade_copied",
        "rollback",
        "rollback_complete",
        "complete",
    ] {
        let f = Fixture::new();
        success(f.install());
        let old = digest(&f.destination());
        let record = f.interrupted_upgrade(phase);
        if phase == "upgrade_prepared" {
            let bytes = fs::read(f.state().join("previous")).unwrap();
            fs::remove_file(f.state().join("previous")).unwrap();
            fs::write(f.state().join("previous.grepglint-pending"), &bytes[..8192]).unwrap();
            fs::set_permissions(
                f.state().join("previous.grepglint-pending"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        if phase == "upgrade_installed" {
            let bytes = fs::read(f.state().join("candidate")).unwrap();
            fs::write(
                f.state().join("maintenance.grepglint-pending"),
                &bytes[..8192],
            )
            .unwrap();
            fs::set_permissions(
                f.state().join("maintenance.grepglint-pending"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        success(f.command("repair").output().unwrap());
        let expected = if phase == "complete" {
            record["digest"].as_str().unwrap()
        } else {
            &old
        };
        assert_eq!(digest(&f.destination()), expected, "{phase}");
        assert_eq!(digest(&f.state().join("maintenance")), expected, "{phase}");
        assert_eq!(f.record()["phase"], "complete");
        if !["complete", "rollback_complete"].contains(&phase) {
            assert_eq!(fs::metadata(f.destination()).unwrap().mode() & 0o777, 0o750);
            assert_eq!(
                fs::metadata(f.state().join("maintenance")).unwrap().mode() & 0o777,
                0o500
            );
        }
        assert!(!f.state().join("candidate").exists());
        assert!(!f.state().join("previous").exists());
    }
}

#[test]
fn failed_upgrade_verification_rolls_back_and_failed_rollback_remains_recoverable() {
    let f = Fixture::new();
    success(f.install());
    let old = digest(&f.destination());
    f.candidate();
    failure(
        f.upgrade()
            .env("PATH", f.root.join("no-git"))
            .output()
            .unwrap(),
        "Upgrade rolled back",
    );
    assert_eq!(digest(&f.destination()), old);
    assert_eq!(digest(&f.state().join("maintenance")), old);
    assert_eq!(f.record()["phase"], "complete");

    let f = Fixture::new();
    success(f.install());
    let record = f.interrupted_upgrade("upgrade_copied");
    fs::write(f.state().join("previous"), b"unexpected edit").unwrap();
    failure(f.command("repair").output().unwrap(), "Rollback failed");
    assert_eq!(
        fs::read(f.state().join("previous")).unwrap(),
        b"unexpected edit"
    );
    assert_eq!(f.record()["phase"], "rollback");
    assert!(f.state().join("candidate").exists());
    // Restore only the disposable test's deliberately damaged recorded backup.
    fs::copy(env!("CARGO_BIN_EXE_grepglint"), f.state().join("previous")).unwrap();
    fs::set_permissions(
        f.state().join("previous"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    success(f.command("repair").output().unwrap());
    assert_eq!(
        digest(&f.destination()),
        record["change"]["prior"]["digest"]
    );
}

#[test]
fn repair_missing_owned_files_offline_and_preserve_modified_files() {
    for missing in ["installed", "maintenance", "both"] {
        let f = Fixture::new();
        success(f.install());
        if missing != "maintenance" {
            fs::remove_file(f.destination()).unwrap();
        }
        if missing != "installed" {
            fs::remove_file(f.state().join("maintenance")).unwrap();
        }
        success(f.command("repair").output().unwrap());
        assert_eq!(
            digest(&f.destination()),
            digest(&f.state().join("maintenance"))
        );
    }
    for modified in ["installed", "maintenance"] {
        let f = Fixture::new();
        success(f.install());
        let path = if modified == "installed" {
            f.destination()
        } else {
            f.state().join("maintenance")
        };
        fs::write(&path, b"user edit").unwrap();
        failure(
            f.command("repair").output().unwrap(),
            "Modified executable preserved",
        );
        assert_eq!(fs::read(path).unwrap(), b"user edit");
    }
}

#[test]
fn incompatible_cache_refused_before_upgrade_changes() {
    let f = Fixture::new();
    success(f.install());
    let database = f.root.join("cache/index.sqlite");
    let db = rusqlite::Connection::open(&database).unwrap();
    db.execute_batch("PRAGMA user_version=99; CREATE TABLE keep(value TEXT); INSERT INTO keep VALUES ('personal');").unwrap();
    drop(db);
    fs::set_permissions(&database, fs::Permissions::from_mode(0o600)).unwrap();
    let bytes = fs::read(&database).unwrap();
    let record = f.record();
    let old = digest(&f.destination());
    f.candidate();
    failure(f.upgrade().output().unwrap(), "Incompatible cache format");
    assert_eq!(f.record(), record);
    assert_eq!(digest(&f.destination()), old);
    assert_eq!(fs::read(database).unwrap(), bytes);
    assert!(!f.state().join("candidate").exists());
}

#[test]
fn partial_upgrade_write_recovers_without_retaining_extra_files() {
    use std::os::unix::process::CommandExt;
    let f = Fixture::new();
    success(f.install());
    let old = digest(&f.destination());
    f.candidate();
    let mut command = f.upgrade();
    unsafe {
        command.pre_exec(|| {
            let cap = libc::rlimit {
                rlim_cur: 8192,
                rlim_max: 8192,
            };
            if libc::setrlimit(libc::RLIMIT_FSIZE, &cap) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    assert!(!command.output().unwrap().status.success());
    assert_eq!(f.record()["phase"], "upgrade_prepared");
    assert_eq!(digest(&f.destination()), old);
    assert!(f.state().join("candidate.grepglint-pending").exists());
    success(f.command("repair").output().unwrap());
    assert_eq!(digest(&f.destination()), old);
    assert_eq!(f.record()["phase"], "complete");
    assert!(!f.state().join("candidate.grepglint-pending").exists());
    assert!(!f.state().join("previous").exists());
}

#[test]
fn trusted_bootstrap_repairs_missing_copy_and_recovers_mixed_upgrade() {
    let bootstrap = Path::new(env!("CARGO_MANIFEST_DIR")).join("scripts/bootstrap.py");
    for missing in ["destination", "maintenance"] {
        let f = Fixture::new();
        success(f.install());
        fs::remove_file(if missing == "destination" {
            f.destination()
        } else {
            f.state().join("maintenance")
        })
        .unwrap();
        success(
            Command::new("python3")
                .arg(&bootstrap)
                .args(["repair", "--state-dir"])
                .arg(f.state())
                .env("PATH", "/usr/bin:/bin")
                .output()
                .unwrap(),
        );
        assert_eq!(
            digest(&f.destination()),
            digest(&f.state().join("maintenance"))
        );
    }
    let f = Fixture::new();
    success(f.install());
    let record = f.interrupted_upgrade("upgrade_installed");
    success(
        Command::new("python3")
            .arg(&bootstrap)
            .args(["repair", "--state-dir"])
            .arg(f.state())
            .env("PATH", "/usr/bin:/bin")
            .output()
            .unwrap(),
    );
    assert_eq!(
        digest(&f.destination()),
        record["change"]["prior"]["digest"]
    );
}

#[test]
fn upgrade_excludes_other_installers_and_search_startup() {
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    success(f.install());
    f.candidate();
    let tools = f.root.join("tools");
    fs::create_dir(&tools).unwrap();
    let marker = f.root.join("verification-started");
    let script = format!(
        "#!/bin/sh\nif mkdir '{}' 2>/dev/null; then /bin/sleep 2; fi\nexec /usr/bin/git \"$@\"\n",
        marker.display()
    );
    fs::write(tools.join("git"), script).unwrap();
    fs::set_permissions(tools.join("git"), fs::Permissions::from_mode(0o700)).unwrap();
    let mut child = f
        .upgrade()
        .env("PATH", format!("{}:/usr/bin:/bin", tools.display()))
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !marker.exists() {
        assert!(Instant::now() < deadline);
        assert!(child.try_wait().unwrap().is_none());
        std::thread::sleep(Duration::from_millis(10));
    }
    failure(
        f.command("repair").output().unwrap(),
        "Another installer operation",
    );
    let output = Command::new(f.destination())
        .args(["search", "--json", "fixture"])
        .current_dir(&f.root)
        .env("GREPGLINT_CACHE_DIR", f.root.join("cache"))
        .env("GREPGLINT_CACHE_MB", "8")
        .env("GREPGLINT_IDLE_SECONDS", "2")
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "search started during maintenance"
    );
    assert!(!f.root.join("cache/index.sqlite").exists());
    success(child.wait_with_output().unwrap());
    assert_eq!(f.record()["phase"], "complete");
}

#[test]
fn upgrade_stops_identified_old_daemon_before_replacement() {
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    success(f.install());
    let mut record = f.record();
    record["idle_seconds"] = 60.into();
    f.write_record(&record);
    let mut child = Command::new(f.destination())
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
    while !matches!(grepglint::maintenance::status(&config), Ok(Some(_))) {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    f.candidate();
    success(f.upgrade().output().unwrap());
    assert!(child.wait().unwrap().success());
    assert!(grepglint::maintenance::status(&config).unwrap().is_none());
    assert_eq!(digest(&f.destination()), digest(&f.root.join("source")));
}

#[test]
fn interrupted_repair_and_record_write_resume() {
    use std::os::unix::process::CommandExt;
    let f = Fixture::new();
    success(f.install());
    fs::remove_file(f.destination()).unwrap();
    let mut command = f.command("repair");
    unsafe {
        command.pre_exec(|| {
            let cap = libc::rlimit {
                rlim_cur: 8192,
                rlim_max: 8192,
            };
            if libc::setrlimit(libc::RLIMIT_FSIZE, &cap) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    assert!(!command.output().unwrap().status.success());
    assert_eq!(f.record()["phase"], "repairing");
    assert!(
        f.destination()
            .with_file_name("grepglint.grepglint-pending")
            .exists()
    );
    success(f.command("repair").output().unwrap());
    assert_eq!(f.record()["phase"], "complete");
    let mut record = f.interrupted_upgrade("upgrade_copied");
    record["phase"] = "complete".into();
    let native: grepglint::setup::Record = serde_json::from_value(record).unwrap();
    let bytes = serde_json::to_vec_pretty(&native).unwrap();
    fs::write(
        f.state().join("record.json.grepglint-pending"),
        &bytes[..bytes.len() - 10],
    )
    .unwrap();
    fs::set_permissions(
        f.state().join("record.json.grepglint-pending"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    success(f.command("repair").output().unwrap());
    assert_eq!(f.record()["phase"], "complete");
    assert!(!f.state().join("record.json.grepglint-pending").exists());
}

#[test]
fn newer_repair_helper_restores_only_recorded_source_bytes() {
    for missing in ["destination", "maintenance", "restored"] {
        let f = Fixture::new();
        success(f.install());
        let old = digest(&f.destination());
        let source = f.root.join("verified-prior");
        fs::copy(f.destination(), &source).unwrap();
        fs::set_permissions(&source, fs::Permissions::from_mode(0o700)).unwrap();
        f.candidate();
        if missing == "restored" {
            let mut record = f.record();
            record["phase"] = "repairing".into();
            f.write_record(&record);
        } else {
            fs::remove_file(if missing == "destination" {
                f.destination()
            } else {
                f.state().join("maintenance")
            })
            .unwrap();
        }
        failure(
            f.command("repair")
                .arg("--repair-source")
                .arg(f.root.join("source"))
                .output()
                .unwrap(),
            "recorded verified release",
        );
        success(
            f.command("repair")
                .arg("--repair-source")
                .arg(&source)
                .output()
                .unwrap(),
        );
        assert_eq!(digest(&f.destination()), old);
        assert_eq!(digest(&f.state().join("maintenance")), old);
        assert_eq!(f.record()["digest"], old);
        assert_eq!(f.record()["phase"], "complete");
    }
}

impl Fixture {
    fn removal(&self, action: &str) -> Output {
        self.command(action)
            .arg(if action == "purge" {
                "--purge-cache"
            } else {
                "--yes"
            })
            .output()
            .unwrap()
    }
    fn offline_removal(&self, action: &str) -> Output {
        let python = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|path| path.join("python3"))
            .find(|path| path.is_file())
            .unwrap();
        Command::new(python)
            .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("scripts/bootstrap.py"))
            .args([action, "--state-dir"])
            .arg(self.state())
            .arg(if action == "purge" {
                "--purge-cache"
            } else {
                "--yes"
            })
            .env("PATH", self.root.join("no-tools"))
            .env("HTTPS_PROXY", "http://127.0.0.1:1")
            .output()
            .unwrap()
    }
    fn cache_bytes(&self) -> Vec<u8> {
        let path = self.root.join("cache/index.sqlite");
        fs::write(&path, b"cached source contents").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        fs::read(path).unwrap()
    }
}

#[test]
fn uninstall_retains_cache_then_new_offline_process_purges_and_repeats() {
    let f = Fixture::new();
    success(f.install());
    let contents = f.cache_bytes();
    let source = digest(&f.root.join("source"));
    success(f.offline_removal("uninstall"));
    assert!(!f.destination().exists());
    assert_eq!(
        fs::read(f.root.join("cache/index.sqlite")).unwrap(),
        contents
    );
    assert!(f.state().join("maintenance").exists());
    success(f.offline_removal("uninstall"));
    let operation = fs::metadata(f.state().join("operation.lock"))
        .unwrap()
        .ino();
    let maintenance = fs::metadata(f.root.join("cache/maintenance.lock"))
        .unwrap()
        .ino();
    success(f.offline_removal("purge"));
    assert!(!f.root.join("cache/index.sqlite").exists());
    assert!(!f.state().join("maintenance").exists());
    assert!(!f.state().join("record.json").exists());
    assert_eq!(
        operation,
        fs::metadata(f.state().join("operation.lock"))
            .unwrap()
            .ino()
    );
    assert_eq!(
        maintenance,
        fs::metadata(f.root.join("cache/maintenance.lock"))
            .unwrap()
            .ino()
    );
    success(f.offline_removal("purge"));
    assert_eq!(source, digest(&f.root.join("source")));
}

#[test]
fn purge_while_installed_retains_management_and_requires_distinct_consent() {
    let f = Fixture::new();
    success(f.install());
    f.cache_bytes();
    failure(f.command("uninstall").output().unwrap(), "--yes");
    failure(
        f.command("purge").arg("--yes").output().unwrap(),
        "--purge-cache",
    );
    success(f.offline_removal("purge"));
    assert!(f.destination().exists());
    assert!(f.state().join("maintenance").exists());
    assert_eq!(f.record()["phase"], "complete");
    assert!(!f.root.join("cache/index.sqlite").exists());
    success(f.removal("purge"));
    success(f.command("verify").output().unwrap());
}

#[test]
fn removal_preserves_modified_and_unexpected_resources() {
    for object in [
        "binary",
        "maintenance",
        "cache-extra",
        "cache-symlink",
        "cache-replaced",
        "database-symlink",
        "database-directory",
        "state-extra",
    ] {
        let f = Fixture::new();
        success(f.install());
        let content = f.cache_bytes();
        let sentinel = f.root.join("sentinel");
        fs::write(&sentinel, b"unrelated").unwrap();
        fs::set_permissions(&sentinel, fs::Permissions::from_mode(0o600)).unwrap();
        match object {
            "binary" => fs::write(f.destination(), b"edited executable").unwrap(),
            "maintenance" => fs::write(
                f.state().join("maintenance"),
                format!("#!/bin/sh\necho executed > '{}'\n", sentinel.display()),
            )
            .unwrap(),
            "cache-extra" => fs::write(f.root.join("cache/extra"), b"unrelated").unwrap(),
            "state-extra" => fs::write(f.state().join("extra"), b"unrelated").unwrap(),
            "cache-symlink" | "cache-replaced" => {
                fs::rename(f.root.join("cache"), f.root.join("original-cache")).unwrap();
                if object == "cache-symlink" {
                    symlink(f.root.join("original-cache"), f.root.join("cache")).unwrap();
                } else {
                    fs::create_dir(f.root.join("cache")).unwrap();
                    fs::set_permissions(f.root.join("cache"), fs::Permissions::from_mode(0o700))
                        .unwrap();
                }
            }
            _ => {
                fs::remove_file(f.root.join("cache/index.sqlite")).unwrap();
                if object == "database-symlink" {
                    symlink(&sentinel, f.root.join("cache/index.sqlite")).unwrap();
                } else {
                    fs::create_dir(f.root.join("cache/index.sqlite")).unwrap();
                }
            }
        }
        assert!(!f.offline_removal("purge").status.success(), "{object}");
        assert!(f.state().join("record.json").exists());
        assert_eq!(fs::read(&sentinel).unwrap(), b"unrelated", "{object}");
        if ["binary", "maintenance", "cache-extra", "state-extra"].contains(&object) {
            assert_eq!(
                fs::read(f.root.join("cache/index.sqlite")).unwrap(),
                content
            );
        }
    }
}

#[test]
fn removal_never_adopts_preexisting_or_legacy_cache_identity() {
    for legacy in [false, true] {
        let f = Fixture::new();
        if !legacy {
            fs::create_dir(f.root.join("cache")).unwrap();
            fs::set_permissions(f.root.join("cache"), fs::Permissions::from_mode(0o700)).unwrap();
        }
        success(f.install());
        if legacy {
            let mut record = f.record();
            record.as_object_mut().unwrap().remove("cache_identity");
            record["schema_version"] = 2.into();
            f.write_record(&record);
        }
        let bytes = f.cache_bytes();
        success(f.removal("uninstall"));
        failure(
            f.offline_removal("purge"),
            "ownership identity was not recorded",
        );
        assert_eq!(fs::read(f.root.join("cache/index.sqlite")).unwrap(), bytes);
        assert!(f.state().join("maintenance").exists());
    }
}

#[test]
fn removal_permission_failures_retain_recoverable_progress() {
    for parent in ["bin", "state"] {
        let f = Fixture::new();
        success(f.install());
        let bytes = f.cache_bytes();
        fs::set_permissions(f.root.join(parent), fs::Permissions::from_mode(0o500)).unwrap();
        assert!(!f.removal("uninstall").status.success());
        assert!(f.destination().exists());
        assert!(f.state().join("maintenance").exists());
        assert_eq!(fs::read(f.root.join("cache/index.sqlite")).unwrap(), bytes);
        fs::set_permissions(f.root.join(parent), fs::Permissions::from_mode(0o700)).unwrap();
        success(f.offline_removal("uninstall"));
        success(f.offline_removal("purge"));
    }
}

#[test]
fn removal_recovers_every_durable_phase_and_final_record_only() {
    for phase in [
        "uninstalling",
        "uninstalled",
        "purging",
        "purge_finalizing",
        "record-only",
    ] {
        let f = Fixture::new();
        success(f.install());
        f.cache_bytes();
        success(f.removal("uninstall"));
        let mut record = f.record();
        record["phase"] = if phase == "record-only" {
            "purge_finalizing"
        } else {
            phase
        }
        .into();
        f.write_record(&record);
        if ["purge_finalizing", "record-only"].contains(&phase) {
            fs::remove_file(f.root.join("cache/index.sqlite")).unwrap();
        }
        if phase == "record-only" {
            fs::remove_file(f.state().join("maintenance")).unwrap();
        }
        success(f.offline_removal(if phase == "uninstalling" {
            "uninstall"
        } else {
            "purge"
        }));
        if phase == "uninstalling" {
            success(f.offline_removal("purge"));
        }
        assert!(!f.state().join("record.json").exists());
    }
}

#[test]
fn removal_finishes_interrupted_upgrade_before_deleting() {
    for phase in [
        "upgrade_prepared",
        "upgrade_retained",
        "upgrade_installed",
        "upgrade_copied",
        "rollback",
        "rollback_complete",
        "complete",
    ] {
        let f = Fixture::new();
        success(f.install());
        f.interrupted_upgrade(phase);
        success(f.removal("uninstall"));
        assert!(!f.destination().exists(), "{phase}");
        assert_eq!(f.record()["phase"], "uninstalled");
        assert!(!f.state().join("candidate").exists());
        assert!(!f.state().join("previous").exists());
        success(f.offline_removal("purge"));
    }
}

#[test]
fn removal_stops_owned_daemon_and_excludes_new_startup_during_wait() {
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let f = Fixture::new();
    success(f.install());
    let cache = f.root.join("cache");
    let mut daemon = Command::new(f.destination())
        .arg("__daemon")
        .env("GREPGLINT_CACHE_DIR", &cache)
        .env("GREPGLINT_CACHE_MB", "8")
        .env("GREPGLINT_IDLE_SECONDS", "60")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let config = grepglint::daemon::Config {
        directory: cache.clone(),
        max_bytes: 8 * 1024 * 1024,
        idle: Duration::from_secs(60),
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    while !matches!(grepglint::maintenance::status(&config), Ok(Some(_))) {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(unsafe { libc::kill(daemon.id() as i32, libc::SIGSTOP) }, 0);
    let child = f
        .command("purge")
        .arg("--purge-cache")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(cache.join("maintenance.lock"))
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if fs2::FileExt::try_lock_shared(&lock).is_err() {
            break;
        }
        fs2::FileExt::unlock(&lock).unwrap();
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(5));
    }
    failure(
        f.command("uninstall").arg("--yes").output().unwrap(),
        "Another installer operation",
    );
    let startup = Command::new(f.destination())
        .arg("__daemon")
        .env("GREPGLINT_CACHE_DIR", &cache)
        .env("GREPGLINT_CACHE_MB", "8")
        .output()
        .unwrap();
    assert!(!startup.status.success());
    assert!(cache.join("index.sqlite").exists());
    assert_eq!(unsafe { libc::kill(daemon.id() as i32, libc::SIGCONT) }, 0);
    let first = child.wait_with_output().unwrap();
    if !first.status.success() {
        success(f.removal("purge"));
    }
    assert!(daemon.wait().unwrap().success());
    assert!(!cache.join("index.sqlite").exists());
    assert!(grepglint::maintenance::status(&config).unwrap().is_none());
}

#[test]
fn removal_refuses_stale_identity_and_preserves_cache() {
    use std::os::unix::net::UnixListener;
    let f = Fixture::new();
    success(f.install());
    let contents = f.cache_bytes();
    let listener = UnixListener::bind(f.root.join("cache/daemon.sock")).unwrap();
    fs::set_permissions(
        f.root.join("cache/daemon.sock"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    drop(listener);
    assert!(!f.removal("purge").status.success());
    assert_eq!(
        fs::read(f.root.join("cache/index.sqlite")).unwrap(),
        contents
    );
    assert!(f.destination().exists());
}

#[test]
fn finalization_recovery_preserves_new_entries_and_honors_locks() {
    for obstruction in ["extra", "cache", "lock", "substitute"] {
        let f = Fixture::new();
        success(f.install());
        success(f.removal("uninstall"));
        let mut record = f.record();
        record["phase"] = "purge_finalizing".into();
        f.write_record(&record);
        fs::remove_file(f.state().join("maintenance")).unwrap();
        let lock = fs::File::open(f.root.join("cache/maintenance.lock")).unwrap();
        match obstruction {
            "extra" => fs::write(f.state().join("extra"), b"keep").unwrap(),
            "cache" => {
                f.cache_bytes();
            }
            "lock" => fs2::FileExt::lock_shared(&lock).unwrap(),
            _ => {
                fs::rename(f.root.join("cache"), f.root.join("old-cache")).unwrap();
                fs::create_dir(f.root.join("cache")).unwrap();
            }
        }
        assert!(
            !f.offline_removal("purge").status.success(),
            "{obstruction}"
        );
        assert!(f.state().join("record.json").exists());
        if obstruction == "cache" {
            success(f.removal("purge"));
            assert!(!f.state().join("record.json").exists());
        }
    }
}

#[test]
fn purge_erases_indexed_source_without_changing_repository() {
    let f = Fixture::new();
    success(f.install());
    let repo = f.root.join("repo");
    fs::create_dir(&repo).unwrap();
    let git = |args: &[&str]| {
        success(
            Command::new("git")
                .args(args)
                .current_dir(&repo)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .output()
                .unwrap(),
        );
    };
    git(&["-c", "init.templateDir=", "init", "-q"]);
    let source = repo.join("source.ts");
    fs::write(
        &source,
        "export function preciousQuartz() { return 'uniquecachedsource'; }\n",
    )
    .unwrap();
    git(&["add", "source.ts"]);
    git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    ]);
    let index = fs::read(repo.join(".git/index")).unwrap();
    let head = fs::read(repo.join(".git/HEAD")).unwrap();
    let original = fs::read(&source).unwrap();
    success(
        Command::new(f.destination())
            .args(["search", "--json", "uniquecachedsource"])
            .current_dir(&repo)
            .env("GREPGLINT_CACHE_DIR", f.root.join("cache"))
            .env("GREPGLINT_CACHE_MB", "8")
            .env("GREPGLINT_IDLE_SECONDS", "60")
            .output()
            .unwrap(),
    );
    assert!(f.root.join("cache/index.sqlite").exists());
    success(f.offline_removal("uninstall"));
    success(f.offline_removal("purge"));
    assert!(!f.root.join("cache/index.sqlite").exists());
    assert_eq!(fs::read(source).unwrap(), original);
    assert_eq!(fs::read(repo.join(".git/index")).unwrap(), index);
    assert_eq!(fs::read(repo.join(".git/HEAD")).unwrap(), head);
}

#[test]
fn cache_creation_collision_and_publication_interruption_preserve_ownership() {
    for point in [
        "staged",
        "published",
        "collision",
        "substitution",
        "copy-failure",
        "publication-record-failure",
    ] {
        let f = Fixture::new();
        success(f.install());
        let cache = f.root.join("cache");
        fs::remove_dir_all(&cache).unwrap();
        let staging = f.root.join(".grepglint-cache-fixture");
        fs::create_dir(&staging).unwrap();
        fs::set_permissions(&staging, fs::Permissions::from_mode(0o700)).unwrap();
        let metadata = fs::metadata(&staging).unwrap();
        let mut record = f.record();
        record["phase"] = "prepared".into();
        record["cache_owned"] = false.into();
        record.as_object_mut().unwrap().remove("cache_identity");
        record["cache_creation"] = serde_json::json!({"directory":staging,"identity":{"device":metadata.dev(),"inode":metadata.ino()}});
        f.write_record(&record);
        match point {
            "published" => fs::rename(&staging, &cache).unwrap(),
            "collision" => {
                fs::create_dir(&cache).unwrap();
                fs::set_permissions(&cache, fs::Permissions::from_mode(0o700)).unwrap();
                fs::write(cache.join("sentinel"), b"preexisting source cache").unwrap();
            }
            "substitution" => {
                fs::rename(&staging, f.root.join("original-staging")).unwrap();
                fs::create_dir(&staging).unwrap();
                fs::set_permissions(&staging, fs::Permissions::from_mode(0o700)).unwrap();
                fs::write(staging.join("sentinel"), b"unrelated").unwrap();
            }
            "publication-record-failure" => {
                fs::set_permissions(f.state(), fs::Permissions::from_mode(0o500)).unwrap();
            }
            "copy-failure" => {
                fs::remove_file(f.destination()).unwrap();
                fs::set_permissions(f.root.join("bin"), fs::Permissions::from_mode(0o500)).unwrap();
            }
            _ => {}
        }
        if point == "substitution" {
            failure(f.install(), "Staged cache directory replaced");
            assert_eq!(fs::read(staging.join("sentinel")).unwrap(), b"unrelated");
            assert!(!cache.exists());
            continue;
        }
        if point == "publication-record-failure" {
            assert!(!f.install().status.success());
            assert!(cache.exists());
            assert!(f.record()["cache_creation"].is_object());
            fs::set_permissions(f.state(), fs::Permissions::from_mode(0o700)).unwrap();
        }
        if point == "copy-failure" {
            assert!(!f.install().status.success());
            assert!(f.record()["cache_identity"].is_object());
            fs::set_permissions(f.root.join("bin"), fs::Permissions::from_mode(0o700)).unwrap();
        }
        success(f.install());
        assert!(f.record().get("cache_creation").is_none());
        assert!(!staging.exists());
        success(f.removal("uninstall"));
        if point == "collision" {
            assert_eq!(f.record()["cache_owned"], false);
            failure(f.removal("purge"), "ownership identity was not recorded");
            assert_eq!(
                fs::read(cache.join("sentinel")).unwrap(),
                b"preexisting source cache"
            );
        } else {
            assert_eq!(f.record()["cache_owned"], true);
            success(f.removal("purge"));
        }
    }
}

#[test]
fn cache_is_not_created_before_preflight_and_first_record_succeed() {
    use std::os::unix::process::CommandExt;
    let f = Fixture::new();
    fs::write(f.root.join("bin"), b"unrelated").unwrap();
    assert!(!f.install().status.success());
    assert!(!f.root.join("cache").exists());
    fs::remove_file(f.root.join("bin")).unwrap();
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
                rlim_cur: 32,
                rlim_max: 32,
            };
            if libc::setrlimit(libc::RLIMIT_FSIZE, &cap) == 0 {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        });
    }
    assert!(!command.output().unwrap().status.success());
    assert!(!f.root.join("cache").exists());
    assert!(!fs::read_dir(&f.root).unwrap().any(|entry| {
        entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".grepglint-cache-")
    }));
    success(f.install());
    assert_eq!(f.record()["cache_owned"], true);
    success(f.removal("uninstall"));
    success(f.removal("purge"));
}

#[test]
fn terminal_removal_cancellation_succeeds_without_mutation() {
    use std::io::Write;
    use std::os::fd::FromRawFd;
    for action in ["uninstall", "purge"] {
        let f = Fixture::new();
        success(f.install());
        let before = fs::read(f.state().join("record.json")).unwrap();
        let mut master = 0;
        let mut slave = 0;
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master,
                    &mut slave,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            },
            0
        );
        let mut input = unsafe { fs::File::from_raw_fd(master) };
        let terminal = unsafe { fs::File::from_raw_fd(slave) };
        let child = f
            .command(action)
            .stdin(terminal)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        input.write_all(b"n\n").unwrap();
        success(child.wait_with_output().unwrap());
        assert_eq!(fs::read(f.state().join("record.json")).unwrap(), before);
        assert!(f.destination().exists());
    }
}

#[test]
fn pending_cache_creation_records_recover_before_allocating_more_directories() {
    for point in [
        "complete",
        "partial-name",
        "partial-identity",
        "changed",
        "substituted",
    ] {
        let f = Fixture::new();
        success(f.install());
        fs::remove_dir_all(f.root.join("cache")).unwrap();
        let staging = f.root.join(".grepglint-cache-ABC123");
        fs::create_dir(&staging).unwrap();
        fs::set_permissions(&staging, fs::Permissions::from_mode(0o700)).unwrap();
        let identity = fs::metadata(&staging).unwrap();
        let mut record = f.record();
        record["phase"] = "prepared".into();
        record["cache_owned"] = false.into();
        record.as_object_mut().unwrap().remove("cache_identity");
        f.write_record(&record);
        let mut proposal = record.clone();
        proposal["cache_creation"] = serde_json::json!({"directory":staging,"identity":{"device":identity.dev(),"inode":identity.ino()}});
        let typed: grepglint::setup::Record = serde_json::from_value(proposal).unwrap();
        let mut bytes = serde_json::to_vec_pretty(&typed).unwrap();
        let text = String::from_utf8(bytes.clone()).unwrap();
        if point == "partial-name" {
            bytes.truncate(text.find("ABC123").unwrap() + 3);
        }
        if point == "partial-identity" {
            bytes.truncate(text.rfind("\"inode\": ").unwrap() + "\"inode\": ".len() + 1);
        }
        if point == "changed" {
            bytes[0] = b'x';
        }
        if point == "substituted" {
            fs::rename(&staging, f.root.join("original-stage")).unwrap();
            fs::create_dir(&staging).unwrap();
            fs::set_permissions(&staging, fs::Permissions::from_mode(0o700)).unwrap();
            fs::write(staging.join("sentinel"), b"keep").unwrap();
        }
        let pending = f.state().join("record.json.grepglint-pending");
        fs::write(&pending, &bytes).unwrap();
        fs::set_permissions(&pending, fs::Permissions::from_mode(0o600)).unwrap();
        let stages = || {
            fs::read_dir(&f.root)
                .unwrap()
                .filter(|entry| {
                    entry
                        .as_ref()
                        .unwrap()
                        .file_name()
                        .to_string_lossy()
                        .starts_with(".grepglint-cache-")
                })
                .count()
        };
        assert_eq!(stages(), 1);
        if ["changed", "substituted"].contains(&point) {
            for _ in 0..2 {
                assert!(!f.install().status.success());
                assert_eq!(stages(), 1);
                assert_eq!(fs::read(&pending).unwrap(), bytes);
            }
            if point == "substituted" {
                assert_eq!(fs::read(staging.join("sentinel")).unwrap(), b"keep");
            }
            continue;
        }
        success(f.install());
        assert!(!pending.exists());
        assert_eq!(stages(), usize::from(point != "complete"));
        success(f.install());
        assert_eq!(stages(), usize::from(point != "complete"));
        assert_eq!(f.record()["cache_owned"], true);
        success(f.removal("uninstall"));
        success(f.removal("purge"));
        assert_eq!(stages(), usize::from(point != "complete"));
    }
}

#[test]
fn repeated_cache_record_write_failures_do_not_accumulate_staging_directories() {
    use std::os::unix::process::CommandExt;
    for failure in ["permissions", "partial-write"] {
        let f = Fixture::new();
        success(f.install());
        fs::remove_dir_all(f.root.join("cache")).unwrap();
        let mut record = f.record();
        record["phase"] = "prepared".into();
        record["cache_owned"] = false.into();
        record.as_object_mut().unwrap().remove("cache_identity");
        f.write_record(&record);
        let typed: grepglint::setup::Record = serde_json::from_value(record).unwrap();
        let limit = serde_json::to_vec_pretty(&typed).unwrap().len() as u64 + 50;
        if failure == "permissions" {
            fs::set_permissions(f.state(), fs::Permissions::from_mode(0o500)).unwrap();
        }
        for _ in 0..3 {
            let mut command = f.command("install");
            if failure == "partial-write" {
                unsafe {
                    command.pre_exec(move || {
                        libc::signal(libc::SIGXFSZ, libc::SIG_IGN);
                        let cap = libc::rlimit {
                            rlim_cur: limit,
                            rlim_max: limit,
                        };
                        if libc::setrlimit(libc::RLIMIT_FSIZE, &cap) == 0 {
                            Ok(())
                        } else {
                            Err(std::io::Error::last_os_error())
                        }
                    });
                }
            }
            assert!(!command.output().unwrap().status.success());
            assert!(!f.root.join("cache").exists());
            assert!(!f.state().join("record.json.grepglint-pending").exists());
            assert!(!fs::read_dir(&f.root).unwrap().any(|entry| {
                entry
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".grepglint-cache-")
            }));
        }
        fs::set_permissions(f.state(), fs::Permissions::from_mode(0o700)).unwrap();
        success(f.install());
        success(f.removal("uninstall"));
        success(f.removal("purge"));
    }
}
