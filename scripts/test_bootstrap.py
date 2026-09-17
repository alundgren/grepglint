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

    def test_noninteractive_requires_action_and_future_actions_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(b.sys.stdin, "isatty", return_value=False):
                with self.assertRaisesRegex(ValueError, "explicit action"):
                    b.main(["--state-dir", str(Path(temp).resolve()/"state")])
            for action in ["upgrade", "repair", "uninstall", "purge"]:
                with self.assertRaisesRegex(ValueError, "not available"):
                    b.main([action, "--state-dir", str(Path(temp).resolve()/"state")])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_cancel_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(b.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="0"):
            self.assertEqual(b.main(["--state-dir", str(Path(temp).resolve()/"state")]), 0)
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
