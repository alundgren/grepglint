# Prototype evaluation

The fixture supports ranked discovery as a useful first step. It does not yet
establish an improvement in total agent tokens or task completion time.

Run `cargo build --release --locked` followed by
`python3 scripts/demo.py --output docs/demo-results.json` to reproduce the
measurements. The script creates a temporary Git repository with authentication,
payment, UI, and cache code plus 40 queue handlers containing distracting
uses of refresh and token. It deletes the fixture after its daemon exits.
[The raw report](demo-results.json) contains all counts and timings.

## Worktree behavior

| Case | New committed parses | Local parses | Observed behavior |
| --- | ---: | ---: | --- |
| First query | 46 | 0 | 53 cached regions; validation function ranks first |
| Unchanged query | 0 | 0 | No path mapping updates |
| Second worktree | 0 | 0 | Reuses 46 contents and the same 53 regions |
| Committed branch change | 1 | 0 | Updates one path |
| Uncommitted edit | 0 | 1 | Replaces the committed region in that worktree |
| Revert | 0 | 0 | Removes the overlay and its stale result |
| Untracked source | 0 | 1 | Appears without registration or refresh commands |
| Two concurrent clients | 0 | 0 | Both succeed against their own worktree view |
| Wake after idle exit | 0 | 0 | Automatically starts and reuses stored contents |

Integration tests also cover simultaneous cold starts, forced daemon exit,
staged changes, staged renames, working files restored to HEAD while the Git
index differs, deletions, unusual filenames, separate clones, unborn HEAD,
duplicate blobs at different paths, cache exhaustion and recovery, overlay
collection, inactive worktree expiry, replacement refs, and amended/rebased/
pruned history. All destructive Git commands in the tests target disposable
fixtures.

## Retrieval compared with rg

These measurements include full CLI JSON output with metadata and three
requested results. Output bytes are reproducible measurements, not token counts.
The broad `rg` comparison uses case-insensitive OR across the query words.
A careful agent could also narrow its `rg` pattern or choose the rarest term.

| Query | First-ranked symbol | Literal rg lines | Any-term rg lines | Grepglint bytes | Any-term rg bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| refresh token validation | validateRefreshToken | 0 | 181 | 1,675 | 12,314 |
| refresh revocation | validateRefreshToken | 0 | 134 | 1,736 | 10,024 |
| webhook signature verification | verify_webhook_signature | 0 | 4 | 1,326 | 301 |
| validateRefreshToken | validateRefreshToken | 3 | 3 | 1,672 | 259 |

Ranking returns the intended function first without requiring a contiguous
phrase. Identifier splitting makes the camelCase and snake_case functions
discoverable with separate words. Rare terms distinguish validation from the
many generic refresh handlers.

The broad refresh query produces about 86% fewer output bytes with Grepglint.
The webhook query and the known identifier are cheaper with `rg`. That is why
the agent instructions recommend Grepglint for discovery and `rg` for precision.
No stemming or synonym matching exists; verification alone would not match
verify. OR matching can also return regions matching just one term.

## Latency and resource observations

On this Linux x86_64 machine, the 46-file initial search took 201 ms including
daemon startup, with 44 ms reported inside the search operation. The second
worktree took 29 ms. Across 250 unchanged searches, median CLI latency was
12.21 ms and p95 was 17.82 ms. The sampled `rg` commands took about 5 to 8 ms.
This prototype improves ordering and sometimes output size; it does not beat
`rg` on raw lookup latency in this fixture.

The database occupied 221,184 bytes, and the release binary was 6,511,056 bytes.
Daemon resident memory was 8,404 KiB after 50 repeated queries and 8,408 KiB
after 250. Linux reported the configured 512 MiB address-space limit. The
daemon exited when idle, then resumed with zero committed files reparsed.
This short run showed stable resident memory; it is not proof of no leaks in
long-running workloads.

The cache exhaustion test sets a 512 KiB database budget, adds an overflowing
source file, checks that indexing fails within that budget, and verifies that
removing the file lets the next query succeed without stale overlay entries.
The normal configurable minimum is 8 MiB.

## What worked, what was awkward, and what to try

Shared blob records and small worktree mappings make reuse easy to inspect.
Filtering the active corpus before applying a result limit prevents stale
cached versions from taking result slots. SQLite transactions also make a
failed refresh recoverable without touching the repository.

Paths need a separate index to preserve blob reuse across renames and copies.
Adding their BM25 scores to code scores is easy to inspect but needs evaluation
on larger repositories. Cache-wide term statistics can change ranking when
another worktree or repository is indexed. The Git status check dominates much
of the warm-query cost. The complete JSON response includes useful diagnostics
but still spends bytes an agent may not need.

Next, record real exploration tasks with and without Grepglint. Count total
tool calls, returned tokens, files read, and successful task completion. Use the
same tasks and repository snapshots, and compare with sensible `rg` strategies.
Test larger multi-language repositories and sustained edit/query workloads
before changing ranking or adding parsers. A compact JSON mode, better result
diversity, and worktree-specific BM25 statistics are candidates if those
measurements justify them. Embeddings and extra daemon services can wait.
