# Installer investigation

A repeatable `./install` entrypoint fits Grepglint. It should report what is
installed, explain any problem, and offer install, upgrade, verify, repair,
uninstall, or cache purge. This is a proposed follow-up; the current working
installation uses `cargo install --path . --locked`.

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

The current protocol serves searches only. Add a daemon build/protocol
handshake and a controlled shutdown request before implementing upgrades.
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
