use crate::{
    chunks,
    files::FilePolicy,
    git::{self, Repository, TreeEntry},
    protocol::{Request, Response, SearchResult, Stats},
    tokens,
};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const SCHEMA_VERSION: i64 = 1;
const WEEK: i64 = 7 * 24 * 60 * 60;

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

pub struct Index {
    db: Connection,
    directory: PathBuf,
    max_bytes: u64,
    last_maintenance: Option<Instant>,
}

impl Index {
    pub fn open(directory: &Path, max_bytes: u64) -> Result<Self> {
        let db = Connection::open(directory.join("index.sqlite"))?;
        db.busy_timeout(Duration::from_millis(500))?;
        db.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=DELETE; PRAGMA auto_vacuum=INCREMENTAL;
            PRAGMA temp_store=MEMORY; PRAGMA cache_size=-8192; PRAGMA mmap_size=0; PRAGMA hard_heap_limit=67108864;")?;
        let page_size: i64 = db.query_row("PRAGMA page_size", [], |r| r.get(0))?;
        let pages: i64 = db.query_row("PRAGMA page_count", [], |r| r.get(0))?;
        ensure!(
            pages * page_size <= max_bytes as i64,
            "Existing cache exceeds GREPGLINT_CACHE_MB; keep the previous limit or remove the cache while the daemon is stopped."
        );
        db.pragma_update(None, "max_page_count", max_bytes as i64 / page_size)?;
        let version: i64 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        ensure!(
            version == 0 || version == SCHEMA_VERSION,
            "Unsupported cache version {version}; this binary expects {SCHEMA_VERSION}."
        );
        if version == 0 {
            ensure!(
                fs2::available_space(directory)? >= max_bytes * 2 + 64 * 1024 * 1024,
                "Not enough free disk space to create the cache; use rg."
            );
            db.execute_batch(include_str!("schema.sql"))?;
            db.pragma_update(None, "user_version", SCHEMA_VERSION)?;
        }
        Ok(Self {
            db,
            directory: directory.to_owned(),
            max_bytes,
            last_maintenance: None,
        })
    }

    pub fn search(&mut self, request: &Request) -> Result<Response> {
        ensure!(
            request.version == crate::protocol::VERSION,
            "Client and daemon protocol versions differ; pause searches and retry after idle exit. Use rg meanwhile."
        );
        ensure!(
            (1..=20).contains(&request.limit),
            "Result limit must be between 1 and 20."
        );
        let expression = tokens::query(&request.query)?;
        let started = Instant::now();
        let deadline = started + Duration::from_secs(30);
        let repo = git::discover(Path::new(&request.cwd), deadline)?;
        let dirty = git::dirty_files(&repo, deadline)?;
        let policy = FilePolicy::load(&repo)?;
        let signature = policy.signature(&dirty)?;
        let mut stats = Stats::default();
        let can_write =
            fs2::available_space(&self.directory)? >= self.max_bytes * 2 + 64 * 1024 * 1024;
        self.db
            .progress_handler(1000, Some(move || Instant::now() >= deadline))?;
        let result = (|| {
            if can_write
                && self
                    .last_maintenance
                    .is_none_or(|last| last.elapsed() >= Duration::from_secs(60))
            {
                stats.evicted_worktrees = self.maintain(&repo.worktree_id)?;
                self.last_maintenance = Some(Instant::now());
            }
            let previous: Option<(Option<String>, String)> = self
                .db
                .query_row(
                    "SELECT head, signature FROM worktrees WHERE id=?",
                    [&repo.worktree_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?;
            let changed = previous
                .as_ref()
                .is_none_or(|(head, sig)| head != &repo.head || sig != &signature);
            let policy_changed = previous.as_ref().is_none_or(|(_, sig)| {
                sig.split_once(':').map(|(policy, _)| policy) != Some(policy.fingerprint.as_str())
            });
            if changed {
                ensure!(
                    can_write,
                    "Not enough free disk space for safe cache writes; free space or use rg."
                );
                stats.head_changed = previous.as_ref().is_none_or(|(head, _)| head != &repo.head);
                let transaction = self.db.unchecked_transaction()?;
                let previous_head = if policy_changed {
                    None
                } else {
                    previous.as_ref().and_then(|(head, _)| head.as_deref())
                };
                let update = self.refresh(&repo, &dirty, &policy, previous_head, &mut stats, deadline).and_then(|()| {
                    let observed = git::discover(&repo.root, deadline)?;
                    let current_dirty = git::dirty_files(&repo, deadline)?;
                    ensure!(observed.head == repo.head && current_dirty == dirty && FilePolicy::load(&repo)?.fingerprint == policy.fingerprint,
                        "Worktree changed during indexing; retry the query to read its current state.");
                    Ok(())
                });
                update.context("Index update failed; no partial search view was published")?;
                transaction.commit()?;
                self.prune_contents(false)?;
            } else if can_write {
                self.db.execute(
                    "UPDATE worktrees SET last_seen=? WHERE id=? AND last_seen < ?",
                    params![now(), repo.worktree_id, now() - 60],
                )?;
            }
            let results = self.rank(
                &repo.worktree_id,
                &expression,
                &request.query,
                request.limit,
            )?;
            stats.active_files = self.db.query_row(&format!("WITH active AS ({ACTIVE_SQL}) SELECT count(*) FROM active WHERE content_id IS NOT NULL"), [&repo.worktree_id], |r| r.get::<_,u32>(0))? as usize;
            stats.overlay_entries = self.db.query_row(
                "SELECT count(*) FROM overlays WHERE worktree_id=?",
                [&repo.worktree_id],
                |r| r.get::<_, u32>(0),
            )? as usize;
            stats.cached_contents = self.db.query_row(
                "SELECT count(*) FROM contents WHERE repo_id=?",
                [&repo.repo_id],
                |r| r.get::<_, u32>(0),
            )? as usize;
            stats.cached_chunks = self.db.query_row("SELECT count(*) FROM chunks JOIN contents ON contents.id=chunks.content_id WHERE repo_id=?", [&repo.repo_id], |r| r.get::<_,u32>(0))? as usize;
            stats.elapsed_ms = started.elapsed().as_millis() as u64;
            Ok(Response {
                repository: repo.common_dir.to_string_lossy().into_owned(),
                worktree: repo.root.to_string_lossy().into_owned(),
                head: repo.head.clone(),
                query: request.query.clone(),
                results,
                stats,
            })
        })();
        self.db.progress_handler(0, None::<fn() -> bool>)?;
        result
    }

    fn refresh(
        &self,
        repo: &Repository,
        dirty: &BTreeMap<String, String>,
        policy: &FilePolicy,
        previous_head: Option<&str>,
        stats: &mut Stats,
        deadline: Instant,
    ) -> Result<()> {
        let signature = policy.signature(dirty)?;
        self.db.execute(
            "INSERT OR IGNORE INTO repositories(id,common_dir) VALUES(?,?)",
            params![repo.repo_id, repo.common_dir.to_string_lossy()],
        )?;
        self.db.execute("INSERT INTO worktrees(id,repo_id,root,head,signature,last_seen) VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET head=excluded.head,signature=excluded.signature,last_seen=excluded.last_seen",
            params![repo.worktree_id, repo.repo_id, repo.root.to_string_lossy(), repo.head, signature, now()])?;
        if previous_head.is_none() {
            self.db.execute(
                "DELETE FROM overlays WHERE worktree_id=?",
                [&repo.worktree_id],
            )?;
        }
        if stats.head_changed || previous_head.is_none() {
            let entries = if let (Some(before), Some(_)) = (previous_head, &repo.head) {
                match git::changed_entries(repo, before, deadline) {
                    Ok(entries) => entries,
                    Err(_) => {
                        self.db.execute(
                            "DELETE FROM base_files WHERE worktree_id=?",
                            [&repo.worktree_id],
                        )?;
                        git::tree_entries(repo, deadline)?
                    }
                }
            } else {
                self.db.execute(
                    "DELETE FROM base_files WHERE worktree_id=?",
                    [&repo.worktree_id],
                )?;
                git::tree_entries(repo, deadline)?
            };
            self.prepare_blobs(repo, &entries, policy, stats, deadline)?;
            for entry in entries {
                if policy.excludes(&entry.path) {
                    stats.record_skip(chunks::SkipReason::ExcludedPath);
                    continue;
                }
                let path_id = self.path_id(repo, &entry.path)?;
                self.db.execute(
                    "DELETE FROM base_files WHERE worktree_id=? AND path_id=?",
                    params![repo.worktree_id, path_id],
                )?;
                if let Some(oid) = entry
                    .oid
                    .filter(|_| entry.mode == "100644" || entry.mode == "100755")
                {
                    let content_id = self
                        .content_id(repo, &format!("git:{oid}"), chunks::parser_for(&entry.path))?
                        .context("Missing prepared blob")?;
                    self.db.execute(
                        "INSERT INTO base_files(worktree_id,path_id,content_id) VALUES(?,?,?)",
                        params![repo.worktree_id, path_id, content_id],
                    )?;
                }
                stats.paths_updated += 1;
            }
            let count: usize = self.db.query_row(
                "SELECT count(*) FROM base_files WHERE worktree_id=?",
                [&repo.worktree_id],
                |r| r.get::<_, u32>(0),
            )? as usize;
            ensure!(
                count <= git::MAX_FILES,
                "Active tree exceeds 20,000 source paths."
            );
        }
        let old_paths = {
            let mut stmt = self.db.prepare("SELECT p.path,o.path_id FROM overlays o JOIN paths p ON p.id=o.path_id WHERE worktree_id=?")?;
            stmt.query_map([&repo.worktree_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?
        };
        for (path, path_id) in old_paths {
            if !dirty.contains_key(&path) {
                self.db.execute(
                    "DELETE FROM overlays WHERE worktree_id=? AND path_id=?",
                    params![repo.worktree_id, path_id],
                )?;
            }
        }
        for (path, fingerprint) in dirty {
            ensure!(
                Instant::now() < deadline,
                "Indexing exceeded the query work budget; use rg."
            );
            if policy.excludes(path) {
                stats.record_skip(chunks::SkipReason::ExcludedPath);
                continue;
            }
            let format = chunks::parser_for(path);
            let path_id = self.path_id(repo, path)?;
            let old: Option<String> = self
                .db
                .query_row(
                    "SELECT fingerprint FROM overlays WHERE worktree_id=? AND path_id=?",
                    params![repo.worktree_id, path_id],
                    |r| r.get(0),
                )
                .optional()?;
            if !stats.head_changed && old.as_deref() == Some(fingerprint) {
                continue;
            }
            let bytes = match git::read_worktree_file(repo, path)? {
                git::WorktreeFile::Contents(bytes) => Some(bytes),
                git::WorktreeFile::Missing => None,
                git::WorktreeFile::Skipped(reason) => {
                    stats.record_skip(reason);
                    None
                }
            };
            let base_identity: Option<String> = self.db.query_row("SELECT c.identity FROM base_files b JOIN contents c ON c.id=b.content_id WHERE b.worktree_id=? AND b.path_id=?", params![repo.worktree_id,path_id], |r| r.get(0)).optional()?;
            if let (Some(bytes), Some(base)) = (&bytes, &base_identity) {
                let oid = base.strip_prefix("git:").unwrap();
                if git::blob_identity(bytes, oid.len()) == oid {
                    self.db.execute(
                        "DELETE FROM overlays WHERE worktree_id=? AND path_id=?",
                        params![repo.worktree_id, path_id],
                    )?;
                    continue;
                }
            }
            let content_id = if let Some(bytes) = bytes {
                let identity = format!("worktree:{}:{}", repo.worktree_id, git::identity(&bytes));
                match self.content_id(repo, &identity, format)? {
                    Some(id) => {
                        stats.cached_contents_reused += 1;
                        Some(id)
                    }
                    None => {
                        let parsed = chunks::extract(&bytes, format);
                        if let Err(reason) = &parsed {
                            stats.record_skip(*reason);
                        } else {
                            stats.overlay_files_parsed += 1;
                        }
                        Some(self.insert_content(repo, &identity, format, parsed.ok())?)
                    }
                }
            } else {
                None
            };
            self.db.execute("INSERT INTO overlays(worktree_id,path_id,fingerprint,content_id) VALUES(?,?,?,?)
                ON CONFLICT(worktree_id,path_id) DO UPDATE SET fingerprint=excluded.fingerprint,content_id=excluded.content_id",
                params![repo.worktree_id,path_id,fingerprint,content_id])?;
        }
        Ok(())
    }

    fn content_id(&self, repo: &Repository, identity: &str, format: &str) -> Result<Option<i64>> {
        Ok(self
            .db
            .query_row(
                "SELECT id FROM contents WHERE repo_id=? AND identity=? AND parser=?",
                params![repo.repo_id, identity, format],
                |r| r.get(0),
            )
            .optional()?)
    }

    fn insert_content(
        &self,
        repo: &Repository,
        identity: &str,
        format: &str,
        chunks: Option<Vec<chunks::Chunk>>,
    ) -> Result<i64> {
        self.db.execute(
            "INSERT INTO contents(repo_id,identity,parser,created,skipped) VALUES(?,?,?,?,?)",
            params![repo.repo_id, identity, format, now(), chunks.is_none()],
        )?;
        let id = self.db.last_insert_rowid();
        for chunk in chunks.unwrap_or_default() {
            self.db.execute(
                "INSERT INTO chunks(content_id,start_line,end_line,symbol,body) VALUES(?,?,?,?,?)",
                params![
                    id,
                    chunk.start as i64,
                    chunk.end as i64,
                    chunk.symbol,
                    chunk.content
                ],
            )?;
            let rowid = self.db.last_insert_rowid();
            self.db.execute(
                "INSERT INTO chunk_fts(rowid,symbol,body) VALUES(?,?,?)",
                params![
                    rowid,
                    tokens::searchable(chunk.symbol.as_deref().unwrap_or("")),
                    tokens::searchable(&chunk.content)
                ],
            )?;
        }
        Ok(id)
    }

    fn path_id(&self, repo: &Repository, path: &str) -> Result<i64> {
        if let Some(id) = self
            .db
            .query_row(
                "SELECT id FROM paths WHERE repo_id=? AND path=?",
                params![repo.repo_id, path],
                |r| r.get(0),
            )
            .optional()?
        {
            return Ok(id);
        }
        self.db.execute(
            "INSERT INTO paths(repo_id,path) VALUES(?,?)",
            params![repo.repo_id, path],
        )?;
        let id = self.db.last_insert_rowid();
        self.db.execute(
            "INSERT INTO path_fts(rowid,path) VALUES(?,?)",
            params![id, tokens::searchable(path)],
        )?;
        Ok(id)
    }

    fn prepare_blobs(
        &self,
        repo: &Repository,
        entries: &[TreeEntry],
        policy: &FilePolicy,
        stats: &mut Stats,
        deadline: Instant,
    ) -> Result<()> {
        let mut missing: BTreeMap<String, BTreeSet<&str>> = BTreeMap::new();
        for entry in entries {
            let Some(oid) = &entry.oid else {
                continue;
            };
            if policy.excludes(&entry.path) {
                continue;
            }
            let format = chunks::parser_for(&entry.path);
            if entry.mode != "100644" && entry.mode != "100755" {
                stats.record_skip(chunks::SkipReason::NotRegularFile);
                continue;
            }
            if self
                .content_id(repo, &format!("git:{oid}"), format)?
                .is_some()
            {
                stats.cached_contents_reused += 1;
            } else {
                missing.entry(oid.clone()).or_default().insert(format);
            }
        }
        if missing.is_empty() {
            return Ok(());
        }
        let sizes = git::blob_sizes(repo, &missing.keys().cloned().collect::<Vec<_>>(), deadline)?;
        let mut batch = Vec::new();
        let mut batch_bytes = 0;
        for (oid, formats) in &missing {
            let size = *sizes.get(oid).context("Missing Git blob size")?;
            if size > chunks::MAX_FILE_BYTES {
                for format in formats {
                    self.insert_content(repo, &format!("git:{oid}"), format, None)?;
                    stats.record_skip(chunks::SkipReason::FileTooLarge);
                }
                continue;
            }
            if batch_bytes + size > 8 * 1024 * 1024 {
                self.index_batch(repo, &batch, &missing, stats, deadline)?;
                batch.clear();
                batch_bytes = 0;
            }
            batch.push(oid.clone());
            batch_bytes += size;
        }
        self.index_batch(repo, &batch, &missing, stats, deadline)
    }

    fn index_batch(
        &self,
        repo: &Repository,
        batch: &[String],
        missing: &BTreeMap<String, BTreeSet<&str>>,
        stats: &mut Stats,
        deadline: Instant,
    ) -> Result<()> {
        if batch.is_empty() {
            return Ok(());
        }
        for (oid, bytes) in git::read_blobs(repo, batch, deadline)? {
            ensure!(
                Instant::now() < deadline,
                "Indexing exceeded the query work budget; use rg."
            );
            for format in &missing[&oid] {
                let parsed = chunks::extract(&bytes, format);
                if let Err(reason) = &parsed {
                    stats.record_skip(*reason);
                } else {
                    stats.blobs_parsed += 1;
                }
                self.insert_content(repo, &format!("git:{oid}"), format, parsed.ok())?;
            }
        }
        Ok(())
    }

    fn rank(
        &self,
        worktree: &str,
        expression: &str,
        query: &str,
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let sql = format!("WITH active AS MATERIALIZED ({ACTIVE_SQL}),
          chunk_matches AS MATERIALIZED (SELECT rowid id,-bm25(chunk_fts,8.0,1.0) weight FROM chunk_fts WHERE chunk_fts MATCH ?2),
          path_matches AS MATERIALIZED (SELECT rowid id,-bm25(path_fts,3.0) weight FROM path_fts WHERE path_fts MATCH ?2),
          candidates AS (
            SELECT a.path_id,c.id,m.weight FROM active a JOIN chunks c ON c.content_id=a.content_id JOIN chunk_matches m ON m.id=c.id
            UNION ALL
            SELECT a.path_id,c.id,m.weight FROM active a JOIN path_matches m ON m.id=a.path_id JOIN chunks c ON c.content_id=a.content_id),
          ranked AS (SELECT path_id,id,sum(weight) score FROM candidates GROUP BY path_id,id ORDER BY score DESC,path_id,id LIMIT ?3)
          SELECT p.path,c.start_line,c.end_line,c.symbol,c.body,b.identity,r.score FROM ranked r
          JOIN chunks c ON c.id=r.id JOIN contents b ON b.id=c.content_id JOIN paths p ON p.id=r.path_id ORDER BY r.score DESC,p.path,c.start_line");
        let mut stmt = self.db.prepare(&sql)?;
        let rows = stmt.query_map(params![worktree, expression, limit as i64], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, u32>(1)? as usize,
                r.get::<_, u32>(2)? as usize,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, f64>(6)?,
            ))
        })?;
        let mut results = Vec::new();
        let terms = tokens::tokens(query);
        for row in rows {
            let (path, start_line, end_line, symbol, body, content_identity, score) = row?;
            let lines: Vec<_> = body.lines().collect();
            let best = lines
                .iter()
                .enumerate()
                .max_by_key(|(_, line)| {
                    let lower = line.to_lowercase();
                    terms
                        .iter()
                        .filter(|term| lower.contains(term.as_str()))
                        .count()
                })
                .map(|(index, _)| index)
                .unwrap_or(0);
            let first = best.saturating_sub(2).min(lines.len().saturating_sub(8));
            let last = (first + 8).min(lines.len());
            let content: String = lines[first..last]
                .iter()
                .map(|line| line.chars().take(180).collect::<String>())
                .collect::<Vec<_>>()
                .join("\n")
                .chars()
                .take(1000)
                .collect();
            let snippet_lines = content.lines().count().max(1);
            let truncated = content != body;
            results.push(SearchResult {
                path,
                start_line,
                end_line,
                symbol,
                content,
                snippet_start_line: start_line + first,
                snippet_end_line: start_line + first + snippet_lines - 1,
                truncated,
                content_identity,
                score,
            });
        }
        Ok(results)
    }

    fn prune_contents(&self, pressure: bool) -> Result<()> {
        self.db.execute("DELETE FROM contents WHERE NOT EXISTS(SELECT 1 FROM base_files WHERE content_id=contents.id)
          AND NOT EXISTS(SELECT 1 FROM overlays WHERE content_id=contents.id)
          AND (identity LIKE 'worktree:%' OR created < ? OR ?)", params![now()-WEEK,pressure])?;
        self.db.execute(
            "DELETE FROM paths WHERE NOT EXISTS(SELECT 1 FROM base_files WHERE path_id=paths.id)
          AND NOT EXISTS(SELECT 1 FROM overlays WHERE path_id=paths.id)",
            [],
        )?;
        Ok(())
    }

    fn used_bytes(&self) -> Result<u64> {
        let pages: i64 = self.db.query_row("PRAGMA page_count", [], |r| r.get(0))?;
        let free: i64 = self
            .db
            .query_row("PRAGMA freelist_count", [], |r| r.get(0))?;
        let page_size: i64 = self.db.query_row("PRAGMA page_size", [], |r| r.get(0))?;
        Ok(((pages - free) * page_size) as u64)
    }

    fn maintain(&self, current: &str) -> Result<usize> {
        let mut evicted = self.db.execute(
            "DELETE FROM worktrees WHERE id != ? AND last_seen < ?",
            params![current, now() - WEEK],
        )?;
        let pressure = self.used_bytes()? > self.max_bytes * 3 / 4;
        self.prune_contents(pressure)?;
        if pressure {
            let mut stmt = self
                .db
                .prepare("SELECT id FROM worktrees WHERE id != ? ORDER BY last_seen,id")?;
            let candidates = stmt
                .query_map([current], |r| r.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for id in candidates {
                if self.used_bytes()? <= self.max_bytes / 2 {
                    break;
                }
                evicted += self.db.execute("DELETE FROM worktrees WHERE id=?", [id])?;
                self.prune_contents(true)?;
            }
        }
        self.db.execute_batch("PRAGMA incremental_vacuum(1024)")?;
        Ok(evicted)
    }
}

const ACTIVE_SQL: &str = "SELECT b.path_id,b.content_id FROM base_files b WHERE b.worktree_id=?1
  AND NOT EXISTS(SELECT 1 FROM overlays o WHERE o.worktree_id=b.worktree_id AND o.path_id=b.path_id)
  UNION ALL SELECT path_id,content_id FROM overlays WHERE worktree_id=?1 AND content_id IS NOT NULL";
