import hashlib
from pathlib import Path
import tempfile
import unittest
import release


class ReleaseContract(unittest.TestCase):
    def test_tag_matches_metadata(self):
        release.validate_tag("v0.1.0", "0.1.0")
        for tag in ("v0.2.0", "0.1.0", "v0.1.0-rc1", "v0.1.0\n"):
            with self.assertRaises(ValueError):
                release.validate_tag(tag, "0.1.0")

    def test_assets_checksums_and_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binaries = [bytearray(32), bytearray(32)]
            binaries[0][:6] = b"\x7fELF\x02\x01"
            binaries[0][18:20] = b"\x3e\x00"
            binaries[1][:8] = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"
            paths = [directory / release.asset_name("v0.1.0", t) for t in release.TARGETS]
            with self.assertRaises(ValueError):
                release.manifest(directory, "v0.1.0")
            for path, binary in zip(paths, binaries):
                path.write_bytes(binary)
            expected = "".join(f"{hashlib.sha256(b).hexdigest()}  {p.name}\n"
                               for p, b in zip(paths, binaries))
            self.assertEqual(release.manifest(directory, "v0.1.0"), expected)
            paths[0].write_bytes(binaries[1])
            with self.assertRaisesRegex(ValueError, "target mismatch"):
                release.manifest(directory, "v0.1.0")
            paths[0].unlink()
            paths[0].symlink_to(paths[1])
            with self.assertRaises(ValueError):
                release.manifest(directory, "v0.1.0")

    def test_unknown_target_and_size(self):
        with self.assertRaises(ValueError):
            release.asset_name("v0.1.0", "x86_64-apple-darwin")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large"
            with path.open("wb") as output:
                output.truncate(release.MAX_BYTES + 1)
            with self.assertRaises(ValueError):
                release.validate_binary(path, release.TARGETS[0])


if __name__ == "__main__":
    unittest.main()
