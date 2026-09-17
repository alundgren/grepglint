use serde::{Deserialize, Serialize};

pub const VERSION: u32 = 1;
pub const MAX_REQUEST: usize = 16 * 1024;
pub const MAX_RESPONSE: u64 = 64 * 1024;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub version: u32,
    pub cwd: String,
    pub query: String,
    pub limit: usize,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchResult {
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub symbol: Option<String>,
    pub content: String,
    pub snippet_start_line: usize,
    pub snippet_end_line: usize,
    pub truncated: bool,
    pub content_identity: String,
    pub score: f64,
}

#[derive(Debug, Default, Deserialize, Serialize)]
pub struct Stats {
    pub head_changed: bool,
    pub paths_updated: usize,
    pub blobs_parsed: usize,
    pub overlay_files_parsed: usize,
    pub cached_contents_reused: usize,
    pub skipped_files: usize,
    pub active_files: usize,
    pub overlay_entries: usize,
    pub cached_contents: usize,
    pub cached_chunks: usize,
    pub evicted_worktrees: usize,
    pub elapsed_ms: u64,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Response {
    pub repository: String,
    pub worktree: String,
    pub head: Option<String>,
    pub query: String,
    pub results: Vec<SearchResult>,
    pub stats: Stats,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum WireResponse {
    Ok { data: Response },
    Error { message: String },
}
