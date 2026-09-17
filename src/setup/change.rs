use super::{Action, Options, Record, files, verify};
use crate::maintenance::Maintenance;
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Change {
    pub prior: Record,
    pub destination_mode: u32,
    pub maintenance_mode: u32,
}

pub fn validate(record: &Record, state: &Path) -> Result<()> {
    if let Some(change) = &record.change {
        ensure!(
            record.schema_version == 2 && change.prior.change.is_none(),
            "Invalid upgrade recovery record"
        );
        ensure!(
            change.prior.phase == "complete",
            "Invalid prior installation phase"
        );
        change.prior.validate(state)?;
        ensure!(
            record.destination == change.prior.destination
                && record.cache == change.prior.cache
                && record.cache_owned == change.prior.cache_owned
                && record.database_bytes == change.prior.database_bytes
                && record.idle_seconds == change.prior.idle_seconds,
            "Upgrade changed recorded paths or settings"
        );
        for mode in [change.destination_mode, change.maintenance_mode] {
            ensure!(
                mode & !0o777 == 0 && mode & 0o022 == 0 && mode & 0o100 != 0,
                "Invalid rollback permissions"
            );
        }
    } else {
        ensure!(
            !record.phase.starts_with("upgrade_") && !record.phase.starts_with("rollback"),
            "Missing upgrade recovery record"
        );
    }
    Ok(())
}

fn checked(path: &Path, allowed: &[&str]) -> Result<Option<String>> {
    if files::absent(path)? {
        return Ok(None);
    }
    let hash = files::hash(path)?;
    ensure!(
        allowed.contains(&hash.as_str()),
        "Modified executable preserved: {}",
        path.display()
    );
    Ok(Some(hash))
}

fn publish(source: &Path, target: &Path, digest: &str, allowed: &[&str]) -> Result<()> {
    let current = checked(target, allowed)?;
    if current.as_deref() != Some(digest) {
        files::replace(source, target, digest, current.as_deref())?;
    }
    Ok(())
}

fn mode(path: &Path, value: u32) -> Result<()> {
    fs::set_permissions(path, fs::Permissions::from_mode(value))?;
    fs::File::open(path)?.sync_all()?;
    Ok(())
}

fn exclusion<'a>(record: &Record, config: &'a crate::daemon::Config) -> Result<Maintenance<'a>> {
    files::directory(&record.cache, false, true)?;
    let mut guard = Maintenance::acquire(config)?;
    if let Some(health) = guard.status()? {
        ensure!(
            health.executable_sha256 == record.digest
                || record
                    .change
                    .as_ref()
                    .is_some_and(|change| health.executable_sha256 == change.prior.digest)
                || record.cargo_digest.as_ref() == Some(&health.executable_sha256),
            "Unrecognized real daemon; pause searches and retry after its idle exit; use rg meanwhile"
        );
        guard.shutdown(&health.instance)?;
    }
    Ok(guard)
}

fn capacity(record: &Record, state: &Path) -> Result<()> {
    // Old pair, candidate, rollback, and both replacement files may coexist.
    let bytes = 7 * files::BINARY_CAP + 64 * 1024 * 1024;
    files::reserve(state, bytes)?;
    files::reserve(
        record
            .destination
            .parent()
            .context("Missing destination parent")?,
        bytes,
    )
}

fn cleanup(record: &mut Record, state: &Path) -> Result<()> {
    let change = record
        .change
        .as_ref()
        .context("Missing upgrade recovery data")?;
    files::remove_matching(&state.join("previous"), &change.prior.digest)?;
    files::remove_matching(&state.join("candidate"), &record.digest)?;
    record.change = None;
    record.save(state)
}

fn rollback(record: &mut Record, state: &Path) -> Result<()> {
    let change = record.change.clone().context("Missing rollback record")?;
    record.phase = "rollback".into();
    record.save(state)?;
    let backup = state.join("previous");
    let retained = state.join("maintenance");
    let allowed = [&record.digest[..], &change.prior.digest[..]];
    checked(&record.destination, &allowed)?;
    checked(&retained, &allowed)?;
    let source = if checked(&backup, &[&change.prior.digest])?.is_some() {
        backup.clone()
    } else if checked(&retained, &allowed)?.as_deref() == Some(&change.prior.digest) {
        retained.clone()
    } else if checked(&record.destination, &allowed)?.as_deref() == Some(&change.prior.digest) {
        record.destination.clone()
    } else {
        bail!("Verified rollback executable missing; recovery data preserved");
    };
    // Establish a stable source before replacing either executable location.
    if files::absent(&backup)? {
        files::copy(&source, &backup, &change.prior.digest)?;
    }
    let executable = std::env::current_exe()?.canonicalize()?;
    let candidate = state.join("candidate");
    let new_source = if checked(&candidate, &[&record.digest])?.is_some() {
        candidate.clone()
    } else {
        ensure!(
            files::hash(&executable)? == record.digest,
            "Candidate missing; use trusted ./install repair to fetch the recorded release"
        );
        executable
    };
    for target in [&record.destination, &retained, &candidate] {
        files::discard_pending(target, &[&backup, &new_source])?;
    }
    publish(&backup, &record.destination, &change.prior.digest, &allowed)?;
    mode(&record.destination, change.destination_mode)?;
    publish(&backup, &retained, &change.prior.digest, &allowed)?;
    mode(&retained, change.maintenance_mode)?;
    verify::executables(&change.prior, state)?;
    record.phase = "rollback_complete".into();
    record.save(state)?;
    finish_rollback(record, state)?;
    Ok(())
}

fn finish_rollback(record: &mut Record, state: &Path) -> Result<()> {
    let prior = record
        .change
        .as_ref()
        .context("Missing rollback data")?
        .prior
        .clone();
    files::remove_matching(&state.join("previous"), &prior.digest)?;
    files::remove_matching(&state.join("candidate"), &record.digest)?;
    prior.save(state)?;
    *record = prior;
    Ok(())
}

pub fn run(options: &Options, state: &Path, mut record: Record) -> Result<()> {
    if record.change.is_some() {
        let config = record.config();
        let _guard = exclusion(&record, &config)?;
        if record.phase == "rollback_complete" {
            let prior = &record.change.as_ref().unwrap().prior;
            verify::installation(prior, state)?;
            finish_rollback(&mut record, state)?;
            println!("Completed interrupted rollback cleanup");
        } else if record.phase == "complete" {
            verify::installation(&record, state)?;
            cleanup(&mut record, state)?;
            println!("Completed interrupted upgrade cleanup; rerun the requested action");
        } else {
            rollback(&mut record, state).context("Rollback failed; recovery files preserved. Run ./install repair after resolving the reported failure")?;
            println!(
                "Interrupted upgrade rolled back; prior executable and maintenance copy restored. Rerun upgrade when ready"
            );
        }
        return Ok(());
    }
    if record.phase != "complete" && record.phase != "repairing" {
        ensure!(
            matches!(options.action, Action::Repair),
            "Installation interrupted; run ./install repair first"
        );
        return super::install(options, state, Some(record));
    }
    ensure!(
        record.phase != "repairing" || matches!(options.action, Action::Repair),
        "Repair interrupted; run ./install repair first"
    );
    let retained = state.join("maintenance");
    let installed = checked(&record.destination, &[&record.digest])?;
    let maintenance = checked(&retained, &[&record.digest])?;
    if matches!(options.action, Action::Repair) {
        if installed.is_some() && maintenance.is_some() && record.phase == "complete" {
            verify::installation(&record, state)?;
            println!("Installation healthy; no changes and real daemon was not restarted");
            return Ok(());
        }
        let source = std::env::current_exe()?.canonicalize()?;
        ensure!(
            files::hash(&source)? == record.digest,
            "Repair requires the recorded verified release or retained copy"
        );
        capacity(&record, state)?;
        let config = record.config();
        let _guard = exclusion(&record, &config)?;
        verify::cache(&record)?;
        record.phase = "repairing".into();
        record.save(state)?;
        publish(
            &source,
            &record.destination,
            &record.digest,
            &[&record.digest],
        )?;
        publish(&source, &retained, &record.digest, &[&record.digest])?;
        let mut verification = record.clone();
        verification.phase = "installed".into();
        verify::installation(&verification, state).context("Repair verification failed; owned copies retained. Run ./install repair after resolving the failure")?;
        record.phase = "complete".into();
        record.save(state)?;
        println!("Repaired missing owned executables; verified installation");
        return Ok(());
    }
    ensure!(
        installed.is_some() && maintenance.is_some(),
        "Owned executable missing; run ./install repair first"
    );
    let source = std::env::current_exe()?.canonicalize()?;
    let digest = files::hash(&source)?;
    let release = options
        .release
        .as_ref()
        .context("Upgrade requires ./install upgrade --release vX.Y.Z")?;
    let commit = options
        .commit
        .as_ref()
        .context("Missing verified release commit")?;
    ensure!(
        options.digest.as_ref() == Some(&digest),
        "Verified executable hash mismatch"
    );
    ensure!(
        *release == format!("v{}", env!("CARGO_PKG_VERSION")),
        "Release version mismatch"
    );
    if *release == record.release && digest == record.digest && *commit == record.commit {
        verify::installation(&record, state)?;
        println!(
            "Already running the requested healthy release; no changes and real daemon was not restarted"
        );
        return Ok(());
    }
    let old = record.clone();
    if let Some(hash) = &old.previous_digest {
        files::remove_matching(&state.join("previous"), hash)?;
    }
    ensure!(
        files::absent(&state.join("previous"))? && files::absent(&state.join("candidate"))?,
        "Unrecorded recovery file preserved"
    );
    for path in [&state.join("previous"), &state.join("candidate")] {
        ensure!(
            files::absent(&files::pending(path))?,
            "Unrecorded pending file preserved"
        );
    }
    capacity(&record, state)?;
    verify::cache(&record)?;
    record.schema_version = 2;
    record.phase = "upgrade_prepared".into();
    record.release = release.clone();
    record.commit = commit.clone();
    record.digest = digest;
    record.previous_digest = None;
    record.previous_mode = None;
    record.change = Some(Box::new(Change {
        destination_mode: fs::symlink_metadata(&record.destination)?.mode() & 0o777,
        maintenance_mode: fs::symlink_metadata(&retained)?.mode() & 0o777,
        prior: old.clone(),
    }));
    record.save(state)?;
    files::copy(&source, &state.join("candidate"), &record.digest)
        .context("Upgrade staging interrupted; installed files unchanged. Run ./install repair")?;
    files::copy(&retained, &state.join("previous"), &old.digest)?;
    record.phase = "upgrade_retained".into();
    record.save(state)?;
    let config = record.config();
    let _guard = exclusion(&record, &config)?;
    let result: Result<()> = (|| {
        verify::cache(&record)?;
        publish(
            &state.join("candidate"),
            &record.destination,
            &record.digest,
            &[&old.digest, &record.digest],
        )?;
        record.phase = "upgrade_installed".into();
        record.save(state)?;
        publish(
            &state.join("candidate"),
            &retained,
            &record.digest,
            &[&old.digest, &record.digest],
        )?;
        record.phase = "upgrade_copied".into();
        record.save(state)?;
        let mut verification = record.clone();
        verification.phase = "installed".into();
        verify::installation(&verification, state)?;
        record.phase = "complete".into();
        record.save(state)?;
        Ok(())
    })();
    if let Err(error) = result {
        match rollback(&mut record, state) {
            Ok(()) => bail!(
                "{error:#}. Upgrade rolled back; both previous executables and permissions restored"
            ),
            Err(rollback_error) => bail!(
                "{error:#}. Rollback failed: {rollback_error:#}. Recovery files preserved; run ./install repair after resolving the failure"
            ),
        }
    }
    cleanup(&mut record, state)?;
    println!("Upgraded to {}; both executables verified", record.release);
    Ok(())
}
