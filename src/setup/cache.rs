use super::{Record, files};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub device: u64,
    pub inode: u64,
}
impl Identity {
    pub fn read(path: &Path) -> Result<Self> {
        files::directory(path, false, true)?;
        let meta = fs::symlink_metadata(path)?;
        Ok(Self {
            device: meta.dev(),
            inode: meta.ino(),
        })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Creation {
    pub directory: PathBuf,
    pub identity: Identity,
}

pub fn validate(record: &Record) -> Result<()> {
    if let Some(creation) = &record.cache_creation {
        files::paths(&creation.directory)?;
        ensure!(
            record.phase == "prepared"
                && !record.cache_owned
                && record.cache_identity.is_none()
                && creation.directory.parent() == record.cache.parent()
                && creation.directory != record.cache
                && creation
                    .directory
                    .file_name()
                    .is_some_and(|name| name.to_string_lossy().starts_with(".grepglint-cache-")),
            "Invalid cache creation recovery record"
        );
    }
    Ok(())
}

pub fn stage(record: &mut Record) -> Result<()> {
    if !files::absent(&record.cache)? {
        return Ok(());
    }
    let parent = record.cache.parent().context("Missing cache parent")?;
    files::paths(parent)?;
    if files::absent(parent)? {
        files::directory(parent, true, false)?;
    }
    ensure!(
        fs::symlink_metadata(parent)?.is_dir(),
        "Cache parent is not a directory"
    );
    let temporary = tempfile::Builder::new()
        .prefix(".grepglint-cache-")
        .rand_bytes(6)
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir_in(parent)?;
    let identity = Identity::read(temporary.path())?;
    files::sync(temporary.path())?;
    files::sync(parent)?;
    // Publish only after the record has durably identified this exclusive directory.
    record.cache_owned = false;
    record.cache_identity = None;
    record.cache_creation = Some(Creation {
        directory: temporary.keep(),
        identity,
    });
    Ok(())
}

pub fn publish(record: &mut Record, state: &Path) -> Result<()> {
    let Some(creation) = record.cache_creation.clone() else {
        return Ok(());
    };
    if !files::absent(&creation.directory)? {
        ensure!(
            Identity::read(&creation.directory)? == creation.identity,
            "Staged cache directory replaced; preserved"
        );
        ensure!(
            fs::read_dir(&creation.directory)?.next().is_none(),
            "Unexpected staged cache contents preserved"
        );
        match files::rename_new(&creation.directory, &record.cache) {
            Ok(()) => files::sync(record.cache.parent().unwrap())?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                files::directory(&record.cache, false, true)?;
                fs::remove_dir(&creation.directory)?;
                files::sync(record.cache.parent().unwrap())?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    let identity = Identity::read(&record.cache)?;
    if identity == creation.identity {
        record.cache_identity = Some(identity);
        record.cache_owned = true;
    }
    record.cache_creation = None;
    record.save(state)
}

/// Reconcile a pending ownership proposal before allocating another directory.
pub fn recover_pending(record: &mut Record, state: &Path) -> Result<()> {
    let pending = files::pending(&state.join("record.json"));
    if record.phase != "prepared"
        || record.cache_creation.is_some()
        || record.cache_identity.is_some()
        || files::absent(&pending)?
    {
        return Ok(());
    }
    let bytes = files::read(&pending, files::RECORD_CAP)?;
    let mut expected = record.clone();
    expected.cache_owned = false;
    if let Ok(proposal) = serde_json::from_slice::<Record>(&bytes)
        && let Some(creation) = &proposal.cache_creation
    {
        proposal.validate(state)?;
        let mut previous = proposal.clone();
        previous.cache_creation = None;
        ensure!(
            serde_json::to_vec_pretty(&previous)? == serde_json::to_vec_pretty(&expected)?
                && serde_json::to_vec_pretty(&proposal)? == bytes,
            "Unexpected pending cache record preserved"
        );
        let directory = if files::absent(&creation.directory)? {
            &record.cache
        } else {
            &creation.directory
        };
        ensure!(
            Identity::read(directory)? == creation.identity,
            "Pending cache identity changed; preserved"
        );
        fs::rename(&pending, state.join("record.json"))?;
        files::sync(state)?;
        *record = proposal;
        return Ok(());
    }
    if partial_creation(&expected, &bytes)? {
        // Partial identity cannot authorize the directory. Only discard the canonical proposal;
        // preserve the unknown empty directory and start a fresh exclusive creation.
        fs::remove_file(&pending)?;
        files::sync(state)?;
        println!(
            "Recovered interrupted cache record; unrecorded empty staging directories remain preserved"
        );
        return Ok(());
    }
    // Existing phase-only recovery also validates pending bytes before another allocation.
    record.save(state)
}

fn partial_creation(record: &Record, input: &[u8]) -> Result<bool> {
    if !serde_json::from_slice::<serde_json::Value>(input).is_err_and(|error| error.is_eof()) {
        return Ok(false);
    }
    let mut proposal = record.clone();
    proposal.cache_creation = Some(Creation {
        directory: record
            .cache
            .parent()
            .unwrap()
            .join(".grepglint-cache-XXXXXX"),
        identity: Identity {
            device: 12345678901234567890,
            inode: 12345678901234567891,
        },
    });
    let template = serde_json::to_string_pretty(&proposal)?;
    let name = template.rfind("XXXXXX").unwrap();
    let device = name + template[name..].find("12345678901234567890").unwrap();
    let inode = device + template[device..].find("12345678901234567891").unwrap();
    enum Part<'a> {
        Fixed(&'a [u8]),
        Name,
        Number,
    }
    let bytes = template.as_bytes();
    let parts = [
        Part::Fixed(&bytes[..name]),
        Part::Name,
        Part::Fixed(&bytes[name + 6..device]),
        Part::Number,
        Part::Fixed(&bytes[device + 20..inode]),
        Part::Number,
        Part::Fixed(&bytes[inode + 20..]),
    ];
    let mut remaining = input;
    for part in parts {
        match part {
            Part::Fixed(value) => {
                if remaining.len() < value.len() {
                    return Ok(value.starts_with(remaining));
                }
                if !remaining.starts_with(value) {
                    return Ok(false);
                }
                remaining = &remaining[value.len()..];
            }
            Part::Name => {
                let count = remaining.len().min(6);
                if !remaining[..count].iter().all(u8::is_ascii_alphanumeric) {
                    return Ok(false);
                }
                if count < 6 {
                    return Ok(true);
                }
                remaining = &remaining[6..];
            }
            Part::Number => {
                let count = remaining
                    .iter()
                    .take_while(|byte| byte.is_ascii_digit())
                    .count();
                if count == 0 {
                    return Ok(remaining.is_empty());
                }
                let digits = &remaining[..count];
                if (count > 1 && digits[0] == b'0')
                    || std::str::from_utf8(digits)?.parse::<u64>().is_err()
                {
                    return Ok(false);
                }
                remaining = &remaining[count..];
                if remaining.is_empty() {
                    return Ok(true);
                }
            }
        }
    }
    Ok(remaining.is_empty())
}

pub fn save_creation(record: &Record, previous: &Record, state: &Path) -> Result<()> {
    let Err(error) = record.save(state) else {
        return Ok(());
    };
    let creation = record.cache_creation.as_ref().unwrap();
    let cleanup = (|| -> Result<()> {
        let current =
            super::load(state)?.context("Installation record disappeared; staging preserved")?;
        if serde_json::to_vec_pretty(&current)? != serde_json::to_vec_pretty(previous)? {
            // A completed rename followed by a sync failure already records the staging identity.
            return Ok(());
        }
        let pending = files::pending(&state.join("record.json"));
        if !files::absent(&pending)? {
            ensure!(
                serde_json::to_vec_pretty(record)?
                    .starts_with(&files::read(&pending, files::RECORD_CAP)?),
                "Changed pending record preserved"
            );
            fs::remove_file(&pending)?;
            files::sync(state)?;
        }
        ensure!(
            Identity::read(&creation.directory)? == creation.identity,
            "Changed staging directory preserved"
        );
        fs::remove_dir(&creation.directory)
            .context("Nonempty or inaccessible staging directory preserved")?;
        files::sync(creation.directory.parent().unwrap())
    })();
    match cleanup {
        Ok(()) => Err(error.context("Cache staging record write failed; no cache was published. Retry after resolving the write failure")),
        Err(cleanup_error) => Err(error.context(format!("Cache staging record write failed; cleanup also refused: {cleanup_error:#}. Preserve {} and retry after resolving the failure", creation.directory.display()))),
    }
}
