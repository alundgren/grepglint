//! Human maintenance. Records live separately from the disposable search cache.
mod cache;
mod change;
mod files;
mod process;
mod remove;
mod verify;

use crate::daemon::Config;
use crate::maintenance::Maintenance;
use anyhow::{Context, Result, bail, ensure};
use clap::{Args, ValueEnum};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::fs::OpenOptions;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Clone, Debug, ValueEnum)]
pub enum Action {
    Install,
    Verify,
    Status,
    Upgrade,
    Repair,
    Uninstall,
    Purge,
}

#[derive(Args)]
pub struct Options {
    #[arg(value_enum)]
    pub action: Action,
    #[arg(long)]
    pub state_dir: Option<PathBuf>,
    #[arg(long)]
    pub destination: Option<PathBuf>,
    #[arg(long)]
    pub cache_dir: Option<PathBuf>,
    #[arg(long)]
    pub release: Option<String>,
    #[arg(long)]
    pub commit: Option<String>,
    #[arg(long)]
    pub digest: Option<String>,
    /// Verified recorded executable to restore when a newer helper runs repair
    #[arg(long)]
    pub repair_source: Option<PathBuf>,
    /// Permit migration of a Cargo-recorded executable, retaining a rollback copy
    #[arg(long)]
    pub migrate_cargo: bool,
    /// Confirm irreversible removal of recorded cached source contents
    #[arg(long)]
    pub purge_cache: bool,
    /// Confirm uninstall without prompting
    #[arg(long)]
    pub yes: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub schema_version: u32,
    pub phase: String,
    pub release: String,
    pub commit: String,
    pub digest: String,
    pub destination: PathBuf,
    pub cache: PathBuf,
    pub cache_owned: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_identity: Option<cache::Identity>,
    pub database_bytes: u64,
    pub idle_seconds: u64,
    pub previous_digest: Option<String>,
    pub previous_mode: Option<u32>,
    pub cargo_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub change: Option<Box<change::Change>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_creation: Option<cache::Creation>,
}

impl Record {
    fn config(&self) -> Config {
        Config {
            directory: self.cache.clone(),
            max_bytes: self.database_bytes,
            idle: Duration::from_secs(self.idle_seconds),
        }
    }
    fn validate(&self, state: &Path) -> Result<()> {
        ensure!(
            matches!(self.schema_version, 1..=3),
            "Unsupported installation record version; state preserved"
        );
        ensure!(
            [
                "prepared",
                "retained",
                "installed",
                "complete",
                "upgrade_prepared",
                "upgrade_retained",
                "upgrade_installed",
                "upgrade_copied",
                "rollback",
                "rollback_complete",
                "repairing",
                "uninstalling",
                "uninstalled",
                "purging",
                "purge_finalizing"
            ]
            .contains(&self.phase.as_str()),
            "Unrecognized installation phase; state preserved"
        );
        ensure!(
            valid_release(&self.release) && hex(&self.commit, 40) && hex(&self.digest, 64),
            "Invalid installation identity"
        );
        ensure!(
            self.previous_digest
                .as_ref()
                .is_none_or(|digest| hex(digest, 64)),
            "Invalid recovery hash"
        );
        ensure!(
            self.previous_digest.is_some() == self.previous_mode.is_some()
                && self
                    .previous_mode
                    .is_none_or(|mode| mode & !0o777 == 0 && mode & 0o022 == 0),
            "Invalid prior executable permissions"
        );
        ensure!(
            self.cargo_digest
                .as_ref()
                .is_none_or(|digest| hex(digest, 64)),
            "Invalid Cargo migration digest"
        );
        ensure!(
            self.cache_identity.is_none() || self.cache_owned,
            "Invalid cache ownership record"
        );
        cache::validate(self)?;
        change::validate(self, state)?;
        files::paths(&self.destination)?;
        files::paths(&self.cache)?;
        ensure!(
            !self.destination.starts_with(state)
                && !state.starts_with(&self.destination)
                && !self.cache.starts_with(state)
                && !state.starts_with(&self.cache)
                && !self.destination.starts_with(&self.cache)
                && !self.cache.starts_with(&self.destination),
            "Destination, state and cache must be separate"
        );
        ensure!(
            self.cache
                .join("daemon.sock")
                .as_os_str()
                .as_encoded_bytes()
                .len()
                < 100,
            "Cache path is too long for Unix sockets"
        );
        ensure!(
            (8 * 1024 * 1024..=1024 * 1024 * 1024).contains(&self.database_bytes)
                && (1..=3600).contains(&self.idle_seconds),
            "Invalid recorded resource limits"
        );
        Ok(())
    }
    fn save(&self, state: &Path) -> Result<()> {
        self.validate(state)?;
        files::save(
            &state.join("record.json"),
            &serde_json::to_vec_pretty(self)?,
        )
    }
}

fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn valid_release(value: &str) -> bool {
    value.strip_prefix('v').is_some_and(|value| {
        let parts: Vec<_> = value.split('.').collect();
        parts.len() == 3
            && parts.iter().all(|part| {
                !part.is_empty()
                    && part.bytes().all(|b| b.is_ascii_digit())
                    && (*part == "0" || !part.starts_with('0'))
            })
    })
}
fn state_path(options: &Options) -> Result<PathBuf> {
    if let Some(path) = &options.state_dir {
        return Ok(path.clone());
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or(home()?.join(".local/state"));
    Ok(base.join("grepglint"))
}
fn home() -> Result<PathBuf> {
    Ok(PathBuf::from(
        std::env::var_os("HOME").context("HOME is required")?,
    ))
}
fn load(state: &Path) -> Result<Option<Record>> {
    let path = state.join("record.json");
    if files::absent(&path)? {
        return Ok(None);
    }
    let record: Record = serde_json::from_slice(&files::read(&path, files::RECORD_CAP)?)?;
    record.validate(state)?;
    Ok(Some(record))
}

pub fn run(options: &Options) -> Result<()> {
    let core_limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    ensure!(
        unsafe { libc::setrlimit(libc::RLIMIT_CORE, &core_limit) } == 0,
        "Cannot disable maintenance core dumps"
    );
    ensure!(
        unsafe { libc::geteuid() } != 0,
        "Run as your normal account, without sudo"
    );
    ensure!(
        matches!(
            options.action,
            Action::Install
                | Action::Verify
                | Action::Status
                | Action::Upgrade
                | Action::Repair
                | Action::Uninstall
                | Action::Purge
        ),
        "This action is not available in this version"
    );
    let state = state_path(options)?;
    files::paths(&state)?;
    if matches!(options.action, Action::Status) {
        if let Some(record) = load(&state)? {
            println!(
                "Installation: {}\nExecutable: {}\nRelease: {}",
                record.phase,
                record.destination.display(),
                record.release
            );
        } else {
            println!("No managed installation");
        }
        return Ok(());
    }
    if matches!(options.action, Action::Uninstall | Action::Purge) && files::absent(&state)? {
        println!("No managed installation remains; nothing to remove");
        return Ok(());
    }
    if matches!(
        options.action,
        Action::Verify | Action::Uninstall | Action::Purge
    ) {
        files::directory(&state, false, true)?;
    } else {
        files::directory(&state, true, true)?;
    }
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(state.join("operation.lock"))?;
    let meta = lock.metadata()?;
    ensure!(
        meta.is_file()
            && meta.nlink() == 1
            && meta.uid() == unsafe { libc::geteuid() }
            && meta.mode() & 0o077 == 0,
        "Unsafe operation lock"
    );
    lock.try_lock_exclusive()
        .context("Another installer operation is running; retry after it finishes")?;
    let record = load(&state)?;
    if let Some(record) = &record {
        ensure!(
            options
                .destination
                .as_ref()
                .is_none_or(|value| value == &record.destination)
                && options
                    .cache_dir
                    .as_ref()
                    .is_none_or(|value| value == &record.cache),
            "Overrides differ from recorded paths; nothing changed"
        );
        ensure!(
            matches!(options.action, Action::Upgrade)
                || options
                    .release
                    .as_ref()
                    .is_none_or(|value| value == &record.release),
            "Upgrade or downgrade requires the upgrade action; recorded release preserved"
        );
    }
    if matches!(options.action, Action::Uninstall | Action::Purge) {
        return remove::run(options, &state, record);
    }
    if record
        .as_ref()
        .is_some_and(|record| record.change.is_some())
    {
        return change::run(options, &state, record.unwrap());
    }
    match options.action {
        Action::Verify => verify::installation(&record.context("No installation record")?, &state),
        Action::Install => install(options, &state, record),
        Action::Upgrade | Action::Repair => change::run(
            options,
            &state,
            record.context("No installation record; run ./install install first")?,
        ),
        _ => unreachable!(),
    }
}

fn cargo_owned(path: &Path) -> Result<bool> {
    let cargo = std::env::var_os("CARGO_HOME")
        .map(PathBuf::from)
        .unwrap_or(home()?.join(".cargo"));
    if path != cargo.join("bin/grepglint") {
        return Ok(false);
    }
    let metadata = cargo.join(".crates2.json");
    if files::absent(&metadata)? {
        return Ok(false);
    }
    let value: serde_json::Value =
        serde_json::from_slice(&files::read(&metadata, files::RECORD_CAP)?)?;
    Ok(value
        .get("installs")
        .and_then(|value| value.as_object())
        .is_some_and(|installs| {
            installs.iter().any(|(name, value)| {
                name.starts_with("grepglint ")
                    && value
                        .get("bins")
                        .and_then(|v| v.as_array())
                        .is_some_and(|bins| bins.iter().any(|v| v.as_str() == Some("grepglint")))
            })
        }))
}

fn consent_to_migrate(consent: bool, explanation: &str) -> Result<()> {
    if consent {
        return Ok(());
    }
    use std::io::{BufRead, IsTerminal, Read, Write};
    ensure!(
        std::io::stdin().is_terminal(),
        "Cargo migration requires explicit --migrate-cargo consent. {explanation}"
    );
    print!("{explanation}. Continue? [y/N] ");
    std::io::stdout().flush()?;
    let mut answer = String::new();
    std::io::stdin().lock().take(16).read_line(&mut answer)?;
    ensure!(
        matches!(answer.trim(), "y" | "Y" | "yes"),
        "Cancelled; no executable or cache changed"
    );
    Ok(())
}

fn path_conflicts(destination: &Path, consent: bool) -> Result<Option<String>> {
    if let Some(value) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&value) {
            let candidate = directory.join("grepglint");
            if !files::absent(&candidate)? && candidate != destination {
                println!(
                    "PATH contains {}. Selected executable: {}",
                    candidate.display(),
                    destination.display()
                );
                ensure!(
                    cargo_owned(&candidate)?,
                    "Unknown PATH executable; resolve it manually before installation"
                );
                consent_to_migrate(
                    consent,
                    "Cargo executable found on PATH; it will remain untouched",
                )?;
                println!(
                    "PATH may select the Cargo executable first. Put {} before its directory in PATH",
                    destination.parent().unwrap().display()
                );
                return Ok(Some(files::hash(&candidate)?));
            }
        }
    }
    Ok(None)
}

fn install(options: &Options, state: &Path, existing: Option<Record>) -> Result<()> {
    if let Some(record) = &existing
        && record.phase == "complete"
    {
        verify::installation(record, state)?;
        println!(
            "Already installed and healthy; no files replaced and real daemon was not restarted"
        );
        return Ok(());
    }
    let source = std::env::current_exe()?.canonicalize()?;
    let digest = files::hash(&source)?;
    let mut record = if let Some(record) = existing {
        ensure!(
            record.digest == digest,
            "Recovery executable differs from recorded verified bytes"
        );
        record
    } else {
        let release = options.release.clone().context("Use the trusted ./install entrypoint, or supply release provenance for this trusted native executable")?;
        let commit = options
            .commit
            .clone()
            .context("Missing verified release commit")?;
        ensure!(
            options.digest.as_deref() == Some(&digest),
            "Verified executable hash mismatch"
        );
        ensure!(
            release == format!("v{}", env!("CARGO_PKG_VERSION")),
            "Release version mismatch"
        );
        let destination = options
            .destination
            .clone()
            .unwrap_or(home()?.join(".local/bin/grepglint"));
        let mut config = Config::from_env()?;
        if let Some(path) = &options.cache_dir {
            config.directory = path.clone();
        }
        let mut record = Record {
            schema_version: 3,
            phase: "prepared".into(),
            release,
            commit,
            digest: digest.clone(),
            destination,
            cache: config.directory,
            cache_owned: false,
            cache_identity: None,
            database_bytes: config.max_bytes,
            idle_seconds: config.idle.as_secs(),
            previous_digest: None,
            previous_mode: None,
            cargo_digest: None,
            change: None,
            cache_creation: None,
        };
        record.validate(state)?;
        ensure!(
            files::absent(&state.join("maintenance"))? && files::absent(&state.join("previous"))?,
            "Unrecorded maintenance or recovery executable preserved"
        );
        for path in [
            &record.destination,
            &state.join("maintenance"),
            &state.join("previous"),
        ] {
            ensure!(
                files::absent(&files::pending(path))?,
                "Unrecorded pending file preserved; inspect it before installation"
            );
        }
        record.cargo_digest = path_conflicts(&record.destination, options.migrate_cargo)?;
        if !files::absent(&record.destination)? {
            ensure!(
                cargo_owned(&record.destination)?,
                "Unknown destination executable; preserve it and resolve manually"
            );
            consent_to_migrate(
                options.migrate_cargo,
                "The selected Cargo executable will be replaced after saving a recovery copy",
            )?;
            record.previous_digest = Some(files::hash(&record.destination)?);
            record.previous_mode =
                Some(std::fs::symlink_metadata(&record.destination)?.mode() & 0o777);
        }
        if !files::absent(&record.cache)? {
            files::directory(&record.cache, false, true)?;
        }
        let parent = record
            .destination
            .parent()
            .context("Missing destination parent")?;
        files::directory(parent, true, false)?;
        files::reserve(parent, 4 * files::BINARY_CAP + 64 * 1024 * 1024)?;
        files::reserve(state, 4 * files::BINARY_CAP + 64 * 1024 * 1024)?;
        record.save(state)?;
        record
    };
    files::reserve(state, 4 * files::BINARY_CAP + 64 * 1024 * 1024)?;
    files::reserve(
        record
            .destination
            .parent()
            .context("Missing destination parent")?,
        4 * files::BINARY_CAP + 64 * 1024 * 1024,
    )?;
    cache::recover_pending(&mut record, state)?;
    if record.phase == "prepared"
        && record.cache_creation.is_none()
        && files::absent(&record.cache)?
    {
        let previous = record.clone();
        cache::stage(&mut record)?;
        cache::save_creation(&record, &previous, state)?;
    }
    cache::publish(&mut record, state)?;
    let maintenance = state.join("maintenance");
    let backup = state.join("previous");
    if files::absent(&maintenance)? {
        files::copy(&source, &maintenance, &record.digest)?;
    }
    ensure!(
        files::hash(&maintenance)? == record.digest,
        "Maintenance copy changed; preserved"
    );
    if record.phase == "prepared" {
        if let Some(previous) = &record.previous_digest {
            if files::absent(&backup)? {
                files::copy(&record.destination, &backup, previous)?;
            }
            ensure!(
                files::hash(&backup)? == *previous,
                "Recovery copy changed; preserved"
            );
        }
        record.phase = "retained".into();
        record.save(state)?;
    }
    let config = record.config();
    if !files::absent(&config.directory)? {
        files::directory(&config.directory, false, true)?;
    }
    files::directory(&config.directory, true, true)?;
    let mut guard = Maintenance::acquire(&config)?;
    if let Some(health) = guard.status()? {
        ensure!(
            health.executable_sha256 == record.digest
                || record.previous_digest.as_ref() == Some(&health.executable_sha256)
                || record.cargo_digest.as_ref() == Some(&health.executable_sha256),
            "Unrecognized real daemon; nothing stopped"
        );
        guard.shutdown(&health.instance)?;
    }
    let result = (|| {
        if !files::absent(&record.destination)? {
            let current = files::hash(&record.destination)?;
            if current != record.digest {
                ensure!(
                    record.previous_digest.as_ref() == Some(&current),
                    "Modified destination preserved"
                );
                // The durable prior copy remains until verification and completion.
                files::replace(
                    &maintenance,
                    &record.destination,
                    &record.digest,
                    Some(&current),
                )?;
            }
        }
        if files::absent(&record.destination)? {
            files::copy(&maintenance, &record.destination, &record.digest)?;
        }
        record.phase = "installed".into();
        record.save(state)?;
        verify::installation(&record, state)?;
        record.phase = "complete".into();
        record.save(state)?;
        if let Some(previous) = &record.previous_digest {
            files::remove_matching(&backup, previous)?;
        }
        println!(
            "Installed {} at {}. Add {} to PATH if needed; shell profiles were not changed",
            record.release,
            record.destination.display(),
            record.destination.parent().unwrap().display()
        );
        Ok(())
    })();
    if let Err(error) = result {
        if !files::absent(&record.destination)?
            && files::hash(&record.destination)? != record.digest
        {
            bail!("{error:#}. Changed destination preserved; recovery record retained");
        }
        if let Some(previous) = &record.previous_digest {
            if files::absent(&record.destination)? {
                files::copy(&backup, &record.destination, previous)?;
            } else {
                files::replace(&backup, &record.destination, previous, Some(&record.digest))?;
            }
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(
                &record.destination,
                std::fs::Permissions::from_mode(record.previous_mode.unwrap()),
            )?;
            std::fs::File::open(&record.destination)?.sync_all()?;
        } else {
            files::remove_matching(&record.destination, &record.digest)?;
        }
        record.phase = "retained".into();
        record.save(state)?;
        return Err(error.context("Installation rolled back; rerun ./install install to resume"));
    }
    Ok(())
}
