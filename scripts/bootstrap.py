#!/usr/bin/env python3
"""Trusted release verification and hash-checked offline maintenance launcher."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = "alundgren/grepglint"
CAP = 128 * 1024 * 1024
META_CAP = 1024 * 1024
TAG = r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path, cap):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and info.st_size <= cap and info.st_mode & 0o022 == 0,
                f"Unsafe or oversized file: {path}")
        data = source.read(cap + 1)
        require(len(data) <= cap, "File grew beyond its limit")
        return data


def path_check(path):
    require(path.is_absolute() and ".." not in path.parts, "Paths must be absolute without '..'")
    for parent in reversed([path] + list(path.parents)):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            require(not stat.S_ISLNK(info.st_mode), f"Symlink path refused: {parent}")
            if stat.S_ISDIR(info.st_mode):
                require(info.st_uid in (0, os.getuid()) and (info.st_mode & 0o022 == 0 or info.st_mode & stat.S_ISVTX), f"Unsafe ancestor directory: {parent}")


def run(args, timeout=30, cap=META_CAP):
    """Bound both streams together; kill the process group on refusal."""
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError as error:
        raise ValueError(f"Required tool {args[0]} is missing; install it and retry") from error
    output = bytearray()
    errors = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                require(time.monotonic() < deadline, f"{args[0]} timed out; no automatic retry")
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(block)
                        require(len(output) + len(errors) <= cap, f"{args[0]} output exceeded limit")
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        require(process.returncode == 0,
                f"{args[0]} failed. Check authentication (gh auth login), rate limits, network, and attestation policy; no files replaced")
        return bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()
        process.stderr.close()


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        require(newurl.startswith("https://"), "Non-HTTPS redirect refused")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def download(url, path, cap):
    require(url.startswith("https://"), "Only HTTPS downloads are allowed")
    deadline = time.monotonic() + 120
    opener = urllib.request.build_opener(HttpsRedirect())
    with opener.open(url, timeout=10) as response, path.open("xb") as output:
        size = 0
        while True:
            require(time.monotonic() < deadline, "Download exceeded 120 seconds")
            block = response.read(65536)
            if not block:
                break
            size += len(block)
            require(size <= cap, "Download exceeded byte limit")
            output.write(block)
        output.flush()
        os.fsync(output.fileno())


def target():
    pair = (platform.system(), platform.machine())
    targets = {("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
               ("Darwin", "arm64"): "aarch64-apple-darwin"}
    require(pair in targets, "Unsupported platform; use a supported release platform or build from source")
    return targets[pair]


def check_header(data, selected):
    if selected == "x86_64-unknown-linux-gnu":
        require(data[:6] == b"\x7fELF\x02\x01" and data[18:20] == b"\x3e\x00", "Wrong Linux executable target")
    else:
        require(data[:8] == b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01", "Wrong macOS executable target")


def commit(tag):
    value = run(["gh", "api", f"repos/{REPO}/commits/{tag}", "--jq", ".sha"]).decode().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", value), "Invalid selected release commit")
    return value


def fetch(tag, directory):
    require(re.fullmatch(TAG, tag), "Select an exact stable release, e.g. --release v0.1.0")
    selected = target()
    version = run(["gh", "--version"]).decode()
    match = re.search(r"gh version (\d+)\.(\d+)\.(\d+)", version)
    require(match and tuple(map(int, match.groups())) >= (2, 80, 0), "GitHub CLI 2.80.0 or newer is required")
    expected_commit = commit(tag)
    metadata = json.loads(run(["gh", "api", f"repos/{REPO}/releases/tags/{tag}"]))
    require(metadata.get("tag_name") == tag and not metadata.get("draft") and not metadata.get("prerelease"), "Unexpected release metadata")
    name = f"grepglint-{tag}-{selected}"
    assets = metadata.get("assets", [])
    for wanted, limit in [(name, CAP), ("SHA256SUMS", 4096)]:
        matches = [asset for asset in assets if asset.get("name") == wanted]
        require(len(matches) == 1 and 0 < matches[0].get("size", 0) <= limit, "Missing, duplicate or oversized release asset")
        url = matches[0]["browser_download_url"]
        require(url == f"https://github.com/{REPO}/releases/download/{tag}/{wanted}", "Unexpected asset URL")
        download(url, directory / wanted, limit)
    entries = {}
    for line in regular(directory / "SHA256SUMS", 4096).decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (grepglint-" + re.escape(tag) + r"-(?:x86_64-unknown-linux-gnu|aarch64-apple-darwin))", line)
        require(match and match[2] not in entries, "Malformed or duplicate checksum entry")
        entries[match[2]] = match[1]
    require(set(entries) == {f"grepglint-{tag}-{item}" for item in ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin"]}, "Missing target checksum")
    binary = directory / name
    data = regular(binary, CAP)
    digest = hashlib.sha256(data).hexdigest()
    require(digest == entries[name], "Release checksum mismatch")
    check_header(data[:32], selected)
    del data
    run(["gh", "attestation", "verify", str(binary), "--repo", REPO,
         "--signer-workflow", f"{REPO}/.github/workflows/release.yml",
         "--source-ref", f"refs/tags/{tag}", "--source-digest", expected_commit,
         "--signer-digest", expected_commit, "--deny-self-hosted-runners"], timeout=60)
    require(commit(tag) == expected_commit, "Release tag changed during verification")
    require(hashlib.sha256(regular(binary, CAP)).hexdigest() == digest, "Verified bytes changed")
    binary.chmod(0o700)
    require(run([str(binary), "--version"], timeout=10, cap=4096).decode().strip() == f"grepglint {tag[1:]}", "Release version mismatch")
    return binary, expected_commit, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install a verified release from this trusted checkout; local maintenance works offline")
    parser.add_argument("action", nargs="?", choices=["install", "verify", "status", "upgrade", "repair", "uninstall", "purge"])
    parser.add_argument("--release")
    parser.add_argument("--destination")
    parser.add_argument("--cache-dir")
    parser.add_argument("--state-dir", default=str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "grepglint"))
    parser.add_argument("--migrate-cargo", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    require(os.getuid() != 0, "Run as your normal account, without sudo")
    state = Path(args.state_dir)
    path_check(state)
    record_path = state / "record.json"
    record = None
    if record_path.exists() or record_path.is_symlink():
        require(stat.S_IMODE(state.stat().st_mode) == 0o700 and state.stat().st_uid == os.getuid(), "State must be private and account-owned")
        record = json.loads(regular(record_path, 65536))
        require(record.get("schema_version") == 1, "Unsupported state version; state preserved")
    if not args.action:
        require(sys.stdin.isatty(), "Noninteractive use requires an explicit action; see --help")
        print(f"Installation: {record['phase'] if record else 'not installed'}")
        print("1 Install or resume\n2 Verify locally\n3 Status\n0 Cancel")
        args.action = {"1": "install", "2": "verify", "3": "status"}.get(input("Choose: ").strip())
        if args.action is None:
            print("Cancelled; nothing changed")
            return 0
    require(args.action not in ["upgrade", "repair", "uninstall", "purge"], "This action is not available in this version")
    native_args = ["setup", args.action, "--state-dir", str(state)]
    for key in ["destination", "cache_dir"]:
        if getattr(args, key):
            native_args += ["--" + key.replace("_", "-"), getattr(args, key)]
    if args.migrate_cargo:
        native_args += ["--migrate-cargo"]
    if args.action == "status":
        print(f"Installation: {record['phase'] if record else 'not installed'}")
        return 0
    if record:
        require(not args.release or args.release == record.get("release"), "Upgrade or downgrade is unavailable; recorded release preserved")
        local = state / "maintenance"
        if local.exists() or local.is_symlink():
            require(hashlib.sha256(regular(local, CAP)).hexdigest() == record.get("digest"), "Maintenance copy changed; refusing to execute it")
            return subprocess.call([str(local)] + native_args)
        require(record.get("phase") == "prepared" and args.action == "install", "Maintenance copy missing; installation is unhealthy")
        args.release = record["release"]
    require(args.action == "install", "No installation record; nothing verified")
    if not args.release and sys.stdin.isatty():
        args.release = input("Exact release tag (e.g. v0.1.0), blank to cancel: ").strip()
        if not args.release:
            return 0
    require(args.release, "Install requires --release vMAJOR.MINOR.PATCH")
    require(shutil.disk_usage(tempfile.gettempdir()).free >= 4 * CAP + 64 * 1024 * 1024, "Insufficient staging disk space")
    with tempfile.TemporaryDirectory(prefix="grepglint-download-") as directory:
        binary, source_commit, digest = fetch(args.release, Path(directory))
        if record:
            require(source_commit == record["commit"] and digest == record["digest"], "Recovery release identity changed")
        return subprocess.call([str(binary)] + native_args + ["--release", args.release,
                               "--commit", source_commit, "--digest", digest])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, UnicodeError, subprocess.SubprocessError) as error:
        print(f"install: {error}", file=sys.stderr)
        sys.exit(1)
