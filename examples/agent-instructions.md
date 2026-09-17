# Code exploration with Grepglint

Run `grepglint tools --json` to discover the available tools and when each is
useful. The catalog is local and does not start a daemon.

Use `grepglint search --json "concept words"` when you know the concept but
not its exact identifier or location. Run it from the checkout you are editing.
Regular clones and linked worktrees both work.
Start with `--limit 3` when a few candidates should be enough.

Use `rg` directly for exact strings, identifiers, and confirmation. After
Grepglint narrows the area, read the indicated region or use symbol/reference
tools. Search excerpts are incomplete evidence; inspect the implementation
before changing it.

```sh
grepglint search --json --limit 3 "refresh token validation"
rg -n 'validateRefreshToken|revocationLedger' src/auth
sed -n '1,100p' src/auth/refresh-token.ts
```

The next query refreshes changed files automatically. Do not register
repositories, rebuild indexes, or start daemons manually. If a query reports
that files changed during indexing, retry once. On a resource-limit error,
use `rg` and continue the task. Avoid repeated broad queries that add no new
information.
