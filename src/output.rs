//! Account-local retained output. Capture never runs in the search daemon.
use crate::daemon::Config;
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub const THRESHOLD: usize = 4096;
pub const CHUNK: usize = 3584;
pub const PAGE: usize = 4096;
const BUFFER: usize = CHUNK * 8;
pub const MAX_OUTPUT: u64 = 8 * 1024 * 1024;
pub const TOTAL: u64 = 32 * 1024 * 1024;
pub const ENTRIES: u64 = 64;
pub const TTL: u64 = 3600;
const DB_LIMIT: u64 = 40 * 1024 * 1024;
const OVERALL: Duration = Duration::from_secs(120);
const IDLE: Duration = Duration::from_secs(10);
const LOCK_WAIT: Duration = Duration::from_secs(2);
const MAX_MANIFEST: u64 = 4096;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    name: String,
    device: u64,
    inode: u64,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Ownership {
    version: u32,
    files: Vec<Identity>,
}

pub struct Store {
    db: Connection,
    directory: PathBuf,
    reserve: u64,
    _maintenance: File,
}

fn now() -> Result<u64> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}
fn private(path: &Path, create: bool) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(create)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let m = file.metadata()?;
    ensure!(
        m.is_file()
            && m.uid() == unsafe { libc::geteuid() }
            && m.mode() & 0o077 == 0
            && m.nlink() == 1,
        "Output storage contains an unsafe file; preserve it and inspect the cache."
    );
    Ok(file)
}
fn lock(file: &File) -> Result<()> {
    lock_checked(file, || Ok(()))
}
fn lock_checked(file: &File, check: impl Fn() -> Result<()>) -> Result<()> {
    let start = Instant::now();
    loop {
        check()?;
        match file.try_lock_exclusive() {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                ensure!(
                    start.elapsed() < LOCK_WAIT,
                    "Output storage is busy; retry within a few seconds."
                );
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(e) => return Err(e.into()),
        }
    }
}
fn directory(path: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    let m = fs::symlink_metadata(path)?;
    ensure!(
        m.is_dir() && m.uid() == unsafe { libc::geteuid() } && m.mode() & 0o077 == 0,
        "Output cache must be a private directory owned by this account."
    );
    Ok(())
}

const INIT_MAGIC: &str = "grepglint-output-init-v1\n";

// The locked intent is complete before any staging file is created. Recovery
// finishes only that unpredictable, recorded staging directory; it deletes nothing.
fn initialize(cache: &Path, gate: &mut File) -> Result<PathBuf> {
    let final_path = cache.join("output-v1");
    if fs::symlink_metadata(&final_path).is_ok() {
        directory(&final_path)?;
        return Ok(final_path);
    }
    let mut state = Vec::new();
    (&mut *gate).take(128).read_to_end(&mut state)?;
    ensure!(
        gate.metadata()?.len() < 128,
        "Invalid output initialization record; preserve it for inspection."
    );
    let state = std::str::from_utf8(&state).context("Invalid output initialization record")?;
    let stage_id = if let Some(id) = state
        .strip_prefix(INIT_MAGIC)
        .and_then(|s| s.strip_suffix('\n'))
    {
        validate_handle(id)?;
        id.to_owned()
    } else {
        ensure!(
            INIT_MAGIC.starts_with(state)
                || state.strip_prefix(INIT_MAGIC).is_some_and(|s| s.len() <= 32
                    && s.bytes()
                        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())),
            "Unexpected output initialization record; preserve it for inspection."
        );
        let id = random_handle()?;
        gate.set_len(0)?;
        gate.seek(SeekFrom::Start(0))?;
        gate.write_all(format!("{INIT_MAGIC}{id}\n").as_bytes())?;
        gate.sync_all()?;
        id
    };
    let stage = cache.join(format!("output-init-{stage_id}"));
    directory(&stage)?;
    let mut identities = Vec::new();
    for name in [
        "output.sqlite",
        "output.sqlite-journal",
        "capture-0.lock",
        "capture-1.lock",
    ] {
        let file = private(&stage.join(name), true)?;
        let metadata = file.metadata()?;
        ensure!(
            metadata.len() == 0,
            "Unexpected contents in output initialization state; preserve it for inspection."
        );
        identities.push(Identity {
            name: name.into(),
            device: metadata.dev(),
            inode: metadata.ino(),
        });
    }
    let bytes = serde_json::to_vec(&Ownership {
        version: 1,
        files: identities,
    })?;
    let mut owner = private(&stage.join("ownership.json"), true)?;
    let mut partial = Vec::new();
    (&mut owner)
        .take(MAX_MANIFEST + 1)
        .read_to_end(&mut partial)?;
    ensure!(
        bytes.starts_with(&partial),
        "Output initialization ownership was changed; preserve it for inspection."
    );
    owner.seek(SeekFrom::Start(partial.len() as u64))?;
    owner.write_all(&bytes[partial.len()..])?;
    owner.sync_all()?;
    fs::rename(&stage, &final_path)?;
    Ok(final_path)
}

impl Store {
    pub fn open(config: &Config) -> Result<Self> {
        Self::open_inner(config, None)
    }

    pub(crate) fn open_for_search(
        config: &Config,
        budget: crate::temporary_rank::Budget,
    ) -> Result<Self> {
        let result = Self::open_inner(config, Some(budget));
        if result.is_err() {
            budget.check()?;
        }
        result
    }

    fn open_inner(config: &Config, budget: Option<crate::temporary_rank::Budget>) -> Result<Self> {
        let check = || match budget {
            Some(budget) => budget.check(),
            None => Ok(()),
        };
        check()?;
        directory(&config.directory)?;
        let maintenance = crate::maintenance::shared(config)?;
        let mut gate = private(&config.directory.join("output-gate"), true)?;
        lock_checked(&gate, check)?;
        let directory_path = initialize(&config.directory, &mut gate)?;
        check()?;
        let owner_path = directory_path.join("ownership.json");
        let names = [
            "output.sqlite",
            "output.sqlite-journal",
            "capture-0.lock",
            "capture-1.lock",
        ];
        let owner = private(&owner_path, false)?;
        ensure!(
            owner.metadata()?.len() <= MAX_MANIFEST,
            "Output ownership record is too large."
        );
        let mut owner_bytes = Vec::new();
        owner.take(MAX_MANIFEST + 1).read_to_end(&mut owner_bytes)?;
        ensure!(
            owner_bytes.len() <= MAX_MANIFEST as usize,
            "Output ownership record is too large."
        );
        let record: Ownership = serde_json::from_slice(&owner_bytes)?;
        ensure!(
            record.version == 1 && record.files.len() == names.len(),
            "Unsupported output ownership record."
        );
        for (identity, name) in record.files.iter().zip(names) {
            check()?;
            ensure!(identity.name == name, "Invalid output ownership record.");
            let f = private(&directory_path.join(name), false)?;
            let m = f.metadata()?;
            ensure!(
                m.dev() == identity.device && m.ino() == identity.inode,
                "Output storage was replaced; preserve it and inspect the cache."
            );
        }
        for suffix in ["-wal", "-shm"] {
            let path = directory_path.join(format!("output.sqlite{suffix}"));
            if fs::symlink_metadata(&path).is_ok() {
                bail!("Unexpected SQLite companion file; preserve it and inspect the cache.");
            }
        }
        ensure!(
            fs::metadata(directory_path.join("output.sqlite"))?.len() <= DB_LIMIT
                && fs::metadata(directory_path.join("output.sqlite-journal"))?.len()
                    <= DB_LIMIT + 1024 * 1024,
            "Output database exceeds its disk budget."
        );
        let db = Connection::open_with_flags(
            fs::canonicalize(&directory_path)?.join("output.sqlite"),
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
                | rusqlite::OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        if let Some(budget) = budget {
            db.progress_handler(1000, Some(move || budget.check().is_err()))?;
        }
        check()?;
        db.busy_timeout(LOCK_WAIT)?;
        db.pragma_update(None, "journal_mode", "PERSIST")?;
        db.pragma_update(None, "journal_size_limit", 0)?;
        let page_size: u32 = db.pragma_query_value(None, "page_size", |r| r.get(0))?;
        ensure!(page_size == 4096, "Unsupported output database page size.");
        db.pragma_update(None, "temp_store", "MEMORY")?;
        db.pragma_update(None, "cache_size", -256)?;
        db.pragma_update(None, "mmap_size", 0)?;
        db.pragma_update(None, "hard_heap_limit", 64 * 1024 * 1024)?;
        let version: u32 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
        ensure!(version <= 1, "Unsupported output database version.");
        db.pragma_update(None, "max_page_count", (DB_LIMIT / 4096) as i64)?;
        db.pragma_update(None, "secure_delete", "ON")?;
        db.pragma_update(None, "foreign_keys", "ON")?;
        check()?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS outputs (
          handle TEXT PRIMARY KEY, created INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
          lines INTEGER NOT NULL DEFAULT 0, digest TEXT, slot INTEGER, committed INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS chunks (handle TEXT NOT NULL REFERENCES outputs(handle) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL, content BLOB NOT NULL, PRIMARY KEY(handle,ordinal));")?;
        db.pragma_update(None, "user_version", 1)?;
        let count: u32 = db.query_row("SELECT count(*) FROM outputs", [], |r| r.get(0))?;
        ensure!(
            count <= ENTRIES as u32,
            "Output record limit exceeded; preserve corrupt storage for inspection."
        );
        let store = Self {
            db,
            directory: directory_path,
            reserve: config.max_bytes * 2 + 64 * 1024 * 1024,
            _maintenance: maintenance,
        };
        check()?;
        store.cleanup_checked(check)?;
        check()?;
        Ok(store)
    }

    fn space(&self) -> Result<()> {
        ensure!(
            fs2::available_space(&self.directory)? >= self.reserve + 2 * DB_LIMIT + 1024 * 1024,
            "Insufficient free disk space for output and repository reserves; consumed input may require rerunning the producer."
        );
        Ok(())
    }

    fn deletion_space(&self) -> Result<()> {
        let journal_allowance =
            fs::metadata(self.directory.join("output.sqlite"))?.len() + 1024 * 1024;
        ensure!(
            fs2::available_space(&self.directory)? >= journal_allowance,
            "Output cleanup needs room for its rollback journal; free some disk space and retry output purge. Retained state is preserved."
        );
        Ok(())
    }

    fn cleanup(&self) -> Result<()> {
        self.cleanup_checked(|| Ok(()))
    }
    fn cleanup_checked(&self, check: impl Fn() -> Result<()>) -> Result<()> {
        check()?;
        self.deletion_space()?;
        self.db.execute(
            "DELETE FROM outputs WHERE committed=1 AND created <= ?1",
            [now()?.saturating_sub(TTL) as i64],
        )?;
        for slot in 0..2 {
            check()?;
            let f = private(&self.directory.join(format!("capture-{slot}.lock")), false)?;
            match f.try_lock_exclusive() {
                Ok(()) => {
                    self.db
                        .execute("DELETE FROM outputs WHERE committed=0 AND slot=?1", [slot])?;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => (),
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }

    fn begin(&mut self) -> Result<Capture> {
        self.space()?;
        let mut selected = None;
        for slot in 0..2 {
            let f = private(&self.directory.join(format!("capture-{slot}.lock")), false)?;
            match f.try_lock_exclusive() {
                Ok(()) => {
                    selected = Some((slot, f));
                    break;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => (),
                Err(e) => return Err(e.into()),
            }
        }
        let (slot, lease) = selected
            .context("Two output captures are already active; retry after one finishes.")?;
        let handle = random_handle()?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute("DELETE FROM outputs WHERE committed=0 AND slot=?1", [slot])?;
        loop {
            let (count, used): (u32,u32) = tx.query_row("SELECT count(*), coalesce(sum(CASE WHEN committed=1 THEN bytes ELSE ?1 END),0) FROM outputs", [MAX_OUTPUT as i64], |r| Ok((r.get(0)?,r.get(1)?)))?;
            if count < ENTRIES as u32 && used + MAX_OUTPUT as u32 <= TOTAL as u32 {
                break;
            }
            let removed = tx.execute("DELETE FROM outputs WHERE handle=(SELECT handle FROM outputs WHERE committed=1 ORDER BY created, rowid LIMIT 1)", [])?;
            ensure!(removed != 0, "Output reservation limit reached.");
        }
        tx.execute(
            "INSERT INTO outputs(handle,created,slot) VALUES(?1,?2,?3)",
            params![handle, now()? as i64, slot],
        )?;
        tx.commit()?;
        Ok(Capture {
            handle,
            _lease: lease,
            count: 0,
            bytes: 0,
            lines: 0,
            last: None,
            digest: Sha256::new(),
        })
    }

    fn append(&self, capture: &mut Capture, bytes: &[u8]) -> Result<()> {
        ensure!(
            capture.bytes + bytes.len() as u64 <= MAX_OUTPUT,
            "Output exceeds 8 MiB; consumed input may require rerunning the producer."
        );
        self.space()?;
        ensure!(
            bytes.len() <= BUFFER,
            "Output transfer exceeds its buffer budget."
        );
        let tx = self.db.unchecked_transaction()?;
        for (i, chunk) in bytes.chunks(CHUNK).enumerate() {
            tx.execute(
                "INSERT INTO chunks VALUES(?1,?2,?3)",
                params![capture.handle, (capture.count + i as u64) as i64, chunk],
            )?;
        }
        tx.commit()?;
        capture.count += bytes.len().div_ceil(CHUNK) as u64;
        capture.bytes += bytes.len() as u64;
        capture.lines += bytes.iter().filter(|&&b| b == b'\n').count() as u64;
        capture.last = bytes.last().copied().or(capture.last);
        capture.digest.update(bytes);
        Ok(())
    }

    fn abort(&self, handle: &str) -> Result<()> {
        self.db.execute(
            "DELETE FROM outputs WHERE handle=?1 AND committed=0",
            [handle],
        )?;
        Ok(())
    }

    fn commit(&self, capture: &Capture) -> Result<()> {
        self.space()?;
        self.db.execute("UPDATE outputs SET committed=1, bytes=?2, lines=?3, digest=?4, slot=NULL WHERE handle=?1 AND committed=0",
            params![capture.handle,capture.bytes as i64,(capture.lines + u64::from(capture.last.is_some_and(|b| b != b'\n'))) as i64,format!("{:x}",capture.digest.clone().finalize())])?;
        Ok(())
    }

    pub fn page(&mut self, handle: &str, cursor: Option<&str>) -> Result<Page> {
        validate_handle(handle)?;
        self.cleanup()?;
        let offset = decode_cursor(handle, cursor)?;
        let tx = self.db.transaction()?;
        let mut selected = Vec::with_capacity(PAGE + 4);
        let Retained {
            bytes,
            lines,
            created,
        } = read_stream(&tx, handle, |total, b| {
            let start = offset.saturating_sub(total).min(b.len() as u64) as usize;
            if total + b.len() as u64 > offset && selected.len() < PAGE + 4 {
                let count = (b.len() - start).min(PAGE + 4 - selected.len());
                selected.extend_from_slice(&b[start..start + count]);
            }
            Ok(())
        })?;
        ensure!(offset < bytes, "Output cursor is out of range.");
        let end = match std::str::from_utf8(&selected) {
            Ok(_) => selected.len(),
            Err(e) if e.error_len().is_none() => e.valid_up_to(),
            Err(_) => bail!("Cursor is not on a UTF-8 boundary or output is corrupt."),
        };
        selected.truncate(end);
        let text = std::str::from_utf8(&selected)?;
        let mut count = text.len().min(PAGE);
        while !text.is_char_boundary(count) {
            count -= 1;
        }
        if let Some(newline) = text[..count].rfind('\n') {
            count = newline + 1;
        }
        ensure!(count > 0, "Output cursor makes no progress.");
        let content = text[..count].to_owned();
        let next = offset + count as u64;
        ensure!(created + TTL > now()?, "Output expired during retrieval.");
        Ok(Page {
            handle: handle.into(),
            content,
            bytes,
            lines,
            next_cursor: (next < bytes).then(|| encode_cursor(handle, next)),
            end_of_output: next == bytes,
        })
    }

    pub fn search(&mut self, handle: &str, query: &str, limit: usize) -> Result<Search> {
        let budget = crate::temporary_rank::Budget::new(1);
        self.search_with_budget(handle, query, limit, budget)
    }

    pub(crate) fn search_with_budget(
        &mut self,
        handle: &str,
        query: &str,
        limit: usize,
        budget: crate::temporary_rank::Budget,
    ) -> Result<Search> {
        crate::temporary_rank::validate(query, limit)?;
        validate_handle(handle)?;
        budget.check()?;
        self.db
            .progress_handler(1000, Some(move || budget.check().is_err()))?;
        let result = (|| {
            let tx = self.db.transaction()?;
            let mut text = Vec::new();
            text.try_reserve_exact(MAX_OUTPUT as usize)
                .context("Cannot allocate the bounded output search buffer; use output page")?;
            let retained = read_stream(&tx, handle, |_, bytes| {
                budget.check()?;
                text.extend_from_slice(bytes);
                Ok(())
            })?;
            let text = std::str::from_utf8(&text).context("Retained output is not UTF-8")?;
            let ranked = crate::temporary_rank::rank(
                crate::temporary_rank::Chunks::new(text),
                query,
                limit,
                budget,
            )
            .context("Output search failed within its 32 MiB temporary database and shared 64 MiB SQLite heap budgets; the original remains available through output page")?;
            budget.check()?;
            ensure!(
                retained.created + TTL > now()?,
                "Output expired during search."
            );
            let results = ranked
                .into_iter()
                .map(|region| {
                    let page_command = format!(
                        "grepglint output page {handle} --json --cursor {}",
                        encode_cursor(handle, region.excerpt_start_byte)
                    );
                    SearchResult {
                        region,
                        page_command,
                    }
                })
                .collect();
            Ok(Search {
                handle: handle.to_owned(),
                bytes: retained.bytes,
                lines: retained.lines,
                results,
                page_command: format!("grepglint output page {handle} --json"),
            })
        })();
        self.db.progress_handler(0, None::<fn() -> bool>)?;
        if result.is_err() {
            budget.check()?;
        }
        result
    }

    pub fn purge(&mut self) -> Result<()> {
        self.deletion_space()?;
        let mut leases = Vec::new();
        for slot in 0..2 {
            let f = private(&self.directory.join(format!("capture-{slot}.lock")), false)?;
            lock(&f).context("Output purge waits at most two seconds for each active capture; retry after capture finishes.")?;
            leases.push(f);
        }
        self.db.execute("DELETE FROM outputs", [])?;
        // secure_delete erases freed content without a second database copy.
        // Keep the bounded empty database and ownership records for reuse.
        Ok(())
    }
}

struct Retained {
    bytes: u64,
    lines: u64,
    created: u64,
}

fn read_stream(
    tx: &Connection,
    handle: &str,
    mut visit: impl FnMut(u64, &[u8]) -> Result<()>,
) -> Result<Retained> {
    let (bytes, lines, created, digest): (u64, u64, u64, String) = tx
            .query_row(
                "SELECT bytes,lines,created,CASE WHEN length(digest)=64 THEN digest ELSE NULL END FROM outputs WHERE handle=?1 AND committed=1",
                [handle],
                |r| {
                    Ok((
                        r.get::<_, u32>(0)? as u64,
                        r.get::<_, u32>(1)? as u64,
                        r.get::<_, u32>(2)? as u64,
                        r.get(3)?,
                    ))
                },
            )
            .optional()?
            .context(
                "Output is missing, expired, evicted, or purged; rerun the producer if needed.",
            )?;
    ensure!(
        created + TTL > now()? && bytes > 0 && bytes <= MAX_OUTPUT,
        "Output cursor is out of range or output has expired."
    );
    let mut stmt = tx.prepare(
        "SELECT ordinal,content,length(content) FROM chunks WHERE handle=?1 ORDER BY ordinal",
    )?;
    let mut rows = stmt.query([handle])?;
    let mut hash = Sha256::new();
    let mut total = 0u64;
    let mut ordinal = 0;
    let mut actual_lines = 0u64;
    let mut last_byte = None;
    while let Some(row) = rows.next()? {
        ensure!(
            row.get::<_, u32>(0)? == ordinal,
            "Retained output is corrupt."
        );
        ensure!(
            (1..=CHUNK as u32).contains(&row.get::<_, u32>(2)?),
            "Retained output is corrupt."
        );
        let b: Vec<u8> = row.get(1)?;
        ensure!(
            !b.is_empty() && b.len() <= CHUNK && total + b.len() as u64 <= bytes,
            "Retained output is corrupt."
        );
        hash.update(&b);
        actual_lines += b.iter().filter(|&&byte| byte == b'\n').count() as u64;
        last_byte = b.last().copied();
        visit(total, &b)?;
        total += b.len() as u64;
        ordinal += 1;
        ensure!(
            ordinal as u64 <= MAX_OUTPUT.div_ceil(CHUNK as u64),
            "Retained output has too many chunks."
        );
    }
    ensure!(
        total == bytes
            && actual_lines + u64::from(last_byte.is_some_and(|b| b != b'\n')) == lines
            && format!("{:x}", hash.finalize()) == digest,
        "Retained output is corrupt; no result was returned."
    );
    Ok(Retained {
        bytes,
        lines,
        created,
    })
}

struct Capture {
    handle: String,
    _lease: File,
    count: u64,
    bytes: u64,
    lines: u64,
    last: Option<u8>,
    digest: Sha256,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Page {
    pub handle: String,
    pub content: String,
    pub bytes: u64,
    pub lines: u64,
    pub next_cursor: Option<String>,
    pub end_of_output: bool,
}
fn validate_handle(s: &str) -> Result<()> {
    ensure!(
        s.len() == 32
            && s.bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()),
        "Invalid output handle."
    );
    Ok(())
}
fn random_handle() -> Result<String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn encode_cursor(handle: &str, offset: u64) -> String {
    let payload = format!("01{handle}{offset:016x}");
    format!("{payload}{:x}", Sha256::digest(payload.as_bytes()))
}
fn decode_cursor(handle: &str, cursor: Option<&str>) -> Result<u64> {
    let Some(c) = cursor else {
        return Ok(0);
    };
    ensure!(
        c.len() == 114
            && c.bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()),
        "Malformed output cursor."
    );
    ensure!(
        &c[..2] == "01"
            && &c[2..34] == handle
            && encode_cursor(handle, u64::from_str_radix(&c[34..50], 16)?) == c,
        "Mismatched or unsupported output cursor."
    );
    Ok(u64::from_str_radix(&c[34..50], 16)?)
}

pub fn printable(s: &str) -> String {
    s.chars()
        .flat_map(|c| {
            if (c.is_control() && c != '\n')
                || matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
            {
                c.escape_default().collect::<Vec<_>>()
            } else {
                vec![c]
            }
        })
        .collect()
}

fn read_stdin(buffer: &mut [u8], start: Instant, last: Instant) -> Result<usize> {
    loop {
        ensure!(
            start.elapsed() < OVERALL && last.elapsed() < IDLE,
            "Output capture timed out; consumed input may require rerunning the producer."
        );
        let mut poll = [
            libc::pollfd {
                fd: 0,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: 1,
                events: 0,
                revents: 0,
            },
        ];
        let timeout = (OVERALL
            .saturating_sub(start.elapsed())
            .min(IDLE.saturating_sub(last.elapsed())))
        .as_millis()
        .min(100) as i32;
        let n = unsafe { libc::poll(poll.as_mut_ptr(), 2, timeout) };
        if n < 0 {
            let e = std::io::Error::last_os_error();
            if e.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(e.into());
        }
        ensure!(
            poll[1].revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) == 0,
            "Output consumer closed the pipe; capture cancelled."
        );
        if poll[0].revents != 0 {
            return Ok(std::io::stdin().read(buffer)?);
        }
    }
}

pub fn bounce(config: &Config) -> Result<()> {
    let start = Instant::now();
    let mut last = start;
    let mut pending = Vec::with_capacity(BUFFER + CHUNK);
    let mut store = None;
    let mut capture = None;
    let mut partial = Vec::with_capacity(4);
    let mut head = Vec::new();
    let mut tail = Vec::new();
    let result = (|| -> Result<()> {
        loop {
            let mut buf = [0u8; CHUNK];
            let n = read_stdin(&mut buf, start, last)?;
            if n > 0 {
                last = Instant::now();
            }
            ensure!(
                !buf[..n].contains(&0),
                "Input contains NUL; capture rejected. Consumed input may require rerunning the producer."
            );
            partial.extend_from_slice(&buf[..n]);
            match std::str::from_utf8(&partial) {
                Ok(_) => partial.clear(),
                Err(e) if e.error_len().is_none() && n > 0 => {
                    partial.drain(..e.valid_up_to());
                }
                Err(_) => bail!(
                    "Input is not valid UTF-8; capture rejected. Consumed input may require rerunning the producer."
                ),
            }
            if n == 0 {
                break;
            }
            if head.len() < 256 {
                head.extend_from_slice(&buf[..n.min(256 - head.len())]);
            }
            tail.extend_from_slice(&buf[..n]);
            if tail.len() > 256 {
                tail.drain(..tail.len() - 256);
            }
            pending.extend_from_slice(&buf[..n]);
            if store.is_none() && pending.len() > THRESHOLD {
                let mut opened = Store::open(config)?;
                let begun = opened.begin()?;
                store = Some(opened);
                capture = Some(begun);
            }
            if let (Some(store), Some(capture)) = (&store, &mut capture) {
                while pending.len() >= BUFFER {
                    store.append(capture, &pending[..BUFFER])?;
                    pending.drain(..BUFFER);
                }
            }
        }
        if let (Some(store), Some(capture)) = (&store, &mut capture)
            && !pending.is_empty()
        {
            store.append(capture, &pending)?;
        }
        if let (Some(store), Some(capture)) = (&store, &capture) {
            store.commit(capture)?;
            let preview = format!(
                "Output {}: {} bytes, {} lines\nHead: {}\n...\nTail: {}\nRetained for at most {} seconds from capture start; may be evicted sooner.\nPage from the beginning: grepglint output page {} --json\nFind relevant sections: grepglint output search {} \"<query>\" --json\nCapture success does not report producer status.\n",
                capture.handle,
                capture.bytes,
                capture.lines + u64::from(capture.last.is_some_and(|b| b != b'\n')),
                printable(&String::from_utf8_lossy(&head)),
                printable(&String::from_utf8_lossy(&tail)),
                TTL,
                capture.handle,
                capture.handle
            );
            ensure!(preview.len() <= 8192, "Output preview exceeds its budget.");
            write_stdout(preview.as_bytes())?;
        } else {
            write_stdout(&pending)?;
        }
        Ok(())
    })();
    if result.is_err()
        && let (Some(store), Some(capture)) = (&store, &capture)
    {
        store.abort(&capture.handle).context(
            "Capture failed and cleanup is pending; next output request retries cleanup.",
        )?;
    }
    result
}

pub fn startup_cleanup(config: &Config) -> Result<()> {
    if config.directory.join("output-v1").try_exists()? {
        Store::open(config)?;
    }
    Ok(())
}

/// Deliver bounded output without leaving a capture stuck behind a stalled reader.
pub fn write_stdout(bytes: &[u8]) -> Result<()> {
    ensure!(bytes.len() <= 64 * 1024, "Output response exceeds 64 KiB.");
    let flags = unsafe { libc::fcntl(1, libc::F_GETFL) };
    ensure!(flags >= 0, "Cannot inspect stdout.");
    ensure!(
        unsafe { libc::fcntl(1, libc::F_SETFL, flags | libc::O_NONBLOCK) } >= 0,
        "Cannot bound stdout writes."
    );
    struct Restore(i32);
    impl Drop for Restore {
        fn drop(&mut self) {
            unsafe {
                libc::fcntl(1, libc::F_SETFL, self.0);
            }
        }
    }
    let _restore = Restore(flags);
    let start = Instant::now();
    let mut remaining = bytes;
    while !remaining.is_empty() {
        ensure!(
            start.elapsed() < LOCK_WAIT,
            "Output consumer stalled; delivery timed out."
        );
        let written = unsafe { libc::write(1, remaining.as_ptr().cast(), remaining.len()) };
        if written > 0 {
            remaining = &remaining[written as usize..];
            continue;
        }
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        if error.kind() != std::io::ErrorKind::WouldBlock {
            return Err(error.into());
        }
        let mut poll = libc::pollfd {
            fd: 1,
            events: libc::POLLOUT,
            revents: 0,
        };
        unsafe {
            libc::poll(&mut poll, 1, 50);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, Config) {
        let dir = tempfile::tempdir().unwrap();
        let config = Config {
            directory: dir.path().join("cache"),
            max_bytes: 128 * 1024 * 1024,
            idle: Duration::from_secs(1),
        };
        (dir, config)
    }
    fn retain(store: &mut Store, count: usize) -> String {
        let mut capture = store.begin().unwrap();
        let bytes = vec![b'x'; BUFFER];
        let mut left = count;
        while left > 0 {
            let n = left.min(BUFFER);
            store.append(&mut capture, &bytes[..n]).unwrap();
            left -= n;
        }
        store.commit(&capture).unwrap();
        capture.handle.clone()
    }
    #[test]
    fn overall_deadline_does_not_extend_with_recent_input() {
        let mut buffer = [0u8; 1];
        assert!(
            read_stdin(
                &mut buffer,
                Instant::now() - OVERALL - Duration::from_secs(1),
                Instant::now()
            )
            .is_err()
        );
    }
    #[test]
    fn recorded_initialization_resumes_before_ownership_publication() {
        let (_dir, config) = fixture();
        directory(&config.directory).unwrap();
        let id = random_handle().unwrap();
        let mut gate = private(&config.directory.join("output-gate"), true).unwrap();
        gate.write_all(format!("{INIT_MAGIC}{id}\n").as_bytes())
            .unwrap();
        let stage = config.directory.join(format!("output-init-{id}"));
        directory(&stage).unwrap();
        private(&stage.join("output.sqlite"), true).unwrap();
        drop(gate);
        let mut store = Store::open(&config).unwrap();
        let handle = retain(&mut store, 5000);
        assert!(store.page(&handle, None).is_ok());
        assert!(!stage.exists());
    }

    #[test]
    fn purge_does_not_require_capture_reserve() {
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let handle = retain(&mut store, 5000);
        store.reserve = u64::MAX - 2 * DB_LIMIT - 1024 * 1024;
        assert!(store.begin().is_err());
        store.purge().unwrap();
        assert!(store.page(&handle, None).is_err());
        assert!(store.search(&handle, "x", 5).is_err());
    }

    #[test]
    fn output_work_participates_in_maintenance_exclusion() {
        let (_dir, config) = fixture();
        let store = Store::open(&config).unwrap();
        let maintenance = crate::maintenance::lock_file(&config, "maintenance.lock").unwrap();
        assert!(maintenance.try_lock_exclusive().is_err());
        drop(store);
        maintenance.try_lock_exclusive().unwrap();
        assert!(Store::open(&config).is_err());
    }

    #[test]
    fn reservations_entries_and_fixed_expiry() {
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let oldest = retain(&mut store, 5000);
        for _ in 0..ENTRIES {
            retain(&mut store, 5000);
        }
        assert!(store.page(&oldest, None).is_err());
        assert!(store.search(&oldest, "x", 5).is_err());
        let handle = retain(&mut store, 5000);
        let created: u32 = store
            .db
            .query_row(
                "SELECT created FROM outputs WHERE handle=?1",
                [&handle],
                |r| r.get(0),
            )
            .unwrap();
        store.page(&handle, None).unwrap();
        assert_eq!(
            created,
            store
                .db
                .query_row(
                    "SELECT created FROM outputs WHERE handle=?1",
                    [&handle],
                    |r| r.get::<_, u32>(0)
                )
                .unwrap()
        );
        store
            .db
            .execute(
                "UPDATE outputs SET created=?1 WHERE handle=?2",
                params![(now().unwrap() - TTL) as i64, handle],
            )
            .unwrap();
        assert!(store.page(&handle, None).is_err());
        assert!(store.search(&handle, "x", 5).is_err());
        assert!(
            store
                .db
                .query_row("SELECT count(*) FROM outputs", [], |r| r.get::<_, u32>(0))
                .unwrap()
                <= 64
        );
    }
    #[test]
    fn aggregate_evicts_and_reservations_include_unfinished_captures() {
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let oldest = retain(&mut store, MAX_OUTPUT as usize);
        for _ in 0..3 {
            retain(&mut store, MAX_OUTPUT as usize);
        }
        let first = store.begin().unwrap();
        let second = store.begin().unwrap();
        assert!(store.begin().is_err());
        let used: u32 = store
            .db
            .query_row(
                "SELECT sum(CASE WHEN committed=1 THEN bytes ELSE ?1 END) FROM outputs",
                [MAX_OUTPUT as i64],
                |r| r.get(0),
            )
            .unwrap();
        assert!(used as u64 <= TOTAL);
        assert!(store.page(&oldest, None).is_err());
        assert!(store.search(&oldest, "x", 5).is_err());
        drop(first);
        drop(second);
        store.cleanup().unwrap();
        assert_eq!(
            store
                .db
                .query_row("SELECT count(*) FROM outputs WHERE committed=0", [], |r| {
                    r.get::<_, u32>(0)
                })
                .unwrap(),
            0
        );
        assert!(
            fs::metadata(store.directory.join("output.sqlite"))
                .unwrap()
                .len()
                <= DB_LIMIT
        );
    }
    #[test]
    fn corrupt_content_and_cursor_boundaries_never_return_partial_data() {
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let handle = retain(&mut store, 10000);
        assert!(
            store
                .page(&handle, Some(&encode_cursor(&handle, 10000)))
                .is_err()
        );
        assert!(
            store
                .page(&handle, Some(&encode_cursor(&handle, u64::MAX)))
                .is_err()
        );
        store
            .db
            .execute(
                "UPDATE chunks SET content=x'616263' WHERE handle=?1 AND ordinal=1",
                [&handle],
            )
            .unwrap();
        assert!(store.page(&handle, None).is_err());
        assert!(store.search(&handle, "x", 5).is_err());
    }
    #[test]
    fn low_space_and_full_database_fail_without_committed_handle() {
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        store.reserve = u64::MAX - 2 * DB_LIMIT - 1024 * 1024;
        assert!(store.begin().is_err());
        store.reserve = 0;
        let mut capture = store.begin().unwrap();
        store.db.pragma_update(None, "max_page_count", 10).unwrap();
        let mut failed = false;
        for _ in 0..50 {
            if store.append(&mut capture, &vec![b'x'; CHUNK]).is_err() {
                failed = true;
                break;
            }
        }
        assert!(failed);
        store.abort(&capture.handle).unwrap();
        assert!(store.page(&capture.handle, None).is_err());
    }
    #[test]
    fn cancelled_search_open_does_not_start_pending_expiry_cleanup() {
        use std::os::fd::AsRawFd;
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let expired = retain(&mut store, 10000);
        let valid = retain(&mut store, 10000);
        store
            .db
            .execute(
                "UPDATE outputs SET created=?1 WHERE handle=?2",
                params![(now().unwrap() - TTL) as i64, expired],
            )
            .unwrap();
        let elapsed = crate::temporary_rank::Budget::until(Instant::now(), 1);
        assert!(
            Store::open_for_search(&config, elapsed)
                .err()
                .unwrap()
                .to_string()
                .contains("deadline")
        );
        let gate = private(&config.directory.join("output-gate"), false).unwrap();
        gate.try_lock_exclusive().unwrap();
        let started = Instant::now();
        let waiting = crate::temporary_rank::Budget::until(started + Duration::from_millis(20), 1);
        assert!(
            Store::open_for_search(&config, waiting)
                .err()
                .unwrap()
                .to_string()
                .contains("deadline")
        );
        assert!(started.elapsed() < Duration::from_secs(1));
        drop(gate);
        let (consumer, server) = std::os::unix::net::UnixStream::pair().unwrap();
        drop(consumer);
        let cancelled = crate::temporary_rank::Budget::new(server.as_raw_fd());
        assert!(
            Store::open_for_search(&config, cancelled)
                .err()
                .unwrap()
                .to_string()
                .contains("cancelled")
        );
        assert_eq!(
            store
                .db
                .query_row("SELECT count(*) FROM outputs", [], |r| r.get::<_, u32>(0))
                .unwrap(),
            2
        );
        // SQLite interruption rolls back pending cleanup, preserving the valid handle.
        store
            .db
            .progress_handler(1, Some(move || cancelled.check().is_err()))
            .unwrap();
        assert!(store.cleanup().is_err());
        store.db.progress_handler(0, None::<fn() -> bool>).unwrap();
        assert_eq!(
            store
                .db
                .query_row("SELECT count(*) FROM outputs", [], |r| r.get::<_, u32>(0))
                .unwrap(),
            2
        );
        assert_eq!(store.page(&valid, None).unwrap().content, "x".repeat(PAGE));
        assert!(store.page(&expired, None).is_err());
    }

    #[test]
    fn cancellation_preserves_pages() {
        use std::os::fd::AsRawFd;
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let handle = retain(&mut store, 10000);
        let before = store.page(&handle, None).unwrap().content;
        let (consumer, server) = std::os::unix::net::UnixStream::pair().unwrap();
        drop(consumer);
        assert!(
            store
                .search_with_budget(
                    &handle,
                    "x",
                    5,
                    crate::temporary_rank::Budget::new(server.as_raw_fd())
                )
                .is_err()
        );
        assert_eq!(store.page(&handle, None).unwrap().content, before);
    }

    #[test]
    fn purge_erases_journal_and_data_without_deleting_ownership() {
        let (_dir, config) = fixture();
        let mut store = Store::open(&config).unwrap();
        let mut capture = store.begin().unwrap();
        let secret = b"distinctive retained source text to erase";
        store.append(&mut capture, secret).unwrap();
        store.commit(&capture).unwrap();
        drop(capture);
        store.purge().unwrap();
        let data = fs::read(store.directory.join("output.sqlite")).unwrap();
        assert!(!data.windows(secret.len()).any(|w| w == secret));
        assert_eq!(
            fs::metadata(store.directory.join("output.sqlite-journal"))
                .unwrap()
                .len(),
            0
        );
        assert!(store.directory.join("ownership.json").exists());
    }
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Search {
    pub handle: String,
    pub bytes: u64,
    pub lines: u64,
    pub results: Vec<SearchResult>,
    pub page_command: String,
}
#[derive(Debug, Deserialize, Serialize)]
pub struct SearchResult {
    #[serde(flatten)]
    pub region: crate::temporary_rank::Region,
    pub page_command: String,
}

pub fn search(config: &Config, handle: &str, query: &str, limit: usize) -> Result<Search> {
    crate::temporary_rank::validate(query, limit)?;
    crate::daemon::output_search(config, handle, query, limit)
}
