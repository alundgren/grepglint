# Native Codex calibration

On 2026-09-19, one new Luna/high pair completed on `ccx-crossorg-217` with
Codex 0.154.0 and Django commit
`674eda1c03a3187905f48afee0f15226aa62fdf3`. Grepglint ran first, then baseline.
Both used native Codex commands, built-in model instructions, the same question,
source and budgets. Treatment also had `grepglint_search` available.

| Observation | Grepglint available | Baseline |
| --- | ---: | ---: |
| Trial wall seconds | 86.45 | 93.92 |
| Input tokens, including cached input | 153,377 | 242,517 |
| Cached input tokens | 114,176 | 193,536 |
| Output tokens, including reasoning | 3,400 | 3,442 |
| Reasoning output tokens | 2,020 | 1,750 |
| Total tokens | 156,777 | 245,959 |
| Observable calls, including outer JavaScript cells | 14 | 20 |
| Native commands | 7 | 11 |
| Grepglint calls | 0 | 0 |
| Captured bytes | 961,337 | 1,140,119 |
| Run memory high-water bytes at trial end | 85,012,480 | 115,544,064 |

Both trials returned valid final JSON with citations. All cited ranges were
valid and covered the three required evidence groups. Factual judgments remain
unscored. Citation overlap alone does not establish correctness.

Neither trial reached a deadline, call limit or machine limit. Both weekly
allowance buckets were unchanged at the available percentage precision.
That observation does not mean the calls were free. Memory is the cumulative
cgroup high-water mark, including cache pages, rather than per-trial RSS.

Luna never used Grepglint. The lower token count and wall time in treatment
therefore do not demonstrate a search benefit. One ungraded pair also cannot
separate the effect of tool availability from model variation. The runner now
supports the intended comparison for fresh local discovery sessions. A benefit
claim still needs factual grading and more authorized observations.

The offline integration tests separately exercise Django's existing default
`SQLITE_FULL` failure and subsequent native-command fallback. The live pair did
not index Django. No task, daemon limit or source revision was substituted.
No full corpus run, live retry or other model was used.

## Evidence and remaining differences

The [runner contract](benchmark-paired.md) records the exact configuration,
machine limits, experimental budgets and differences from a personal Codex
workspace. Source stays read-only, command networking is disabled, and project
dependencies are not installed. Repository code execution is allowed. Reference
answers, other worktrees and authentication remain outside native tool access.
The Codex control process is trusted.

[Pinned client evidence](../benchmarks/native-codex-evidence.json) and fresh
offline proofs check native listing and truncation, shell and Python execution,
nested commands, process polling, source preservation, private-file restrictions,
request construction and background-process cleanup. Unknown shell source ranges
remain unknown in scoring. Missing command output produces an unknown byte total.

The live run is `run-ff73c5ddf189abe007070559`, with implementation hash
`4004e06c5fdaf797337a970568763bc3fa021efcf0e75e627eedd54a77e39789`.
Its selected proof is `run-94e992abc42ac732e9f36b4f`. After the live pair, scratch
ownership moved to systemd's runtime directory so an abrupt worker exit also
removes temporary files. The final offline proof is
`run-aa4fd13263cf46a0722a908f`, with implementation hash
`3c3d799d79744b8d3756180c05e5de4de51c4f17fad7e4a0c4aac04a5984d49b`.
It passed capability checks, cleanup checks and scorer audit replay. The live
run and its original proof remain unchanged.

The final benchmark suite ran 152 tests successfully, with nine opt-in tests
skipped. All six native Linux integration tests passed separately. These include
abrupt worker exit, cancellation, invalid answers, transport loss, native output
truncation and Django indexing failure with fallback. The release binary built
successfully, and documentation link and whitespace checks passed.

The original failed run, `run-fb6ad9b0ced6d6d67c1b8bb7`, remains an incomplete
historical result. It was neither resumed nor replayed. Account bindings,
confirmation values, answers and raw transcripts stay in the private artifact
store; this note contains only aggregate observations and public source identity.
