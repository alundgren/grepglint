# Paired discovery runs

`benchmarks/paired.py` compares native Codex exploration with the same client
plus `grepglint_search`. The selected client is stock Codex app-server 0.154.0,
using `gpt-5.6-luna` at `high`. New records identify this environment as
`tool_environment: native-codex-v1`. The default plan, `--fake`, and `--prove` perform no inference and read no account
authentication. These trial and usage records carry `simulation: true`.
The separate ChatGPT commands below require explicit readiness and one approval
for the complete selected run. A successful fake answer tests storage and protocol behavior;
it is not a factual answer to the corpus question or a live measurement.

## Select and inspect

No arguments prints help. Task selection never defaults to the entire corpus.
The frozen manifest validator runs before planning. Explicit IDs retain their
command-line order; `--all` sorts the 40 IDs. Repetitions follow each task.
SHA-256 of `seed:task-id:repetition` determines each pair's configuration order.
Seed 0 is the recorded default. Seeds are unsigned 64-bit integers. Repetitions
must be 1 through 10 and a run may contain at most 160 trials.

```sh
python3 benchmarks/paired.py --all --dry-run --seed 0
python3 benchmarks/paired.py --task ccx-crossorg-217 --task django-rate-limit-design-001 --dry-run
```

Use actual task IDs from `manifest.json`. Unknown and repeated IDs fail.
The dry run prints the exact ordered trials, repetitions, partitions, common
answer instructions, source commit/tree IDs, client/model/effort, limits and
worst-case artifact reservation. No binary, credentials or downloaded source
is needed for planning. Without `--snapshots`, prepared source is explicitly
`not_checked`. Pass `--snapshots` to verify selected prepared source offline.
Nothing fetches implicitly. Both members use the same question, answer
instructions, source and budgets; treatment adds only `grepglint_search`.

## Prepare and run

Use the [source preparation command](benchmark-corpus.md#prepare-source)
separately. It fetches only the explicitly selected public pin, never runs
repository code, and builds the known one-commit repository. For example:

```sh
python3 benchmarks/prepare.py django-674eda1c --output /tmp/my-corpus-sources
cargo build --release --locked
python3 benchmarks/paired.py --task ccx-crossorg-217 --fake \
  --snapshots /tmp/my-corpus-sources --artifacts /private/paired-runs
```

The host requirements and pinned binaries are the same as
[stock-client verification](benchmark-verification.md#run-the-check).
Unsupported prerequisites fail before trial work. The source contract verifies
the complete inventory's blob hashes and modes, local commit/tree, known Git
configuration, absence of remotes and additional reachable history, and an
aggregate hash of source and Git metadata before and after each trial.
The full original pin remains in the trial identity even though preparation
creates a local one-commit repository. Native commands and Grepglint see the
same immutable files and Git data.

## Native tools and remaining differences

The baseline has Codex's `exec_command`, `write_stdin`, `apply_patch`,
`view_image` and `update_plan`, including normal JavaScript orchestration and
wait behavior. There are no custom listing, reading or grep tools in either
catalog. Treatment adds only the existing `grepglint_search` definition, with
its description taken from the built binary. Grepglint use is optional.

The runner omits the old `environments: []` override, enables native shell
execution, and retains Codex's built-in model instructions. Common task and
answer instructions are developer instructions, identical in both trials.
The model can choose `rg`, `find`, `sed`, Python, pipelines, command output
limits and process polling. A large listing receives Codex's own truncated
output and the turn continues. The runner neither forces pagination nor tells
the model to avoid large requests. The pinned implementation defaults to
10,000 output tokens for unified execution and retains at most 1 MiB in its
command output buffer. These are client behavior, not experimental tuning.
See [pinned source evidence](../benchmarks/native-codex-evidence.json) and the
[official app-server protocol](https://learn.chatgpt.com/docs/app-server).

This is a fresh, unattended, single-agent local discovery session. Compared
with a person's existing Codex workspace, it has no personal configuration,
installed skills, project instruction injection, plugins, apps, web search,
additional MCP servers, memory, hooks, delegation or shell snapshot. Repository
instruction files remain readable source. There is no human assistance, and the
final answer must use the common JSON format. The checkout contains one local
commit, no remotes or later history. System programs are available, but project
dependencies are not installed automatically. Network access for commands is
disabled. These differences apply equally to both configurations and limit the
claim to local code discovery with this client, rather than every Codex setup.

The earlier ban on executing repository code is removed for paired trials.
Native shell access necessarily permits interpreters and scripts. Code may run,
including repository code, with read-only source and writable temporary space.
Tests that require source-tree writes or missing dependencies can fail normally.
Source preparation, offline scoring, and the older smoke verifier keep their
own no-execution rules. This change does not alter the daemon or its defaults.

Codex starts with a clean environment and an ephemeral home inside the service's
private runtime directory on tmpfs. Systemd removes that directory when the
service stops, including after an abrupt worker exit.
Its own filesystem sandbox grants native commands system runtime files, the
pinned Codex runtime, selected source, and private scratch directories. Auth,
reference answers, other worktrees, controller artifacts and the rest of the
host home are outside those readable roots. Only the app-server control process
reads a private, temporary copy of the installed authentication file. The copy
is removed with the client home. That process now runs directly under
the cgroup; it is trusted to enforce its native tool permissions. This is not a
claim of confinement against a compromised Codex binary. An outer Bubblewrap
around Codex was incompatible with native nested sandbox startup on this host.
No host setting is changed to bypass that failure.

The Grepglint callback keeps its existing separate read-only source mount,
private 896 MiB cache, network namespace, Landlock and seccomp restrictions.
Both trials initialize that worker for equal setup costs; the baseline cannot
call it. Every trial receives a new client home, thread and cache. Scratch
writes and child processes count against the service's memory ceiling. Native
commands retain their own concurrency behavior; only Grepglint callbacks are
serialized. Completed turns stop native background terminals before the next trial.
Cancellation kills the whole service, including background commands.

Offline fake responses use a recursive native listing, a shell read and `rg`.
Treatment first requests the generic text `benchmark`; it is measured, not a
prewarming operation. No fake query is derived from a question or reference
answer. `non-use`, malformed/missing/oversized answer, and transport-loss
scenarios remain available. `--prove` also exercises native truncation,
discarded nested calls, process yield/poll, blocked private reads and writes,
network restrictions, disabled skill dispatch and deterministic no-assistance.

A Grepglint error returns to the agent and native tools remain available.
The Django calibration task stays `ccx-crossorg-217`. Its default `SQLITE_FULL`
error is retained in the raw structured result with subsequent fallback call
IDs in trial metadata. The runner never raises daemon limits or substitutes a
different task. A recoverable search error alone does not fail a trial.

## Selected ChatGPT runs

CI and ordinary implementation checks use no inference. Live execution needs
explicit user authorization. Issue #18 scopes the calibration: only `ccx-crossorg-217`, one repetition per
configuration, seed 0, fresh treatment cache and default daemon limits. Its
known Django cold-index failure stays visible. Larger selections are a runner
capability, not permission to use account allowance.

An account-free preview of all 40 tasks produces 80 sequential trials:

```sh
python3 benchmarks/paired.py --all --repetitions 1 --seed 0 --dry-run
```

First prepare the selected sources and produce an offline capability proof
using the installed stock Codex 0.154.0 client. It uses local scripted Responses,
the actual task prompts, prepared source, native tools and both catalogs.
It performs no account access or inference. It must pass under the current
implementation and binary hashes; previous restricted-handler proofs cannot
approve this environment.

```sh
python3 benchmarks/paired.py --task ccx-crossorg-217 --seed 0 --prove \
  --snapshots /private/prepared-sources --artifacts /private/paired-runs
```

Record the printed proof run directory. An explicit readiness request then
checks the pinned client and JavaScript host, implementation/build identities,
selected offline proof, source contents, exact `gpt-5.6-luna` with `high`,
ChatGPT account and included weekly allowance. No model turn starts here.
The output displays the complete order, task IDs/partitions/repetitions,
identities, per-trial limits, aggregate worst-case time/calls/output,
reservation, account identity, quota observations and previous attempt history.
It prints a private confirmation tied to that immutable run.

```sh
python3 benchmarks/paired.py --provider chatgpt --readiness /private/paired-runs/PROOF_RUN \
  --snapshots /private/prepared-sources --artifacts /private/paired-runs
```

Only a separate operator decision permits executing the displayed run. Pass
its exact run directory and confirmation once. There are no per-trial prompts.
This example is a command template, not authorization to execute it:

```sh
python3 benchmarks/paired.py --provider chatgpt --execute /private/paired-runs/READY_RUN \
  --confirm CONFIRMATION_FROM_READINESS --snapshots /private/prepared-sources
```

`--codex`, `--grepglint` and `--auth` select the installed paths when defaults
are unsuitable. Authentication is available to the stock client control process. It is outside
the native tools' readable roots and absent from handler mounts and saved events. No API billing, purchases, reset redemption, model substitution,
transport retry or reconnect is allowed. Each trial has a fresh ephemeral
thread and a fresh handler cache. Grepglint use remains optional.

Fresh quota reads run before every inference submission and after every
attempted trial. A single timed observation also covers coincident pair/run
boundaries. Usage increases and new observation times do not require another
approval. Account changes, missing included quota, bucket changes, incompatible
resets, proof changes or model/effort changes stop the run. Raw reset times stay
private; comparison preserves the narrowly tested zero-usage moving full-week
normalization. Percentages can reflect concurrent account activity and delayed
or rounded reporting. They never imply per-tool tokens, money or zero cost.

Attempt history is independent of run artifacts and the exhausted two-attempt
smoke ledger. The owner-only, append-only file is
`~/.local/state/grepglint/paired-attempts-v1.jsonl`. Its lock excludes concurrent
account benchmark execution. It retains planned run/trial identities, consumed
confirmations, durable reservations immediately before submission, outcomes and
unstarted entries. An uncertain write remains consumed. New processes, quota
resets and artifact cleanup cannot renew it. A later run receives a new
confirmation identifying earlier history. Changed history invalidates an older
unused confirmation.

The ledger has a 16 MiB cap, at most 64 run authorizations, 64 KiB event limit,
and a 256 KiB history reservation per outstanding run. It never evicts history.
At capacity, preserve the file and stop; there is no automatic reset command.
A partial write blocks further execution and remains available for inspection.
A stopped run cannot resume, even if entries were never started. Inspect the
failure and retained artifacts before requesting a new readiness check and
separate approval. Missing measurements and post-trial quota failures remain
explicit. Cleaning an unused ready run cancels its confirmation before deleting owned
artifacts. Cleanup never deletes this ledger or changes the smoke allowance.

Live trial records use `schema_version: 2`, `simulation: false` under the same
`paired-discovery-v1` contract. The [trial schema](../benchmarks/paired-schema.json)
adds authorization/proof identities and private before/after quota observations.
`inference_performed: true` means submission was reserved and may have reached
the provider, including uncertain transport. Version 1 stays simulation-only;
changing its provenance flags still fails validation. The scorer accepts the
new records after checking authorization, selected proof, configuration and
audited submission identities. Embedded quota observations remain separate from
manual comparative quota judgments. The shareable runner export allows only
weekly bucket IDs, times, percentages and availability, never raw account data.

The [earlier provider evidence](https://github.com/alundgren/grepglint/issues/39#issuecomment-5732530859)
established account transport and usage events under the old smoke contract.
It does not prove the native tool environment. Fresh selected offline evidence
checks the new constructed requests and execution, and any live validation
records the new protocol-visible calls. Undisclosed provider-side instructions
and capabilities remain outside the observable evidence.

## Limits and retained artifacts

Experimental budgets are 30 minutes and 1,000 audited calls per trial, identical
for both configurations. There is no experimental token cap or cumulative
model-visible output cap. Thirty minutes leaves room for multi-file discovery,
optional scripts, the normal 30-second index deadline and fallback. One thousand
calls allows sustained small reads and polling without requiring the agent to
bundle work to save calls. These are generous ceilings, not targets or evidence
that every corpus task fits. A budget hit is an incomplete trial and must be
reported before drawing a benefit claim. Report observed usage against the
ceilings; do not score a restriction-induced failure as a token saving.

Machine protection is separate:

| Resource | Limit |
| --- | --- |
| Run | 160 trials, one executing at a time, no retries or resume |
| CPU, memory, tasks | One aggregate CPU, 2 GiB RAM, zero swap, 128 tasks |
| Grepglint callbacks | One executing, eight queued |
| Native capture | 8 MiB protocol/audit frames; 1 MiB arguments; 7 MiB encoded results |
| Events/output | 128 MiB shared streaming capture per trial |
| Answer | 64 KiB raw UTF-8 text, at most 100 citations |
| Metadata | 1 MiB per trial; 5 MiB run/ownership/publication overhead |
| Temporary storage | Client home/scratch on tmpfs under the aggregate memory cap; 896 MiB handler cache |
| Retention | Eight runs, 24 GiB aggregate reservation, no expiry or eviction |
| Free disk | 1 GiB plus 576 MiB beyond the selected reservation |

The 8 MiB frame allowance accommodates the native 1 MiB output buffer even
with JSON escaping and metadata. The 128 MiB capture budget includes duplicate
protocol/audit copies and offline request history; it does not truncate what the
model sees. Exhausting capture stops with an explicit incomplete result. The
memory allowance includes native client processes, commands, cache, scratch
and controller buffers while leaving most of this 8 GiB host to other work.
The production Grepglint response limit and callback deadline remain unchanged.
No measurements have established that these ceilings suffice for every task.

An 80-trial run reserves 10,826,547,200 bytes; 160 trials reserve
21,647,851,520 bytes. Files and directories are owner-only. Reservation precedes
execution. Initial metadata is assembled in a private `initializing-run-ID`
directory and published only when every not-started record is complete.
Metadata replacements preserve the previous file until an fsynced replacement
is ready; a bounded ownership intent records both possible file identities.
An interrupted publication remains readable as incomplete, and its temporary
files count against the reservation. Successful teardown seals file identities and hashes and releases
unused reserved bytes. A killed controller leaves its unsealed reservation
charged. No new run can evict it. Concurrent execution and cleanup are refused
using account-level locks; the worker retains its lock if its launcher dies.

Each run contains `plan.json`, `run.json`, `ownership.json`, and a JSON record
plus streaming JSONL audit for every planned trial. All trials initially say
`not-started`. An attempted trial becomes `completed` or `failed`. Cancellation,
transport loss, required-audit failure, isolation failure, invalid final answer,
deadline or exhausted limits stop the run. The current trial remains and later
trials stay explicitly `not-started`. Factual correctness is not available here
and cannot stop execution. Partial runs remain valid incomplete scoring inputs.

The previous recursive `file_list` failure remains a historical result under
`controlled-handlers-v1`. That tool is absent from new paired trials. A native
`rg --files` exceeding 64 KiB continues with the client's truncation notice.
Handler failures that terminate a client still retain their original error.
A missing post-trial quota observation never replaces an earlier trial failure;
the allowance change remains unknown when a reading is missing.

```sh
python3 benchmarks/paired.py --validate /private/paired-runs/run-ID
python3 benchmarks/paired.py --validate /private/paired-runs/run-ID \
  --export /private/shareable-summary.json
python3 benchmarks/paired.py --cleanup /private/paired-runs/run-ID
```

Validation is read-only and bounded; exit 3 means incomplete or invalid, with a
diagnostic. Cleanup requires a stopped, sealed, owned run and exact file
contents. Edited, replaced, unknown or unsealed files are preserved. After an
abrupt kill, inspect the partial run and recorded systemd unit before manually
removing only the directory you own. A cleanup failure retains the run for
inspection. Neither cleanup nor this runner touches smoke attempt accounting.
Use a new run after resolving a failure; nothing retries automatically.

Exports allow only documented IDs/hashes, configuration/partition/status,
counts, numeric measurements and token counters with simulation provenance.
Quota is null for fake runs. They omit transcripts, source, prompts, raw
answers, arbitrary diagnostics, call arguments and private absolute paths.
Raw artifacts stay local until explicit cleanup.

## Scorer input version 1

The [JSON schema](../benchmarks/paired-schema.json) describes trial records.
[Fixtures](../benchmarks/schema-fixtures) cover a successful trial, malformed
answer, failure and an unstarted partner in a partial pair. Validate each with:

```sh
python3 benchmarks/paired.py --validate-record benchmarks/schema-fixtures/successful.json
```

`contract: paired-discovery-v1` and `schema_version: 1` identify this format.
The local validator also enforces byte limits, provenance and cross-record
identities. Completed records also require answer syntax, build/client/model
identities, and a replay of the JSONL call audit matching its digest, counts,
returned-range/result references, usage and raw final answer. It rejects attempts to relabel simulated records as live.

| Field group | Meaning |
| --- | --- |
| Run/pair/trial | IDs, task, partition, repetition, seed and zero-based execution order |
| Identities | Manifest, source lock, task, implementation, client and Grepglint build hashes; original/local commit and tree |
| Configuration | Requested/reported model and effort, question, common instructions in plan, prompt/catalog/configuration hashes |
| Answer | Raw final text and parsed `{explanation,evidence[]}`; explicit valid/missing/malformed/oversized status |
| Audit | Relative JSONL filename, digest, record/call counts and completion status |
| Tools | Call IDs, result record numbers, bytes, actual returned ranges, success/errors and elapsed seconds |
| Grepglint | Explicit non-use, first-index/search time, later search times, error IDs and actual fallback call IDs |
| Measurements | Trial/tool/source-verification times, cgroup memory peak and sampled cache/database/journal sizes |
| Usage | Provider response counters, per-field completeness, explicit simulation and inclusion rules |

JSONL records have `sequence`, `elapsed_seconds`, `session`, `kind`, and `value`.
`handler.completed` retains exact arguments and results, including structured
Grepglint errors. Raw protocol messages and Responses requests remain local.
Call IDs join raw calls, native command events and Grepglint callbacks. The
call budget counts those observable calls. Nested helper operations such as
`write_stdin` polling and plan updates do not always expose a separate call ID,
so these counts are not a complete census of every JavaScript callback. An
outer JavaScript cell association remains unavailable where the client does
not report it. Provider usage still includes the complete model interaction.

Native shell output can be transformed by arbitrary commands. The runner
retains command events and model-visible results but does not infer source
ranges from them. Retrieved file/region coverage is therefore unavailable for
native trials; final cited evidence remains scorable. Grepglint callback ranges
still record actual snippets. Returned bytes for native commands describe
app-server command output, which may exceed the text shown to the model. When
the client omits output from a completion event, its byte count and the trial
byte total remain unknown. Tool time sums reported command durations and
callback timings; some native events omit durations. Neither measurement is
a substitute for provider token counts or trial wall time. The runner checks
citation syntax, regular files and line bounds, but does not judge factual
correctness or compare against an oracle.
That belongs to the [scoring contract](benchmark-scoring.md).

Response IDs deduplicate usage; conflicting duplicate counters fail. Missing
optional counters remain null with false completeness. Cumulative client
usage notifications are not added to response totals. Cached input is included
in input, and reasoning is included in output, so neither is added again.
Model totals exclude source preparation, indexing and human grading. Source
preparation is a separate command and its time is unknown unless recorded
externally. No per-tool tokens, money or weekly-percentage conversions are
inferred. Required raw-response/call events still must be complete.

Memory uses cgroup `memory.peak`, a cumulative run high-water including children
and tmpfs pages. It is not an isolated per-trial RSS value. Cache, database and
journal file sizes are sampled while draining subprocess output and on return;
reported peaks are lower bounds, not guarantees of observing every transient
byte. Hard cgroup, tmpfs and database ceilings are independent of sampling.
Native scratch is disposable tmpfs charged to the same cgroup. The smaller
smoke-fixture limits remain unchanged.

The accepted evidence limitation remains: checks cover the pinned client's
constructed request and protocol-visible provider calls, not undisclosed
provider-side instructions or capabilities. Fake runs add no claim about
account model availability or provider behavior.

## Validation and observations

The [native calibration record](benchmark-native-calibration.md) contains the
bounded Luna observation and its interpretation.

```sh
python3 -m unittest discover -s benchmarks/tests -q -b
python3 benchmarks/paired_stress.py
GREPGLINT_CODEX_INTEGRATION=1 GREPGLINT_PAIRED_SNAPSHOTS=/tmp/my-corpus-sources \
  PYTHONPATH=benchmarks/tests python3 -m unittest -q -b test_paired_linux
```

The opt-in tests require Django prepared separately and the built binary. They
exercise both real-client configurations, fresh caches, source preservation,
Django indexing failure and native fallback, treatment non-use, native
truncation and process polling, audit replay, private-path restrictions,
invalid/missing/oversized answers, transport loss and cancellation. The older
Linux verifier independently covers cgroup memory/CPU enforcement.
No live turns occur.

One disposable controller stress run performed 960 reads with intervening
edits across 12 epochs in 2.08 seconds. Tracemalloc observed a 679,982-byte Python
allocation peak; retained allocations ranged from 10,650 to 21,196 bytes and
the largest epoch audit was 18,411 bytes. Temporary data was removed. This
measures Python controller buffers, not native dependencies or immutable corpus
trial behavior. The run and trial byte ceilings still apply under sustained use.
