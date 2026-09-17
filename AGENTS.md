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
