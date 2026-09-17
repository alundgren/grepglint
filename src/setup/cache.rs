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
