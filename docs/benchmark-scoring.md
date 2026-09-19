# Discovery answer and scoring contract

A final answer is a JSON object with an `explanation` string and an `evidence`
array. Each evidence item contains `path`, `start` and `end`: a
repository-relative source path and positive, inclusive line numbers. For
example:

```json
{
  "explanation": "Describe the behavior and how the cited code establishes it.",
  "evidence": [{"path": "src/example.cs", "start": 12, "end": 24}]
}
```

The runner associates the answer with the task and pinned repository. Paths
must refer to existing regular files in that repository. Absolute paths,
parent traversal, links outside the tree, invalid line ranges and evidence
from another revision are invalid. Grepglint's optional `symbol` field is not
required. Identical rules apply to every language and both tool configurations.

A task is correct only when the explanation covers every required factual
claim, valid source evidence supports each claim, and the answer contains no
material contradiction. A filename hit is insufficient. A contradiction
about the requested behavior makes the task incorrect even if retrieval
metrics are perfect. Correctness is a factual judgment, separate from the
retrieval diagnostics below. The later scorer must retain the claim-by-claim
judgment and reason for rejection or adjudication.

## Evidence groups

Every claim names one or more mandatory evidence groups. All named groups
must be satisfied. A group contains one or more interchangeable alternative
regions; any one can satisfy that group. Multiple groups represent separate
requirements, even when their ranges occur in the same file. Optional groups
have `required: false`, do not establish a required claim, and never increase
the missing-required count. An empty alternative list is invalid.

For exact-match diagnostics, deduplicate submitted path/range tuples first.
Only valid citations contribute overlap. Malformed citations remain visible
as answer errors and do not disappear from the report. Report these metrics:

- **File precision** is the number of distinct cited files accepted by any
  mandatory or optional region, divided by all distinct cited path strings.
  Invalid and unrelated paths count against precision. With no citations,
  precision is zero.
- **File recall** is the fraction of mandatory groups for which the answer
  cites a valid range in at least one alternative's file. Count each group
  once, even when several alternatives or repeated citations match it. This
  group-based denominator avoids penalizing interchangeable files. Also
  report the number of distinct matched files so the metric is unambiguous.
- **Region overlap** for each mandatory group is the maximum, across its
  alternatives, of covered lines divided by that alternative's line count.
  Covered lines are the union of intersections with valid submitted ranges in
  that file. The interval intersection has size
  `max(0, min(end1, end2) - max(start1, start2) + 1)`.
- **Region recall** is the fraction of mandatory groups with positive overlap.
  Report the per-group fractions alongside it. A group contributes at most
  one hit. Optional groups may be reported separately but are never missing
  requirements.

Positive overlap is a retrieval diagnostic, not proof that the cited lines
support the explanation. A one-line intersection may omit the behavior that
matters. Likewise, citing an entire file can give perfect overlap but poor
answers. Report total cited lines after merging overlapping ranges per file.
The factual adjudicator must check the evidence itself.

A previously unlisted alternative can be accepted only with an explicit,
source-grounded adjudication recorded for both configurations. Do not change
held-out rubrics in response to which tool performed better. Version any
rubric correction and identify all affected trials before comparing scores.

Tool errors, source-unavailable errors, exhausted budgets, invalid final
answers and failed cold indexing remain trial outcomes. They must not be
removed from denominators or turned into missing tasks. Corpus validation
performs no agent runs or answer scoring; implementing this contract belongs
to the scoring/report issue.

## Durable runner input

The [offline paired runner](benchmark-paired.md#scorer-input-version-1) writes
versioned trial JSON and streamed call JSONL. It retains simulated provenance,
raw answers, actual returned ranges and incomplete pairs. Simulation must never
be accepted as live measurement. Factual grading remains separate from the runner.

## Offline report command

[Offline discovery scoring](benchmark-reporting.md) documents the implemented
`benchmarks/score.py prepare` and `report` commands, blind human judgments,
versioned alternative evidence, quota observations, privacy and bounded inputs.
