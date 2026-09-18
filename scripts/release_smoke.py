#!/usr/bin/env python3
"""Exercise a native release executable using only disposable files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

binary = Path(sys.argv[1]).resolve()
expected_version = sys.argv[2].removeprefix("v")
assert subprocess.check_output([binary, "--version"], text=True).strip() == f"grepglint {expected_version}"
with tempfile.TemporaryDirectory(prefix="grepglint-release-") as temporary:
    root = Path(temporary)
    repo = root / "repo"
    repo.mkdir()
    env = {**os.environ, "GREPGLINT_CACHE_DIR": str(root / "cache"), "GREPGLINT_IDLE_SECONDS": "2"}

    def run(*args):
        return subprocess.check_output(args, cwd=repo, env=env, text=True, timeout=40)

    run("git", "init", "-q")
    source = repo / "auth.ts"
    source.write_text("export function validateRefreshToken() { return 'releasecheck'; }\n")
    run("git", "add", ".")
    run("git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    try:
        first = json.loads(run(binary, "search", "--json", "releasecheck"))
        assert first["results"] and first["stats"]["blobs_parsed"] > 0
        warm = json.loads(run(binary, "search", "--json", "releasecheck"))
        assert warm["stats"]["blobs_parsed"] == 0 and warm["stats"]["paths_updated"] == 0
        source.write_text("export function validateRefreshToken() { return 'changedcheck'; }\n")
        dirty = json.loads(run(binary, "search", "--json", "changedcheck"))
        assert dirty["results"] and dirty["stats"]["overlay_files_parsed"] == 1
    finally:
        run(binary, "shutdown")
print("release binary version, cold/warm search, dirty freshness and shutdown passed")
