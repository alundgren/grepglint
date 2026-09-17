# Release binaries and verification

The release workflow builds raw executables with locked Cargo dependencies on
GitHub-hosted runners. No archive extraction is needed to install them.
A release is selected by an exact `vMAJOR.MINOR.PATCH` tag matching the
`grepglint` package version reported by Cargo metadata. Prereleases are not
part of this initial contract.

| Target | Raw executable asset | Oldest tested runtime |
| --- | --- | --- |
| `x86_64-unknown-linux-gnu` | `grepglint-vVERSION-x86_64-unknown-linux-gnu` | Ubuntu 22.04 x86_64, glibc 2.35 |
| `aarch64-apple-darwin` | `grepglint-vVERSION-aarch64-apple-darwin` | macOS 14 Apple Silicon |

For version 0.1.0, `vVERSION` is `v0.1.0`. The Linux build uses
`ubuntu-22.04`; the macOS build uses `macos-14` and sets
`MACOSX_DEPLOYMENT_TARGET=14.0`. Both use Rust 1.89.0. The workflow checks
runner architecture and executable headers, runs the test suite, and runs a
disposable Git fixture through each release executable. These are tested
baselines, not a claim that every distribution or later OS is compatible.
Older systems, musl Linux, Linux ARM64, Intel macOS and Windows are unsupported.

Every release also contains `SHA256SUMS`, with exactly one SHA-256 line per
executable. Hashes refer to raw executable bytes, matching daemon health's
executable digest. Each asset is at most 128 MiB. Attestations stored by GitHub
cover those same executable bytes, not an upload archive. The checksum file
is useful for transfer checks but is not independently trusted.

## Download and verify before executing

Install GitHub CLI 2.80.0 or newer and Git. The minimum required capability is
`gh attestation verify` with `--signer-workflow`, `--source-ref`,
`--source-digest`, `--signer-digest`, and `--deny-self-hosted-runners`.
Authenticate with `gh auth login`, including for the public repository's
attestation API. Automated callers can supply `GH_TOKEN` with read access.
Do not print tokens or store them in the installation record.

Select the tag deliberately and resolve its full source commit through GitHub
or a trusted checkout. Do not take the expected commit from an unverified
asset or its adjacent checksum file. For example, in a trusted checkout after
fetching the selected tag, `git rev-parse 'v0.1.0^{commit}'` gives the commit.
Record the selected tag and commit before downloading, and retain both in the
installation record. Refuse a tag that resolves differently during the operation.

The following example is for Linux. Substitute the documented macOS target
when needed and replace the commit placeholder with that resolved full ID.
Use a new private temporary directory, with `umask 077`.

```sh
tag=v0.1.0
commit=REPLACE_WITH_SELECTED_FULL_SOURCE_COMMIT
target=x86_64-unknown-linux-gnu
asset="grepglint-$tag-$target"
gh release download "$tag" --repo alundgren/grepglint \
  --pattern "$asset" --pattern SHA256SUMS
gh attestation verify "$asset" --repo alundgren/grepglint \
  --signer-workflow alundgren/grepglint/.github/workflows/release.yml \
  --source-ref "refs/tags/$tag" --source-digest "$commit" \
  --signer-digest "$commit" --deny-self-hosted-runners
```

Require a zero exit status before marking the binary executable or running it.
The verification binds bytes to this repository, this signer workflow, the
selected tag, its commit and a GitHub-hosted runner. Merely supplying `--repo`
would accept artifacts from other workflows or revisions in the repository.
Check the selected asset's SHA-256 against its unique entry in `SHA256SUMS`,
then save the verified byte digest for ownership and daemon identity checks.
Use `sha256sum` on Linux or `shasum -a 256` on macOS. The installer must reject
missing, duplicate, malformed or wrong-target entries and enforce the size cap
while downloading. Do not run an asset to discover its version before verifying
its provenance. After verification, check its target and `--version`, then run
the installation fixture.

Attestations establish provenance. They do not establish that the selected
source, dependencies or build tools are free from malicious code or bugs.
This initial process trusts GitHub, the repository's maintainers, pinned actions,
Rust distribution and GitHub-hosted runner images. Independently isolated
builders, reproducible-build comparisons, custom signing keys and package
manager distribution remain future hardening.

## Maintainer procedure

1. Review and merge the intended source and `Cargo.lock`. Set the intended
   version in `Cargo.toml`. Run ordinary CI and the nonpublishing `Release`
   workflow on that exact commit. Branch pushes, pull requests and manual
   dispatch build and test both targets and assemble/check the complete
   checksum manifest. They never attest or publish.
2. Require both native builds, fixture checks, negative contract tests and the
   `checksums` job to pass. Confirm the source commit and action pins in review.
   The initial pins use checkout v4.2.2, upload-artifact v4.6.2,
   download-artifact v4.3.0 and attest-build-provenance v2.4.0. Their resolved
   commit IDs and action metadata were checked, including the attestation
   action's pinned internal actions. This is not an audit of all bundled code.
3. Only a maintainer creates and pushes `v<Cargo version>` at that checked
   commit. Do not move or reuse a released tag. A tag push reruns all checks;
   only then can the tag-only `publish` job obtain publication and attestation
   permissions. Its inputs are artifacts from that same run. PR code receives
   no such permissions. Keep tag creation restricted to trusted maintainers;
   repository protection configuration is an operational responsibility.
4. The workflow attests both executable files and creates the release. Inspect
   the release assets, source commit and run result. If publication fails,
   inspect remote state before retrying. There is no overwrite or automatic
   deletion path for an existing release.
5. The final human release-verification item owns the first actual publication,
   hosted-attestation verification and end-user download/install smoke run on
   both supported platforms. Implementing this workflow publishes nothing.
   Local contract tests and CI artifacts do not prove hosted verification.

For local development, run `python3 -m unittest discover -s scripts -p
test_release.py`, build with `cargo build --release --locked --target TARGET`,
and run `python3 scripts/release_smoke.py target/TARGET/release/grepglint
vVERSION`. Fixture files and caches are temporary; personal repositories and
an existing daemon are not used.

References: [GitHub attestation verification flags](https://cli.github.com/manual/gh_attestation_verify),
[GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations),
[hosted runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
