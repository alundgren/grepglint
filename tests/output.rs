use serde_json::Value;
use std::{
    fs,
    io::Write,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use tempfile::TempDir;

struct Fixture {
    root: TempDir,
}
impl Fixture {
    fn new() -> Self {
        Self {
            root: tempfile::tempdir().unwrap(),
        }
    }
    fn command(&self) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_grepglint"));
        c.current_dir(self.root.path())
            .env("GREPGLINT_CACHE_DIR", self.root.path().join("cache"));
        c
    }
    fn bounce(&self, input: &[u8]) -> std::process::Output {
        let mut child = self
            .command()
            .args(["output", "bounce"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdin = child.stdin.take().unwrap();
        let input = input.to_vec();
        let writer = std::thread::spawn(move || {
            let _ = stdin.write_all(&input);
        });
        let result = child.wait_with_output().unwrap();
        writer.join().unwrap();
        result
    }
    fn handle(&self, input: &[u8]) -> String {
        let o = self.bounce(input);
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        assert!(o.stdout.len() <= 8192);
        String::from_utf8(o.stdout)
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .trim_end_matches(':')
            .to_owned()
    }
    fn page(&self, handle: &str, cursor: Option<&str>) -> std::process::Output {
        let mut c = self.command();
        c.args(["output", "page", handle, "--json"]);
        if let Some(cursor) = cursor {
            c.args(["--cursor", cursor]);
        }
        c.output().unwrap()
    }
}

#[test]
fn pass_through_is_exact_and_does_not_create_cache() {
    let f = Fixture::new();
    for bytes in [vec![], b"a\r\n\x1b[31m\t".to_vec(), vec![b'x'; 4096]] {
        let o = f.bounce(&bytes);
        assert!(o.status.success());
        assert_eq!(o.stdout, bytes);
        assert!(!f.root.path().join("cache").exists());
    }
    let handle = f.handle(&vec![b'x'; 4097]);
    assert_eq!(handle.len(), 32);
    assert!(!f.root.path().join("cache/daemon.sock").exists());
}

#[test]
fn pages_reconstruct_unicode_controls_crlf_and_long_lines() {
    let f = Fixture::new();
    let input = format!(
        "{}é🙂\r\n{}\x1b[31m\t\x07\r\n終",
        "x".repeat(3583),
        "long".repeat(3500)
    )
    .into_bytes();
    let handle = f.handle(&input);
    let mut cursor = None;
    let mut reconstructed = Vec::new();
    loop {
        let o = f.page(&handle, cursor.as_deref());
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stdout));
        assert!(o.stdout.len() <= 65536);
        let p: Value = serde_json::from_slice(&o.stdout).unwrap();
        assert_eq!(f.page(&handle, cursor.as_deref()).stdout, o.stdout);
        reconstructed.extend_from_slice(p["content"].as_str().unwrap().as_bytes());
        cursor = p["next_cursor"].as_str().map(str::to_owned);
        if cursor.is_none() {
            assert_eq!(p["end_of_output"], true);
            break;
        }
    }
    assert_eq!(input, reconstructed);
    let human = f
        .command()
        .args(["output", "page", &handle])
        .output()
        .unwrap();
    assert!(!human.stdout.contains(&27));
    let other = f.handle(&vec![b'y'; 9000]);
    let p: Value = serde_json::from_slice(&f.page(&handle, None).stdout).unwrap();
    assert!(!f.page(&other, p["next_cursor"].as_str()).status.success());
    for c in ["bad", &"a".repeat(10000)] {
        assert!(!f.page(&handle, Some(c)).status.success());
    }
}

#[test]
fn invalid_text_and_oversize_never_publish_handles() {
    let f = Fixture::new();
    for suffix in [&[0][..], &[255][..], &[0xe2, 0x82][..]] {
        let mut input = vec![b'a'; 20000];
        input.extend(suffix);
        let o = f.bounce(&input);
        assert!(!o.status.success());
        assert!(o.stdout.is_empty());
    }
    let o = f.bounce(&vec![b'x'; 8 * 1024 * 1024 + 1]);
    assert!(!o.status.success());
    assert!(o.stdout.is_empty());
    let db =
        rusqlite::Connection::open(f.root.path().join("cache/output-v1/output.sqlite")).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM outputs", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn restart_purge_permissions_and_unrelated_files() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let handle = f.handle(&vec![b'a'; 10000]);
    let unrelated = f.root.path().join("cache/output-v1/keep.txt");
    fs::write(&unrelated, b"unrelated").unwrap();
    for name in [
        "output.sqlite",
        "output.sqlite-journal",
        "ownership.json",
        "capture-0.lock",
        "capture-1.lock",
    ] {
        assert_eq!(
            fs::metadata(f.root.path().join("cache/output-v1").join(name))
                .unwrap()
                .permissions()
                .mode()
                & 0o077,
            0
        );
    }
    assert!(f.page(&handle, None).status.success());
    for _ in 0..2 {
        let p = f.command().args(["output", "purge"]).output().unwrap();
        assert!(p.status.success(), "{}", String::from_utf8_lossy(&p.stderr));
    }
    assert_eq!(fs::read(unrelated).unwrap(), b"unrelated");
    assert!(!f.page(&handle, None).status.success());
}

#[test]
fn replaced_storage_is_preserved() {
    use std::os::unix::fs::symlink;
    for name in [
        "output.sqlite",
        "output.sqlite-journal",
        "capture-0.lock",
        "ownership.json",
    ] {
        let f = Fixture::new();
        let handle = f.handle(&vec![b'a'; 6000]);
        let path = f.root.path().join("cache/output-v1").join(name);
        fs::remove_file(&path).unwrap();
        let sentinel = f.root.path().join("sentinel");
        fs::write(&sentinel, b"keep").unwrap();
        symlink(&sentinel, &path).unwrap();
        assert!(!f.page(&handle, None).status.success());
        assert!(
            !f.command()
                .args(["output", "purge"])
                .status()
                .unwrap()
                .success()
        );
        assert_eq!(fs::read(sentinel).unwrap(), b"keep");
    }
}

#[test]
fn stalled_capture_does_not_block_clients_and_crash_recovers() {
    let f = Fixture::new();
    let mut children = Vec::new();
    for _ in 0..2 {
        let mut c = f
            .command()
            .args(["output", "bounce"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        c.stdin
            .as_mut()
            .unwrap()
            .write_all(&vec![b'x'; 12000])
            .unwrap();
        children.push(c);
        std::thread::sleep(Duration::from_millis(150));
    }
    let start = Instant::now();
    let third = f.bounce(&vec![b'y'; 9000]);
    assert!(!third.status.success());
    assert!(start.elapsed() < Duration::from_secs(5));
    let purge = f.command().args(["output", "purge"]).output().unwrap();
    assert!(!purge.status.success());
    for c in &mut children {
        c.kill().unwrap();
        c.wait().unwrap();
    }
    let handle = f.handle(&vec![b'z'; 8000]);
    assert!(f.page(&handle, None).status.success());
    let db =
        rusqlite::Connection::open(f.root.path().join("cache/output-v1/output.sqlite")).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM outputs WHERE committed=0", [], |r| {
            r.get::<_, i64>(0)
        })
        .unwrap(),
        0
    );
}

#[test]
fn broken_downstream_pipe_cancels_capture_promptly() {
    let f = Fixture::new();
    let mut c = f
        .command()
        .args(["output", "bounce"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    c.stdin
        .as_mut()
        .unwrap()
        .write_all(&vec![b'x'; 9000])
        .unwrap();
    drop(c.stdout.take());
    let start = Instant::now();
    use wait_timeout::ChildExt;
    let status = c.wait_timeout(Duration::from_secs(3)).unwrap();
    if status.is_none() {
        c.kill().unwrap();
    }
    assert!(status.is_some_and(|s| !s.success()));
    assert!(start.elapsed() < Duration::from_secs(3));
}

#[test]
fn stalled_input_has_idle_deadline() {
    let f = Fixture::new();
    let mut c = f
        .command()
        .args(["output", "bounce"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = c.stdin.take().unwrap();
    stdin.write_all(&vec![b'x'; 9000]).unwrap();
    let start = Instant::now();
    let o = c.wait_with_output().unwrap();
    drop(stdin);
    assert!(!o.status.success());
    assert!(o.stdout.is_empty());
    assert!(start.elapsed() < Duration::from_secs(13));
}

#[test]
fn legacy_daemon_socket_is_not_contacted_by_output_commands() {
    use std::os::unix::{fs::PermissionsExt, net::UnixListener};
    let f = Fixture::new();
    let cache = f.root.path().join("cache");
    fs::create_dir(&cache).unwrap();
    fs::set_permissions(&cache, fs::Permissions::from_mode(0o700)).unwrap();
    let listener = UnixListener::bind(cache.join("daemon.sock")).unwrap();
    listener.set_nonblocking(true).unwrap();
    let handle = f.handle(&vec![b'x'; 6000]);
    assert!(f.page(&handle, None).status.success());
    assert!(
        f.command()
            .args(["output", "purge"])
            .output()
            .unwrap()
            .status
            .success()
    );
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    assert!(cache.join("daemon.sock").exists());
}

#[test]
fn repository_search_and_idle_restart_work_during_capture() {
    let f = Fixture::new();
    assert!(
        Command::new("git")
            .args(["init", "-q"])
            .current_dir(f.root.path())
            .status()
            .unwrap()
            .success()
    );
    fs::write(
        f.root.path().join("auth.rs"),
        "fn validate_refresh_token() { /* refresh token validation */ }\n",
    )
    .unwrap();
    fs::write(f.root.path().join(".gitignore"), "cache/\n").unwrap();
    let handle = f.handle(&vec![b'x'; 7000]);
    let mut c = f
        .command()
        .args(["output", "bounce"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    c.stdin
        .as_mut()
        .unwrap()
        .write_all(&vec![b'x'; 9000])
        .unwrap();
    let start = Instant::now();
    let search = f
        .command()
        .env("GREPGLINT_IDLE_SECONDS", "1")
        .args(["search", "refresh token", "--json"])
        .output()
        .unwrap();
    assert!(
        search.status.success(),
        "{}",
        String::from_utf8_lossy(&search.stdout)
    );
    assert!(start.elapsed() < Duration::from_secs(30));
    c.kill().unwrap();
    c.wait().unwrap();
    std::thread::sleep(Duration::from_millis(1300));
    assert!(!f.root.path().join("cache/daemon.sock").exists());
    assert!(f.page(&handle, None).status.success());
    let search = f
        .command()
        .env("GREPGLINT_IDLE_SECONDS", "1")
        .args(["search", "refresh token", "--json"])
        .output()
        .unwrap();
    assert!(search.status.success());
    std::thread::sleep(Duration::from_millis(1300));
}

#[test]
fn interrupted_initialization_recovers_without_manual_deletion() {
    use std::os::unix::process::CommandExt;
    for limit in [0, 100] {
        let f = Fixture::new();
        let mut command = f.command();
        unsafe {
            command.pre_exec(move || {
                let bound = libc::rlimit {
                    rlim_cur: limit,
                    rlim_max: limit,
                };
                if libc::setrlimit(libc::RLIMIT_FSIZE, &bound) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command
            .args(["output", "bounce"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(&vec![b'x'; 6000])
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        let handle = f.handle(&vec![b'x'; 6000]);
        assert!(f.page(&handle, None).status.success());
        let leftovers = fs::read_dir(f.root.path().join("cache"))
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().starts_with("output-init-"))
            .count();
        assert_eq!(leftovers, 0);
    }
}

#[test]
fn legitimate_symlinked_ancestor_works_and_final_file_symlinks_fail() {
    use std::os::unix::fs::symlink;
    let f = Fixture::new();
    let real = f.root.path().join("real");
    fs::create_dir(&real).unwrap();
    let alias = f.root.path().join("alias");
    symlink(&real, &alias).unwrap();
    let mut c = f.command();
    c.env("GREPGLINT_CACHE_DIR", alias.join("cache"));
    let mut child = c
        .args(["output", "bounce"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(&vec![b'x'; 6000])
        .unwrap();
    let o = child.wait_with_output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let handle = String::from_utf8(o.stdout)
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .trim_end_matches(':')
        .to_owned();
    let path = real.join("cache/output-v1/output.sqlite");
    fs::rename(&path, real.join("saved.sqlite")).unwrap();
    symlink(real.join("saved.sqlite"), &path).unwrap();
    assert!(
        !f.command()
            .env("GREPGLINT_CACHE_DIR", alias.join("cache"))
            .args(["output", "page", &handle, "--json"])
            .output()
            .unwrap()
            .status
            .success()
    );
}

impl Fixture {
    fn search(&self, handle: &str, query: &str) -> std::process::Output {
        self.command()
            .env("GREPGLINT_IDLE_SECONDS", "1")
            .args(["output", "search", handle, query, "--json"])
            .output()
            .unwrap()
    }
    fn search_json(&self, handle: &str, query: &str) -> Value {
        let output = self.search(handle, query);
        assert!(
            output.status.success(),
            "{} {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(output.stdout.len() <= 65536);
        serde_json::from_slice(&output.stdout).unwrap()
    }
}

#[test]
fn search_finds_middle_diagnostics_and_poor_queries_preserve_exact_retrieval() {
    let f = Fixture::new();
    let input = format!(
        "{}\nNodePtyModuleLoadError node-pty linux-x64 constructEvent SQLITE_BUSY v24.20.0 src/auth/session.ts\n{}",
        "test component ... ok\n".repeat(2000),
        "warning: unused compiler variable\n".repeat(2000)
    );
    let handle = f.handle(input.as_bytes());
    let other = f.handle(
        format!(
            "{} otherHandleSecret",
            "unrelated information\n".repeat(500)
        )
        .as_bytes(),
    );
    for query in [
        "NodePtyModuleLoadError",
        "node-pty",
        "linux-x64",
        "constructEvent",
        "SQLITE_BUSY",
        "v24.20.0",
        "src/auth/session.ts",
    ] {
        let result = f.search_json(&handle, query);
        assert_eq!(result["handle"], handle);
        let first = &result["results"][0];
        let text = first["content"].as_str().unwrap();
        assert!(text.contains(query), "{query}: {text}");
        assert_eq!(
            text,
            &input[first["excerpt_start_byte"].as_u64().unwrap() as usize
                ..first["excerpt_end_byte"].as_u64().unwrap() as usize]
        );
        assert_eq!(first["excerpt_start_line"], 2002);
        let command: Vec<_> = first["page_command"]
            .as_str()
            .unwrap()
            .split_whitespace()
            .collect();
        let page = f.command().args(&command[1..]).output().unwrap();
        assert!(page.status.success());
        let page: Value = serde_json::from_slice(&page.stdout).unwrap();
        assert!(page["content"].as_str().unwrap().contains(query));
    }
    assert!(
        f.search_json(&other, "NodePtyModuleLoadError")["results"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let missed = f.search_json(&handle, "test component");
    assert!(missed["results"].as_array().unwrap().iter().all(|r| {
        !r["content"]
            .as_str()
            .unwrap()
            .contains("NodePtyModuleLoadError")
    }));
    let empty = f.search_json(&handle, "nonexistentDiagnostic");
    assert!(empty["results"].as_array().unwrap().is_empty());
    assert!(empty["page_command"].as_str().unwrap().contains(&handle));
    let mut restored = Vec::new();
    let mut cursor = None;
    loop {
        let output = f.page(&handle, cursor.as_deref());
        assert!(output.status.success());
        let page: Value = serde_json::from_slice(&output.stdout).unwrap();
        restored.extend_from_slice(page["content"].as_str().unwrap().as_bytes());
        cursor = page["next_cursor"].as_str().map(str::to_owned);
        if cursor.is_none() {
            break;
        }
    }
    assert_eq!(restored, input.as_bytes());
    assert!(!f.root.path().join(".git").exists());
    let db = rusqlite::Connection::open(f.root.path().join("cache/index.sqlite")).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM repositories", [], |r| r
            .get::<_, u32>(0))
            .unwrap(),
        0
    );
}

#[test]
fn search_limits_controls_purge_and_maximum_output() {
    let f = Fixture::new();
    let input = format!(
        "{} constructEvent\x1b[31m\t\u{202e} {}",
        "🙂".repeat(3000),
        "é".repeat(3000)
    );
    let handle = f.handle(input.as_bytes());
    let result = f.search_json(&handle, "constructEvent");
    assert!(
        result["results"][0]["content"]
            .as_str()
            .unwrap()
            .contains("constructEvent")
    );
    let human = f
        .command()
        .args(["output", "search", &handle, "constructEvent"])
        .output()
        .unwrap();
    assert!(human.status.success());
    assert!(!human.stdout.contains(&0x1b));
    assert!(!String::from_utf8_lossy(&human.stdout).contains('\u{202e}'));
    for query in ["***".to_owned(), "x".repeat(2001)] {
        let output = f.search(&handle, &query);
        assert!(!output.status.success());
        assert!(serde_json::from_slice::<Value>(&output.stdout).unwrap()["error"].is_string());
    }
    assert!(!f.search("malformed", "x").status.success());
    let limit = f
        .command()
        .args(["output", "search", &handle, "x", "--limit", "21", "--json"])
        .output()
        .unwrap();
    assert!(!limit.status.success());
    let crowded = f.handle(&b"\n".repeat(600_000));
    let failed = f.search(&crowded, "anything");
    assert!(!failed.status.success());
    assert!(String::from_utf8_lossy(&failed.stdout).contains("8,192"));
    assert!(f.page(&crowded, None).status.success());
    let mut maximum = vec![b'z'; 8 * 1024 * 1024];
    let diagnostic = b" constructEvent ";
    let start = maximum.len() / 2;
    maximum[start..start + diagnostic.len()].copy_from_slice(diagnostic);
    let max_handle = f.handle(&maximum);
    assert!(
        f.search_json(&max_handle, "constructEvent")["results"][0]["content"]
            .as_str()
            .unwrap()
            .contains("constructEvent")
    );
    assert!(
        f.command()
            .args(["output", "purge"])
            .output()
            .unwrap()
            .status
            .success()
    );
    assert!(!f.search(&handle, "constructEvent").status.success());
}

#[test]
fn legacy_search_refusal_keeps_exact_paging_and_socket() {
    use std::{
        io::{BufRead, BufReader},
        os::unix::{fs::PermissionsExt, net::UnixListener},
    };
    let f = Fixture::new();
    let handle = f.handle(&vec![b'x'; 6000]);
    let socket = f.root.path().join("cache/daemon.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = String::new();
        BufReader::new(stream.try_clone().unwrap())
            .read_line(&mut request)
            .unwrap();
        assert!(request.contains("output_search"));
        stream
            .write_all(b"{\"status\":\"error\",\"message\":\"Unknown control request\"}\n")
            .unwrap();
    });
    let response = f.search(&handle, "x");
    assert!(!response.status.success());
    assert!(
        String::from_utf8_lossy(&response.stdout).contains("exact output page remains available")
    );
    server.join().unwrap();
    assert!(socket.exists());
    assert!(f.page(&handle, None).status.success());
}

#[test]
fn disconnect_cancels_daemon_ranking_and_preserves_original() {
    let f = Fixture::new();
    let handle = f.handle(&vec![b'z'; 8 * 1024 * 1024]);
    let before = f.page(&handle, None).stdout;
    let mut child = f
        .command()
        .env("GREPGLINT_IDLE_SECONDS", "1")
        .args(["output", "search", &handle, "z", "--json"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let observer =
        rusqlite::Connection::open(f.root.path().join("cache/output-v1/output.sqlite")).unwrap();
    observer.busy_timeout(Duration::ZERO).unwrap();
    let start = Instant::now();
    loop {
        match observer.execute_batch("BEGIN EXCLUSIVE") {
            Ok(()) => observer.execute_batch("ROLLBACK").unwrap(),
            Err(_) => break,
        }
        assert!(
            start.elapsed() < Duration::from_secs(5),
            "ranker never began its read transaction"
        );
        std::thread::sleep(Duration::from_millis(2));
    }
    drop(child.stdout.take());
    let cancelled = Instant::now();
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(!status.success());
            break;
        }
        assert!(cancelled.elapsed() < Duration::from_secs(3));
        std::thread::sleep(Duration::from_millis(10));
    }
    let status = f.command().args(["status", "--json"]).output().unwrap();
    assert!(status.status.success());
    assert!(cancelled.elapsed() < Duration::from_secs(3));
    assert_eq!(f.page(&handle, None).stdout, before);
    let db =
        rusqlite::Connection::open(f.root.path().join("cache/output-v1/output.sqlite")).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'corpus%'",
            [],
            |r| r.get::<_, u32>(0)
        )
        .unwrap(),
        0
    );
}

#[test]
fn search_response_limit_counts_json_escaping_and_metadata() {
    let f = Fixture::new();
    let input = format!("match{}\n", "\x01".repeat(140)).repeat(2000);
    let handle = f.handle(input.as_bytes());
    let response = f
        .command()
        .env("GREPGLINT_IDLE_SECONDS", "1")
        .args([
            "output", "search", &handle, "match", "--json", "--limit", "20",
        ])
        .output()
        .unwrap();
    assert!(!response.status.success());
    assert!(response.stdout.len() < 65536);
    assert!(
        serde_json::from_slice::<Value>(&response.stdout).unwrap()["error"]
            .as_str()
            .unwrap()
            .contains("64 KiB")
    );
    let small = f
        .command()
        .args([
            "output", "search", &handle, "match", "--json", "--limit", "1",
        ])
        .output()
        .unwrap();
    assert!(small.status.success());
    assert!(small.stdout.len() <= 65536);
    assert!(f.page(&handle, None).status.success());
}
