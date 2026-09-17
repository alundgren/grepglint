# Installer investigation

This investigation informed the managed installation below. The historical
comparison and proposals explain the choices; the managed installation section
states current behavior. Source development still uses
`cargo install --path . --locked`.

## What Codex Scope does

The scout examined `alundgren/codex-scope` at commit
`831d03d32d4952a3888664c51cac897085b331a1`. The parent also checked the entrypoint,
state-aware menu, and upgrade/recovery code directly.

Its [14-line shell entrypoint](https://github.com/alundgren/codex-scope/blob/831d03d32d4952a3888664c51cac897085b331a1/linux/install.sh#L1-L14)
uses adjacent prebuilt binaries when available, otherwise builds with Cargo,
then enters the native `setup` command. The shell does not contain the installer
logic. An installed recovery binary and management launcher keep maintenance
available after initial setup.

The [setup flow](https://github.com/alundgren/codex-scope/blob/831d03d32d4952a3888664c51cac897085b331a1/linux/src/setup/flow.rs#L801-L900)
detects the installation record and offers actions appropriate to its phase.
Existing installations offer upgrade, inspect, verify, uninstall, and purge.
Interrupted operations prompt for recovery before proceeding. There is no
separate repair action; recovery followed by rerunning the entrypoint fills
part of that role.

The [installation record](https://github.com/alundgren/codex-scope/blob/831d03d32d4952a3888664c51cac897085b331a1/linux/src/setup/managed.rs#L15-L65)
tracks phase, owned files and hashes, directories, and changes to configuration.
The installer writes recovery state before mutation. Its
[upgrade procedure](https://github.com/alundgren/codex-scope/blob/831d03d32d4952a3888664c51cac897085b331a1/linux/src/setup/upgrade.rs#L79-L201)
checks ownership, saves the old binaries, records an upgrade in progress,
replaces them, verifies the result, and restores the previous version on failure.

[Uninstall and purge](https://github.com/alundgren/codex-scope/blob/831d03d32d4952a3888664c51cac897085b331a1/linux/src/setup/managed.rs#L311-L595)
remove only resources that still match recorded ownership. Edited files remain
for inspection. Purge is a separate action for retained data. The implementation
uses an operation lock and runs as the normal OS account. Its
[fixture tests](https://github.com/alundgren/codex-scope/blob/831d03d32d4952a3888664c51cac897085b331a1/linux/tests/setup/managed.rs#L386-L724)
cover interrupted upgrades, preservation of edited resources, repeated
uninstall, and purge refusal.

## A smaller version for Grepglint

Grepglint needs one executable, an installation record, and its private cache.
The installer can omit Codex hook changes, Tailscale routes, credentials, and
systemd configuration. Search should retain automatic daemon startup and idle
exit. Installer controls belong to human maintenance, while the agent-facing
`tools` catalog continues to describe repository exploration tools.

| Action | Proposed behavior |
| --- | --- |
| Install | Explain selected paths, install a verified release binary, record ownership, and run a fixture check |
| Upgrade | Check installed ownership, save the previous executable, stop the old daemon safely, replace atomically, verify, and roll back on failure |
| Verify | Check binary version, running daemon version, socket permissions, cache access, resource settings, and a disposable repository search |
| Repair | Recover an interrupted operation or restore missing owned files; explain unexpected edits before touching them |
| Uninstall | Stop the owned daemon and remove the owned executable; retain cached source contents unless purge is chosen |
| Purge | Explicitly remove the recorded cache and retained installer files after shutdown |

Use a small durable record under the OS account's state directory. Record
managed paths, binary hashes, the current operation, and rollback information.
Serialize installer runs and record intended changes before performing them.
An unchanged binary and healthy installation should finish without a restart.

Prebuilt release assets would make normal installation easier. The measured
release binary is about 6.2 MiB; building locally needs Rust, a C compiler,
dependencies, and a much larger build cache. Grepglint has no published binary
release assets yet. Source installation remains useful for development.

## Prerequisites in Grepglint

The investigation identified a daemon build/protocol handshake and controlled
shutdown as prerequisites. Both are now available through the maintenance API.
An upgraded CLI must be able to identify and stop the old daemon; otherwise
the old executable can remain active until its idle timeout. A PID file alone
is insufficient proof of process identity.

Verification can use a temporary Git fixture and a separate
`GREPGLINT_CACHE_DIR`. It should test search, cache reuse, and dirty-file
freshness without indexing personal repositories. Also check the real daemon's
identity and configured limits through the proposed health request. Test
failed upgrades, interruption recovery, modified executables, repeated
installation, repeated uninstall, and purge ownership using temporary paths.

The next implementation should deliver the human flow and these lifecycle
operations together. It should not require agents to learn install or refresh
commands before using search.

## Managed installation

Run `./install` only from a checkout you trust. Its four-line shell launcher
runs the adjacent trusted Python 3 bootstrap. Git, Python 3, and authenticated
GitHub CLI 2.80.0 or newer are required for first installation. See
[release verification](releases.md) for the supported platforms and trust
policy. Nothing downloaded executes until checksum, executable target,
attestation and unchanged release tag checks pass. The verified executable's
version is then checked before native installation.

```sh
./install install --release v0.1.0
./install verify
./install status
```

With a terminal, `./install` shows the recorded phase and offers install or
resume, verify, status, or cancel. Without a terminal, select an action
explicitly. Installation also requires an exact release tag. Cancellation
before installation changes nothing. Exit zero means success or cancellation;
one means refusal or failed verification; command-line usage errors exit two.
Upgrade, general repair, uninstall and purge currently return unavailable.

The default executable is `~/.local/bin/grepglint`. Add that directory to PATH
when needed; installation never edits profiles. `--destination`, `--cache-dir`
and `--state-dir` accept absolute paths with no symlink components or `..`.
On macOS, use the physical path, such as `/private/tmp` instead of `/tmp`.
Destination, cache and state paths must be separate. Existing destination
parents cannot be writable by another account. State and cache must be private
and account-owned. Installation refuses root execution.

The version-one `record.json` lives under `$XDG_STATE_HOME/grepglint` or
`~/.local/state/grepglint`. It records selected tag, resolved commit, executable
SHA-256, paths, cache ownership, effective settings, prior executable hash and
operation phase. `maintenance` is the retained verified executable; `previous`
is the single temporary Cargo recovery copy. These paths and their
`.grepglint-pending` files are reserved by the durable installation intent.
Unknown record versions, changed executables, symlinks, FIFOs, multiple hard
links and unsafe permissions are refused. No Cargo registry is edited.
Preexisting cache contents remain unowned even when the installation uses that
cache. Lock files remain in place.

If Cargo installed the selected destination, the terminal asks for consent.
For unattended use, `--migrate-cargo` explicitly
permits replacement after its `.crates2.json` ownership entry is checked.
The prior bytes remain in private recovery storage until verification passes.
The same flag acknowledges a separate Cargo executable found on PATH, which
is left untouched. Output explains which executable is selected and possible
PATH shadowing. Unknown executables require manual resolution.

Rerun the same install command after interruption. The durable phases are
`prepared`, `retained`, `installed`, and `complete`. An interrupted partial copy
is reused only when its bytes match the verified source prefix. Unexpected
pending contents are preserved and reported. Publication uses an atomic
same-filesystem rename; fresh publication refuses an existing destination.
Failure after replacement restores the prior bytes or removes only the new
matching executable. It retains the record and maintenance copy for retry.
If interruption preceded the retained copy, the trusted bootstrap re-downloads
and verifies the recorded release. No unverified recovery helper runs.

After retention, `./install verify` and `./install install` use the recorded,
hash-checked maintenance executable without gh or network. The installed
executable is not needed to launch recovery. A modified maintenance copy is
refused before execution. A stable direct local invocation is
`~/.local/state/grepglint/maintenance setup verify`; supply `--state-dir` when
using a different state directory. Direct native installation is for an
already trusted executable with explicit provenance arguments; normal release
installation must use the trusted bootstrap.

Verification checks recorded executable bytes/version, private cache access,
and the running daemon's identity and effective resource limits. A missing
real daemon is reported without starting it. A disposable Git repository and
separate 8 MiB cache prove cold search, warm reuse, dirty freshness and removal
of old search results. Its daemon is stopped before cleanup; failed shutdown
preserves the fixture and reports its path. Verification never repairs an
unhealthy installation. Installing the same healthy release runs verification
without replacing files or restarting the real daemon.

### Installer resource limits

| Resource | Limit |
| --- | --- |
| Raw asset | 128 MiB, enforced while downloading and copying |
| Checksum manifest / state record | 4 KiB / 64 KiB |
| Bootstrap subprocess output | 1 MiB combined; version output 4 KiB |
| Bootstrap subprocess time | 30 seconds each; attestation 60 seconds; downloaded version 10 seconds |
| Download | 120 seconds checked between reads, 10-second socket timeout; no retries; HTTPS-only redirects |
| Native subprocess | 20 seconds, 64 KiB combined output; process group killed on failure |
| Verification fixture | Four searches; 8 MiB database, at most 8 MiB rollback journal; two-second idle fallback |
| Disk preflight | 576 MiB free on staging, destination and state filesystems; fixture needs 80 MiB |
| Concurrency | One installer per state directory; daemon exclusion through replacement, fixture and completion |

The disk preflight covers the bounded download, retained executable,
destination staging and one prior copy with a 64 MiB reserve. Checks are
conservative when paths share a filesystem. External disk consumption can
still exhaust space after preflight; filesystem errors preserve the durable
recovery state. Executable copying and hashing use 64 KiB buffers. The Python
bootstrap holds at most one 128 MiB asset plus bounded metadata while hashing.
No downloads or maintenance commands enter the agent tool catalog. Development
installation with `cargo install --path . --locked` remains supported.

A Linux measurement using a locally built release executable and synthetic
fixture provenance took about 0.7 seconds for native installation and 0.4 to
0.5 seconds for first and repeated verification. The executable was about
9 MB; retained installer data was also about 9 MB. Sampled installation,
staging and fixture files peaked near 27.2 MB. Summed RSS sampled across the
native installer, search processes, fixture daemon and Git peaked near
19.7 MiB. Sampling can miss short peaks and counts shared pages per process;
these observations are not memory guarantees. Native installation returned
265 output bytes, verification 133, with no stderr. Measurements exclude gh,
network transfer and Python download verification. Each verification creates
a new fixture; its internal second search proves cache reuse. Repeated whole
verification measures a warmed machine, not a retained personal index.
