#!/usr/bin/env python3
"""Deterministic Linux command capture/recovery measurements; no network or model calls."""
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time


def allocated(cache):
    return sum(p.stat().st_blocks * 512 for p in cache.rglob("*") if p.is_file())


def summary(rows):
    return {
        "calls": len(rows),
        "total_seconds": round(sum(r["seconds"] for r in rows), 4),
        "median_seconds": round(statistics.median(r["seconds"] for r in rows), 4),
        "max_seconds": round(max(r["seconds"] for r in rows), 4),
        "peak_rss_kib": max(r["peak_rss_kib"] for r in rows),
        "returned_bytes": sum(r["returned_bytes"] for r in rows),
    }


def main():
    binary = Path(__file__).resolve().parents[1] / "target/release/grepglint"
    results = []
    with tempfile.TemporaryDirectory(prefix="gg-exec-") as temporary:
        root = Path(temporary)
        cache = root / "cache"
        env = dict(os.environ, GREPGLINT_CACHE_DIR=str(cache), GREPGLINT_IDLE_SECONDS="1")
        peak_disk = [0]
        peak_spool = [0]
        current_pid = [None]
        stop = threading.Event()

        def sample():
            while not stop.wait(0.01):
                try:
                    spool = 0
                    pid = current_pid[0]
                    if pid:
                        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
                        for child in children:
                            for fd in Path(f"/proc/{child}/fd").iterdir():
                                target = str(fd.readlink())
                                if "output-v1/" in target and target.endswith("(deleted)"):
                                    spool += fd.stat().st_blocks * 512
                    peak_spool[0] = max(peak_spool[0], spool)
                    peak_disk[0] = max(peak_disk[0], allocated(cache) + spool)
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    pass

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()

        def run(args, expected=0):
            report = root / "time.txt"
            start = time.monotonic()
            child = subprocess.Popen(["/usr/bin/time", "-f", "%M", "-o", str(report), str(binary), *args],
                                     cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            current_pid[0] = child.pid
            stdout, stderr = child.communicate(timeout=60)
            current_pid[0] = None
            assert child.returncode == expected, (child.returncode, stdout[:2048], stderr[:2048])
            row = {"seconds": round(time.monotonic() - start, 6), "returned_bytes": len(stdout),
                   "metadata_bytes": len(stderr), "peak_rss_kib": int(report.read_text().splitlines()[-1])}
            return stdout, stderr, row

        handles = []
        count = 0
        for size in [1024, 8 * 1024 * 1024]:
            line = b"test pkg::compile ... ok " + b"x" * 70 + b"\r\n"
            data = (line * (size // len(line) + 1))[:size]
            marker = b"\nMIDDLE_DIAGNOSTIC missingDependency\n"
            middle = size // 2
            data = data[:middle] + marker + data[middle + len(marker):]
            (root / "input").write_bytes(data)
            for profile in ["unchanged", "preview16k", "preview32k"]:
                run(["output", "purge"]) if cache.exists() else None
                for cycle in range(3):
                    stdout, stderr, row = run(["output", "exec", "--shell", "/bin/sh", "--profile", profile,
                                              "--command", "printf x >> counter; cat input; exit 7"], expected=7)
                    count += 1
                    assert (root / "counter").stat().st_size == count
                    metadata = json.loads(stderr.splitlines()[-1])
                    assert metadata["original_bytes"] == size
                    assert metadata["returned_bytes"] == len(stdout)
                    assert metadata["producer_status"] == 7
                    row.update(size=size, profile=profile, cycle=cycle, metadata=metadata)
                    results.append(row)
                    if metadata["handle"]:
                        handles.append(metadata["handle"])
                        assert marker.strip() not in stdout and len(stdout) <= 8192
                    else:
                        assert stdout == data
        handle = handles[-1]
        search, _, search_row = run(["output", "search", handle, "MIDDLE_DIAGNOSTIC", "--json"])
        assert "MIDDLE_DIAGNOSTIC" in search.decode()
        run(["output", "search", handle, "x" * 2001, "--json"], expected=1)
        cursor = None
        digest = hashlib.sha256()
        pages = []
        recovered = 0
        while True:
            args = ["output", "page", handle, "--json"]
            if cursor:
                args += ["--cursor", cursor]
            page_bytes, _, row = run(args)
            if not pages:
                repeat, _, _ = run(args)
                assert repeat == page_bytes
            page = json.loads(page_bytes)
            content = page["content"].encode()
            assert len(content) <= 4096
            digest.update(content)
            recovered += len(content)
            pages.append(row)
            cursor = page["next_cursor"]
            if cursor is None:
                assert page["end_of_output"]
                break
        assert digest.digest() == hashlib.sha256(data).digest() and recovered == len(data)
        _, _, purge = run(["output", "purge"])
        stop.set()
        thread.join()
        print(json.dumps({"platform": "Linux", "sample_interval_ms": 10, "command_runs": count,
                          "execution_once": True, "observations": results, "search": search_row,
                          "recovery": dict(summary(pages), original_bytes=recovered, sha256=digest.hexdigest(),
                                           exact=True, repeated_cursor=True, failed_search_preserved_paging=True),
                          "sampled_peak_disk_bytes_including_anonymous_spool": peak_disk[0],
                          "sampled_peak_anonymous_spool_bytes": peak_spool[0],
                          "remaining_disk_bytes_after_purge": allocated(cache), "purge": purge}, indent=2))
        run(["shutdown"])


if __name__ == "__main__":
    main()
