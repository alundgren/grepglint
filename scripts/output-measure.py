#!/usr/bin/env python3
"""Linux release observations in disposable local caches; no model/network calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

binary = Path(__file__).resolve().parents[1] / "target/release/grepglint"
with tempfile.TemporaryDirectory(prefix="gg-output-") as temporary:
    root = Path(temporary)
    cache = root / "cache"
    env = dict(os.environ, GREPGLINT_CACHE_DIR=str(cache), GREPGLINT_IDLE_SECONDS="2")
    peak_disk = [0]
    stop = threading.Event()

    def sample_disk():
        while not stop.wait(0.01):
            try:
                peak_disk[0] = max(peak_disk[0], sum(p.stat().st_blocks * 512 for p in cache.rglob("*") if p.is_file()))
            except FileNotFoundError:
                pass

    sampler = threading.Thread(target=sample_disk, daemon=True)
    sampler.start()
    observations = []

    def run(args, data=None):
        report = root / "time.txt"
        start = time.monotonic()
        result = subprocess.run(["/usr/bin/time", "-f", "%M", "-o", str(report), str(binary), *args],
                                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root, env=env, check=True)
        observations.append({"command": args[:2], "seconds": round(time.monotonic()-start, 4),
                             "returned_bytes": len(result.stdout), "peak_rss_kib": int(report.read_text())})
        return result.stdout

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("cache/\ntime.txt\n")
    (root / "auth.rs").write_text("fn validate_refresh_token() { /* refresh token validation */ }\n")
    log = b"test compiler::resolve_symbol ... ok\r\nwarning: unused variable src/lib.rs:42\n" * 27000
    handles = []
    for cycle in range(20):
        preview = run(["output", "bounce"], log)
        handle = preview.decode().split()[1].rstrip(":")
        handles.append(handle)
        page = json.loads(run(["output", "page", handle, "--json"]))
        assert page["content"].encode() == log[:len(page["content"].encode())]
        run(["search", "refresh token", "--json"])
    daemon = int((cache / "daemon.pid").read_text())
    daemon_memory = [line for line in Path(f"/proc/{daemon}/status").read_text().splitlines() if line.startswith(("VmHWM:", "VmRSS:", "Threads:"))]
    # Reconstruct once, outside measured cycles, to verify all pages of a long log.
    cursor = None
    restored = bytearray()
    while True:
        args = [str(binary), "output", "page", handles[-1], "--json"]
        if cursor:
            args += ["--cursor", cursor]
        page = json.loads(subprocess.check_output(args, cwd=root, env=env))
        restored.extend(page["content"].encode())
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert restored == log
    expired = subprocess.run([str(binary), "output", "page", handles[0], "--json"], cwd=root, env=env, stdout=subprocess.PIPE)
    assert expired.returncode != 0, "Expected pressure eviction after sustained captures"
    run(["output", "purge"])
    stop.set()
    sampler.join()
    remaining = sum(p.stat().st_blocks*512 for p in cache.rglob("*") if p.is_file())
    print(json.dumps({"platform": "Linux", "log_bytes": len(log), "cycles": 20,
                      "observations": observations, "daemon_memory": daemon_memory,
                      "sampled_peak_disk_bytes": peak_disk[0], "remaining_disk_bytes_after_purge": remaining,
                      "full_reconstruction": True, "oldest_evicted": True}, indent=2))
    time.sleep(2.2)
