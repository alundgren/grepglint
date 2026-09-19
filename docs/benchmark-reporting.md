# Offline discovery scoring

`benchmarks/score.py` grades saved answers and reports paired observations without
network access, credentials, inference, source execution or daemon changes. The
[corpus scoring contract](benchmark-scoring.md) remains authoritative. Historical
synthetic results in [evaluation.md](evaluation.md) are not inputs to this report.

The supported input is `paired-discovery-v1`, produced by the
[paired runner](benchmark-paired.md). Schema version 1 is simulated and excluded
from live comparisons. Version 2 records authorized ChatGPT trials and adds
selected-proof, authorization and audited submission checks. The scorer refuses
other contracts, including preflight receipts. Relabeling version 1 records as
live is rejected. Embedded live quota observations stay separate from manual
comparative quota judgments.

## Prepare and grade

Use an existing private run directory. No source preparation or model call is
needed to score it. Repeat `--run` to include compatible saved runs.

```sh
python3 benchmarks/score.py prepare \
  --run "$HOME/.local/state/grepglint/paired-runs/run-EXAMPLE" \
  --output /tmp/discovery-review
```

This creates `private-key.json` and `review.json`. Give only `review.json` to the
human grader. Keep the key away from the grader until judgments are finished.
Random labels and randomized answer order hide trial order and configuration
metadata. Answer text itself can reveal its tools; this procedure cannot blind
self-identifying prose. The form includes the question, source revision, answer,
required claims and accepted evidence groups. Pinned evidence files are under
`benchmarks/evidence/SOURCE_ID/`; alternatives also carry source URLs when present
in the original corpus. Check the actual cited lines, not just their filenames.

Edit only each answer's `judgment`. For every claim, set `supported` and
`evidence_supported` to `true` or `false`, and write a source-grounded `reason`.
Set `material_contradiction` and the overall `reason`. Leave undecided fields
`null`. A judgment passes only when every claim and its supporting evidence pass
and there is no material contradiction. Missing decisions remain unscored.
An unsuccessful trial, an invalid citation or an answer without evidence cannot
become a pass by filling in the form.

```sh
python3 benchmarks/score.py report \
  --run "$HOME/.local/state/grepglint/paired-runs/run-EXAMPLE" \
  --key /tmp/discovery-review/private-key.json \
  --review /tmp/discovery-review/review.json \
  --output /tmp/discovery-report
```

`report.json` and `report.md` come from the same data. Repeating the command with
unchanged inputs into a new directory produces identical bytes. Omit both
`--key` and `--review` to inspect retrieval, consumption and missing judgments
before grading. Never edit answers or rubrics inside the review form. The private
key binds labels to the full saved records and the oracle hash, so changed inputs
or rubrics require a new review form.

## Metrics and incomplete work

Both retrieved and cited evidence get independent file precision, group-based
file recall, per-group unioned line overlap, region recall, distinct file counts
and merged line counts. Invalid paths count against file precision. Mandatory
alternatives are interchangeable, and optional groups never add missing
requirements. Returned ranges are the audit's actual ranges, including partial
excerpts, never the containing chunk's range. Citing an entire file can improve
overlap without making its explanation correct.

Paths, file types and inclusive line bounds are checked against the frozen
source inventory. Corpus validation reconstructs its pinned Git tree and verifies
the stored oracle evidence bytes against Git blobs. This does not require the
original working tree to remain on disk. Human factual grading must still inspect
the source content, including any newly accepted alternative.

The scorer retains every planned trial, including not-started trials, malformed
or empty answers, timeouts, source failures and partial runs. Counts distinguish
failed execution, failed human judgments, missing judgments and simulation
exclusions. Completed trial audits are replayed, including every constructed
request's effective tool definitions, registration metadata and instructions.
Assistant and tool messages may grow the conversation without changing those
identities. Pairing rejects duplicate
run/trial identities, different source revisions, task or prompt hashes, model
identities and incompatible configuration or build identities. Run IDs scope
trial IDs, so separate runs may each contain `t0001`.

Partial records are marked `partial_unverified`; their saved measurements remain
visible without claiming a complete audit. Cold-index reference outcomes stay
separate from the trial's observed Grepglint calls, non-use, errors and fallback.
The Django calibration reference still records `SQLITE_FULL` under default limits.
No default limits are raised to make a score look better.

The JSON retains provider counters and completeness. Audit replay deduplicates
response IDs and ignores cumulative notifications as additional contributions.
Input includes cached input, and output includes reasoning; those subcounts are
reported separately and never added again. Missing fields remain unknown. Source
preparation and verification, first-search indexing, tool time and trial wall time
are separate. The runner's first-search indexing measurement includes search time.
Memory is the cgroup aggregate high-water mark, including cache pages, and disk
peaks are sampled lower bounds. No per-tool tokens or subscription prices are
inferred from bytes, time or percentages.

Development and held-out counts are separate, with configuration, question group,
language and exact repository source-byte size breakdowns. Pairs retain task,
run and repetition identities. Task summaries show observed wall-time delta ranges
and judgment counts across repetitions, counting each task once. No significance
or general-benefit claim is made from one task.

## Reviewed oracle corrections

Keep corrections outside the frozen corpus. Pass the same `--corrections FILE`
to both `prepare` and `report`. The correction format is:

```json
{
  "schema_version": 1,
  "version": 2,
  "manifest_sha256": "SHA256_OF_FROZEN_MANIFEST_BYTES",
  "amendments": [{
    "task_id": "TASK_ID",
    "reason": "Why the reviewed pinned source warrants this correction.",
    "source_citation": {"path": "relative/file.py", "start": 10, "end": 20},
    "add_alternatives": [{"group_id": "g1", "path": "relative/file.py", "start": 10, "end": 20}],
    "claim_texts": {"c1": "Corrected required factual claim."}
  }]
}
```

Use empty `add_alternatives` or `claim_texts` when only the other needs correction.
The human must review the reason and citation before supplying this file. The
scorer checks citation bounds but does not substitute for that judgment. Claim
IDs and required groups remain stable. Increment `version` for every revision;
the content hash also distinguishes corrections with accidentally reused version
numbers. Every selected trial for an affected task is rescored with the same
correction, across both configurations and all repetitions. Old judgments are
rejected after an oracle change. The report retains the complete correction and
identifies affected trials through their task IDs. Do not adjust held-out rubrics
based on which configuration performed better.

## Weekly observations

Runner v1 has no live account quota. Optionally attach manually observed weekly
snapshots with `--quota FILE`. This keeps an observation distinct from audited
model counters. It never makes a simulated pair attributable to live usage.
The file binds to the `input_sha256` in the key or ungraded report:

```json
{
  "schema_version": 1,
  "input_sha256": "INPUT_HASH",
  "pairs": [{
    "run_id": "run-EXAMPLE",
    "pair_id": "p0001",
    "observation": {
      "before": {"remaining_percent": 90, "observed_at": "2026-09-18T10:00:00Z", "bucket": "weekly", "reset_at": "2026-09-20T00:00:00Z"},
      "after": {"remaining_percent": 90, "observed_at": "2026-09-18T10:05:00Z", "bucket": "weekly", "reset_at": "2026-09-20T00:00:00Z"},
      "concurrent_activity": false
    }
  }]
}
```

`concurrent_activity` may be `true`, `false` or `null` for unknown. The report
shows both snapshots, bucket/reset timestamps and before-minus-after percentage
points. An unchanged rounded percentage means no observable percentage-point
change, not zero cost. Reset crossings, increasing remaining percentages,
concurrent activity or unknown activity prevent attribution solely to the pair.
Exact recorded session counters remain visible regardless of quota comparability.

## Privacy, limits and recovery

All outputs default to local owner-only files in a new owner-only directory.
The scorer refuses to overwrite an existing output directory. Inputs are never
modified. To create a shareable version, repeat `report` with `--shareable` and
a new `--output`. Export construction allowlists counts, measurements, corpus
identities, statuses and weekly observation fields. It omits raw answers,
judgment prose, source paths, account payloads, free-form diagnostics, absolute
private paths and unrelated transcript contents. Do not share the local report,
review form or private key as if they were exports.

Each external JSON input and aggregate trial metadata are capped at 32 MiB;
runner metadata remains capped at 1 MiB per file. At most eight runs and 160
combined trials are accepted. Audits are streamed with 1 MiB frames and 16 MiB
per trial, with the runner validator's deadline. Evidence processing accepts at
most 10,000 ranges per trial and 100 tool calls. Corrections accept at most 100
amendments and 100 alternative additions per amendment. Combined generated
output is capped at 32 MiB. These limits bound work; they are not measurements
of live model performance.

An invalid identity, malformed input or exceeded bound stops scoring with a
nonzero exit code. Preserve the original run, correct the separate input or
select fewer runs, then retry with a new output directory. A partially written
output directory can be inspected and removed explicitly; it is never reused
or automatically cleaned up. If an interrupted runner has no published plan,
there is no trial set to score yet; retain it for runner recovery. For a published
partial plan, score it as incomplete. No automatic retry or inference occurs.

Offline tests:

```sh
PYTHONPATH=benchmarks/tests python3 -m unittest -q -b test_score
python3 -m unittest discover -s benchmarks/tests -q -b
```
