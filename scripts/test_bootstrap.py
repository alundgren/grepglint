"""No network: hostile releases must never reach downloaded setup execution."""
import hashlib
import json
import os
import http.server
import ssl
import subprocess
from contextlib import contextmanager
import threading
import time
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bootstrap as b


@contextmanager
def https_fixture():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        config = root / "certificate.conf"
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost\nbasicConstraints=critical,CA:true\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-config", str(config), "-keyout", str(root / "key.pem"), "-out", str(root / "certificate.pem")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       env={**os.environ, "HOME": str(root), "RANDFILE": str(root / "random-state")})

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"x" * 100 if self.path == "/slow" else b"verified transfer"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if self.path == "/slow":
                            time.sleep(0.02)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / "certificate.pem", root / "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(root / "certificate.pem"), "no_proxy": "localhost,127.0.0.1"}):
                yield f"https://localhost:{server.server_port}", root
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class BootstrapTests(unittest.TestCase):
    def test_unsupported_platform_and_https(self):
        with patch.object(b.platform, "system", return_value="Windows"):
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                b.target()
        with self.assertRaisesRegex(ValueError, "Non-HTTPS"):
            b.HttpsRedirect().redirect_request(None, None, 302, "", {}, "http://example.invalid")
        with self.assertRaisesRegex(ValueError, "Only HTTPS"):
            b.download("http://example.invalid", Path("unused"), 10)

    def test_bounded_output_failure_and_deadline(self):
        with self.assertRaisesRegex(ValueError, "output exceeded"):
            b.run(["python3", "-c", "print('x'*10000)"], cap=100)
        with self.assertRaisesRegex(ValueError, "timed out"):
            b.run(["python3", "-c", "import time;time.sleep(5)"], timeout=0.05)
        with self.assertRaisesRegex(ValueError, "failed"):
            b.run(["python3", "-c", "raise SystemExit(1)"])

    def test_exited_parent_descendant_is_stopped_on_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "survived"
            child = "import time; from pathlib import Path; time.sleep(0.5); Path(" + repr(str(marker)) + ").touch()"
            parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
            with self.assertRaisesRegex(ValueError, "timed out"):
                b.run([sys.executable, "-c", parent], timeout=0.1)
            time.sleep(0.6)
            self.assertFalse(marker.exists(), "Descendant survived timeout cleanup")

    def test_download_worker_launches_fresh_interpreter(self):
        with https_fixture() as (url, root):
            output = root / "asset"
            original_run = b.run
            with patch.object(b, "run", wraps=original_run) as launch:
                b.download(url + "/fast", output, 1000)
            args = launch.call_args.args[0]
            self.assertEqual(args[:3], [sys.executable, str(Path(b.__file__).resolve()), "__download"])
            self.assertEqual(output.read_bytes(), b"verified transfer")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_trickle_body_has_an_absolute_deadline(self):
        with https_fixture() as (url, root):
            started = time.monotonic()
            with patch.object(b, "DOWNLOAD_SECONDS", 0.5):
                with self.assertRaisesRegex(ValueError, "absolute elapsed-time"):
                    b.download(url + "/slow", root / "asset", 1000)
            self.assertLess(time.monotonic() - started, 1.2)
            self.assertEqual((root / "asset").stat().st_size, 0)

    def test_release_rejections_before_execution(self):
        for failure in ["checksum", "duplicate", "missing", "wrong-target", "attestation", "changed-tag", "oversize", "old-gh", "metadata"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                marker = root / "executed"
                # If the bootstrap ever executes this unverified asset, it leaves evidence.
                payload = f"#!/bin/sh\ntouch '{marker}'\n".encode()
                tag = "v0.1.0"
                name = f"grepglint-{tag}-x86_64-unknown-linux-gnu"
                digest = hashlib.sha256(payload).hexdigest()
                hashes = f"{digest}  {name}\n{'b'*64}  grepglint-{tag}-aarch64-apple-darwin\n"
                if failure == "checksum": hashes = hashes.replace(digest, "c"*64)
                if failure == "duplicate": hashes += f"{digest}  {name}\n"
                if failure == "missing": hashes = hashes.splitlines()[0] + "\n"
                metadata = {"tag_name": tag, "draft": False, "prerelease": False,
                            "assets": [{"name": item, "size": len(payload) if item == name else len(hashes),
                                        "browser_download_url": f"https://github.com/{b.REPO}/releases/download/{tag}/{item}"}
                                       for item in [name, "SHA256SUMS"]]}
                if failure == "oversize": metadata["assets"][0]["size"] = b.CAP + 1
                if failure == "metadata": metadata["tag_name"] = "v9.0.0"
                commits = []
                original_run = b.run

                def run(args, **kwargs):
                    if args[0] != "gh": return original_run(args, **kwargs)
                    if args[1] == "--version": return b"gh version 2.79.0" if failure == "old-gh" else b"gh version 2.80.0"
                    if args[1] == "attestation":
                        self.assertIn("--deny-self-hosted-runners", args)
                        self.assertIn("--signer-digest", args)
                        self.assertEqual(args[args.index("--repo")+1], b.REPO)
                        self.assertEqual(args[args.index("--signer-workflow")+1], b.REPO+"/.github/workflows/release.yml")
                        self.assertEqual(args[args.index("--source-ref")+1], "refs/tags/"+tag)
                        self.assertEqual(args[args.index("--source-digest")+1], "a"*40)
                        self.assertEqual(args[args.index("--signer-digest")+1], "a"*40)
                        if failure == "attestation": raise ValueError("attestation refused")
                        return b""
                    if "/commits/" in args[2]:
                        commits.append(1)
                        return ("d"*40 if failure == "changed-tag" and len(commits) == 2 else "a"*40).encode()
                    return json.dumps(metadata).encode()

                def download(url, path, cap):
                    path.write_bytes(hashes.encode() if path.name == "SHA256SUMS" else payload)
                    path.chmod(0o600)

                def header(data, selected):
                    if failure == "wrong-target": raise ValueError("wrong target")

                with patch.object(b, "target", return_value="x86_64-unknown-linux-gnu"), patch.object(b, "run", side_effect=run), patch.object(b, "download", side_effect=download), patch.object(b, "check_header", side_effect=header):
                    with self.assertRaises(ValueError): b.fetch(tag, root)
                self.assertFalse(marker.exists(), failure)

    def test_real_header_checks(self):
        with self.assertRaisesRegex(ValueError, "Wrong Linux"):
            b.check_header(b"#!/bin/sh", "x86_64-unknown-linux-gnu")
        with self.assertRaisesRegex(ValueError, "Wrong macOS"):
            b.check_header(b"\x7fELF" + bytes(30), "aarch64-apple-darwin")

    def test_noninteractive_requires_action_and_no_state_removal_is_local(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(b.sys.stdin, "isatty", return_value=False):
                with self.assertRaisesRegex(ValueError, "explicit action"):
                    b.main(["--state-dir", str(Path(temp).resolve()/"state")])
            for action in ["uninstall", "purge"]:
                with patch.object(b, "fetch", side_effect=AssertionError("network")):
                    self.assertEqual(b.main([action, "--state-dir", str(Path(temp).resolve()/"state")]), 0)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_upgrade_and_missing_file_repair_use_existing_verifier(self):
        for action in ("upgrade", "repair"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                state = root / "state"
                state.mkdir(mode=0o700)
                destination = root / "binary"
                destination.write_bytes(b"old verified binary")
                destination.chmod(0o700)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                record = dict(schema_version=1, phase="complete", release="v0.1.0", commit="a"*40,
                              digest=digest, destination=str(destination), cache=str(root/"cache"),
                              cache_owned=True, database_bytes=8*1024*1024, idle_seconds=2,
                              previous_digest=None, previous_mode=None, cargo_digest=None)
                (state/"record.json").write_text(json.dumps(record))
                (state/"record.json").chmod(0o600)
                if action == "upgrade":
                    (state/"maintenance").write_bytes(destination.read_bytes())
                    (state/"maintenance").chmod(0o700)
                else:
                    destination.unlink()
                before = (state/"record.json").read_bytes()
                with patch.object(b, "fetch", side_effect=ValueError("attestation refused")) as fetch, patch.object(b.subprocess, "call") as execute:
                    args = [action, "--state-dir", str(state)]
                    if action == "upgrade": args += ["--release", "v0.2.0"]
                    with self.assertRaisesRegex(ValueError, "attestation refused"): b.main(args)
                    self.assertEqual(fetch.call_args.args[0], "v0.2.0" if action == "upgrade" else "v0.1.0")
                    execute.assert_not_called()
                self.assertEqual((state/"record.json").read_bytes(), before)
                if action == "upgrade": self.assertEqual(destination.read_bytes(), b"old verified binary")
                self.assertFalse((state/"candidate").exists())
                record["unknown"] = "preserve"
                (state/"record.json").write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "record fields"): b.main([action, "--state-dir", str(state)])

    def test_preceding_installer_repair_compatibility(self):
        for missing in ("destination", "maintenance", "none", "interrupted", "repairing", "repairing-both"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                state = root / "state"
                state.mkdir(mode=0o700)
                destination = root / "binary"
                maintenance = state / "maintenance"
                marker = root / "action"
                # The preceding installer accepts help/install/verify but rejects repair.
                old = f"#!/bin/sh\ncase \"$2\" in --help) echo 'install verify status';; repair) echo 'This action is not available in this version' >&2; exit 1;; install|verify) echo \"$2\" > '{marker}';; *) exit 2;; esac\n".encode()
                for path in (destination, maintenance):
                    path.write_bytes(old)
                    path.chmod(0o700)
                source = maintenance
                if missing == "destination": destination.unlink()
                if missing == "maintenance": maintenance.unlink(); source = destination
                if missing == "repairing": destination.unlink()
                record = dict(phase="retained" if missing == "interrupted" else "repairing" if missing in ("repairing", "repairing-both") else "complete",
                              destination=str(destination), release="v0.1.0", digest=hashlib.sha256(old).hexdigest())
                args = ["setup", "repair", "--state-dir", str(state)]
                with patch.object(b, "repair_with_helper", return_value=0) as helper:
                    self.assertEqual(b.repair_local(record, args, source, "v0.2.0"), 0)
                    if missing in ("none", "interrupted"):
                        helper.assert_not_called()
                        self.assertEqual(marker.read_text().strip(), "verify" if missing == "none" else "install")
                    else:
                        helper.assert_called_once_with(record, args, source, "v0.2.0")
                        self.assertFalse(marker.exists())

    def test_old_removal_requires_explicit_local_helper_without_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state = root / "state"
            state.mkdir(mode=0o700)
            marker = root / "action"
            old = b"#!/bin/sh\nif [ \"$2\" = --help ]; then echo --repair-source; else exit 99; fi\n"
            maintenance = state / "maintenance"
            maintenance.write_bytes(old)
            maintenance.chmod(0o700)
            helper = root / "helper"
            helper.write_text(f"#!/bin/sh\nif [ \"$2\" = --help ]; then echo --purge-cache; else echo \"$2\" > '{marker}'; fi\n")
            helper.chmod(0o700)
            record = dict(schema_version=2, phase="complete", release="v0.1.0", commit="a"*40,
                          digest=hashlib.sha256(old).hexdigest(), destination=str(root/"binary"), cache=str(root/"cache"),
                          cache_owned=True, database_bytes=8*1024*1024, idle_seconds=2,
                          previous_digest=None, previous_mode=None, cargo_digest=None)
            (state/"record.json").write_text(json.dumps(record))
            (state/"record.json").chmod(0o600)
            with patch.object(b, "fetch", side_effect=AssertionError("network")):
                with self.assertRaisesRegex(ValueError, "predates offline removal"):
                    b.main(["uninstall", "--yes", "--state-dir", str(state)])
                self.assertFalse(marker.exists())
                self.assertEqual(b.main(["uninstall", "--yes", "--state-dir", str(state), "--removal-helper", str(helper)]), 0)
                self.assertEqual(marker.read_text().strip(), "uninstall")
                marker.unlink()
                maintenance.write_bytes(b"edited")
                with self.assertRaisesRegex(ValueError, "Maintenance copy changed"):
                    b.main(["purge", "--purge-cache", "--state-dir", str(state), "--removal-helper", str(helper)])
                self.assertFalse(marker.exists())

    def test_finalization_cancel_precedes_lock_or_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with patch.object(b.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="n"), patch.object(b, "local_lock", side_effect=AssertionError("lock attempted")):
                self.assertEqual(b.finish_purge(root, {"cache": str(root/"cache")}, False), 0)
            with patch.object(b.sys.stdin, "isatty", return_value=False):
                with self.assertRaisesRegex(ValueError, "--purge-cache"):
                    b.finish_purge(root, {"cache": str(root/"cache")}, False)
            self.assertEqual(list(root.iterdir()), [])

    def test_cancel_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(b.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="0"):
            self.assertEqual(b.main(["--state-dir", str(Path(temp).resolve()/"state")]), 0)
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
