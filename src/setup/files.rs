use anyhow::{Context, Result, ensure};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::{Component, Path};

pub const BINARY_CAP: u64 = 128 * 1024 * 1024;
pub const RECORD_CAP: u64 = 64 * 1024;

pub fn paths(path: &Path) -> Result<()> {
    ensure!(
        path.is_absolute(),
        "Use an absolute path: {}",
        path.display()
    );
    ensure!(
        path.components()
            .all(|p| !matches!(p, Component::ParentDir | Component::CurDir)),
        "Paths cannot contain . or .."
    );
    let mut current = std::path::PathBuf::new();
    for part in path.components() {
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(meta) => {
                ensure!(
                    !meta.file_type().is_symlink(),
                    "Symlink path refused: {}",
                    current.display()
                );
                if meta.is_dir() {
                    ensure!(
                        (meta.uid() == 0 || meta.uid() == unsafe { libc::geteuid() })
                            && (meta.mode() & 0o022 == 0 || meta.mode() & 0o1000 != 0),
                        "Unsafe ancestor directory: {}",
                        current.display()
                    );
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

pub fn directory(path: &Path, create: bool, private: bool) -> Result<()> {
    paths(path)?;
    if create {
        let created: Vec<_> = path.ancestors().take_while(|parent| matches!(fs::symlink_metadata(parent),Err(error) if error.kind()==std::io::ErrorKind::NotFound)).collect();
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)?;
        for directory in created {
            sync(directory)?;
            if let Some(parent) = directory.parent() {
                sync(parent)?;
            }
        }
    }
    let meta = fs::symlink_metadata(path)?;
    ensure!(
        meta.is_dir() && meta.uid() == unsafe { libc::geteuid() },
        "Directory must be owned by this account: {}",
        path.display()
    );
    ensure!(
        meta.mode() & if private { 0o077 } else { 0o022 } == 0,
        "Directory permissions are unsafe: {}",
        path.display()
    );
    Ok(())
}

pub fn open(path: &Path, cap: u64) -> Result<File> {
    paths(path)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let meta = file.metadata()?;
    ensure!(
        meta.is_file()
            && meta.uid() == unsafe { libc::geteuid() }
            && meta.nlink() == 1
            && meta.len() <= cap
            && meta.mode() & 0o022 == 0,
        "Unsafe, linked, or oversized file: {}",
        path.display()
    );
    Ok(file)
}

pub fn read(path: &Path, cap: u64) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    open(path, cap)?.take(cap + 1).read_to_end(&mut data)?;
    ensure!(data.len() as u64 <= cap, "File grew beyond limit");
    Ok(data)
}

pub fn hash(path: &Path) -> Result<String> {
    let mut file = open(path, BINARY_CAP)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut size = 0;
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size += count as u64;
        ensure!(size <= BINARY_CAP, "Executable exceeds 128 MiB");
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub fn sync(path: &Path) -> Result<()> {
    File::open(path)?
        .sync_all()
        .context("Cannot durably sync directory")
}

pub fn pending(path: &Path) -> std::path::PathBuf {
    let mut name = path.file_name().unwrap().to_os_string();
    name.push(".grepglint-pending");
    path.with_file_name(name)
}

fn pending_file(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?)
}

pub fn save(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().context("Missing parent directory")?;
    let stage = pending(path);
    if !absent(&stage)? {
        let partial = read(&stage, RECORD_CAP)?;
        let mut value: serde_json::Value = serde_json::from_slice(bytes)?;
        let mut recognized = false;
        for phase in ["prepared", "retained", "installed", "complete"] {
            value["phase"] = phase.into();
            // Preserve struct field order, which is stable in the durable format.
            let candidate: super::Record = serde_json::from_value(value.clone())?;
            if serde_json::to_vec_pretty(&candidate)?.starts_with(&partial) {
                recognized = true;
            }
        }
        ensure!(recognized, "Unexpected pending record preserved");
        fs::remove_file(&stage)?;
    }
    let mut file = pending_file(&stage)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    fs::rename(&stage, path)?;
    sync(parent)
}

pub fn copy(source: &Path, destination: &Path, digest: &str) -> Result<()> {
    replace(source, destination, digest, None)
}

pub fn replace(source: &Path, destination: &Path, digest: &str, prior: Option<&str>) -> Result<()> {
    let parent = destination
        .parent()
        .context("Missing destination directory")?;
    let stage = pending(destination);
    if !absent(&stage)? {
        let mut partial = open(&stage, BINARY_CAP)?;
        let mut original = open(source, BINARY_CAP)?;
        let mut left = [0u8; 65536];
        let mut right = [0u8; 65536];
        loop {
            let count = partial.read(&mut left)?;
            if count == 0 {
                break;
            }
            original.read_exact(&mut right[..count])?;
            ensure!(
                left[..count] == right[..count],
                "Unexpected pending executable preserved"
            );
        }
        fs::remove_file(&stage)?;
    }
    let mut input = open(source, BINARY_CAP)?;
    let mut output = pending_file(&stage)?;
    let size = std::io::copy(
        &mut std::io::Read::by_ref(&mut input).take(BINARY_CAP + 1),
        &mut output,
    )?;
    ensure!(
        size <= BINARY_CAP && hash(&stage)? == digest,
        "Executable changed while copying"
    );
    use std::os::unix::fs::PermissionsExt;
    output.set_permissions(fs::Permissions::from_mode(0o700))?;
    output.sync_all()?;
    if let Some(prior) = prior {
        ensure!(
            hash(destination)? == prior,
            "Destination changed before replacement; preserved"
        );
        fs::rename(&stage, destination)?;
    } else {
        let from = std::ffi::CString::new(stage.as_os_str().as_encoded_bytes())?;
        let to = std::ffi::CString::new(destination.as_os_str().as_encoded_bytes())?;
        #[cfg(target_os = "linux")]
        let result = unsafe {
            libc::renameat2(
                libc::AT_FDCWD,
                from.as_ptr(),
                libc::AT_FDCWD,
                to.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        #[cfg(target_os = "macos")]
        let result = unsafe { libc::renamex_np(from.as_ptr(), to.as_ptr(), libc::RENAME_EXCL) };
        ensure!(
            result == 0,
            "Cannot publish executable without replacing an unexpected file: {}",
            std::io::Error::last_os_error()
        );
    }
    sync(parent)
}

pub fn absent(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(false),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(true),
        Err(error) => Err(error.into()),
    }
}

pub fn remove_matching(path: &Path, digest: &str) -> Result<()> {
    if !absent(path)? {
        ensure!(
            hash(path)? == digest,
            "Changed file preserved: {}",
            path.display()
        );
        fs::remove_file(path)?;
        sync(path.parent().unwrap())?;
    }
    Ok(())
}

pub fn reserve(path: &Path, required: u64) -> Result<()> {
    ensure!(
        fs2::available_space(path)? >= required,
        "Insufficient free disk space; need {required} bytes before installation"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn disk_reserve_refuses_before_copy() {
        let root = tempfile::Builder::new()
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap();
        assert!(reserve(root.path(), u64::MAX).is_err());
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }

    #[test]
    fn changed_pending_and_linked_files_are_preserved() {
        let root = tempfile::Builder::new()
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap();
        let root = root.path().canonicalize().unwrap();
        let source = root.join("source");
        fs::write(&source, b"verified").unwrap();
        fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
        let destination = root.join("destination");
        let stage = pending(&destination);
        fs::write(&stage, b"unrelated").unwrap();
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(copy(&source, &destination, &hash(&source).unwrap()).is_err());
        assert_eq!(fs::read(stage).unwrap(), b"unrelated");
        fs::hard_link(&source, root.join("link")).unwrap();
        assert!(hash(&source).is_err());
    }
}
