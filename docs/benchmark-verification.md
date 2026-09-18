# Stock Codex local verification

`codex_preflight.py verify` runs stock Codex against scripted local Responses
events. It exercises source handlers, hidden direct handlers, nested JavaScript
calls and cancellation without model inference or account credentials. A pass
applies to the local contract only. ChatGPT verification stays pending.

The original command and its [unsupported diagnostic](benchmark-preflight.md)
remain available. The new receipt uses schema version 2 and contract
`stock-codex-local-v2`. It does not reinterpret version 1 results.

## Run the check

The supported route is Linux x86-64 with cgroup v2, a working systemd user
manager, Bubblewrap with user namespaces and `--size` support, Landlock,
seccomp, Python 3.10 or later, Git and `rg`. The observed run used kernel
7.0.0-31-generic, Bubblewrap 0.11.1 and Python 3.14.4. Namespace restrictions
or unavailable cgroup controllers cause a refusal before any provider turn.
The command does not install packages or alter system settings.

Build Grepglint and point at the installed stock Codex executable:

```sh
cargo build --release --locked
python3 benchmarks/codex_preflight.py verify \
  --codex /absolute/path/to/codex \
  --grepglint "$PWD/target/release/grepglint" \
  --artifacts /private/verification \
  --export /private/verification-summary.json
```

The command checks Codex 0.154.0 against SHA-256
`3188814c35471432d4123203e0eb38e5bddc60226e3d7ddf0e59e649ea140022`.
Its sibling `codex-code-mode-host` must match
`0c57be435e73b70d9106c850d751cd259a7f04da958a453d7ef59090d82b70f1`.
The JavaScript host is enabled explicitly and an in-process fallback is disabled.
[Source evidence](../benchmarks/verification-evidence.json) records the upstream
commit, file hashes, dispatch rules and provider metadata limitations.
The retained [local result](../benchmarks/verification-observed.json) contains
the sanitized proof hashes and checks from the supported host. It records 57
calls across the two configurations. It contains no source or transcript text.

Exit 0 means local checks passed. Exit 2 means host prerequisites are unsupported.
Exit 3 means execution or evidence is incomplete. The status names the failed
check, an absolute receipt path and the corrective action. Relative artifact
directories resolve from the invoking shell before service startup.
Missing packages, a user manager, namespaces, Landlock or cgroup support require
fixing that host prerequisite. A changed Codex binary requires new source and
protocol verification. A partial audit cannot authorize a smoke session.

## Process and file access

The controller runs in a transient systemd user service. Codex, its standalone
JavaScript host, serialized handler process, `rg`, Git and the Grepglint daemon
inherit the service's cgroup. The controller checks the effective limits and
the immediate children's membership. Linux preserves that membership across
forks, execs and process-session changes.

Codex has a separate Bubblewrap mount tree containing system runtime files,
the pinned binaries, fixed instructions and a 64 MiB temporary home. It cannot
see the prepared source, host home, account configuration, controller artifacts,
reference answers or later Git history. Its network namespace is shared for
the explicitly configured loopback model transport. That transport receives no
credentials, has zero retries, and never forwards a request. Web, apps, plugins,
MCP servers, memory, hooks and unrelated execution features are disabled.

Handlers run in another mount, PID and network namespace. They can read a
prepared fixture tree mounted read-only and write a fresh 384 MiB cache in
memory. System runtime files and the handler implementation are read-only.
Reference answers, credentials, controller artifacts and external source
sentinels are not mounted. Landlock denies executing files in the source tree.
A seccomp filter permits only Unix-domain sockets, which the real Grepglint
daemon needs. Internet sockets are denied even within the empty network namespace.

The baseline handlers are `text_search`, `file_list` and `read_file`. The first
two invoke `rg` through fixed argument arrays. Queries never enter a shell.
Reads validate relative paths and line ranges and open every path component
without following symlinks. Responses retain exact content and byte offsets.
Source hashes must match before and after both sessions. Repository AGENTS.md
is data for source tools and is never loaded as an instruction file.

The treatment adds `grepglint_search`. Its description comes from the built
binary's tool catalog. It calls `grepglint search --json` with default limits
and preserves the real JSON output, exit status and error text. The fixture
checks a hit, an empty result and an invalid query. A failed Grepglint call
leaves the baseline `rg` handler available. No daemon setting or production
code changes for this check.

## Tools and audit

Both sessions use ephemeral threads, exact `gpt-5.6-luna` with `high`, empty
environments, `agents.enabled=false`, and
`features.code_mode.excluded_tool_namespaces=["skills"]`.
Allowed auxiliary handlers are JavaScript execution, its wait tool, input
requests, `skills.list` and `skills.read`. Every input request gets the same
no-assistance answer immediately.

The checker reads the real request's declared tools, deferred declarations,
nested aliases and runtime registration metadata. It also invokes the hidden
direct skill handlers. Host and bundled skill discovery are disabled, no
executor environment exists, and no apps MCP server exists for orchestrator
packages. The pinned provider returns an empty complete catalog in that last
case before applying visibility filters. This excludes enabled hidden packages
as well as visible ones. Listing both authorities and reading unavailable
packages checks the resulting behavior. Both nested skill aliases must fail.

JavaScript gets V8 computation and registered tool callbacks. It has no file,
process or network APIs, and its module loader rejects imports. Scripted probes
check these failures, exact nested names, concurrent work, discarded results,
bounded allocations, CPU loops, and yielded/waited cells. Separate deliberately
contaminated instruction, skill, configuration and harmless hook fixtures must
be detected by the retained diagnostic checker.

`audit.jsonl` is appended and flushed during execution. It contains raw events
enabled by `experimentalRawEvents=true`, controller requests and responses,
exact JavaScript source, handler arguments, results, byte counts, ranges,
outcomes and timings. Call IDs join the records. Duplicate notifications do
not count as additional calls. Missing events, changed fields or incomplete
callback lifecycles prevent a pass. A final thread summary is never the audit.
The app-server does not provide a reliable outer-cell ID for nested callbacks;
that association is recorded as unavailable. No token allocation per tool is made.

## Limits, artifacts and cleanup

| Resource | Enforced limit |
| --- | --- |
| Work and lifetime | 50-second work budget, 52-second service lifetime plus bounded teardown, 60-second overall allowance |
| Processes | One Codex client, one handler worker, eight queued callbacks, one callback executing at a time |
| Calls | 100 total, including nested callbacks and denied calls |
| Protocol | 1 MiB frames, 16 MiB captured content, 16 KiB tool arguments, 64 KiB tool responses |
| Memory | 1 GiB aggregate cgroup ceiling, zero swap, including cache pages and all owned children |
| CPU | One aggregate CPU, 100,000 microseconds per 100,000-microsecond period |
| Threads and processes | 128 aggregate tasks |
| Disk reserve | 1 GiB free beyond the existing 320 MiB corpus reserve, checked before audit and receipt writes |
| Retention | Four owned runs, one-hour expiry checked on the next invocation; at most 16 MiB audit plus 1 MiB receipt per run |

A sacrificial allocation of 1,100 MiB must be killed by the memory ceiling.
Two bounded CPU loops must produce cgroup throttling. These checks run before
Codex, and their peak memory is reported separately from ordinary session work.
The allocation test deliberately reaches 1 GiB for a short time.

The controller creates a private directory and mode-0600 files before starting
the service. The initial receipt is explicitly incomplete. Ordinary interrupt,
deadline, protocol failure and disconnect stop owned processes and retain the
partial audit. The systemd lifetime also bounds an orphaned service if the
controller is killed. A receipt becomes passed only after service cleanup is
confirmed. Temporary fixtures and caches are disposable.

Retention checks directory/file identities and final content hashes before
deletion. Unknown files, replaced files, edited artifacts and an unfinished
ownership record are preserved and block automatic cleanup. Inspect those
records, then move or remove only the run directory you own before retrying.
The optional export contains allowlisted statuses, identities, hashes, counts
and limits. It omits transcripts, source, instruction text and credentials.

## Human smoke gate

Default invocation and CI are offline. The explicit entry point is:

```sh
python3 benchmarks/codex_preflight.py smoke \
  --provider chatgpt \
  --local-receipt /private/verification/run-ID/receipt.json \
  --codex /absolute/path/to/codex \
  --grepglint "$PWD/target/release/grepglint" \
  --receipt /private/smoke-check.json
```

It requires a successful local receipt matching the implementation and binaries.
With this pinned client it then reports `provider_effective_request_unverified`
and stops before reading authentication or sending a provider turn. The
supported metadata exposes model names, effort choices and three provider
capability flags. It does not expose the complete effective instructions,
hidden dispatch registrations and available package catalogs. Model prose or
the absence of an unexpected call cannot supply those facts.

Account-backed execution therefore remains pending. The retained fake-provider
checks cover the required allowance of one baseline and one treatment session,
120 seconds and 20 calls each, a persistent two-attempt ledger, exact ChatGPT
authentication, Luna/high, explicit included weekly quota, resets, missing usage
and provider uncertainty. They do not establish a working account-backed launch.
No real account turn was run during implementation. Keep #16 unresolved until
the provider evidence and account-backed execution path are established.

Run validation without account access:

```sh
python3 -m unittest discover -s benchmarks/tests -v
python3 benchmarks/validate.py
GREPGLINT_CODEX_INTEGRATION=1 python3 -m unittest discover \
  -s benchmarks/tests -p test_verification_linux.py -v
```

The last command requires the Linux setup and built binaries above. It runs
the real client against the local stub and tests interruption during a session.
