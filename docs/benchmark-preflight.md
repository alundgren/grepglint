# Codex preflight without inference

The [preflight command](../benchmarks/codex_preflight.py) captures the actual
Codex 0.154.0 client's first Responses request for both benchmark tool
configurations. A local stub returns fixed text. It never performs model
inference, reads account authentication, or contacts an account model service.

The [observed receipt](../benchmarks/preflight-observed.json) reports
`unsupported`. Private configuration excludes the seeded content and hooks,
but the client still exposes skill tools and reports legacy delegation tools.
Empty `environments` does not establish source isolation. A runner must refuse
trials until its configuration and host file restrictions are established.

## Reproduce

Use Python 3.10 or later and the actual Linux Codex 0.154.0 executable. The
command records its version, byte count and SHA-256. A different version returns
`incomplete`. The diagnostic currently refuses real-client execution on other
platforms and when machine-level Codex configuration files exist, because it
does not isolate those settings. Offline tests also run on macOS.

```sh
python3 benchmarks/codex_preflight.py \
  --codex /absolute/path/to/codex \
  --receipt /private/preflight/run.json \
  --export /private/preflight/share.json
```

Use new filenames for each run. The command creates private output directories
and mode `0600` files. It refuses existing files, symbolic links and output
directories readable by other accounts. Both files contain the same sanitized
JSON. The optional export can be copied for review. No raw request, stderr,
instruction text, account identity, session ID or credential is retained.

Exit `2` means the observations identify an unsupported configuration. Exit
`3` means the check is incomplete, for example a timeout, missing tool catalog
or process failure. Exit `0` is reserved for a future supported contract; this
implementation cannot establish host isolation or ChatGPT equivalence and
therefore never grants trial approval. A probe's `request_status` only describes
that captured request. The top-level `status` is the runner gate.

```sh
python3 -m unittest discover -s benchmarks/tests -v
```

These tests exercise the transport and checker without downloading or starting
Codex. They cover nested and deferred tools, catalogs inside `input`, Code Mode
declarations, empty versus missing catalogs, instruction/tool contamination,
credential rejection, oversized requests and output, failure, timeout,
descendant cleanup, private receipts and probe serialization.

## What the command launches

There are four sequential probes, exclusion and contamination for each of
control and Grepglint. Only one client and one stub run at a time. An
account-owned file lock also prevents concurrent command invocations.

Each child gets a new temporary HOME, CODEX_HOME, XDG directories and working
directory. Its environment is an allowlist without inherited tokens, API
keys, proxy settings or account configuration. Only the disposable fixture
configuration can change. The original repository and personal Codex files
are never edited. No corpus source or reference answers are needed.

The model provider is `preflight`, with a freshly bound `127.0.0.1` URL,
`wire_api="responses"`, no authentication, zero request/stream retries, disabled
WebSockets and a two-second idle timeout. The stub accepts exactly one POST to
`/v1/responses`, rejects credential headers, and replies with deterministic SSE
containing zero-token usage and `preflight-complete`. It never asks for a tool
call. No source-access tool implementation is included in this diagnostic.

`fixtures()` in the command is the exact launch configuration. It explicitly
disables startup integrations, remote models, apps, plugins, browser, web,
shell, memory, image and agent feature switches. It disables telemetry, history
and request compression. The receipt still checks what the client registers;
those switches alone are not evidence that a capability is absent.

The controller initializes with `experimentalApi=true`. It starts an ephemeral
thread with `approvalPolicy="never"`, `sandbox="read-only"`,
`modelProvider="preflight"`, `model="gpt-5.6-luna"`, `environments=[]` and
controlled `dynamicTools`. `turn/start` repeats empty environments and requests
`effort="high"`. The tools are `text_search`, `file_list` and `read_file`;
Grepglint adds only `grepglint_search`. The receipt retains their definitions.

The same fixed base instruction file is used in the exclusion pair. Their
configuration hashes and normalized instruction hashes must match, and the
effective tool difference must be exactly `grepglint_search`. Paths in the
temporary fixture are normalized only for comparison; raw instruction hashes
are also retained. The contamination pair has the same equality checks.

The command budgets 56 seconds for work and four seconds for cleanup, within
the 60-second deadline. RPC and HTTP frames are limited to 1 MiB; HTTP headers
to 16 KiB; stderr to 1 MiB per child; total captured bytes across all probes to
16 MiB. Subprocess output is drained as it arrives. Child core dumps are
disabled and individual child-created files are capped at 16 MiB. That file
cap is not a total temporary-disk guarantee. All owned process groups are
killed on success or failure, even if their leader has already exited, and
temporary fixtures are removed. The receipt reports captured bytes, elapsed
time, observed temporary-file bytes and child peak RSS on Linux.

## Fixtures and observations

Each probe seeds a project AGENTS.md, a separate polluted Codex home with global
AGENTS.md, a skill, a configuration instruction and a harmless SessionStart
hook. An outside-source instruction file sits beside the source directory.

Exclusion selects the clean Codex home, disables skill discovery and skill
instruction injection, disables hooks, and sets `project_doc_max_bytes=0`.
No sentinel appears and the hook does not run. This establishes exclusion of
these fixtures under that combination of settings. It is not an OS restriction
on the client process.

Contamination selects the polluted home, enables skill discovery/instructions
and hooks, and points `model_instructions_file` at the outside-source fixture.
It also supplies `outside_source_read`, a deliberately forbidden tool
declaration. The controller obtains the harmless hook's current hash from
`hooks/list`, checks its exact command, and records trust using
`config/value/write` against the disposable config file only. The hook writes
one marker file and returns one sentinel instruction. This tests actual client
hook execution without running repository code.

Both contamination receipts contain global, skill, configuration, outside-source
and hook sentinels and record the hook marker. Project instructions stay
excluded in both modes. The checker requires both successful exclusion and
detected contamination before classifying the diagnostic as complete.

In the recorded run, all four requests asked for Luna/high. The entire command
took 2.037 seconds, captured 99,294 bytes, observed 9,760,802 temporary-file
bytes, and reported 124,084 KiB peak client RSS. These are observations from
one local machine, not performance guarantees.

## Complete tool and instruction accounting

The checker reads catalogs at the top level and under `input`, including
`additional_tools`, nested namespaces and deferred declarations. It also reads
the exact TypeScript declarations in the Code Mode description. Examples such
as `tools.exec_command(...)` do not count as registered tools.

It requests `features.tool_registry.turn_metadata_includes_tool_info=true` and
reads the authoritative `tool_namespaces_info` from the captured request's
`x-codex-turn-metadata`. Only names, direct/deferred flags and Code Mode aliases
are exported from this metadata. This catches tools omitted from the visible
Code Mode description. A missing registry or unparsed catalog is incomplete,
not an empty tool set.

The observed exclusion pair includes `skills__list` and `skills__read`, plus
five `multi_agent_v1__*` registrations including `spawn_agent`. The pinned
[metadata implementation](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/core/src/tools/tool_namespaces_info.rs)
omits hidden entries and entries with no direct, deferred or Code Mode access.
The receipt records these registrations as blockers without invoking them.
No resource, shell, patch, app or browser tool was reported in this exact
configuration. That absence applies only to this capture.

`functions.exec` remains the JavaScript tool wrapper. `functions.wait` waits on
an existing wrapper call, and `functions.request_user_input` asks for input.
The receipt identifies these utilities and their purpose; it includes them in
the complete catalog. They do not cancel the skill and delegation blockers.

Instruction records include the request location, role, byte count, SHA-256
and origin. Origins distinguish controlled launch instructions, each fixture,
client permission/environment text and the probe's user prompt. Unexpected
blocks are hashed and marked `client_generated_or_unattributed`; the receipt
does not invent a file origin or print the text.

## Limits of the evidence

[Pinned source and schema evidence](../benchmarks/preflight-evidence.json)
identifies the `rust-v0.154.0` source commit and SHA-256 of each examined file.
The experimental ThreadStartParams and TurnStartParams schemas were generated
by the actual pinned client. The ordinary checked-in TypeScript schema omits
experimental fields, so it cannot establish the `environments` contract alone.

The local provider proves request construction and fixture behavior for this
binary and configuration. It does not prove ChatGPT account model availability,
effort support, provider-specific catalogs/instructions, transport, or
server-side tools. The proposed ChatGPT configuration and those unresolved
differences are recorded in the evidence file. No account check or account
`turn/start` was performed.

Private settings and empty environments also do not confine the Codex process
to the source directory. Later runner refinement must choose and verify that
restriction while keeping reference answers, verifiers and the controller
outside model-accessible files. It must resolve the remaining callable tools
and re-run preflight for the actual provider contract before calibration.
