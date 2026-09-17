# Architecture notes

`main.rs` owns the CLI and tool catalog. `daemon.rs` owns the socket, automatic
startup, OS lock, and process limits. `git.rs` discovers repositories and reads
bounded Git output. `files.rs` applies file-selection rules. `chunks.rs` extracts code regions. `tokens.rs` expands
identifiers. `index.rs` owns freshness, cache maintenance, and ranking; the
schema is in `schema.sql`.

## Stored data

One SQLite database contains repository records, worktree records, cached
contents and chunks, paths, base mappings, and overlay mappings. Code FTS rows
belong to immutable content chunks, while path FTS rows belong to repository
paths. A second worktree adds mappings without copying either FTS index.
The same blob used with different parser versions has separate parsed entries.

A base mapping associates a worktree path with a committed content entry.
An overlay associates the path with local content or a deletion marker.
The SQL query excludes any base path that has an overlay, then joins the
remaining active content to FTS matches. It applies the result limit after
that filtering. Neither cached historical code nor another worktree's dirty
files can enter the result through the cache alone.

The daemon serializes requests. Each refresh runs in a SQLite transaction.
Failures, including a full database, roll back without publishing a partial
worktree view. Cache entries are disposable; Git and working files remain the
source of truth. Git commands disable optional index writes, filesystem-monitor
hooks, interactive prompts, and automatic fetching of missing partial-clone
objects. Replacement refs are disabled so a cached blob SHA always identifies
the original object bytes. Differences in working files still enter the overlay.

## Chunks and ranking

Tree-sitter extracts JavaScript/TypeScript functions, class members, and named
top-level declarations. Class headers are separate from methods. Other
UTF-8 text files use 60-line chunks with six-line overlap; long structural
regions use the same limit. Parser work has a 100 ms deadline per file and
falls back to line chunks if it times out. Extensions select a parser but do
not determine whether a file can be searched. `files.rs` keeps default path
exclusions separate from parsing and applies root `.grepglintignore` overrides
using the `ignore` crate's Git ignore matcher. Ignored untracked files,
symlinks, invalid UTF-8, binary data, oversized files, minified lines, and lock
files are excluded. Conflicted regular files are searchable as currently stored,
including conflict markers. Submodule contents belong to their own repository.

The worktree signature combines a versioned file-policy fingerprint with the
dirty-file signature. A policy change rebuilds committed path mappings and
dirty overlays, reusing cached content where possible. This also refreshes
older caches that omitted unfamiliar extensions even if HEAD has not changed.
The configuration is read with the same bounded, regular-file checks as source
files and rechecked before committing a refresh. Skip reasons are reported as
refresh-time counters, not persisted as a complete inventory of excluded paths.

Identifier expansion retains the original spelling as a lowercase search token
and adds components of camelCase, snake_case, kebab-case, and paths. Queries
use the same expansion, quote terms as literal FTS input, and combine them with
OR. This permits partial matches when one word is absent. It also means a
compound query can return a region matching only one component. Use `rg` to
confirm exact matches.

Code [BM25](https://sqlite.org/fts5.html#the_bm25_function) weights symbol text at 8 and body text at 1. Path matches have their
own BM25 score with weight 3. Scores for the same active region add together.
Separate path scoring preserves blob sharing. This is a deliberately simple
approximation of field-weighted ranking, not a tuned retrieval model.

FTS statistics cover cached contents across worktrees and repositories, including
recent historical blobs. Those statistics affect scores even though inactive
content cannot become a result. Scores can change after cache maintenance.
Calculating BM25 statistics for each active worktree is a possible later
experiment if measurements show the shared statistics hurt relevance.

## Resource and freshness boundaries

The daemon owns one SQLite connection with an 8 MiB page cache and a 64 MiB
SQLite heap limit. It keeps no application-level cache of whole repositories
in memory. Batched source reads total at most 8 MiB per batch. Each file is
bounded before parsing. The OS queue holds waiting clients; the service does
not create a thread or task for each client. Git subprocesses use short-lived
bounded readers and a deadline. SQLite's progress callback enforces the same
30-second work budget. Request receipt and response writing have separate
timeouts.

The database uses a rollback journal rather than a WAL, so an unattended WAL
cannot grow between checkpoints. A hard page count bounds the main database.
SQLite may temporarily use another database's worth of space for rollback.
Maintenance runs on demand at most once per minute. It removes expired worktree
mappings, unreferenced old committed contents, and obsolete overlay contents.
Above 75% of the database budget it also evicts least-recently-used worktrees,
excluding the current one, until use falls below 50% or no candidate remains.
Evicted worktrees register themselves again on their next query. Free pages
are reused, and incremental vacuum returns some of them to disk.

The cache is machine-local and scoped to an OS account. Separate cache
directories are intended for tests and independent experiments. A file lock
prevents multiple daemon writers to the same cache. A crashed daemon leaves
the cache available for restart; idle shutdown removes its socket and PID file.

Git history can be rewritten or deleted. A tree comparison does not require
ancestry, and missing old commits trigger a fresh tree listing. Before a refresh
commits, the daemon checks HEAD and dirty fingerprints again. This catches
changes during indexing, but is not an atomic filesystem snapshot. An edit
after the final check is visible on the following query. Stat fingerprints
do not defend against deliberate manipulation that preserves every observed
metadata field. Git's assume-unchanged and skip-worktree flags, sparse checkout,
content filters, and remote filesystems are outside the validated workflow.

Resource limits are guardrails, not a proof that the native SQLite and
tree-sitter libraries contain no bugs. Linux applies an address-space limit
and disables core dumps; macOS has application limits but no equivalent
address-space ceiling in this implementation. Benchmark sustained use before
enabling this tool broadly across agents.

## Retained output

`output.rs` owns a separate SQLite database for immutable output handles and
ordered byte chunks. The CLI captures stdin, validates UTF-8 incrementally,
and commits a handle only after EOF. Small input never opens storage. The
search daemon never waits for a producer. Capture and paging do not require protocol negotiation with a running search
daemon, so older search daemons can coexist. Output search uses an additive
`output_search` request and gives a paging fallback when an older daemon refuses it.

Two OS file locks limit captures. A short SQLite transaction reserves the full
per-output allowance against aggregate bytes and entry count before capture
writes. Abandoned reservations are removed only after acquiring their capture
lock. Paging reads a consistent transaction, checks all chunks and their digest,
and returns bounded exact text. Purge excludes capture with the same locks;
SQLite coordinates it with readers. All cleanup visits bounded table records
and fixed recorded filenames. There is no recursive traversal or deletion.

The database uses secure deletion and a persistent rollback journal truncated
after each committed transaction. Both files retain recorded inode identities.
The 40 MiB database cap and 41 MiB journal allowance include retained/staged
payload, indexes, deleted-page reuse, and rollback work. No second payload copy
or vacuum file is created. Private ownership metadata remains after purge for
later installer integration. Store initialization refuses unrecorded or replaced
files. A locked initialization intent names one unpredictable staging directory.
Recovery completes only its empty files and matching partial ownership record,
then renames the complete directory into place. The existing repository reserve
and daemon limits remain unchanged. Capture enforces the full free-space reserve;
purge only requires its bounded rollback allowance. Output clients hold the
shared maintenance lock for their store lifetime, so managed changes exclude
output work as well as search.

## Daemon maintenance

`maintenance.rs` owns account and socket checks, bounded control requests, and
`Maintenance`, an OS file-lock guard for callers that need to stop the daemon
and keep it stopped during filesystem changes. `status` connects without
starting a daemon or creating a cache. `shutdown` acquires the guard, identifies
the instance, sends its random instance ID, and waits for the daemon lock to
become available. It never sends process signals or treats a PID file as proof
of identity. The daemon removes its socket only if its device and inode still
match, and preserves a changed PID file.

Control messages add `command: health` and `command: shutdown` to protocol
version 1. Existing search messages remain valid. Health returns package
version, a SHA-256 of the executable read once at startup, random instance ID,
protocol version, cache directory, and effective resource limits. It contains
no indexed contents or queries. Both endpoints verify the Unix socket peer's
OS account. Controls also require a private, account-owned cache and socket.
An unknown protocol, stale socket, or changed instance produces a refusal.
A legacy daemon must exit on its normal idle timeout. Pause searches and retry
after that exit; use `rg` in the meantime.

Lock acquisition order is explicit:

- Startup takes `maintenance.lock` shared, then tries `daemon.lock` exclusive
  without waiting. The daemon releases the shared lock after initialization.
- A running daemon keeps `daemon.lock` and tries `maintenance.lock` shared
  before each search. Failure immediately rejects that work. It never waits
  for the maintenance lock while holding the daemon lock.
- Maintenance takes `maintenance.lock` exclusive before inspecting or stopping
  the daemon, then waits for `daemon.lock` after shutdown acknowledgement.
  Health and shutdown handling do not take a shared maintenance lock.

This avoids a cycle of waiting locks. Acquisition and shutdown share a
35-second deadline. Existing work keeps its 30-second budget and two-second
response timeout. Each control exchange has a three-second absolute deadline;
a busy or slow daemon can cause a bounded refusal. Lock retries sleep for
10 ms only during an explicit maintenance request. No service polls in the
background. Competing searches can delay acquisition until the deadline;
retry after pausing searches if this happens.

Holding `Maintenance` prevents new startup and expensive work until the guard
is dropped or its process exits. CLI shutdown releases it before returning,
so a later search can restart automatically. Future managed changes must keep
the guard alive across those changes. Keep both lock files in place, including
when purging cached source: unlinking an open lock could let two callers lock
different files. Legacy clients and daemons do not participate in this lock
protocol, so maintenance refuses legacy controls rather than claiming exclusion
over them.

The added steady-state data is one health record and an empty maintenance lock
file. Startup reads at most 128 MiB of executable bytes using a fixed-size
streaming buffer to compute build identity. Controls keep the existing 16 KiB
request and 64 KiB response caps. No dependency, worker thread, source cache,
or diagnostic log is added.

## Temporary output ranking

`temporary_rank.rs` ranks caller-provided bounded chunks with a disposable
in-memory FTS5 database. It reuses `tokens.rs` query and identifier expansion.
The corpus contains only the requested output. Ties use source byte position.
Generic chunks overlap and split long lines at valid UTF-8 boundaries; repository
file eligibility rules do not apply to retained output.

The daemon serializes output search with repository work, using the existing
64 MiB SQLite heap limit across all connections and the existing Linux process
limit. `Store` validates one complete retained stream and keeps its SQLite read
transaction until ranking finishes. The Rust source allocation is bounded to
8 MiB. The temporary database has a 32 MiB logical page cap and cannot spill to
disk. Its connection and buffers drop on success or failure. No output FTS rows
or source content enter the repository database.

The socket's lifetime and a 30-second deadline bound traversal, chunk generation,
indexing and ranking. The CLI detects downstream closure during response waits
and closes that socket. Capture, page and purge remain direct operations;
producer waits never occupy a daemon request. A concurrent output writer may
reach its existing two-second busy timeout while a search holds the consistent
read transaction. A later page remains byte-exact if the handle is still retained.
