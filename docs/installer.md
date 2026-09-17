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
./install upgrade --release v0.2.0
./install repair
./install uninstall --yes
./install purge --purge-cache
```

With a terminal, `./install` shows the recorded phase and offers install or
resume, verify, status, upgrade, repair, uninstall, purge, or cancel. Without a terminal, select an action
explicitly. Installation also requires an exact release tag. Cancellation
before installation changes nothing. Exit zero means success or cancellation;
one means refusal or failed verification; command-line usage errors exit two.
Uninstall retains cached source contents; purge requires separate consent.
Upgrade requires an exact release tag; repair uses recorded identities.

The default executable is `~/.local/bin/grepglint`. Add that directory to PATH
when needed; installation never edits profiles. `--destination`, `--cache-dir`
and `--state-dir` accept absolute paths with no symlink components or `..`.
On macOS, use the physical path, such as `/private/tmp` instead of `/tmp`.
Destination, cache and state paths must be separate. Existing destination
parents cannot be writable by another account. State and cache must be private
and account-owned. Installation refuses root execution.

The `record.json` lives under `$XDG_STATE_HOME/grepglint` or
`~/.local/state/grepglint`. It records selected tag, resolved commit, executable
SHA-256, paths, cache ownership, effective settings, prior executable hash and
operation phase. Existing version-one records remain readable. Upgrade records
use version two and retain the complete prior record and both permission modes
until completion. `maintenance` is the retained verified executable; `previous`
is the single temporary recovery copy. During upgrade, `candidate` holds the
verified replacement. These paths and their
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

### Upgrade and repair

`./install upgrade --release vX.Y.Z` stages and attests the candidate using the
same release policy as installation. Native setup checks both existing copies,
free disk space and cache format before recording intended changes. It retains
one prior executable and the candidate, then holds daemon maintenance exclusion
through shutdown, both replacements, isolated verification and durable
completion. Only a daemon whose executable hash matches the current or prior
record is stopped. Unknown or legacy daemons require pausing searches until
their normal idle exit; use `rg` meanwhile.

Upgrade records progress through `upgrade_prepared`, `upgrade_retained`,
`upgrade_installed`, `upgrade_copied` and `complete`. The candidate and prior
copy remain until completion is durable. A failed verification restores both
prior executable locations and their original permissions. `rollback` and
`rollback_complete` record restoration and cleanup separately. A failed
restoration reports failure and preserves recovery data for `./install repair`.
Repair never overwrites an edited file to make recovery succeed.

An interrupted upgrade is rolled back before another action proceeds. Rerun
upgrade afterward if still wanted. A completed upgrade with interrupted cleanup
finishes cleanup. The bootstrap selects a hash-checked recorded candidate for
recovery; if it is absent, it fetches that exact recorded release and requires
the same commit and digest. This lets the recovery code understand the new
record even when the retained old executable predates that format. The old
record is restored after successful rollback cleanup.

`./install repair` restores missing owned files from a verified retained or
installed copy. If both are missing, it downloads and attests the exact recorded
release. For older installer executables that do not expose the native `--repair-source`
option, healthy repair uses their existing verify action without downloading
anything. Interrupted first installation uses their existing install action.
Missing-file repair may need newer maintenance code. The bootstrap prints its
selected helper release and verifies it through the same release policy, then
passes the hash-checked recorded executable as the restoration source. The
installed release remains unchanged. `./install repair --release vX.Y.Z`
selects that helper explicitly; otherwise the repository's latest stable release
is selected and verified. If it still lacks repair support, repair refuses with
instructions to choose a newer release. No unverified helper runs.

The durable `repairing` phase records missing-file restoration until verification
succeeds. Repair also resumes an interrupted first installation. Changed bytes,
unsafe file objects, unknown fields and unknown record versions cause a refusal
that preserves the files. Healthy repair reports no changes. Upgrading to the
same healthy release leaves both files and the real daemon instance unchanged.

Cache checks open an existing database read-only and require its recorded
SQLite format version to match the binary. An incompatible cache is refused
before replacement or migration. No real-cache schema migration or personal
repository search occurs during setup. Existing cache ownership is unchanged.
The fixture uses a different cache while the real-cache exclusion stays held.

The operation lock rejects competing installer actions immediately. Search
startup and requests refuse work during maintenance. Operations perform a
fixed number of bounded subprocesses, with no automatic retry or restart loop.
Interrupted partial copies are removed only after their bytes match a verified
source prefix. Unknown temporary contents remain for inspection. Cleanup only
removes files named by the durable record; stable coordination locks remain.

### Installer resource limits

| Resource | Limit |
| --- | --- |
| Raw asset | 128 MiB, enforced while downloading and copying |
| Checksum manifest / state record | 4 KiB / 64 KiB |
| Bootstrap subprocess output | 1 MiB combined; version output 4 KiB |
| Bootstrap subprocess time | 30 seconds each; attestation 60 seconds; downloaded version 10 seconds |
| Download | 120-second absolute fresh-interpreter worker deadline, including DNS/headers/body; 10-second socket timeout; no retries; HTTPS-only redirects |
| Native subprocess | 20 seconds, 64 KiB combined output; process group killed on failure |
| Verification fixture | Four searches; 8 MiB database, at most 8 MiB rollback journal; two-second idle fallback |
| Disk preflight | Install/staging 576 MiB; upgrade/repair 960 MiB on destination and state filesystems; fixture 80 MiB |
| Concurrency | One installer per state directory; daemon exclusion through replacement, fixture and completion |

The disk preflight covers the bounded download, retained executable,
destination staging and one prior copy with a 64 MiB reserve. Checks are
conservative when paths share a filesystem. Upgrade additionally reserves space
for both old executables, the candidate, the rollback copy and replacement
staging. External disk consumption can
still exhaust space after preflight; filesystem errors preserve the durable
recovery state. Executable copying and hashing use 64 KiB buffers. The Python
bootstrap holds at most one 128 MiB asset plus bounded metadata while hashing.
Core dumps are disabled for bootstrap and native maintenance processes.
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

A Linux release-build sample measured native upgrade at 0.58 seconds and repair
of a missing installed or retained executable at 0.41 to 0.45 seconds. Sampled
upgrade files peaked at 54.1 MB including the staged source, both executables,
recovery files and fixture; completed retained installer data was 9.0 MB.
Summed installer, daemon and Git RSS samples peaked near 19.6 MiB during
upgrade. Healthy upgrade and repair took 0.45 to 0.46 seconds and preserved the
real daemon instance; their samples reached 29.3 MiB with that daemon running.
Outputs were 179 to 218 bytes with no stderr. These are single local samples
with synthetic provenance, excluding download, GitHub CLI and Python costs.
Sampling can miss short peaks and double-count shared pages. They establish
observed behavior, not worst-case resource guarantees.


## Offline uninstall and purge

`./install uninstall --yes` stops the identified daemon and removes the recorded
installed executable. It retains cached source contents, the verified native
maintenance copy, ownership record and coordination locks. Without `--yes`, a
terminal gets a confirmation showing the paths; unattended use refuses.

`./install purge --purge-cache` separately confirms irreversible deletion of
recorded cached source contents. Run it after uninstall to remove retained
installer files too. A new process uses the hash-checked maintenance copy,
even when the installed executable and gh are absent and networking is
unavailable. Purge while installed keeps the executable, maintenance copy and
minimal record so the installation remains manageable. It prints that outcome.

Cache ownership includes the device and inode of a private directory created
by this installer. The installer completes path/capacity preflight and saves
an initial record before creating a private staging directory beside the
selected cache. It records that exclusive directory's identity before atomic
publication without replacement. A concurrently created cache remains unowned.
Recorded staging/publication can resume after interruption. If interruption
precedes the staging record, an empty unrecorded `.grepglint-cache-*` directory
may remain beside the selected cache; it is preserved rather than adopted or
removed by name. It contains no indexed source contents and does not make the
selected cache owned. A matching path or the older `cache_owned` flag alone is
insufficient. Preexisting caches, including Cargo caches, are never adopted.
Purge refuses those caches and leaves their ownership record and maintenance
copy for inspection. It also refuses substituted directories, symlinks,
unexpected entries, edited executables, unsafe types and unknown record
versions. Inspect the specific reported path, preserve any data you need, and
move an unrelated entry only when you know it belongs to you. Retry the same
command afterward. Do not recursively remove a recorded directory.

Cleanup enumerates only `index.sqlite` and its rollback journal as removable
cache data. Empty `maintenance.lock`, `daemon.lock`, and `operation.lock` files
and their directories remain. Their stable inodes protect concurrent callers.
An unexpected diagnostic or other extra file causes incomplete cleanup rather
than automatic deletion. Permission failures return nonzero and retain durable
progress; restore access only on the exact owned path before retrying.

The native command keeps maintenance exclusion while stopping the daemon,
deleting data and completing the record. It never signals a PID from a file.
An unknown daemon or stale socket fails safely; pause searches, allow the
identified old process to exit normally, then retry. Installer operations
serialize on their existing operation lock. Daemon exclusion retains its
35-second deadline and three-second control-exchange limit.

If interruption removes the maintenance executable but leaves the final
ownership record, the trusted entrypoint can finish locally. It validates that
record, cache directory identity, absence of an installed executable, and the
remaining entries while holding operation, maintenance and daemon locks.
It then removes only the ownership record. If another process has repopulated
the cache, it preserves the record and asks for the explicit local helper
below. A completed purge repeated with no owned state is a local no-op.

### Older retained releases

Earlier releases cannot execute the new removal commands. The bootstrap checks
removal capability independently of repair capability and never downloads a
helper during removal. Use `--removal-helper /absolute/path/to/grepglint` to
explicitly select a trusted current native executable obtained or built
separately. For example, run `./install uninstall --yes --removal-helper
/absolute/path/to/grepglint` on one command line. The same flag applies to
purge. This is an explicit source-development trust choice, not release
attestation verification; only select bytes you have independently trusted.
The helper must be an account-owned, unlinked regular file with safe ancestors
and permissions. Modified recorded maintenance bytes are still refused before
any helper execution. No Cargo invocation, metadata update or network access
occurs during cleanup.

Older records without cache device/inode identity cannot authorize cache
deletion. A current helper can uninstall their verified executable, but purge
returns a clear ownership refusal and retains cache and recovery data. This
compatibility limit deliberately avoids adopting existing source caches.
Interrupted upgrade records are recovered using recorded local copies before
removal. Missing verified recovery bytes cause a refusal, preserving the
remaining files; repair those exact bytes separately before retrying removal.

One Linux release-build sample measured native uninstall at 0.038 seconds,
repeat uninstall at 0.020 seconds, and purge at 0.036 seconds. GNU time reported
5.2 to 5.5 MiB maximum native-process RSS. Uninstall retained about 9.1 MB of
maintenance executable and record; final purge retained zero file bytes in the
state directory, plus its empty lock. Output was 314 to 316 bytes without
stderr. Repeated bootstrap purge took 0.12 seconds. These are individual
observations with synthetic local provenance, a small disposable cache and no
running daemon during timing. They do not measure sustained load, gh/network,
attestation, or a busy daemon's shutdown cost.
