use serde_json::Value;
use std::{
    fs,
    os::fd::AsRawFd,
    process::{Child, Command, Output, Stdio},
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
            .env("GREPGLINT_CACHE_DIR", self.root.path().join("cache"))
            .env("GREPGLINT_IDLE_SECONDS", "1");
        c
    }
    fn exec(&self, script: &str, profile: &str) -> Command {
        let mut c = self.command();
        c.args([
            "output",
            "exec",
            "--shell",
            "/bin/sh",
            "--profile",
            profile,
            "--command",
            script,
        ]);
        c
    }
    fn input(&self, bytes: &[u8]) {
        fs::write(self.root.path().join("input"), bytes).unwrap();
    }
    fn db(&self) -> rusqlite::Connection {
        let db = rusqlite::Connection::open(self.root.path().join("cache/output-v1/output.sqlite"))
            .unwrap();
        db.busy_timeout(Duration::from_secs(3)).unwrap();
        db.pragma_update(None, "journal_mode", "PERSIST").unwrap();
        db.pragma_update(None, "journal_size_limit", 0).unwrap();
        db
    }
    fn page(&self, handle: &str, cursor: Option<&str>) -> Output {
        let mut c = self.command();
        c.args(["output", "page", handle, "--json"]);
        if let Some(cursor) = cursor {
            c.args(["--cursor", cursor]);
        }
        c.output().unwrap()
    }
    fn start_paused(&self) -> Child {
        let child = self.exec("cat input; touch ready; while [ ! -f release ]; do sleep 0.02; done; printf done; exit 7", "preview16k")
            .stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        self.wait_ready();
        child
    }
    fn wait_ready(&self) {
        let until = Instant::now() + Duration::from_secs(15);
        while !self.root.path().join("ready").exists() {
            assert!(Instant::now() < until);
            std::thread::sleep(Duration::from_millis(10));
        }
        // The producer can finish its write before the wrapper has consumed the pipe.
        while !self
            .root
            .path()
            .join("cache/output-v1/output.sqlite")
            .exists()
        {
            assert!(Instant::now() < until);
            std::thread::sleep(Duration::from_millis(10));
        }
        loop {
            if self
                .db()
                .query_row("SELECT count(*) FROM outputs WHERE committed=0", [], |r| {
                    r.get::<_, i64>(0)
                })
                .unwrap_or(0)
                > 0
            {
                break;
            }
            assert!(Instant::now() < until);
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn release(&self) {
        fs::write(self.root.path().join("release"), b"").unwrap();
    }
    fn empty(&self) {
        assert_eq!(
            self.db()
                .query_row("SELECT count(*) FROM outputs", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
}
fn metadata(o: &Output) -> Value {
    String::from_utf8_lossy(&o.stderr)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|v| v["type"] == "grepglint-command")
        .unwrap_or_else(|| panic!("missing metadata: {:?}", o))
}
fn handle(o: &Output) -> String {
    metadata(o)["handle"].as_str().unwrap().into()
}

#[test]
fn exact_small_output_shell_environment_quoting_redirects_and_status() {
    let f = Fixture::new();
    let o = f.exec("printf '%s\\r\\n' \"$VALUE\" | cat; printf err >&2; printf redirected > result; printf x >> counter; exit 23", "preview16k")
        .args(["--env", "VALUE=a 'quoted' $(touch forbidden) value", "--cwd"]).arg(f.root.path()).output().unwrap();
    assert_eq!(o.status.code(), Some(23));
    assert_eq!(o.stdout, b"a 'quoted' $(touch forbidden) value\r\nerr");
    assert!(!f.root.path().join("forbidden").exists());
    assert_eq!(fs::read(f.root.path().join("counter")).unwrap(), b"x");
    assert_eq!(
        fs::read(f.root.path().join("result")).unwrap(),
        b"redirected"
    );
    assert!(!f.root.path().join("cache").exists());
    let m = metadata(&o);
    assert_eq!(m["producer_status"], 23);
    assert_eq!(m["returned_bytes"], o.stdout.len());
    assert!(!String::from_utf8_lossy(&o.stderr).contains("quoted"));
}

#[test]
fn fixed_profiles_have_only_the_documented_threshold_difference() {
    let f = Fixture::new();
    for size in [0, 16384, 16385, 32768, 32769] {
        let input = vec![b'x'; size];
        f.input(&input);
        for (profile, threshold) in [
            ("unchanged", usize::MAX),
            ("preview16k", 16384),
            ("preview32k", 32768),
        ] {
            let o = f.exec("cat input", profile).output().unwrap();
            assert!(o.status.success(), "{:?}", o);
            let m = metadata(&o);
            assert_eq!(m["original_bytes"], size);
            assert_eq!(m["returned_bytes"], o.stdout.len());
            if size <= threshold {
                assert_eq!(o.stdout, input);
                assert!(m["handle"].is_null());
            } else {
                assert_eq!(m["capture"], "captured");
                assert!(o.stdout.len() <= 8192);
                let text = String::from_utf8(o.stdout).unwrap();
                assert!(text.starts_with("Incomplete output preview."));
                assert!(text.find("output page").unwrap() < text.find("Head:").unwrap());
            }
        }
    }
}

#[test]
fn omitted_diagnostic_search_and_repeatable_pages_reconstruct_combined_stream() {
    let f = Fixture::new();
    let input = format!(
        "{}\r\nMIDDLE_DIAGNOSTIC missingDependency\r\n{}終",
        "a\x1b[31m🙂\t\r\n".repeat(1800),
        "z\x07\n".repeat(5000)
    )
    .into_bytes();
    f.input(&input);
    let o = f
        .exec(
            "printf x >> counter; cat input; printf stderr_end >&2; exit 9",
            "preview16k",
        )
        .output()
        .unwrap();
    assert_eq!(o.status.code(), Some(9));
    let h = handle(&o);
    assert!(!String::from_utf8_lossy(&o.stdout).contains("MIDDLE_DIAGNOSTIC"));
    let search = f
        .command()
        .args(["output", "search", &h, "MIDDLE_DIAGNOSTIC", "--json"])
        .output()
        .unwrap();
    assert!(search.status.success(), "{:?}", search);
    assert!(String::from_utf8_lossy(&search.stdout).contains("MIDDLE_DIAGNOSTIC"));
    let failed = f
        .command()
        .args(["output", "search", &h, &"x".repeat(2001), "--json"])
        .output()
        .unwrap();
    assert!(!failed.status.success());
    let mut cursor = None;
    let mut recovered = Vec::new();
    loop {
        let page = f.page(&h, cursor.as_deref());
        assert!(page.status.success());
        assert_eq!(page.stdout, f.page(&h, cursor.as_deref()).stdout);
        let p: Value = serde_json::from_slice(&page.stdout).unwrap();
        recovered.extend_from_slice(p["content"].as_str().unwrap().as_bytes());
        cursor = p["next_cursor"].as_str().map(str::to_owned);
        if cursor.is_none() {
            assert_eq!(p["end_of_output"], true);
            break;
        }
    }
    assert_eq!(recovered, [input, b"stderr_end".to_vec()].concat());
    assert_eq!(fs::read(f.root.path().join("counter")).unwrap(), b"x");
    let _ = f.command().arg("shutdown").output();
}

#[test]
fn invalid_late_bytes_and_oversize_forward_every_byte_once() {
    let f = Fixture::new();
    for suffix in [&[0][..], &[255][..], &[0xe2, 0x82][..]] {
        let input = [vec![b'a'; 100000], suffix.to_vec()].concat();
        f.input(&input);
        let o = f
            .exec("cat input; printf x >> counter; exit 19", "preview16k")
            .output()
            .unwrap();
        assert_eq!(o.status.code(), Some(19));
        assert_eq!(o.stdout, input);
        assert!(metadata(&o)["handle"].is_null());
        f.empty();
    }
    let input = vec![b'b'; 8 * 1024 * 1024 + 1234];
    f.input(&input);
    let o = f
        .exec("cat input; printf x >> counter", "preview32k")
        .output()
        .unwrap();
    assert!(o.status.success());
    assert_eq!(o.stdout, input);
    assert_eq!(metadata(&o)["capture_error"], "oversized");
    f.empty();
    assert_eq!(fs::read(f.root.path().join("counter")).unwrap(), b"xxxx");
}

#[test]
fn unavailable_storage_bypasses_without_retrying_producer() {
    let f = Fixture::new();
    let input = vec![b'x'; 50000];
    f.input(&input);
    fs::write(f.root.path().join("cache"), b"unrelated").unwrap();
    let o = f
        .exec("cat input; printf x >> counter; exit 17", "preview16k")
        .output()
        .unwrap();
    assert_eq!(o.status.code(), Some(17));
    assert_eq!(o.stdout, input);
    assert_eq!(metadata(&o)["capture_error"], "storage-unavailable");
    assert_eq!(fs::read(f.root.path().join("cache")).unwrap(), b"unrelated");
    assert_eq!(fs::read(f.root.path().join("counter")).unwrap(), b"x");
}

#[test]
fn retention_failure_after_prefix_replays_spool_and_cleans_owned_state() {
    let f = Fixture::new();
    let input = vec![b'q'; 100000];
    f.input(&input);
    let child = f.start_paused();
    f.db().execute_batch("CREATE TRIGGER fail_append BEFORE INSERT ON chunks BEGIN SELECT RAISE(FAIL, 'injected full store'); END;").unwrap();
    f.release();
    let o = child.wait_with_output().unwrap();
    assert_eq!(o.status.code(), Some(7));
    assert_eq!(o.stdout, [input, b"done".to_vec()].concat());
    assert_eq!(metadata(&o)["capture_error"], "retention-failed");
    f.empty();
    assert_eq!(
        fs::read_dir(f.root.path().join("cache/output-v1"))
            .unwrap()
            .count(),
        5
    );
}

#[test]
fn yielded_capture_emits_nothing_until_final_preview_and_expiry_falls_back() {
    let f = Fixture::new();
    let input = vec![b'q'; 50000];
    f.input(&input);
    let child = f.start_paused();
    let mut poll = libc::pollfd {
        fd: child.stdout.as_ref().unwrap().as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    assert_eq!(unsafe { libc::poll(&mut poll, 1, 100) }, 0);
    f.db().execute("UPDATE outputs SET created=0", []).unwrap();
    f.release();
    let o = child.wait_with_output().unwrap();
    assert_eq!(o.status.code(), Some(7));
    assert_eq!(o.stdout, [input, b"done".to_vec()].concat());
    assert_eq!(metadata(&o)["capture_error"], "retention-failed");
    f.empty();
}

#[test]
fn quiet_command_uses_caller_deadline_instead_of_stdin_idle_limit() {
    let f = Fixture::new();
    let o = f
        .exec("printf before; sleep 10.2; printf after", "preview16k")
        .args(["--timeout-seconds", "15"])
        .output()
        .unwrap();
    assert!(o.status.success(), "{:?}", o);
    assert_eq!(o.stdout, b"beforeafter");
}

#[test]
fn cancellation_and_deadline_stop_process_group_and_remove_partial_capture() {
    for explicit in [true, false] {
        let f = Fixture::new();
        f.input(&vec![b'x'; 100000]);
        let mut command = f.exec(
            "cat input; (sleep 2; touch escaped) & touch ready; wait",
            "preview16k",
        );
        if !explicit {
            command.args(["--timeout-seconds", "1"]);
        }
        let child = command
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        f.wait_ready();
        let start = Instant::now();
        if explicit {
            unsafe {
                libc::kill(child.id() as i32, libc::SIGTERM);
            }
        }
        let o = child.wait_with_output().unwrap();
        assert!(start.elapsed() < Duration::from_secs(4));
        assert_eq!(o.status.code(), Some(if explicit { 143 } else { 124 }));
        assert_eq!(metadata(&o)["capture"], "cancelled");
        f.empty();
        std::thread::sleep(Duration::from_millis(2200));
        assert!(!f.root.path().join("escaped").exists());
    }
}

#[test]
fn two_concurrent_captures_hold_slots_and_third_forwards_original() {
    let f = Fixture::new();
    let input = vec![b'x'; 50000];
    f.input(&input);
    let first = f.start_paused();
    fs::remove_file(f.root.path().join("ready")).unwrap();
    let second = f.start_paused();
    let deadline = Instant::now() + Duration::from_secs(5);
    while f
        .db()
        .query_row("SELECT count(*) FROM outputs WHERE committed=0", [], |r| {
            r.get::<_, i64>(0)
        })
        .unwrap()
        != 2
    {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let third = f.exec("cat input", "preview16k").output().unwrap();
    assert!(third.status.success());
    assert_eq!(third.stdout, input);
    assert_eq!(metadata(&third)["capture_error"], "storage-unavailable");
    f.release();
    for child in [first, second] {
        let o = child.wait_with_output().unwrap();
        assert_eq!(o.status.code(), Some(7));
        assert_eq!(metadata(&o)["capture"], "captured");
    }
}

#[test]
fn corruption_and_expiry_refuse_pages_of_command_output() {
    let f = Fixture::new();
    f.input(&vec![b'x'; 50000]);
    let o = f.exec("cat input", "preview16k").output().unwrap();
    let h = handle(&o);
    f.db()
        .execute(
            "UPDATE chunks SET content=x'61' WHERE handle=?1 AND ordinal=0",
            [&h],
        )
        .unwrap();
    assert!(!f.page(&h, None).status.success());
    let o = f.exec("cat input", "preview16k").output().unwrap();
    let h = handle(&o);
    f.db()
        .execute("UPDATE outputs SET created=0 WHERE handle=?1", [&h])
        .unwrap();
    assert!(!f.page(&h, None).status.success());
}

#[test]
fn closed_consumer_cancels_quiet_command() {
    let f = Fixture::new();
    let mut child = f
        .exec("sleep 5; touch escaped", "preview16k")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    drop(child.stdout.take());
    let start = Instant::now();
    let o = child.wait_with_output().unwrap();
    assert!(!o.status.success());
    assert!(start.elapsed() < Duration::from_secs(3));
    assert_eq!(metadata(&o)["capture"], "cancelled");
}

#[test]
fn unchanged_profile_measures_binary_output_without_storage() {
    let f = Fixture::new();
    let input = vec![0, 255, 1, 10];
    f.input(&input);
    let o = f.exec("cat input", "unchanged").output().unwrap();
    assert_eq!(o.stdout, input);
    assert!(o.status.success());
    assert_eq!(metadata(&o)["original_bytes"], 4);
    assert!(metadata(&o)["capture_error"].is_null());
    assert!(!f.root.path().join("cache").exists());
}

#[test]
fn spool_write_failure_flushes_prefix_before_remaining_output() {
    use std::os::unix::process::CommandExt;
    let f = Fixture::new();
    let input = vec![b'q'; 700000];
    f.input(&input);
    let mut command = f.exec("cat input; printf x >> counter; exit 13", "preview16k");
    unsafe {
        command.pre_exec(|| {
            libc::signal(libc::SIGXFSZ, libc::SIG_DFL);
            let limit = libc::rlimit {
                rlim_cur: 256 * 1024,
                rlim_max: 256 * 1024,
            };
            if libc::setrlimit(libc::RLIMIT_FSIZE, &limit) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let o = command.output().unwrap();
    assert_eq!(o.status.code(), Some(13));
    assert_eq!(o.stdout, input);
    assert_eq!(metadata(&o)["capture_error"], "spool-write");
    f.empty();
    assert_eq!(fs::read(f.root.path().join("counter")).unwrap(), b"x");
}

#[test]
fn invalid_cache_configuration_does_not_prevent_execution() {
    let f = Fixture::new();
    let input = vec![b'x'; 50000];
    f.input(&input);
    let o = f
        .exec("cat input; exit 3", "preview16k")
        .env("GREPGLINT_CACHE_DIR", "relative")
        .output()
        .unwrap();
    assert_eq!(o.status.code(), Some(3));
    assert_eq!(o.stdout, input);
    assert_eq!(metadata(&o)["capture_error"], "storage-unavailable");
}

#[cfg(target_os = "linux")]
#[test]
fn near_limit_spool_is_anonymous_bounded_and_released_on_cancellation() {
    let f = Fixture::new();
    f.input(&vec![b'x'; 8 * 1024 * 1024]);
    let child = f.start_paused();
    let until = Instant::now() + Duration::from_secs(10);
    loop {
        let sizes: Vec<_> = fs::read_dir(format!("/proc/{}/fd", child.id()))
            .unwrap()
            .filter_map(|e| {
                let path = e.ok()?.path();
                let target = fs::read_link(&path).ok()?;
                let text = target.to_string_lossy();
                if text.contains("output-v1/") && text.ends_with("(deleted)") {
                    Some(fs::metadata(path).ok()?.len())
                } else {
                    None
                }
            })
            .collect();
        assert!(sizes.len() <= 1);
        assert!(sizes.iter().sum::<u64>() <= 8 * 1024 * 1024);
        if sizes == [8 * 1024 * 1024] {
            break;
        }
        assert!(Instant::now() < until);
        std::thread::sleep(Duration::from_millis(10));
    }
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    let o = child.wait_with_output().unwrap();
    assert_eq!(o.status.code(), Some(143));
    f.empty();
    assert_eq!(
        fs::read_dir(f.root.path().join("cache/output-v1"))
            .unwrap()
            .count(),
        5
    );
}

#[test]
fn producer_keeps_inherited_file_size_limit_and_signal_disposition() {
    use std::os::unix::process::CommandExt;
    for ignored in [false, true] {
        let f = Fixture::new();
        let mut command = f.exec(
            "exec dd if=/dev/zero of=oversized bs=65536 count=8",
            "preview16k",
        );
        unsafe {
            command.pre_exec(move || {
                libc::signal(
                    libc::SIGXFSZ,
                    if ignored {
                        libc::SIG_IGN
                    } else {
                        libc::SIG_DFL
                    },
                );
                let limit = libc::rlimit {
                    rlim_cur: 256 * 1024,
                    rlim_max: 256 * 1024,
                };
                if libc::setrlimit(libc::RLIMIT_FSIZE, &limit) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let o = command.output().unwrap();
        assert_eq!(
            o.status.code(),
            Some(if ignored { 1 } else { 128 + libc::SIGXFSZ })
        );
        assert_eq!(
            fs::metadata(f.root.path().join("oversized")).unwrap().len(),
            256 * 1024
        );
        assert_eq!(
            metadata(&o)["producer_signal"],
            if ignored {
                Value::Null
            } else {
                Value::from(libc::SIGXFSZ)
            }
        );
    }
}
