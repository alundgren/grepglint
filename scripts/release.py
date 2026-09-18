#!/usr/bin/env python3
"""Validate and assemble the raw executable release contract."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-apple-darwin")
MAX_BYTES = 128 * 1024 * 1024


def version():
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--offline", "--no-deps", "--format-version=1"],
        text=True))
    return next(p["version"] for p in metadata["packages"] if p["name"] == "grepglint")


def validate_tag(tag, expected_version):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or tag != "v" + expected_version:
        raise ValueError("selected tag must be v<Cargo package version>, without a prerelease suffix")


def asset_name(tag, target):
    if target not in TARGETS:
        raise ValueError("unsupported release target")
    return f"grepglint-{tag}-{target}"


def validate_binary(path, target):
    if target not in TARGETS:
        raise ValueError("unsupported release target")
    if path.is_symlink() or not path.is_file() or not 32 <= path.stat().st_size <= MAX_BYTES:
        raise ValueError(f"missing, nonregular, empty, or oversized executable: {path}")
    with path.open("rb") as source:
        header = source.read(32)
    if target == TARGETS[0]:
        valid = header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00"
    else:
        valid = header[:8] == b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"
    if not valid:
        raise ValueError(f"executable target mismatch: {path} expected {target}")


def manifest(directory, tag):
    lines = []
    for target in TARGETS:
        name = asset_name(tag, target)
        path = directory / name
        validate_binary(path, target)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {name}\n")
    return "".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("tag")
    tag.add_argument("tag", nargs="?")
    check = sub.add_parser("check")
    check.add_argument("path", type=Path)
    check.add_argument("target", choices=TARGETS)
    sums = sub.add_parser("manifest")
    sums.add_argument("directory", type=Path)
    sums.add_argument("tag")
    args = parser.parse_args()
    if args.command == "tag":
        selected = args.tag or "v" + version()
        validate_tag(selected, version())
        print(selected)
    elif args.command == "check":
        validate_binary(args.path, args.target)
    else:
        validate_tag(args.tag, version())
        (args.directory / "SHA256SUMS").write_text(manifest(args.directory, args.tag))


if __name__ == "__main__":
    main()
