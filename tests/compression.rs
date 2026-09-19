use rusqlite::Connection;
use serde_json::Value;
use std::{fs, path::Path, process::Command};

fn command(repo: &Path, cache: &Path, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_grepglint"))
        .current_dir(repo)
        .env("GREPGLINT_CACHE_DIR", cache)
        .env("GREPGLINT_IDLE_SECONDS", "1")
        .args(args)
        .output()
        .unwrap()
}

fn search(repo: &Path, cache: &Path) -> Value {
    let out = command(repo, cache, &["search", "compressionmarker", "--json"]);
    assert!(
        out.status.success(),
        "{} {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

#[test]
fn real_cli_compresses_by_default_and_reuses_mixed_chunks() {
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("repo");
    fs::create_dir(&repo).unwrap();
    assert!(
        Command::new("git")
            .args(["init", "-q"])
            .arg(&repo)
            .status()
            .unwrap()
            .success()
    );
    fs::write(
        repo.join("source.txt"),
        "compressionmarker café validates the current request and returns a result\n".repeat(180),
    )
    .unwrap();
    fs::write(
        repo.join("raw.txt"),
        "compressionmarker short raw content\n",
    )
    .unwrap();
    let cache = temp.path().join("cache");
    let original = search(&repo, &cache);
    let results = original["results"].as_array().unwrap();
    assert!(results.iter().any(|r| r["path"] == "source.txt"));
    assert!(results.iter().any(|r| r["path"] == "raw.txt"));
    assert!(command(&repo, &cache, &["shutdown"]).status.success());
    let reused = search(&repo, &cache);
    assert_eq!(reused["results"], original["results"]);
    assert_eq!(reused["stats"]["overlay_files_parsed"], 0);
    assert!(command(&repo, &cache, &["shutdown"]).status.success());
    let db = Connection::open(cache.join("index.sqlite")).unwrap();
    for codec in [0, 1] {
        let count: i64 = db
            .query_row(
                "SELECT count(*) FROM chunks WHERE body_codec=?",
                [codec],
                |r| r.get(0),
            )
            .unwrap();
        assert!(count > 0);
    }
    // Previously cached raw bodies remain readable without changing FTS scores.
    let (id, body, length): (i64, Vec<u8>, i64) = db
        .query_row(
            "SELECT id,body,body_bytes FROM chunks WHERE body_codec=1 LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    let raw = zstd::bulk::decompress(&body, usize::try_from(length).unwrap()).unwrap();
    db.execute(
        "UPDATE chunks SET body=?,body_codec=0 WHERE id=?",
        rusqlite::params![raw, id],
    )
    .unwrap();
    drop(db);
    assert_eq!(search(&repo, &cache)["results"], original["results"]);
    assert!(command(&repo, &cache, &["shutdown"]).status.success());
    let db = Connection::open(cache.join("index.sqlite")).unwrap();
    db.execute(
        "UPDATE chunks SET body_bytes=1000000000 WHERE body_codec=1",
        [],
    )
    .unwrap();
    drop(db);
    let invalid = command(&repo, &cache, &["search", "compressionmarker", "--json"]);
    assert!(!invalid.status.success());
    assert!(String::from_utf8_lossy(&invalid.stdout).contains("byte limit"));
    assert!(command(&repo, &cache, &["shutdown"]).status.success());
}
