#!/usr/bin/env python3
"""Run a disposable worktree demo and record retrieval and resource measurements."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import time


PROJECT = Path(__file__).resolve().parents[1]


def run(args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def git(root, *args):
    return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args], root).stdout.strip()


def memory(pid):
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return None
    fields = dict(line.split(":", 1) for line in status.read_text().splitlines())
    return {name: int(fields[name].split()[0]) for name in ["VmRSS", "VmSize", "VmPeak"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, default=PROJECT / "target/release/grepglint")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this file")
    args = parser.parse_args()
    binary = args.binary.resolve()
    report = {"environment": {"platform": os.uname().sysname, "architecture": os.uname().machine}, "workflows": [], "comparisons": []}
    with tempfile.TemporaryDirectory(prefix="grepglint-demo-") as temporary:
        base = Path(temporary)
        root, cache, worktree = base / "main", base / "cache", base / "feature"
        shutil.copytree(PROJECT / "fixtures/shop", root)
        jobs = root / "src/jobs"
        jobs.mkdir()
        for i in range(40):
            (jobs / f"queue-{i}.ts").write_text(
                f"export function refreshQueue{i}() {{\n"
                "  // Refresh the queue's token counter for the dashboard.\n"
                f"  const token = {i};\n"
                "  return { token, pending: [], lastRefresh: Date.now() };\n}\n"
            )
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "fixture@example.invalid")
        git(root, "add", ".")
        git(root, "commit", "-m", "Initial search fixture")
        env = {**os.environ, "GREPGLINT_CACHE_DIR": str(cache), "GREPGLINT_IDLE_SECONDS": "2"}

        def search(directory, query):
            start = time.perf_counter()
            output = run([str(binary), "search", "--json", "--limit", "3", query], directory, env)
            elapsed = (time.perf_counter() - start) * 1000
            return json.loads(output.stdout), elapsed, len(output.stdout.encode())

        def record(name, response, elapsed):
            report["workflows"].append({"case": name, "stats": response["stats"], "wall_ms": round(elapsed, 2),
                                        "results": [result["path"] for result in response["results"]]})

        first, elapsed, _ = search(root / "src/auth", "refresh token validation")
        assert first["stats"]["blobs_parsed"] == 47  # Includes .gitignore as text.
        assert first["results"][0]["symbol"] == "validateRefreshToken"
        record("first search", first, elapsed)
        second, elapsed, _ = search(root, "refresh token validation")
        assert second["stats"]["blobs_parsed"] == 0 and second["stats"]["paths_updated"] == 0
        record("unchanged search", second, elapsed)
        git(root, "worktree", "add", "-b", "feature", str(worktree))
        shared, elapsed, _ = search(worktree, "refresh token validation")
        assert shared["stats"]["blobs_parsed"] == 0
        assert shared["stats"]["cached_chunks"] == first["stats"]["cached_chunks"]
        record("second worktree", shared, elapsed)
        (worktree / "src/branch.ts").write_text("export function branchamber() { return true; }\n")
        git(worktree, "add", ".")
        git(worktree, "commit", "-m", "Add branch handler")
        branch, elapsed, _ = search(worktree, "branchamber")
        assert branch["stats"]["blobs_parsed"] == 1
        record("committed branch change", branch, elapsed)
        changed_path = worktree / "src/auth/refresh-token.ts"
        changed_path.write_text("export function dirtyquartz() { return true; }\n")
        dirty, elapsed, _ = search(worktree, "dirtyquartz")
        assert dirty["stats"]["overlay_files_parsed"] == 1 and len(dirty["results"]) == 1
        assert not search(root, "dirtyquartz")[0]["results"]
        assert not search(worktree, "revocationLedger")[0]["results"]
        record("uncommitted edit", dirty, elapsed)
        git(worktree, "restore", "src/auth/refresh-token.ts")
        reverted, elapsed, _ = search(worktree, "dirtyquartz")
        assert not reverted["results"] and reverted["stats"]["overlay_entries"] == 0
        record("reverted edit", reverted, elapsed)
        (worktree / "src/new.ts").write_text("export const untrackedberyl = true;\n")
        untracked, elapsed, _ = search(worktree, "untrackedberyl")
        assert len(untracked["results"]) == 1
        record("untracked file", untracked, elapsed)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(search, root, "refresh validation"), pool.submit(search, worktree, "webhook signature")]
            for i, future in enumerate(futures):
                response, elapsed, _ = future.result()
                assert response["results"]
                record(f"concurrent client {i + 1}", response, elapsed)

        for query in ["refresh token validation", "refresh revocation", "webhook signature verification", "validateRefreshToken"]:
            response, elapsed, output_bytes = search(root, query)
            literal_start = time.perf_counter()
            literal = subprocess.run(["rg", "-n", "-i", "-F", query, "."], cwd=root, capture_output=True, text=True)
            literal_ms = (time.perf_counter() - literal_start) * 1000
            assert literal.returncode in (0, 1)
            broad_start = time.perf_counter()
            broad = subprocess.run(["rg", "-n", "-i", "|".join(re.escape(term) for term in query.split()), "."], cwd=root, capture_output=True, text=True)
            broad_ms = (time.perf_counter() - broad_start) * 1000
            assert broad.returncode in (0, 1)
            report["comparisons"].append({
                "query": query, "top_symbol": response["results"][0]["symbol"],
                "top_path": response["results"][0]["path"], "ranked_regions": len(response["results"]),
                "grepglint_json_bytes": output_bytes, "grepglint_wall_ms": round(elapsed, 2),
                "rg_literal_lines": len(literal.stdout.splitlines()), "rg_literal_bytes": len(literal.stdout.encode()), "rg_literal_wall_ms": round(literal_ms, 2),
                "rg_any_term_lines": len(broad.stdout.splitlines()), "rg_any_term_bytes": len(broad.stdout.encode()), "rg_any_term_wall_ms": round(broad_ms, 2),
            })

        timings = []
        snapshots = {}
        pid = int((cache / "daemon.pid").read_text())
        for i in range(250):
            response, elapsed, _ = search(root, "refresh token validation")
            assert response["stats"]["blobs_parsed"] == 0
            timings.append(elapsed)
            if i in (49, 249):
                snapshots[str(i + 1)] = memory(pid)
        report["warm_search"] = {"queries": len(timings), "median_wall_ms": round(statistics.median(timings), 2), "p95_wall_ms": round(sorted(timings)[237], 2)}
        report["daemon_memory_kib_after_queries"] = snapshots
        report["database_bytes"] = (cache / "index.sqlite").stat().st_size
        report["cache_files"] = {path.name: path.stat().st_size for path in sorted(cache.iterdir())}
        report["binary_bytes"] = binary.stat().st_size
        if Path(f"/proc/{pid}/limits").exists():
            report["address_space_limit"] = next(line for line in Path(f"/proc/{pid}/limits").read_text().splitlines() if line.startswith("Max address space"))
        deadline = time.monotonic() + 5
        while (cache / "daemon.sock").exists():
            assert time.monotonic() < deadline, "daemon failed to exit when idle"
            time.sleep(0.05)
        report["idle_shutdown_observed"] = True
        resumed, elapsed, _ = search(root, "refresh token validation")
        assert resumed["stats"]["blobs_parsed"] == 0
        record("wake after idle shutdown", resumed, elapsed)
        deadline = time.monotonic() + 5
        while (cache / "daemon.sock").exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)

    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
