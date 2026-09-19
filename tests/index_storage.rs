use grepglint::{index::Index, tokens};
use rusqlite::{Connection, params};

fn matches(db: &Connection, table: &str, query: &str) -> Vec<(i64, f64)> {
    db.prepare(&format!(
        "SELECT rowid,bm25({table},8.0,1.0) FROM {table} WHERE {table} MATCH ? ORDER BY 2,rowid"
    ))
    .unwrap()
    .query_map([tokens::query(query).unwrap()], |r| {
        Ok((r.get(0)?, r.get(1)?))
    })
    .unwrap()
    .map(Result::unwrap)
    .collect()
}

#[test]
fn contentless_ranking_deletion_and_rollback_match_stored_content() {
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch(include_str!("../src/schema.sql")).unwrap();
    db.execute_batch(
        "PRAGMA foreign_keys=ON;
         INSERT INTO repositories VALUES('repo','/repo');
         INSERT INTO contents VALUES(1,'repo','blob','parser',0,0);
         CREATE VIRTUAL TABLE reference USING fts5(symbol,body,tokenize='unicode61 tokenchars ''_''');",
    )
    .unwrap();
    for (i, (symbol, body)) in [
        ("refreshToken", "refreshToken validates the current token"),
        ("validate", "refresh token token token"),
        ("HTTPServer", "request middleware exception"),
        ("migrationGraph", "migration dependency graph"),
    ]
    .iter()
    .enumerate()
    {
        let id = i as i64 + 1;
        db.execute(
            "INSERT INTO chunks VALUES(?,1,1,2,?,?,0,?)",
            params![id, symbol, body.as_bytes(), body.len() as i64],
        )
        .unwrap();
        for table in ["chunk_fts", "reference"] {
            db.execute(
                &format!("INSERT INTO {table}(rowid,symbol,body) VALUES(?,?,?)"),
                params![id, tokens::searchable(symbol), tokens::searchable(body)],
            )
            .unwrap();
        }
    }
    for query in [
        "refreshToken",
        "refresh token",
        "HTTP server",
        "migration graph",
    ] {
        let expected = matches(&db, "reference", query);
        assert!(!expected.is_empty());
        assert_eq!(matches(&db, "chunk_fts", query), expected);
    }
    let before = matches(&db, "chunk_fts", "refreshToken");
    db.execute_batch("BEGIN; DELETE FROM chunks WHERE id=1;")
        .unwrap();
    assert!(
        matches(&db, "chunk_fts", "refreshToken")
            .iter()
            .all(|r| r.0 != 1)
    );
    db.execute_batch("ROLLBACK;").unwrap();
    assert_eq!(matches(&db, "chunk_fts", "refreshToken"), before);
    db.execute_batch("DELETE FROM contents WHERE id=1;")
        .unwrap();
    assert!(matches(&db, "chunk_fts", "refresh token middleware graph").is_empty());

    db.execute_batch("INSERT INTO paths VALUES(1,'repo','src/auth/token.ts');")
        .unwrap();
    db.execute(
        "INSERT INTO path_fts(rowid,path) VALUES(1,?)",
        [tokens::searchable("src/auth/token.ts")],
    )
    .unwrap();
    assert_eq!(matches(&db, "path_fts", "auth").len(), 1);
    db.execute_batch("DELETE FROM paths;").unwrap();
    assert!(matches(&db, "path_fts", "auth").is_empty());
    db.execute_batch(
        "INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check');
                      INSERT INTO path_fts(path_fts) VALUES('integrity-check');",
    )
    .unwrap();
    let copies: i64 = db.query_row(
        "SELECT count(*) FROM sqlite_schema WHERE name IN ('chunk_fts_content','path_fts_content')",
        [], |r| r.get(0),
    ).unwrap();
    assert_eq!(copies, 0);
}

#[test]
fn unrecognized_cache_versions_and_schemas_are_preserved() {
    for version in [1, 2, 99] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("index.sqlite");
        let db = Connection::open(&path).unwrap();
        if matches!(version, 1 | 2) {
            db.execute_batch(if version == 1 {
                include_str!("../src/schema-v1.sql")
            } else {
                include_str!("../src/schema-v2.sql")
            })
            .unwrap();
        }
        db.execute_batch("CREATE TABLE keep(value); INSERT INTO keep VALUES('saved');")
            .unwrap();
        db.pragma_update(None, "user_version", version).unwrap();
        db.pragma_update(None, "journal_mode", "PERSIST").unwrap();
        drop(db);
        let before = std::fs::read(&path).unwrap();
        let error = Index::open(directory.path(), 8 * 1024 * 1024)
            .err()
            .unwrap();
        assert!(error.to_string().contains(if matches!(version, 1 | 2) {
            "preserved without changes"
        } else {
            "No migration performed"
        }));
        assert_eq!(std::fs::read(&path).unwrap(), before);
    }
}

#[test]
fn recognized_legacy_cache_is_reclaimed_without_touching_other_files() {
    for version in [1, 2] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("index.sqlite");
        let db = Connection::open(&path).unwrap();
        db.execute_batch(if version == 1 {
            include_str!("../src/schema-v1.sql")
        } else {
            include_str!("../src/schema-v2.sql")
        })
        .unwrap();
        db.pragma_update(None, "user_version", version).unwrap();
        db.execute(
            "INSERT INTO chunk_fts(rowid,symbol,body) VALUES(1,'old',?)",
            ["old cache ".repeat(100_000)],
        )
        .unwrap();
        drop(db);
        let before = std::fs::metadata(&path).unwrap().len();
        std::fs::write(directory.path().join("unrelated"), "keep").unwrap();
        drop(Index::open(directory.path(), 8 * 1024 * 1024).unwrap());
        let db = Connection::open(&path).unwrap();
        assert_eq!(
            db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            3
        );
        assert!(matches(&db, "chunk_fts", "old").is_empty());
        assert!(std::fs::metadata(&path).unwrap().len() < before / 2);
        assert_eq!(
            std::fs::read_to_string(directory.path().join("unrelated")).unwrap(),
            "keep"
        );
        assert!(!directory.path().join("index.sqlite-journal").exists());
        drop(Index::open(directory.path(), 8 * 1024 * 1024).unwrap());
    }
}
