# Code exploration with Grepglint

Run `grepglint tools --json` to discover the available tools and when each is
useful. The catalog is local and does not start a daemon.

Grepglint is optional. Consider `grepglint search --json "migration dependency graph"`
when you have related words or identifiers but no focused file, or when broad
text search returns too many matches. Use a compact group of relevant terms,
such as `migration dependency graph` or `request middleware exception`, without
regex or FTS operators. Run it from the current checkout. Regular clones and
linked worktrees both work. Start with `--limit 3` when a few candidates should
be enough.

Results are ranked paths, line ranges and short excerpts. They are lexical
suggestions, not exhaustive references or guaranteed answers. Read the relevant
regions to verify them. Use `rg` for exact strings, regex, or all occurrences;
directly read a file when its location is already known.

The first search builds a bounded local index and may take several seconds.
Repository files remain unchanged and no network is used.

```sh
grepglint search --json --limit 3 "refresh token validation"
rg -n 'validateRefreshToken|revocationLedger' src/auth
sed -n '1,100p' src/auth/refresh-token.ts
```

The next query refreshes changed files automatically. Do not register
repositories, rebuild indexes, or start daemons manually. If a query reports
that files changed during indexing, retry once. On a resource-limit error,
use `rg` and file reads to continue the task. Repeating the same query will not
fix a capacity failure. Avoid repeated broad queries that add no new
information.

For repeatable, noisy tool output, use `producer | grepglint output bounce`.
Use `2>&1` to include stderr and shell `pipefail` to preserve producer failure.
Capture success alone says nothing about producer status. Input must be finite
UTF-8 without NUL and at most 8 MiB. Failure can consume input; rerun the producer
if needed. Keep a separate copy of irreplaceable output.

For retained output, run the printed `grepglint output page <handle> --json`
command. Concatenate decoded `content`, using each `next_cursor` until
`end_of_output`. The preview does not advance this traversal. Handles expire
within one hour and may be evicted sooner.

To find relevant sections before paging, use
`grepglint output search <handle> "error identifier" --json --limit 3`.
Follow a result's paging command to inspect its original region. Lexical OR
ranking can miss an answer or match only one component; no-match and ranking
limit errors still leave exact paging available. Search only uses this handle's
contents and does not need a Git checkout. It starts the daemon if needed to
share the existing resource budget with repository queries.
Use `grepglint output purge` to erase owned output contents. Repository caches
are preserved.
