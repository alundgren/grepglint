use crate::chunks::MAX_FILE_BYTES;
use anyhow::{Context, Result, bail, ensure};
use sha1::Sha1;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::Instant,
};
use wait_timeout::ChildExt;

const MAX_OUTPUT: u64 = 16 * 1024 * 1024;
pub const MAX_FILES: usize = 20_000;

fn command(root: &Path) -> Command {
    let mut command = Command::new("git");
    command.current_dir(root).args([
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "status.renames=false",
    ]);
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_NO_LAZY_FETCH", "1")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .env("LC_ALL", "C");
    command
}

fn run(
    root: &Path,
    args: &[&str],
    input: Vec<u8>,
    deadline: Instant,
    allow_missing: bool,
) -> Result<Vec<u8>> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    ensure!(
        !remaining.is_zero(),
        "Search exceeded its 30 second work budget; use rg for this query."
    );
    let mut child = command(root)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("Cannot start Git")?;
    let mut stdin = child.stdin.take().unwrap();
    let writer = thread::spawn(move || stdin.write_all(&input));
    let stdout = child.stdout.take().unwrap();
    let out = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(MAX_OUTPUT + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let stderr = child.stderr.take().unwrap();
    let err = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take(64 * 1024)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let status = child.wait_timeout(remaining)?;
    if status.is_none() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let output = out.join().unwrap()?;
    let error = err.join().unwrap()?;
    let _ = writer.join().unwrap();
    let status = status.context("Git exceeded the query deadline; use rg for this query.")?;
    ensure!(
        output.len() <= MAX_OUTPUT as usize,
        "Git output exceeded 16 MiB; this repository is too large for the prototype."
    );
    if !status.success() && !(allow_missing && status.code() == Some(1)) {
        bail!(
            "Git {} failed: {}",
            args[0],
            String::from_utf8_lossy(&error).trim()
        );
    }
    Ok(output)
}

fn line(root: &Path, args: &[&str], deadline: Instant, allow_missing: bool) -> Result<String> {
    let mut value = String::from_utf8(run(root, args, Vec::new(), deadline, allow_missing)?)?;
    if value.ends_with('\n') {
        value.pop();
    }
    Ok(value)
}

pub fn identity(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[derive(Debug)]
pub struct Repository {
    pub root: PathBuf,
    pub common_dir: PathBuf,
    pub repo_id: String,
    pub worktree_id: String,
    pub head: Option<String>,
}

pub fn discover(cwd: &Path, deadline: Instant) -> Result<Repository> {
    ensure!(
        cwd.is_absolute(),
        "The client must send an absolute working directory."
    );
    let root = fs::canonicalize(line(
        cwd,
        &["rev-parse", "--show-toplevel"],
        deadline,
        false,
    )?)
    .context("Not inside an accessible Git worktree")?;
    let common_dir = fs::canonicalize(root.join(line(
        &root,
        &["rev-parse", "--git-common-dir"],
        deadline,
        false,
    )?))?;
    let head = line(
        &root,
        &["rev-parse", "--verify", "--quiet", "HEAD"],
        deadline,
        true,
    )?;
    Ok(Repository {
        repo_id: identity(common_dir.as_os_str().as_encoded_bytes()),
        worktree_id: identity(root.as_os_str().as_encoded_bytes()),
        root,
        common_dir,
        head: (!head.is_empty()).then_some(head),
    })
}

#[derive(Clone, Debug)]
pub struct TreeEntry {
    pub path: String,
    pub oid: Option<String>,
    pub mode: String,
}

pub fn tree_entries(repo: &Repository, deadline: Instant) -> Result<Vec<TreeEntry>> {
    let Some(head) = &repo.head else {
        return Ok(Vec::new());
    };
    let output = run(
        &repo.root,
        &["ls-tree", "-r", "-z", head],
        Vec::new(),
        deadline,
        false,
    )?;
    let mut entries = Vec::new();
    for record in output.split(|&c| c == 0).filter(|s| !s.is_empty()) {
        let record =
            std::str::from_utf8(record).context("Non-UTF-8 Git paths are not supported yet")?;
        let (metadata, path) = record.split_once('\t').context("Invalid Git tree record")?;
        let mut fields = metadata.split(' ');
        let mode = fields.next().unwrap().to_owned();
        fields.next();
        entries.push(TreeEntry {
            path: path.to_owned(),
            oid: fields.next().map(str::to_owned),
            mode,
        });
        ensure!(
            entries.len() <= MAX_FILES,
            "Repository exceeds the prototype limit of 20,000 tracked paths."
        );
    }
    Ok(entries)
}

pub fn changed_entries(
    repo: &Repository,
    before: &str,
    deadline: Instant,
) -> Result<Vec<TreeEntry>> {
    let after = repo.head.as_deref().context("HEAD has no commit")?;
    let output = run(
        &repo.root,
        &[
            "diff-tree",
            "--raw",
            "--no-abbrev",
            "-r",
            "-z",
            "--no-commit-id",
            "--no-renames",
            before,
            after,
        ],
        Vec::new(),
        deadline,
        false,
    )?;
    let text = std::str::from_utf8(&output).context("Non-UTF-8 Git paths are not supported yet")?;
    let fields: Vec<_> = text.split('\0').filter(|s| !s.is_empty()).collect();
    ensure!(
        fields.len() <= MAX_FILES * 2 && fields.len() % 2 == 0,
        "Too many or invalid Git changes"
    );
    fields
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            let metadata: Vec<_> = pair[0].split(' ').collect();
            ensure!(metadata.len() == 5, "Invalid Git diff record");
            Ok(TreeEntry {
                path: pair[1].to_owned(),
                oid: (metadata[4] != "D").then(|| metadata[3].to_owned()),
                mode: metadata[1].to_owned(),
            })
        })
        .collect()
}

pub fn blob_sizes(
    repo: &Repository,
    oids: &[String],
    deadline: Instant,
) -> Result<BTreeMap<String, usize>> {
    let input = format!("{}\n", oids.join("\n")).into_bytes();
    let output = run(
        &repo.root,
        &["cat-file", "--batch-check"],
        input,
        deadline,
        false,
    )?;
    let mut sizes = BTreeMap::new();
    for record in std::str::from_utf8(&output)?.lines() {
        let fields: Vec<_> = record.split(' ').collect();
        ensure!(
            fields.len() == 3 && fields[1] == "blob",
            "Git blob is unavailable locally: {record}"
        );
        sizes.insert(fields[0].to_owned(), fields[2].parse()?);
    }
    Ok(sizes)
}

pub fn read_blobs(
    repo: &Repository,
    oids: &[String],
    deadline: Instant,
) -> Result<BTreeMap<String, Vec<u8>>> {
    let input = format!("{}\n", oids.join("\n")).into_bytes();
    let output = run(&repo.root, &["cat-file", "--batch"], input, deadline, false)?;
    let mut remaining = output.as_slice();
    let mut blobs = BTreeMap::new();
    while !remaining.is_empty() {
        let end = remaining
            .iter()
            .position(|&b| b == b'\n')
            .context("Invalid blob header")?;
        let header: Vec<_> = std::str::from_utf8(&remaining[..end])?.split(' ').collect();
        ensure!(
            header.len() == 3 && header[1] == "blob",
            "Invalid blob response"
        );
        let size: usize = header[2].parse()?;
        ensure!(
            size <= MAX_FILE_BYTES && remaining.len() > end + 1 + size,
            "Blob exceeded its declared limit"
        );
        blobs.insert(
            header[0].to_owned(),
            remaining[end + 1..end + 1 + size].to_vec(),
        );
        remaining = &remaining[end + size + 2..];
    }
    Ok(blobs)
}

pub fn dirty_files(repo: &Repository, deadline: Instant) -> Result<BTreeMap<String, String>> {
    let output = run(
        &repo.root,
        &[
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ],
        Vec::new(),
        deadline,
        false,
    )?;
    let mut dirty = BTreeMap::new();
    for record in output.split(|&b| b == 0).filter(|s| !s.is_empty()) {
        ensure!(record.len() >= 4, "Invalid Git status record");
        let path = std::str::from_utf8(&record[3..])
            .context("Non-UTF-8 Git paths are not supported yet")?
            .to_owned();
        let status = std::str::from_utf8(&record[..2])?;
        let fingerprint = match fs::symlink_metadata(repo.root.join(&path)) {
            Ok(m) => format!(
                "{status}:{}:{}:{}:{}:{}:{}:{}:{}",
                m.dev(),
                m.ino(),
                m.mode(),
                m.size(),
                m.mtime(),
                m.mtime_nsec(),
                m.ctime(),
                m.ctime_nsec()
            ),
            Err(e)
                if e.kind() == std::io::ErrorKind::NotFound
                    || e.raw_os_error() == Some(libc::ENOTDIR) =>
            {
                format!("{status}:missing")
            }
            Err(e) => return Err(e.into()),
        };
        dirty.insert(path, fingerprint);
        ensure!(
            dirty.len() <= MAX_FILES,
            "More than 20,000 changed or untracked paths; add generated files to .gitignore."
        );
    }
    Ok(dirty)
}

pub fn read_worktree_file(repo: &Repository, path: &str) -> Result<Option<Vec<u8>>> {
    let absolute = repo.root.join(path);
    let metadata = match fs::symlink_metadata(&absolute) {
        Ok(m) => m,
        Err(e)
            if e.kind() == std::io::ErrorKind::NotFound
                || e.raw_os_error() == Some(libc::ENOTDIR) =>
        {
            return Ok(None);
        }
        Err(e) => return Err(e.into()),
    };
    if !metadata.is_file() || metadata.len() > MAX_FILE_BYTES as u64 {
        return Ok(None);
    }
    if !fs::canonicalize(&absolute)?.starts_with(&repo.root) {
        return Ok(None);
    }
    let mut bytes = Vec::new();
    fs::File::open(absolute)?
        .take(MAX_FILE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    Ok((bytes.len() <= MAX_FILE_BYTES).then_some(bytes))
}

pub fn blob_identity(bytes: &[u8], length: usize) -> String {
    let header = format!("blob {}\0", bytes.len());
    if length == 64 {
        let mut digest = Sha256::new();
        digest.update(header);
        digest.update(bytes);
        format!("{:x}", digest.finalize())
    } else {
        let mut digest = Sha1::new();
        digest.update(header);
        digest.update(bytes);
        format!("{:x}", digest.finalize())
    }
}
