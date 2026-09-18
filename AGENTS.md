# Project principles

Grepglint helps people and coding agents get work done. Respect their time,
working files, and machine resources. The editor, builds, tests, and other
agents need room to work. Every service we add must earn its resource cost
and remain easy to understand, stop, recover, and remove.

These principles guide implementation and review. Read [the architecture](docs/architecture.md)
and [current resource limits](README.md#resource-limits) before changing the
daemon, storage, or request handling. Keep documented guarantees consistent
with what the implementation and tests actually establish.

## Bound resource use

- Set explicit limits on memory, cache size, queued work, concurrency,
  subprocess output, parsing, and responses. Future daemon services must share
  a machine resource budget; individually bounded services can still exhaust
  a machine together.
- Account for peak usage during indexing, transactions, recovery, and upgrades.
  Include journals, temporary files, logs, and child processes. Preserve free
  disk space for the rest of the person's work.
- Reuse unchanged content across queries and worktrees. Reparse changed files
  only when needed. Keep caches disposable, reclaim obsolete content, and give
  retained data a bounded lifetime or eviction policy.
- Prefer request-driven work. Sleep when idle, avoid unnecessary polling, and
  keep background work at a low priority. Do not spend CPU on speculative work
  without a measured benefit to the current task.
- Measure resource use under sustained queries and edits. Rust's memory safety
  does not prove bounded memory use or freedom from leaks in native dependencies.

## Keep work moving

- Give requests deadlines and bounded queues. Support cancellation and avoid
  continuing expensive work after its caller no longer needs it. A slow client
  or large repository must not indefinitely block other agents.
- Keep help, tool discovery, and status inexpensive. Return small, useful
  results with enough information to choose the next action.
- Handle daemon startup and index freshness automatically during normal use.
  People should not have to learn index maintenance to finish a coding task.
- Make limits visible and give an immediate alternative when they are reached.
  For code search, falling back to `rg` should let the task continue.

## Recover without creating more work

- Expect interruption, full disks, malformed requests, missing files, stale
  sockets, and concurrent edits. Preserve a consistent state when an operation
  fails, and recover safe temporary state automatically when possible.
- Limit retries and use backoff where appropriate. Avoid restart loops, repeated
  reparsing of failing files, and accumulating diagnostics.
- Explain what failed, whether anything changed, and the useful next step.
  Keep exit codes and structured errors reliable. A failed refresh must not
  silently return a stale or incomplete view as a successful fresh result.
- Keep failure isolated. One service or repository should not bring down
  unrelated work. Prefer a smaller capability with predictable failure behavior
  over a feature that needs frequent manual repair.

## Protect trust and ownership

- Repository exploration is read-only. Do not modify source files, Git state,
  hooks, configuration, or other tools as a side effect of searching.
- Treat Git and working files as the source of truth. Check the current checkout
  rather than assuming history is linear or old objects still exist. Cached
  history and other worktrees' dirty files must not appear as current results.
- Keep repository contents and queries local. Use private storage permissions
  and avoid recording source, queries, or credentials in diagnostics. Network
  access for an explicit installation or update must remain separate from
  local exploration.
- Record ownership of installed files and runtime resources. Cleanup,
  uninstall, and recovery must preserve unrelated or unexpectedly edited files.
  Provide an explicit way to purge cached source contents.
- Make upgrades recoverable. Identify the running daemon, coordinate with
  concurrent clients, and verify the new version before discarding rollback
  data. A PID file alone is not proof of process identity.

## Keep changes understandable and prove useful behavior

Prefer small modules, explicit data flow, modest dependencies, and mechanisms
that can be inspected. Add configuration and machinery only for a concrete
need. Keep the available tools and their intended uses clear to agents.

For behavior changes, test the affected success and failure paths, especially
resource limits, cancellation, restart, and worktree isolation. Use disposable
repositories for destructive Git tests. Documentation-only changes need
inspection and link checks, not new runtime tests.

Measure cold and warm latency, peak memory, disk growth, and returned output
when those costs change. Judge exploration improvements by real tasks as well
as fixtures. Distinguish observed results from guarantees, and explain known
limits plainly. Saving a few search tokens is not a win if the tool delays a
build, fills a disk, or leaves someone debugging the daemon.

## Testing

Use the existing Cargo and Python runners. Keep test output capture enabled.
Rust's `-- --quiet` prints progress dots,
failure details, and a summary per test executable. Python's `-q -b` prints
failures and a short summary, including buffered output when a test fails.
Do not filter logs with `grep` or discard stderr; preserve failure details
and the runner's exit status.

During debugging, rerun the failing test or a small related group. Choose
`--lib` for Rust unit tests or `--test NAME` for `tests/NAME.rs` to avoid
building and running unrelated test executables. Examples:

```sh
# Discover full test names without running them.
cargo test --locked --test maintenance -- --list

# One integration test, with an exact name.
cargo test --locked --test maintenance -- --quiet --exact missing_daemon_help_and_catalog_do_not_start_or_create_cache

# Related tests whose names contain shutdown.
cargo test --locked --test maintenance -- --quiet shutdown

# A few specific tests: filters after -- are ORed together.
cargo test --locked --test maintenance -- --quiet --exact two_simultaneous_shutdowns_are_idempotent shutdown_binds_to_socket_inspected_under_the_guard

# All tests in one integration-test file.
cargo test --locked --test maintenance -- --quiet

# Unit tests matching a module path or test name.
cargo test --locked --lib -- --quiet search

# One Python test; add more dotted names to run a selected group.
PYTHONPATH=scripts python3 -m unittest -q -b test_release.ReleaseContract.test_tag_matches_metadata

# One Python benchmark test module.
PYTHONPATH=benchmarks/tests python3 -m unittest -q -b test_corpus
```

Use full names from `--list` with `--exact`, including module paths for unit
tests. A filter that matches nothing can still exit successfully; check the
reported test count. Do not add `--nocapture` or `--show-output` unless you
need passing-test logs for a specific investigation.

After focused checks pass, run the suites affected by the change once. Run
all suites for changes spanning Rust and Python, or when the affected tests
are unclear. CI and release validation always run the full applicable suites:

```sh
cargo test --locked --all-targets -- --quiet
python3 -m unittest discover -s scripts -p "test_*.py" -q -b
python3 -m unittest discover -s benchmarks/tests -q -b
```

Keep ignored tests opt-in. The production maintenance timeout test and its
command are documented in [README.md](README.md).
