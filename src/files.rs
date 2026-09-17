use crate::git::{self, Repository, WorktreeFile};
use anyhow::{Context, Result, bail, ensure};
use ignore::gitignore::{Gitignore, GitignoreBuilder};

const POLICY_VERSION: &str = "text-files-v2";
const CONFIG: &str = ".grepglintignore";
const MAX_CONFIG_BYTES: usize = 16 * 1024;
const MAX_CONFIG_LINES: usize = 256;

pub struct FilePolicy {
    overrides: Gitignore,
    pub fingerprint: String,
}

impl FilePolicy {
    pub fn signature(&self, dirty: &std::collections::BTreeMap<String, String>) -> Result<String> {
        Ok(format!(
            "{}:{}",
            self.fingerprint,
            git::identity(serde_json::to_string(dirty)?.as_bytes())
        ))
    }

    pub fn load(repo: &Repository) -> Result<Self> {
        let bytes = match git::read_worktree_file(repo, CONFIG)? {
            WorktreeFile::Missing => Vec::new(),
            WorktreeFile::Contents(bytes) => bytes,
            WorktreeFile::Skipped(reason) => bail!("Cannot read {CONFIG}: {reason:?}"),
        };
        ensure!(
            bytes.len() <= MAX_CONFIG_BYTES,
            "{CONFIG} exceeds the 16 KiB limit; shorten its rules or use rg."
        );
        let text = std::str::from_utf8(&bytes).context(".grepglintignore must be UTF-8")?;
        ensure!(
            text.lines().count() <= MAX_CONFIG_LINES,
            "{CONFIG} exceeds the 256-line limit; shorten its rules or use rg."
        );
        let mut builder = GitignoreBuilder::new(&repo.root);
        for (index, line) in text.lines().enumerate() {
            builder
                .add_line(Some(repo.root.join(CONFIG)), line)
                .with_context(|| format!("Invalid {CONFIG} rule on line {}", index + 1))?;
        }
        Ok(Self {
            overrides: builder.build()?,
            fingerprint: git::identity(format!("{POLICY_VERSION}\n{text}").as_bytes()),
        })
    }

    pub fn excludes(&self, path: &str) -> bool {
        if path == CONFIG || path.split('/').any(|part| part == ".git") {
            return true;
        }
        let matched = self.overrides.matched_path_or_any_parents(path, false);
        if matched.is_whitelist() {
            return false;
        }
        if matched.is_ignore() {
            return true;
        }
        let mut parts = path.rsplit('/');
        let file = parts.next().unwrap_or(path);
        [
            "pnpm-lock.yaml",
            "package-lock.json",
            "yarn.lock",
            "bun.lock",
            "Cargo.lock",
        ]
        .contains(&file)
            || parts.any(|part| {
                [
                    "node_modules",
                    "vendor",
                    "dist",
                    "build",
                    "coverage",
                    "target",
                    ".next",
                    ".cache",
                ]
                .contains(&part)
            })
    }
}
