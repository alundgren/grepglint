use super::{Record, files, process};
use crate::{
    daemon::Config,
    maintenance,
    protocol::{Health, Response},
};
use anyhow::{Context, Result, ensure};
use std::os::unix::fs::PermissionsExt;
use std::{fs, path::Path, process::Command, time::Duration};

fn health(record: &Record, value: &Health) -> Result<()> {
    ensure!(
        value.build_version == record.release.trim_start_matches('v')
            && value.executable_sha256 == record.digest,
        "Running daemon differs from recorded executable; verification did not stop or repair it"
    );
    ensure!(
        value.protocol_version == crate::protocol::VERSION
            && value.cache_directory == record.cache.to_string_lossy()
            && value.database_bytes == record.database_bytes
            && value.idle_seconds == record.idle_seconds
            && value.request_bytes == 16384
            && value.response_bytes == 65536
            && value.work_seconds == 30
            && value.sqlite_heap_bytes == 64 * 1024 * 1024
            && value.address_space_bytes
                == if cfg!(target_os = "linux") {
                    Some(512 * 1024 * 1024)
                } else {
                    None
                },
        "Running daemon settings differ from recorded effective limits"
    );
    Ok(())
}

pub fn installation(record: &Record, state: &Path) -> Result<()> {
    ensure!(
        record.phase == "complete" || record.phase == "installed",
        "Installation interrupted; rerun ./install install to resume"
    );
    executables(record, state)?;
    cache(record)?;
    match maintenance::status(&record.config())? {
        Some(value) => {
            health(record, &value)?;
            println!("Real daemon identity and effective limits verified");
        }
        None => println!("No real daemon is running; verification will not start it"),
    }
    fixture(&record.destination)?;
    println!("Verified binary, private cache, isolated search, reuse and dirty freshness");
    Ok(())
}

pub fn executables(record: &Record, state: &Path) -> Result<()> {
    ensure!(
        files::hash(&state.join("maintenance"))? == record.digest,
        "Maintenance copy modified; verification failed"
    );
    ensure!(
        files::hash(&record.destination)? == record.digest,
        "Installed executable modified; verification failed"
    );
    let version = process::run(Command::new(&record.destination).arg("--version"))?;
    ensure!(
        String::from_utf8(version)?.trim()
            == format!("grepglint {}", record.release.trim_start_matches('v')),
        "Installed version mismatch"
    );
    Ok(())
}

pub fn cache(record: &Record) -> Result<()> {
    files::directory(&record.cache, false, true)?;
    let cache_name = std::ffi::CString::new(record.cache.as_os_str().as_encoded_bytes())?;
    ensure!(
        unsafe { libc::access(cache_name.as_ptr(), libc::R_OK | libc::W_OK | libc::X_OK) } == 0,
        "Cache is not readable and writable"
    );
    let directory = fs::File::open(&record.cache)?;
    ensure!(directory.metadata()?.is_dir(), "Cache inaccessible");
    // Opening with read/write checks access without changing cache contents.
    let cache_file = record.cache.join("index.sqlite");
    if !files::absent(&cache_file)? {
        let _ = files::open_writable(&cache_file, record.database_bytes)
            .context("Cache database is not readable and writable")?;
    }
    if !files::absent(&cache_file)? {
        let db = rusqlite::Connection::open_with_flags(
            &cache_file,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        db.busy_timeout(Duration::from_millis(500))?;
        let version: i64 = db.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        ensure!(
            version == crate::index::SCHEMA_VERSION
                || (matches!(version, 1 | 2) && crate::index::is_legacy_cache(&db)?),
            "Incompatible cache format {version}; no migration performed. Preserve the cache and use the previous release or rg"
        );
    }
    Ok(())
}

fn fixture(binary: &Path) -> Result<()> {
    let temporary = tempfile::Builder::new()
        .prefix("gg-v-")
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()?;
    let root = temporary.path().canonicalize()?;
    let repo = root.join("repo");
    fs::create_dir(&repo)?;
    let config = Config {
        directory: root.join("cache"),
        max_bytes: 8 * 1024 * 1024,
        idle: Duration::from_secs(2),
    };
    files::reserve(&root, 80 * 1024 * 1024)?;
    let command = |program: &std::ffi::OsStr| {
        let mut command = Command::new(program);
        command
            .current_dir(&repo)
            .env("GREPGLINT_CACHE_DIR", &config.directory)
            .env("GREPGLINT_CACHE_MB", "8")
            .env("GREPGLINT_IDLE_SECONDS", "2")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_TERMINAL_PROMPT", "0");
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("GIT_") {
                command.env_remove(key);
            }
        }
        command
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_TERMINAL_PROMPT", "0");
        command
    };
    process::run(command("git".as_ref()).args(["-c", "init.templateDir=", "init", "-q"]))?;
    let source = repo.join("fixture.ts");
    fs::write(
        &source,
        "export function fixtureQuartz() { return 'amberunique'; }\n",
    )?;
    process::run(command("git".as_ref()).args(["add", "fixture.ts"]))?;
    process::run(command("git".as_ref()).args([
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    ]))?;
    let query = |text: &str| -> Result<Response> {
        Ok(serde_json::from_slice(&process::run(
            command(binary.as_os_str()).args(["search", "--json", text]),
        )?)?)
    };
    let result = (|| {
        let cold = query("amberunique")?;
        ensure!(
            !cold.results.is_empty() && cold.stats.blobs_parsed == 1,
            "Fixture cold search failed"
        );
        let warm = query("amberunique")?;
        ensure!(
            !warm.results.is_empty()
                && warm.stats.blobs_parsed == 0
                && warm.stats.paths_updated == 0,
            "Fixture cache reuse failed"
        );
        fs::write(
            source,
            "export function fixtureQuartz() { return 'opalunique'; }\n",
        )?;
        let dirty = query("opalunique")?;
        ensure!(
            !dirty.results.is_empty() && dirty.stats.overlay_files_parsed == 1,
            "Fixture dirty freshness failed"
        );
        ensure!(
            query("amberunique")?.results.is_empty(),
            "Fixture returned stale content"
        );
        Ok(())
    })();
    if let Err(error) = maintenance::shutdown(&config, None) {
        let retained = temporary.keep();
        return Err(error.context(format!(
            "Fixture cleanup refused; preserved {} for inspection",
            retained.display()
        )));
    }
    temporary.close()?;
    result
}
