use super::cache::Identity;
use super::{Action, Options, Record, files};
use crate::maintenance::Maintenance;
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use std::fs;
use std::io::{BufRead, IsTerminal, Read, Write};
use std::path::Path;

fn confirm(accepted: bool, message: &str, flag: &str) -> Result<bool> {
    println!("{message}");
    if accepted {
        return Ok(true);
    }
    ensure!(
        std::io::stdin().is_terminal(),
        "Explicit {flag} is required; nothing removed"
    );
    print!("Continue? [y/N] ");
    std::io::stdout().flush()?;
    let mut answer = String::new();
    std::io::stdin().lock().take(16).read_line(&mut answer)?;
    Ok(matches!(answer.trim(), "y" | "Y" | "yes"))
}

fn identity(record: &Record) -> Result<()> {
    let expected = record.cache_identity.as_ref().context("Cache ownership identity was not recorded. Cache preserved; automatic adoption is unavailable. Inspect the exact cache path manually; ownership state remains for recovery")?;
    ensure!(
        Identity::read(&record.cache)? == *expected,
        "Cache directory was replaced; preserved {}",
        record.cache.display()
    );
    Ok(())
}

fn state_entries(state: &Path) -> Result<()> {
    // Enumeration is bounded even if an unexpected directory contains many entries.
    for entry in fs::read_dir(state)?.take(16) {
        let entry = entry?;
        ensure!(
            [
                "record.json",
                "record.json.grepglint-pending",
                "operation.lock",
                "maintenance",
                "previous",
                "candidate",
                "maintenance.grepglint-pending",
                "previous.grepglint-pending",
                "candidate.grepglint-pending"
            ]
            .iter()
            .any(|name| entry.file_name() == *name),
            "Unexpected installer entry preserved: {}. Inspect this exact file and move it only if you own it; retry afterward",
            entry.path().display()
        );
    }
    Ok(())
}

fn cleanup_files(record: &Record, state: &Path) -> Result<()> {
    let maintenance = state.join("maintenance");
    for target in [
        &record.destination,
        &maintenance,
        &state.join("previous"),
        &state.join("candidate"),
    ] {
        files::discard_pending(target, &[&maintenance])?;
    }
    if let Some(digest) = &record.previous_digest {
        files::remove_matching(&state.join("previous"), digest)?;
    }
    ensure!(
        files::absent(&state.join("previous"))? && files::absent(&state.join("candidate"))?,
        "Unrecorded recovery file preserved; inspect the exact file before retrying"
    );
    Ok(())
}

fn purge_cache(record: &Record) -> Result<()> {
    identity(record)?;
    let allowed = [
        "index.sqlite",
        "index.sqlite-journal",
        "maintenance.lock",
        "daemon.lock",
    ];
    for entry in fs::read_dir(&record.cache)?.take(8) {
        let entry = entry?;
        ensure!(
            allowed.iter().any(|name| entry.file_name() == *name),
            "Unexpected cache entry preserved: {}. Inspect this exact file and move it only if you own it; retry afterward",
            entry.path().display()
        );
        let _ = files::open(&entry.path(), 2 * record.database_bytes)?;
    }
    // Lock files remain on their stable inodes so waiting clients cannot bypass exclusion.
    for name in ["index.sqlite-journal", "index.sqlite"] {
        identity(record)?;
        let path = record.cache.join(name);
        if !files::absent(&path)? {
            let _ = files::open(&path, record.database_bytes)?;
            fs::remove_file(&path).with_context(|| {
                format!("Cannot remove {}; ownership state retained", path.display())
            })?;
            files::sync(&record.cache)?;
        }
    }
    Ok(())
}

pub fn run(options: &Options, state: &Path, record: Option<Record>) -> Result<()> {
    let Some(mut record) = record else {
        ensure!(
            fs::read_dir(state)?
                .all(|entry| entry.is_ok_and(|entry| entry.file_name() == "operation.lock")),
            "Unrecorded installer files preserved; inspect the state directory"
        );
        println!("No managed installation remains; nothing to remove");
        return Ok(());
    };
    let purge = matches!(options.action, Action::Purge);
    let confirmed = if purge {
        confirm(
            options.purge_cache,
            &format!(
                "Purge cached source contents at {} and recovery files at {}. Installed executable {} is retained if present.",
                record.cache.display(),
                state.display(),
                record.destination.display()
            ),
            "--purge-cache",
        )?
    } else {
        confirm(
            options.yes,
            &format!(
                "Uninstall {}. Cached source contents at {} and verified offline maintenance at {} will remain.",
                record.destination.display(),
                record.cache.display(),
                state.join("maintenance").display()
            ),
            "--yes",
        )?
    };
    if !confirmed {
        println!("Cancelled; nothing removed");
        return Ok(());
    }
    if record.change.is_some() {
        super::change::run(options, state, record)?;
        record = super::load(state)?.context("Recovery record missing")?;
    }
    ensure!(
        [
            "complete",
            "uninstalling",
            "uninstalled",
            "purging",
            "purge_finalizing"
        ]
        .contains(&record.phase.as_str()),
        "Installation or repair interrupted; use the trusted entrypoint to finish repair before removal. Nothing removed"
    );
    ensure!(
        record.phase != "purge_finalizing" || purge,
        "Purge finalization pending; rerun ./install purge --purge-cache"
    );
    state_entries(state)?;
    let maintenance = state.join("maintenance");
    if record.phase != "purge_finalizing" || !files::absent(&maintenance)? {
        ensure!(
            files::hash(&maintenance)? == record.digest,
            "Maintenance copy changed; preserved"
        );
    }
    if !files::absent(&record.destination)? {
        ensure!(
            files::hash(&record.destination)? == record.digest,
            "Modified installed executable preserved: {}",
            record.destination.display()
        );
    }
    if record.cache_identity.is_some() {
        identity(&record)?;
    }
    files::directory(&record.cache, false, true)?;
    let config = record.config();
    let mut guard = Maintenance::acquire(&config)?;
    if let Some(health) = guard.status()? {
        ensure!(
            health.executable_sha256 == record.digest
                && health.cache_directory == record.cache.to_string_lossy(),
            "Unrecognized real daemon; nothing stopped. Pause searches and retry after idle exit; use rg meanwhile"
        );
        guard.shutdown(&health.instance)?;
    }
    let daemon_lock = crate::maintenance::lock_file(&config, "daemon.lock")?;
    daemon_lock
        .try_lock_exclusive()
        .context("Daemon still holds its lock; cleanup refused")?;
    if record.cache_identity.is_some() {
        identity(&record)?;
    }
    if !purge {
        record.phase = "uninstalling".into();
        record.save(state)?;
        files::remove_matching(&record.destination, &record.digest)?;
        cleanup_files(&record, state)?;
        record.phase = "uninstalled".into();
        record.save(state)?;
        println!(
            "Uninstalled. Cached source contents and verified maintenance copy retained. Erase recorded cache with ./install purge --purge-cache"
        );
        return Ok(());
    }
    identity(&record)?;
    if record.phase != "purge_finalizing" {
        record.phase = "purging".into();
        record.save(state)?;
    }
    purge_cache(&record)?;
    cleanup_files(&record, state)?;
    if !files::absent(&record.destination)? {
        record.phase = "complete".into();
        record.save(state)?;
        println!(
            "Purged cached source contents. Installed executable, verified maintenance copy and minimal ownership record retained to manage the installation. Empty coordination locks remain."
        );
        return Ok(());
    }
    record.phase = "purge_finalizing".into();
    record.save(state)?;
    files::remove_matching(&maintenance, &record.digest)?;
    // The trusted entrypoint can finish this phase without executing a missing copy.
    fs::remove_file(state.join("record.json"))?;
    files::sync(state)?;
    println!(
        "Purged recorded cached source contents and installer files. Empty cache/state directories and stable coordination locks remain."
    );
    Ok(())
}
