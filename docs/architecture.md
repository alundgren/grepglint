# Architecture notes

`main.rs` owns the CLI and tool catalog. `daemon.rs` owns the socket, automatic
startup, OS lock, and process limits. `git.rs` discovers repositories and reads
bounded Git output. `chunks.rs` extracts code regions. `tokens.rs` expands
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
recognized text files use 60-line chunks with six-line overlap; long structural
regions use the same limit. Parser work has a 100 ms deadline per file and
falls back to line chunks if it times out. See `parser_for` for supported
extensions and excluded generated directories. Ignored untracked files,
symlinks, invalid UTF-8, binary data, oversized files, minified lines, and lock
files are excluded. Conflicted regular files are searchable as currently stored,
including conflict markers. Submodule contents belong to their own repository.

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
