#!/usr/bin/env python3
"""Release output-search observations; disposable local data, no model calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

binary = Path(__file__).resolve().parents[1] / "target/release/grepglint"
with tempfile.TemporaryDirectory(prefix="gg-rank-") as temporary:
    root = Path(temporary)
    cache = root / "cache"
    env = dict(os.environ, GREPGLINT_CACHE_DIR=str(cache), GREPGLINT_IDLE_SECONDS="2")
    stop = threading.Event()
    peaks = {"disk_bytes": 0, "daemon_rss_kib": 0, "daemon_hwm_kib": 0}
    samples = []

    def sample():
        while not stop.wait(0.01):
            try:
                peaks["disk_bytes"] = max(peaks["disk_bytes"], sum(p.stat().st_blocks * 512 for p in cache.rglob("*") if p.is_file()))
                pid = int((cache / "daemon.pid").read_text())
                memory = {line.split(":")[0]: int(line.split()[1]) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith(("VmRSS:", "VmHWM:"))}
                peaks["daemon_rss_kib"] = max(peaks["daemon_rss_kib"], memory["VmRSS"])
                peaks["daemon_hwm_kib"] = max(peaks["daemon_hwm_kib"], memory["VmHWM"])
            except (FileNotFoundError, ValueError):
                pass

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    def run(args, data=None, success=True):
        start = time.monotonic()
        result = subprocess.run([str(binary), *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root, env=env)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        samples.append({"command": args[:2], "seconds": round(time.monotonic() - start, 5), "response_bytes": len(result.stdout), "success": success})
        assert len(result.stdout) <= 65536
        return result.stdout

    def retain(data):
        preview = run(["output", "bounce"], data)
        return preview.decode().split()[1].rstrip(":"), len(preview)

    def search(handle, query):
        return json.loads(run(["output", "search", handle, query, "--json"]))

    def page(handle, cursor=None):
        args = ["output", "page", handle, "--json"]
        if cursor:
            args += ["--cursor", cursor]
        return json.loads(run(args))

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("cache/\n")
    (root / "auth.rs").write_text("fn validate_refresh_token() { /* refresh token validation */ }\n")
    cases = []
    fixtures = [
        ("noisy-tests", b"test component ... ok\n" * 2000, b"NodePtyModuleLoadError node-pty linux-x64\n", b"test component ... ok\n" * 2000, "NodePtyModuleLoadError"),
        ("compiler-warnings", b"warning: unused compiler variable\n" * 2000, b"error: constructEvent failed with SQLITE_BUSY\n", b"warning: unused compiler variable\n" * 2000, "constructEvent SQLITE_BUSY"),
        ("middle-version-path", b"progress checkpoint complete\n" * 2000, b"v24.20.0 src/auth/session.ts\n", b"progress checkpoint complete\n" * 2000, "src/auth/session.ts"),
    ]
    for name, head, diagnostic, tail, query in fixtures:
        original = head + diagnostic + tail
        handle, preview_bytes = retain(original)
        first_sample = len(samples)
        found = search(handle, query)
        assert diagnostic.decode().strip() in found["results"][0]["content"]
        region_args = found["results"][0]["page_command"].split()[1:]
        region = json.loads(run(region_args))
        assert diagnostic.decode().strip() in region["content"]
        missed = search(handle, "unrelatedNonexistentToken")
        assert not missed["results"]
        restored = bytearray()
        cursor = None
        page_count = page_bytes = 0
        while True:
            decoded = page(handle, cursor)
            page_count += 1
            page_bytes += samples[-1]["response_bytes"]
            restored.extend(decoded["content"].encode())
            cursor = decoded["next_cursor"]
            if cursor is None:
                break
        assert restored == original
        cases.append({"name": name, "original_bytes": len(original), "preview_bytes": preview_bytes,
                      "search_bytes": samples[first_sample]["response_bytes"], "region_page_bytes": samples[first_sample + 1]["response_bytes"],
                      "ranked_workflow_retrievals": 2, "poor_query_search_bytes": samples[first_sample + 2]["response_bytes"],
                      "exact_page_count": page_count, "exact_page_response_bytes": page_bytes, "reconstructed_after_searches": True})

    alternating_start = len(samples)
    for _ in range(20):
        search(handle, query)
        run(["search", "refresh token", "--json"])
    alternating = samples[alternating_start:]
    before_limits = dict(peaks)
    maximum = bytearray(b"z" * (8 * 1024 * 1024))
    maximum[4 * 1024 * 1024:4 * 1024 * 1024 + 16] = b" constructEvent "
    maximum_handle, _ = retain(maximum)
    assert "constructEvent" in search(maximum_handle, "constructEvent")["results"][0]["content"]
    limited, _ = retain(b"\n" * 600000)
    limited_before = page(limited)["content"]
    error = json.loads(run(["output", "search", limited, "missing", "--json"], success=False))
    assert "8,192" in error["error"]
    assert page(limited)["content"] == limited_before
    health = json.loads(run(["status", "--json"]))
    names_before_purge = sorted(str(p.relative_to(cache)) for p in cache.rglob("*") if p.is_file())
    run(["output", "purge"])
    stop.set()
    sampler.join()
    print(json.dumps({"platform": "Linux", "cases": cases, "alternating_cycles": 20, "alternating": alternating,
                      "observations": samples, "peaks_before_large_limit_fixtures": before_limits, "sampled_peaks": peaks,
                      "maximum_output_bytes": len(maximum), "maximum_output_search_success": True,
                      "chunk_limit_error_preserved_paging": True, "shared_limits": {k: health[k] for k in ("sqlite_heap_bytes", "address_space_bytes", "work_seconds", "response_bytes")},
                      "cache_files_before_purge": names_before_purge}, indent=2))
    time.sleep(2.2)
