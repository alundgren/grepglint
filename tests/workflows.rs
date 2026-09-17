use grepglint::protocol::Response;
use rusqlite::Connection;
use std::{
    fs,
    io::Write,
    os::unix::{
        fs::{PermissionsExt, symlink},
        net::UnixStream,
    },
    path::{Path, PathBuf},
    process::{Command, Output},
    thread,
    time::{Duration, Instant},
};
use tempfile::TempDir;

struct Fixture {
    temp: TempDir,
    root: PathBuf,
    cache: PathBuf,
}

fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &to.join(entry.file_name()));
        } else {
            fs::copy(entry.path(), to.join(entry.file_name())).unwrap();
        }
    }
}

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(root)
        .args([
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .unwrap()
        .trim_end()
        .to_owned()
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("main");
        let cache = temp.path().join("cache");
        copy_tree(
            &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/shop"),
            &root,
        );
        git(&root, &["init", "-b", "main"]);
        git(&root, &["config", "user.name", "Fixture"]);
        git(&root, &["config", "user.email", "fixture@example.invalid"]);
        git(&root, &["add", "."]);
        git(&root, &["commit", "-m", "Initial fixture"]);
        Self { temp, root, cache }
    }
    fn command(&self, root: &Path) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_grepglint"));
        command
            .current_dir(root)
            .env("GREPGLINT_CACHE_DIR", &self.cache)
            .env("GREPGLINT_IDLE_SECONDS", "2");
        command
    }
    fn raw(&self, root: &Path, query: &str) -> Output {
        self.command(root)
            .args(["search", "--json", query])
            .output()
            .unwrap()
    }
    fn search(&self, root: &Path, query: &str) -> Response {
        let output = self.raw(root, query);
        assert!(
            output.status.success(),
            "{} {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }
    fn worktree(&self) -> PathBuf {
        let path = self.temp.path().join("second worktree");
        git(
            &self.root,
            &["worktree", "add", "-b", "feature", path.to_str().unwrap()],
        );
        path
    }
    fn stop(&self) {
        if let Ok(text) = fs::read_to_string(self.cache.join("daemon.pid")) {
            let pid: i32 = text.parse().unwrap();
            // This PID belongs to the daemon started with this test's private cache.
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
            let lock = fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(self.cache.join("daemon.lock"))
                .unwrap();
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                if fs2::FileExt::try_lock_exclusive(&lock).is_ok() {
                    break;
                }
                assert!(Instant::now() < deadline);
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop();
    }
}

#[test]
fn a_full_cache_rolls_back_and_the_next_small_query_recovers() {
    let fixture = Fixture::new();
    fs::create_dir(&fixture.cache).unwrap();
    let maximum = 512 * 1024;
    let mut index = grepglint::index::Index::open(&fixture.cache, maximum).unwrap();
    let request = grepglint::protocol::Request {
        version: 1,
        cwd: fixture.root.to_str().unwrap().to_owned(),
        query: "refresh validation".into(),
        limit: 5,
    };
    let first = index.search(&request).unwrap();
    assert!(!first.results.is_empty());
    let large: String = (0..8000)
        .map(|i| format!("cacheoverflow{i:08x} extra{i:08x} field{i:08x}\n"))
        .collect();
    assert!(large.len() < grepglint::chunks::MAX_FILE_BYTES);
    fs::write(fixture.root.join("src/overflow.txt"), large).unwrap();
    let error = index.search(&request).unwrap_err();
    assert!(format!("{error:#}").contains("full"), "{error:#}");
    assert!(
        fs::metadata(fixture.cache.join("index.sqlite"))
            .unwrap()
            .len()
            <= maximum
    );
    fs::remove_file(fixture.root.join("src/overflow.txt")).unwrap();
    let recovered = index.search(&request).unwrap();
    assert_eq!(recovered.stats.overlay_entries, 0);
    assert_eq!(recovered.stats.cached_contents, first.stats.cached_contents);
    assert!(!recovered.results.is_empty());
}

#[test]
fn old_worktree_registrations_expire_automatically() {
    let fixture = Fixture::new();
    fixture.search(&fixture.root, "refresh");
    let worktree = fixture.worktree();
    fixture.search(&worktree, "refresh");
    fixture.stop();
    let db = Connection::open(fixture.cache.join("index.sqlite")).unwrap();
    db.execute(
        "UPDATE worktrees SET last_seen=0 WHERE root=?",
        [worktree.to_str().unwrap()],
    )
    .unwrap();
    let result = fixture.search(&fixture.root, "refresh");
    assert_eq!(result.stats.evicted_worktrees, 1);
    let count: i64 = db
        .query_row("SELECT count(*) FROM worktrees", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1);
    assert_eq!(fixture.search(&worktree, "refresh").stats.blobs_parsed, 0);
}

#[test]
fn same_blob_at_two_paths_has_one_code_index_and_two_path_matches() {
    let fixture = Fixture::new();
    let first = fixture.search(&fixture.root, "revocationLedger");
    fs::copy(
        fixture.root.join("src/auth/refresh-token.ts"),
        fixture.root.join("src/alternate.ts"),
    )
    .unwrap();
    git(&fixture.root, &["add", "."]);
    git(&fixture.root, &["commit", "-m", "Add alternate path"]);
    let second = fixture.search(&fixture.root, "revocationLedger");
    assert_eq!(second.stats.blobs_parsed, 0);
    assert_eq!(second.stats.cached_chunks, first.stats.cached_chunks);
    assert_eq!(second.results.len(), 2);
    assert_eq!(
        fixture.search(&fixture.root, "alternate").results[0].path,
        "src/alternate.ts"
    );
    git(&fixture.root, &["rm", "src/alternate.ts"]);
    git(&fixture.root, &["commit", "-m", "Remove alternate path"]);
    assert!(
        fixture
            .search(&fixture.root, "alternate")
            .results
            .is_empty()
    );
}

#[test]
fn replacement_refs_cannot_change_the_content_associated_with_a_blob_sha() {
    let fixture = Fixture::new();
    let original = git(
        &fixture.root,
        &["rev-parse", "HEAD:src/auth/refresh-token.ts"],
    );
    let replacement = fixture.temp.path().join("replacement.ts");
    fs::write(&replacement, "export const replacementobsidian = true;\n").unwrap();
    let substitute = git(
        &fixture.root,
        &["hash-object", "-w", replacement.to_str().unwrap()],
    );
    git(&fixture.root, &["replace", &original, &substitute]);
    assert!(git(&fixture.root, &["cat-file", "blob", &original]).contains("replacementobsidian"));
    assert!(
        fixture
            .search(&fixture.root, "replacementobsidian")
            .results
            .is_empty()
    );
    assert_eq!(
        fixture
            .search(&fixture.root, "revocationLedger")
            .results
            .len(),
        1
    );
}

#[test]
fn worktrees_share_blobs_and_only_current_regions_are_searchable() {
    let fixture = Fixture::new();
    let first = fixture.search(&fixture.root.join("src/auth"), "refresh token validation");
    assert_eq!(first.stats.blobs_parsed, 6);
    assert_eq!(
        first.results[0].symbol.as_deref(),
        Some("validateRefreshToken")
    );
    assert_eq!(first.results[0].path, "src/auth/refresh-token.ts");
    let second = fixture.search(&fixture.root, "refresh token validation");
    assert_eq!(second.stats.blobs_parsed, 0);
    assert_eq!(second.stats.paths_updated, 0);
    assert!(!second.stats.head_changed);
    let worktree = fixture.worktree();
    let shared = fixture.search(&worktree, "refresh token validation");
    assert_eq!(shared.repository, first.repository);
    assert_ne!(shared.worktree, first.worktree);
    assert_eq!(shared.stats.blobs_parsed, 0);
    assert_eq!(shared.stats.cached_chunks, first.stats.cached_chunks);
    fs::write(
        worktree.join("src/auth/branch.ts"),
        "export function branchOnlyCredential() { return 'branchamber'; }\n",
    )
    .unwrap();
    git(&worktree, &["add", "."]);
    git(&worktree, &["commit", "-m", "Add credential handler"]);
    let branch = fixture.search(&worktree, "branchamber");
    assert_eq!(branch.stats.blobs_parsed, 1);
    assert_eq!(branch.stats.paths_updated, 1);
    assert_eq!(branch.results.len(), 1);
    assert!(
        fixture
            .search(&fixture.root, "branchamber")
            .results
            .is_empty()
    );
    git(&worktree, &["reset", "--hard", "HEAD~"]);
    assert!(fixture.search(&worktree, "branchamber").results.is_empty());
}

#[test]
fn overlay_tracks_edits_reverts_deletions_staging_untracked_and_renames() {
    let fixture = Fixture::new();
    let worktree = fixture.worktree();
    fixture.search(&fixture.root, "refresh");
    fixture.search(&worktree, "refresh");
    let path = worktree.join("src/auth/refresh-token.ts");
    fs::write(&path, "export function dirtyquartz() { return true; }\n").unwrap();
    let dirty = fixture.search(&worktree, "dirtyquartz");
    assert_eq!(dirty.stats.overlay_files_parsed, 1);
    assert_eq!(dirty.results.len(), 1);
    assert!(dirty.results[0].content_identity.starts_with("worktree:"));
    assert!(
        fixture
            .search(&worktree, "revocationLedger")
            .results
            .is_empty()
    );
    assert!(
        fixture
            .search(&fixture.root, "dirtyquartz")
            .results
            .is_empty()
    );
    fs::write(&path, "export function neweropal() { return true; }\n").unwrap();
    assert_eq!(
        fixture
            .search(&worktree, "neweropal")
            .stats
            .overlay_files_parsed,
        1
    );
    assert!(fixture.search(&worktree, "dirtyquartz").results.is_empty());
    git(&worktree, &["restore", "src/auth/refresh-token.ts"]);
    let reverted = fixture.search(&worktree, "neweropal");
    assert!(reverted.results.is_empty());
    assert_eq!(reverted.stats.overlay_entries, 0);
    fs::remove_file(&path).unwrap();
    assert!(
        fixture
            .search(&worktree, "revocationLedger")
            .results
            .is_empty()
    );
    git(&worktree, &["restore", "src/auth/refresh-token.ts"]);
    fs::write(
        worktree.join("src/new file\nwith newline.ts"),
        "export const untrackedberyl = true;\n",
    )
    .unwrap();
    assert_eq!(
        fixture.search(&worktree, "untrackedberyl").results[0].path,
        "src/new file\nwith newline.ts"
    );
    git(&worktree, &["add", "."]);
    assert_eq!(fixture.search(&worktree, "untrackedberyl").results.len(), 1);
    git(
        &worktree,
        &["mv", "src/new file\nwith newline.ts", "src/renamed.ts"],
    );
    assert_eq!(
        fixture.search(&worktree, "untrackedberyl").results[0].path,
        "src/renamed.ts"
    );
    // The index may differ from HEAD while the on-disk file equals HEAD.
    fs::write(&path, "export const stagedtopaz = true;\n").unwrap();
    git(&worktree, &["add", "src/auth/refresh-token.ts"]);
    let original = fs::read(fixture.root.join("src/auth/refresh-token.ts")).unwrap();
    fs::write(&path, original).unwrap();
    assert!(fixture.search(&worktree, "stagedtopaz").results.is_empty());
    assert_eq!(
        fixture.search(&worktree, "revocationLedger").results.len(),
        1
    );
}

#[test]
fn amended_rebased_and_pruned_history_does_not_require_ancestry() {
    let fixture = Fixture::new();
    let branch = fixture.worktree();
    fs::write(
        branch.join("src/revision.ts"),
        "export const originalzircon = true;\n",
    )
    .unwrap();
    git(&branch, &["add", "."]);
    git(&branch, &["commit", "-m", "Revision one"]);
    fixture.search(&branch, "originalzircon");
    fs::write(
        branch.join("src/revision.ts"),
        "export const amendedcobalt = true;\n",
    )
    .unwrap();
    git(&branch, &["add", "."]);
    git(&branch, &["commit", "--amend", "--no-edit"]);
    let amended = fixture.search(&branch, "amendedcobalt");
    assert_eq!(amended.stats.blobs_parsed, 1);
    assert!(fixture.search(&branch, "originalzircon").results.is_empty());
    fs::write(
        fixture.root.join("src/upstream.ts"),
        "export const upstreamjade = true;\n",
    )
    .unwrap();
    git(&fixture.root, &["add", "."]);
    git(&fixture.root, &["commit", "-m", "Upstream change"]);
    git(&branch, &["rebase", "main"]);
    let rebased = fixture.search(&branch, "upstreamjade");
    assert_eq!(rebased.stats.blobs_parsed, 1);
    let old = git(&branch, &["rev-parse", "HEAD"]);
    git(&branch, &["reset", "--hard", "main"]);
    // Destructive Git operations are confined to this disposable fixture.
    git(
        &fixture.root,
        &["reflog", "expire", "--expire=now", "--all"],
    );
    git(&fixture.root, &["gc", "--prune=now"]);
    assert!(
        !Command::new("git")
            .current_dir(&branch)
            .args(["cat-file", "-e", &old])
            .output()
            .unwrap()
            .status
            .success()
    );
    let pruned = fixture.search(&branch, "amendedcobalt");
    assert!(pruned.results.is_empty());
    assert_eq!(pruned.stats.blobs_parsed, 0);
}

#[test]
fn simultaneous_cold_clients_share_a_daemon_and_survive_restart() {
    let fixture = Fixture::new();
    let worktree = fixture.worktree();
    let mut a = fixture
        .command(&fixture.root)
        .args(["search", "--json", "refresh validation"])
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let b = fixture
        .command(&worktree)
        .args(["search", "--json", "webhook signature"])
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = String::new();
    std::io::Read::read_to_string(a.stdout.as_mut().unwrap(), &mut output).unwrap();
    assert!(a.wait().unwrap().success());
    let b = b.wait_with_output().unwrap();
    assert!(b.status.success(), "{}", String::from_utf8_lossy(&b.stdout));
    let a: Response = serde_json::from_str(&output).unwrap();
    let b: Response = serde_json::from_slice(&b.stdout).unwrap();
    assert_eq!(a.stats.blobs_parsed + b.stats.blobs_parsed, 6);
    assert_eq!(
        b.results[0].symbol.as_deref(),
        Some("verify_webhook_signature")
    );
    fixture.stop();
    let restarted = fixture.search(&fixture.root, "refresh validation");
    assert_eq!(restarted.stats.blobs_parsed, 0);
    let deadline = Instant::now() + Duration::from_secs(5);
    while fixture.cache.join("daemon.sock").exists() {
        assert!(
            Instant::now() < deadline,
            "daemon did not shut down when idle"
        );
        thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(
        fixture
            .search(&fixture.root, "refresh validation")
            .stats
            .blobs_parsed,
        0
    );
}

#[test]
fn ignored_binary_symlink_and_large_files_do_not_enter_results() {
    let fixture = Fixture::new();
    fs::create_dir_all(fixture.root.join("node_modules/pkg")).unwrap();
    fs::write(
        fixture.root.join("node_modules/pkg/secret.ts"),
        "ignoredmica",
    )
    .unwrap();
    fs::write(fixture.root.join("src/binary.ts"), b"binaryonyx\0").unwrap();
    fs::write(fixture.root.join("src/huge.ts"), vec![b'a'; 600_000]).unwrap();
    fs::write(fixture.temp.path().join("outside.ts"), "externalgarnet").unwrap();
    symlink(
        fixture.temp.path().join("outside.ts"),
        fixture.root.join("src/link.ts"),
    )
    .unwrap();
    assert!(
        fixture
            .search(&fixture.root, "ignoredmica binaryonyx externalgarnet")
            .results
            .is_empty()
    );
    let permission = fs::metadata(&fixture.cache).unwrap().permissions().mode() & 0o777;
    assert_eq!(permission, 0o700);
    assert_eq!(
        fs::metadata(fixture.cache.join("daemon.sock"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let mut stream = UnixStream::connect(fixture.cache.join("daemon.sock")).unwrap();
    let _ = stream.write_all(&vec![b'x'; 17_000]);
    drop(stream);
    assert!(!fixture.search(&fixture.root, "refresh").results.is_empty());
}

#[test]
fn fresh_repository_without_commits_and_separate_clones_work() {
    let fixture = Fixture::new();
    fixture.search(&fixture.root, "refresh");
    let clone = fixture.temp.path().join("clone");
    git(
        fixture.temp.path(),
        &[
            "clone",
            fixture.root.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    let separate = fixture.search(&clone, "refresh");
    assert_eq!(separate.stats.blobs_parsed, 6);
    let empty = fixture.temp.path().join("empty");
    fs::create_dir(&empty).unwrap();
    git(&empty, &["init", "-b", "main"]);
    fs::write(
        empty.join("initial.ts"),
        "export const unbornjasper = true;\n",
    )
    .unwrap();
    let initial = fixture.search(&empty, "unbornjasper");
    assert!(initial.head.is_none());
    assert_eq!(initial.results.len(), 1);
}

#[test]
fn obsolete_overlays_are_collected_and_catalog_needs_no_daemon() {
    let fixture = Fixture::new();
    let output = fixture
        .command(&fixture.root)
        .args(["tools", "--json"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let catalog: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(catalog["tools"][0]["name"], "search");
    assert!(!fixture.cache.exists());
    for i in 0..20 {
        fs::write(
            fixture.root.join("src/dirty.ts"),
            format!("export const iterationmarker{i} = true;\n"),
        )
        .unwrap();
        let result = fixture.search(&fixture.root, &format!("iterationmarker{i}"));
        assert_eq!(result.results.len(), 1);
    }
    let db = Connection::open(fixture.cache.join("index.sqlite")).unwrap();
    let overlays: i64 = db
        .query_row(
            "SELECT count(*) FROM contents WHERE identity LIKE 'worktree:%'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(overlays, 1);
    assert!(
        fs::metadata(fixture.cache.join("index.sqlite"))
            .unwrap()
            .len()
            < 128 * 1024 * 1024
    );
}
