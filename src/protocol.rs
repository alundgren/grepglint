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
    /// Skip decisions made during this refresh, not an inventory of cached files.
    #[serde(default)]
    pub skip_reasons: std::collections::BTreeMap<crate::chunks::SkipReason, usize>,
    pub active_files: usize,
    pub overlay_entries: usize,
    pub cached_contents: usize,
    pub cached_chunks: usize,
    pub evicted_worktrees: usize,
    pub elapsed_ms: u64,
}

impl Stats {
    pub fn record_skip(&mut self, reason: crate::chunks::SkipReason) {
        self.skipped_files += 1;
        *self.skip_reasons.entry(reason).or_default() += 1;
    }
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
    Ok { data: Box<Response> },
    Error { message: String },
}

/// Controls are additive; existing version-one search requests remain valid.
#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum ControlRequest {
    Health { version: u32 },
    Shutdown { version: u32, instance: String },
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Health {
    pub build_version: String,
    pub executable_sha256: String,
    pub protocol_version: u32,
    pub instance: String,
    pub cache_directory: String,
    pub database_bytes: u64,
    pub idle_seconds: u64,
    pub request_bytes: usize,
    pub response_bytes: u64,
    pub work_seconds: u64,
    pub sqlite_heap_bytes: u64,
    pub address_space_bytes: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum ControlResponse {
    Healthy { data: Health },
    Stopped { instance: String },
    Error { message: String },
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "command", deny_unknown_fields)]
pub enum OutputRequest {
    #[serde(rename = "output_search")]
    Search {
        version: u32,
        handle: String,
        query: String,
        limit: usize,
    },
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum OutputResponse {
    Ok { data: crate::output::Search },
    Error { message: String },
}
