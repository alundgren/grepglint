# Real-repository search benchmark handoff

## Next session's objective

Refine a 30–50-task pilot that tests whether adding Grepglint helps an agent
find and explain relevant code in real repositories. Produce a reviewed task
selection, scoring approach, and bounded implementation plan. The pilot is
for finding problems and estimating likely benefits, not establishing a
general performance claim. Do not launch paid agent runs or fork repositories
as part of refinement without agreeing on the concrete targets and budget.

## Existing behavior and validation

Grepglint uses SQLite FTS5/BM25, with identifier splitting and no embeddings.
JavaScript/TypeScript receive syntax-aware chunks; other UTF-8 text uses
60-line chunks with six-line overlap. File extensions now select a parser
rather than eligibility. Razor, `.cshtml`, unknown extensions, and extensionless
text are supported. Root `.grepglintignore` rules can override the default
directory/lockfile exclusions. Skip-reason counts describe refresh work, not
a complete inventory of excluded files.

Important limits are 20,000 paths, 512 KiB per source file, 12,000 lines per
file, 4,000 bytes per line, a 256 MiB default cache, and a 30-second work budget.
Inspect the code and README for the exact accounting before selecting large
repositories. C#, Python, Go, and other non-JS/TS languages do not receive
syntax-derived symbol metadata. Score source ranges/files consistently across
languages rather than assuming the `symbol` field is always populated.

Current evaluation is synthetic. `fixtures/shop`, `tests/workflows.rs`, and
`scripts/demo.py` verify retrieval examples, cache reuse, and worktree freshness.
The demo now reads 47 initial contents, including `.gitignore` and 40 generated
queue handlers. `docs/evaluation.md` and `docs/demo-results.json` retain the
original measurements; they do not establish real-repository agent benefits.

Separate future investigations:

- [UTF-16 decoding #1](https://github.com/alundgren/grepglint/issues/1).
- [Dependency/generated-file filtering and ranking #2](https://github.com/alundgren/grepglint/issues/2), including `node_modules`, other language ecosystems, and generated source.

Both issues are `needs-refinement`. Neither is required to start this pilot;
record coverage gaps rather than silently selecting only favorable cases.

## Research worth reusing

- [Sourcegraph CodeScaleBench](https://github.com/sourcegraph/CodeScaleBench)
  is the strongest initial source of tasks. It publishes repository manifests,
  instructions, expected files/symbols, verifiers, and traces. Benchmark code is
  Apache-2.0. Its public branch documents multiple changing suites; pin a commit
  and suite ID and inspect the actual task count. Prefer single-repository,
  read-only discovery tasks first. For example,
  [Django middleware discovery](https://github.com/sourcegraph/CodeScaleBench/tree/public/benchmarks/csb/crossrepo/ccx-crossorg-217)
  has expected files and symbols spanning middleware loading and request handling.
- [Microsoft eShop](https://github.com/dotnet/eShop) is MIT licensed and had about
  1,100 tracked files when inspected. It offers realistic commerce questions,
  but no ready-made search oracle. Author and independently verify the answers.
  The old eShopOnContainers repository is archived and points here.
- [RepoQA](https://github.com/evalplus/repoqa) offers 500 function-finding tasks
  across 50 repositories and five languages. Its published protocol supplies
  repository context directly. Using it through interactive agent tools is an
  adaptation and must be reported as such.
- [Google Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo)
  is an Apache-2.0, multilingual commerce demo with 11 services and roughly
  360 tracked files when inspected. It is an optional alternative, not a large
  repository stress test.

Dataset/tool licenses do not replace the licenses of underlying repositories.
Use pinned static checkouts initially, preserve notices, keep fork Actions
disabled if forks are needed, and avoid installing dependencies or starting
applications for source-discovery tasks.

## Proposed pilot to refine

A possible starting allocation is 30 published tasks and 10 independently
authored eShop questions. This allocation is a proposal, not a selected task
manifest. Include straightforward identifier lookups as a control, behavioral
questions without answer locations, and questions requiring several files.
Report those groups separately. Include both JS/TS and other languages and
record repository size and the number of files Grepglint can actually search.

Compare two configurations:

1. An agent with grep, file discovery, and file reads.
2. The same agent with the same tools plus Grepglint.

Keep model/version, reasoning settings, task wording, source revisions, and
budgets equal. Let the baseline formulate sensible searches; do not force it
to grep the complete natural-language question verbatim. Record the Grepglint
tool description and actual calls so non-use is visible. Control cache state
between trials and report cold indexing separately from warm retrieval.

Keep expected answers, gold patches, verifier code, and reference trajectories
outside the agent-visible checkout. Limit task access to the pinned source;
avoid answer leakage through network access or later Git history. Both
configurations must see the same source content. Review each oracle for valid
alternative answers; changed files in a gold patch are not automatically all
files relevant to understanding a task.

Primary outcomes should be correct answers with source evidence, total model
tokens/cost, and elapsed time. Also capture relevant-file/region recall, tool
calls, bytes returned, and indexing resource use. Do not equate bytes with
tokens or better retrieval with successful task completion. Plan repeated,
paired trials, provisionally three per task, and report failures and variation.

## Decisions and deliverables for refinement

- Select exact tasks, repositories, pinned commits, licenses, and language/size
  groups. Check each against Grepglint's limits before accepting it.
- Decide whether the pilot ends at finding/explaining code or includes code
  changes. Read-only discovery is the proposed first scope.
- Define per-task acceptable answers, file/range scoring, and a review method
  for authored questions. Separate development tasks from held-out evaluation
  tasks before tuning retrieval.
- Select the agent runner, models, repeat count, token-accounting boundary,
  spending limit, cache policy, and artifact format.
- Produce a task manifest, a scoring specification, and implementation work
  sized for a first reproducible run. No tasks or real-codebase trials have
  been implemented yet.

Useful evaluation designs:
[Cursor's search-tool ablation](https://cursor.com/blog/semsearch),
[Sourcegraph's evaluation methodology](https://sourcegraph.com/blog/how-to-evaluate-sourcegraph-on-your-own-codebase),
and [AugmentQA's internal question-answer evaluation](https://www.augmentcode.com/blog/you-make-your-evals-then-your-evals-make-you-introducing-augmentqa).
Sourcegraph reports that improved retrieval did not necessarily improve
aggregate task completion. AugmentQA's described internal corpus is not a
public runnable dataset.
