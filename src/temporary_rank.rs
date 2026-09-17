//! Short-lived lexical ranking of caller-provided text regions.
use crate::tokens;
use anyhow::{Result, ensure};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};

const CHUNK_BYTES: usize = 8192;
const CHUNK_LINES: usize = 60;
const OVERLAP_BYTES: usize = 1024;
const MAX_CHUNKS: usize = 8192;
const EXPANDED_BYTES: usize = 32 * 1024 * 1024;
const DATABASE_BYTES: usize = 32 * 1024 * 1024;

#[derive(Clone, Copy)]
pub(crate) struct Budget {
    deadline: Instant,
    consumer: std::os::fd::RawFd,
}
impl Budget {
    pub(crate) fn new(consumer: std::os::fd::RawFd) -> Self {
        Self {
            deadline: Instant::now() + Duration::from_secs(30),
            consumer,
        }
    }
    pub(crate) fn check(self) -> Result<()> {
        ensure!(
            Instant::now() < self.deadline,
            "Output search exceeded its 30-second work deadline; use output page."
        );
        consumer_connected(self.consumer)?;
        Ok(())
    }
}

pub(crate) fn consumer_connected(consumer: std::os::fd::RawFd) -> Result<()> {
    let mut poll = libc::pollfd {
        fd: consumer,
        events: 0,
        revents: 0,
    };
    let result = unsafe { libc::poll(&mut poll, 1, 0) };
    ensure!(
        result >= 0 || std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted,
        "Cannot check output search cancellation."
    );
    ensure!(
        poll.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) == 0,
        "Output consumer closed the pipe; search cancelled."
    );
    Ok(())
}

pub(crate) fn validate(query: &str, limit: usize) -> Result<String> {
    ensure!(
        (1..=20).contains(&limit),
        "Output search limit must be between 1 and 20."
    );
    tokens::query(query)
}

#[derive(Clone, Copy)]
pub(crate) struct Chunk<'a> {
    text: &'a str,
    start_byte: usize,
    start_line: usize,
}

pub(crate) struct Chunks<'a> {
    text: &'a str,
    offset: usize,
    line: usize,
}
impl<'a> Chunks<'a> {
    pub(crate) fn new(text: &'a str) -> Self {
        Self {
            text,
            offset: 0,
            line: 1,
        }
    }
}
impl<'a> Iterator for Chunks<'a> {
    type Item = Chunk<'a>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.offset == self.text.len() {
            return None;
        }
        let start = self.offset;
        let mut end = (start + CHUNK_BYTES).min(self.text.len());
        while !self.text.is_char_boundary(end) {
            end -= 1;
        }
        if let Some((position, _)) = self.text[start..end]
            .match_indices('\n')
            .nth(CHUNK_LINES - 1)
        {
            end = start + position + 1;
        }
        let chunk = Chunk {
            text: &self.text[start..end],
            start_byte: start,
            start_line: self.line,
        };
        let mut next = end;
        if end < self.text.len() {
            let lower = (end.saturating_sub(OVERLAP_BYTES)).max(start + 1);
            let mut boundaries = self.text[start..end]
                .match_indices('\n')
                .rev()
                .map(|(pos, _)| start + pos + 1)
                .filter(|&pos| pos < end && pos >= lower);
            if let Some(first) = boundaries.next() {
                next = boundaries.take(5).last().unwrap_or(first);
            } else {
                next = end.saturating_sub(256).max(start + 1);
                while !self.text.is_char_boundary(next) {
                    next += 1;
                }
            }
        }
        self.line += self.text[start..next]
            .bytes()
            .filter(|&b| b == b'\n')
            .count();
        self.offset = next;
        Some(chunk)
    }
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Region {
    pub start_byte: u64,
    pub end_byte: u64,
    pub start_line: usize,
    pub end_line: usize,
    pub excerpt_start_byte: u64,
    pub excerpt_end_byte: u64,
    pub excerpt_start_line: usize,
    pub excerpt_end_line: usize,
    pub content: String,
    pub clipped_before: bool,
    pub clipped_after: bool,
    pub score: f64,
}

pub(crate) fn rank<'a>(
    chunks: impl IntoIterator<Item = Chunk<'a>>,
    query: &str,
    limit: usize,
    budget: Budget,
) -> Result<Vec<Region>> {
    let expression = validate(query, limit)?;
    budget.check()?;
    let mut db = Connection::open_in_memory()?;
    db.pragma_update(None, "page_size", 4096)?;
    db.pragma_update(None, "max_page_count", (DATABASE_BYTES / 4096) as i64)?;
    db.pragma_update(None, "cache_size", -256)?;
    db.pragma_update(None, "temp_store", "MEMORY")?;
    db.pragma_update(None, "hard_heap_limit", 64 * 1024 * 1024)?;
    db.progress_handler(1000, Some(move || budget.check().is_err()))?;
    db.execute_batch(
        "CREATE VIRTUAL TABLE corpus USING fts5(body, position UNINDEXED, detail=full);",
    )?;
    let mut source = Vec::new();
    let mut expanded_bytes = 0;
    let tx = db.transaction()?;
    {
        let mut insert = tx.prepare("INSERT INTO corpus(rowid,body,position) VALUES (?1,?2,?3)")?;
        for chunk in chunks {
            budget.check()?;
            ensure!(
                source.len() < MAX_CHUNKS,
                "Output search exceeds 8,192 chunks; use output page."
            );
            ensure!(
                chunk.text.len() <= CHUNK_BYTES
                    && chunk.text.bytes().filter(|&b| b == b'\n').count() <= CHUNK_LINES,
                "Temporary ranking chunk exceeds its byte or line budget."
            );
            let expanded = tokens::searchable(chunk.text);
            expanded_bytes += expanded.len();
            ensure!(
                expanded.len() <= 64 * 1024 && expanded_bytes <= EXPANDED_BYTES,
                "Output search token expansion exceeds its 64 KiB chunk or 32 MiB corpus budget; use output page."
            );
            insert.execute(params![
                source.len() as i64 + 1,
                expanded,
                chunk.start_byte as i64
            ])?;
            source.push(chunk);
        }
    }
    tx.commit()?;
    budget.check()?;
    let mut query_stmt = db.prepare("SELECT rowid, bm25(corpus) FROM corpus WHERE corpus MATCH ?1 ORDER BY bm25(corpus), CAST(position AS INTEGER), rowid LIMIT ?2")?;
    let hits = query_stmt.query_map(params![expression, limit as i64], |r| {
        Ok((r.get::<_, u32>(0)? as usize, r.get::<_, f64>(1)?))
    })?;
    let terms: std::collections::BTreeSet<_> = tokens::tokens(query).into_iter().collect();
    let terms: Vec<_> = terms.into_iter().take(32).collect();
    let mut result = Vec::new();
    for hit in hits {
        budget.check()?;
        let (id, score) = hit?;
        result.push(excerpt(source[id - 1], &terms, -score));
    }
    budget.check()?;
    Ok(result)
}

fn excerpt(chunk: Chunk<'_>, terms: &[String], score: f64) -> Region {
    // Pick the line with the most query components, then center on its first match.
    let mut best = (0, 0);
    let mut offset = 0;
    for line in chunk.text.split_inclusive('\n') {
        let lower = line.to_lowercase();
        let count = terms
            .iter()
            .filter(|term| lower.contains(term.as_str()))
            .count();
        if count > best.0 {
            best = (count, offset);
        }
        offset += line.len();
    }
    let line = chunk.text[best.1..]
        .split_inclusive('\n')
        .next()
        .unwrap_or("");
    // Lowercasing can change byte lengths. Locate the original character by
    // incrementally tracking its lowercase offset rather than reusing that offset.
    let lower = line.to_lowercase();
    let match_offset = terms
        .iter()
        .filter_map(|term| lower.find(term.as_str()))
        .min()
        .unwrap_or(0);
    let mut folded = 0;
    let mut original = 0;
    for (position, c) in line.char_indices() {
        if folded >= match_offset {
            original = position;
            break;
        }
        folded += c.to_lowercase().map(char::len_utf8).sum::<usize>();
        original = position;
    }
    let anchor = best.1 + original;
    let mut start = anchor.saturating_sub(256);
    while !chunk.text.is_char_boundary(start) {
        start += 1;
    }
    if let Some(pos) = chunk.text[start..anchor].rfind('\n') {
        start += pos + 1;
    }
    let mut end = (start + 1000).min(chunk.text.len());
    while !chunk.text.is_char_boundary(end) {
        end -= 1;
    }
    if let Some((pos, _)) = chunk.text[start..end].match_indices('\n').nth(7) {
        end = start + pos + 1;
    }
    let line_at =
        |pos: usize| chunk.start_line + chunk.text[..pos].bytes().filter(|&b| b == b'\n').count();
    let last_line = |start: usize, end: usize| {
        line_at(end) - usize::from(end > start && chunk.text.as_bytes()[end - 1] == b'\n')
    };
    Region {
        start_byte: chunk.start_byte as u64,
        end_byte: (chunk.start_byte + chunk.text.len()) as u64,
        start_line: chunk.start_line,
        end_line: last_line(0, chunk.text.len()),
        excerpt_start_byte: (chunk.start_byte + start) as u64,
        excerpt_end_byte: (chunk.start_byte + end) as u64,
        excerpt_start_line: line_at(start),
        excerpt_end_line: last_line(start, end),
        content: chunk.text[start..end].to_owned(),
        clipped_before: start > 0,
        clipped_after: end < chunk.text.len(),
        score,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn search(text: &str, query: &str) -> Vec<Region> {
        rank(Chunks::new(text), query, 5, Budget::new(1)).unwrap()
    }
    #[test]
    fn chunks_cover_unicode_long_lines_with_bounded_overlap() {
        let text = format!("{}{}終", "🙂é".repeat(4000), "short\n".repeat(400));
        let chunks: Vec<_> = Chunks::new(&text).collect();
        let mut end = 0;
        for chunk in chunks {
            assert!(chunk.text.len() <= CHUNK_BYTES);
            assert!(chunk.text.bytes().filter(|&b| b == b'\n').count() <= CHUNK_LINES);
            assert!(chunk.start_byte <= end);
            assert!(end - chunk.start_byte <= OVERLAP_BYTES);
            assert_eq!(
                chunk.start_line,
                1 + text[..chunk.start_byte]
                    .bytes()
                    .filter(|&b| b == b'\n')
                    .count()
            );
            end = chunk.start_byte + chunk.text.len();
        }
        assert_eq!(end, text.len());
    }
    #[test]
    fn diagnostic_identifiers_punctuation_and_stable_source_ties() {
        for name in [
            "NodePtyModuleLoadError",
            "node-pty",
            "linux-x64",
            "constructEvent",
            "SQLITE_BUSY",
            "v24.20.0",
            "src/auth/session.ts",
        ] {
            let text = format!(
                "{}\nreal failure {name}\n{}",
                "unrelated success\n".repeat(1000),
                "unrelated success\n".repeat(1000)
            );
            let results = search(&text, &format!("\"{name}\"()*"));
            assert!(results[0].content.contains(name), "{name}");
        }
        let chunks = [
            Chunk {
                text: "same failure",
                start_byte: 100,
                start_line: 3,
            },
            Chunk {
                text: "same failure",
                start_byte: 0,
                start_line: 1,
            },
        ];
        let results = rank(chunks, "failure", 2, Budget::new(1)).unwrap();
        assert_eq!(results[0].start_byte, 0);
        assert_eq!(results[1].start_byte, 100);
        assert!(search("hello", "absent").is_empty());
        assert!(validate("***", 5).is_err());
        assert!(validate(&"x".repeat(2001), 5).is_err());
        assert!(validate("x", 0).is_err());
    }
    #[test]
    fn limits_and_deadline_fail_instead_of_ranking_a_prefix() {
        let text = "\n".repeat(MAX_CHUNKS * CHUNK_LINES);
        assert!(
            format!(
                "{:#}",
                rank(Chunks::new(&text), "x", 5, Budget::new(1)).unwrap_err()
            )
            .contains("8,192")
        );
        let expired = Budget {
            deadline: Instant::now(),
            consumer: 1,
        };
        assert!(
            format!("{:#}", rank(Chunks::new("x"), "x", 5, expired).unwrap_err())
                .contains("deadline")
        );
        let (receiver, sender) = std::os::unix::net::UnixStream::pair().unwrap();
        drop(receiver);
        use std::os::fd::AsRawFd;
        assert!(
            consumer_connected(sender.as_raw_fd())
                .unwrap_err()
                .to_string()
                .contains("cancelled")
        );
    }
    #[test]
    fn ranking_resource_failure_releases_the_temporary_database() {
        let text = "word ".repeat(CHUNK_BYTES / 5);
        let chunks = (0..MAX_CHUNKS).map(|n| Chunk {
            text: &text,
            start_byte: n * text.len(),
            start_line: n + 1,
        });
        let error = rank(chunks, "word", 1, Budget::new(1)).unwrap_err();
        let message = format!("{error:#}");
        assert!(
            message.contains("full") || message.contains("memory") || message.contains("budget"),
            "{message}"
        );
        assert_eq!(search("recovered", "recovered").len(), 1);
    }

    #[test]
    fn long_line_excerpt_centers_on_match_with_exact_locations() {
        let text = format!(
            "{} constructEvent failure {}",
            "🙂".repeat(4000),
            "é".repeat(4000)
        );
        let result = search(&text, "constructEvent").remove(0);
        assert!(result.content.contains("constructEvent"));
        assert_eq!(
            result.content,
            text[result.excerpt_start_byte as usize..result.excerpt_end_byte as usize]
        );
        assert_eq!(result.excerpt_start_line, 1);
        assert_eq!(result.excerpt_end_line, 1);
        assert!(result.content.len() <= 1000);
    }
}
