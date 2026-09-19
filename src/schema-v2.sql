CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    common_dir TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS worktrees (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repositories(id),
    root TEXT NOT NULL,
    head TEXT,
    signature TEXT NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contents (
    id INTEGER PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repositories(id),
    identity TEXT NOT NULL,
    parser TEXT NOT NULL,
    created INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    UNIQUE(repo_id,identity,parser)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    symbol TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_content ON chunks(content_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(symbol,body,content='',contentless_delete=1,tokenize='unicode61 tokenchars ''_''');
CREATE TRIGGER IF NOT EXISTS chunks_deleted AFTER DELETE ON chunks BEGIN
    DELETE FROM chunk_fts WHERE rowid=old.id;
END;
CREATE TABLE IF NOT EXISTS paths (
    id INTEGER PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repositories(id),
    path TEXT NOT NULL,
    UNIQUE(repo_id,path)
);
CREATE VIRTUAL TABLE IF NOT EXISTS path_fts USING fts5(path,content='',contentless_delete=1,tokenize='unicode61 tokenchars ''_''');
CREATE TRIGGER IF NOT EXISTS paths_deleted AFTER DELETE ON paths BEGIN
    DELETE FROM path_fts WHERE rowid=old.id;
END;
CREATE TABLE IF NOT EXISTS base_files (
    worktree_id TEXT NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    path_id INTEGER NOT NULL REFERENCES paths(id),
    content_id INTEGER NOT NULL REFERENCES contents(id),
    PRIMARY KEY(worktree_id,path_id)
);
CREATE INDEX IF NOT EXISTS base_content ON base_files(content_id);
CREATE INDEX IF NOT EXISTS base_path ON base_files(path_id);
CREATE TABLE IF NOT EXISTS overlays (
    worktree_id TEXT NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    path_id INTEGER NOT NULL REFERENCES paths(id),
    fingerprint TEXT NOT NULL,
    content_id INTEGER REFERENCES contents(id),
    PRIMARY KEY(worktree_id,path_id)
);
CREATE INDEX IF NOT EXISTS overlay_content ON overlays(content_id);
CREATE INDEX IF NOT EXISTS overlay_path ON overlays(path_id);
