# Optional retrieval wording

The CLI catalog and native benchmark tool now describe Grepglint as an option
when related words or identifiers are known but no focused file is available,
or when broad text search returns too many matches. Examples include
`migration dependency graph` and `request middleware exception`. The native
function still accepts only `query`; the CLI's `--limit` is not a native argument.

The description calls results lexical suggestions, requires reading the relevant
regions to verify them, and directs exact strings, regex and all-occurrence
searches to `rg`. Known files can be read directly. It also explains cold indexing
and fallback after failure. Repeating a query does not fix a capacity failure.
Tool use remains optional. No semantic-understanding or exhaustive-reference
claim is made.

## Evidence limits

The [previous 40-task comparison](https://github.com/alundgren/grepglint/issues/52)
recorded two Grepglint calls across 40 treatment trials. Both failed during Django
indexing with `SQLITE_FULL`; neither returned results. The other 38 trials did not
use Grepglint. Lower treatment token counts therefore do not establish retrieval
benefit. Sampled non-use transcripts showed `rg` and targeted reads, but did not
explain why Grepglint was skipped. Wording is a possible adoption problem, not a
proven cause. The attempted queries reached the handler; there is no evidence
that query syntax caused the capacity failures.

The initial wording change used only offline declaration and protocol checks.
Those establish what the client receives, not improved adoption or correctness.
The subsequently authorized live pair is recorded below. Existing historical
plans, transcripts and results remain unchanged.

## Later evaluation

A separately authorized pilot can repeat the eight development tasks while
keeping tool use optional and rubrics fixed. Report non-use, calls, successful
retrieval, indexing failures, correctness, time and tokens separately. More calls
or fewer tokens alone do not establish usefulness.

For an old-versus-new wording comparison, use the same current binary and resource
limits for both, so storage fixes do not confound the comparison. The 32 formerly
held-out tasks were already consumed by the full comparison and are no longer an
untouched holdout. No model trials are part of this wording change.

## Offline verification

The updated release binary passed the final native-declaration check and the
selected Django protocol proof on 2026-09-19. Both configurations completed using
scripted local responses, without account access or model inference. The sealed
proof is `run-6111e334c64e02696e5b28a7` in the private
`retrieval-wording-validation/runs` artifact directory. Its validation reports two
completed simulated trials.

The proof records runner implementation SHA-256
`0f23d55da2b21347fe72ca5b8c27592ffbc92318be9ca56670ff243addd44774`
and binary SHA-256
`d7694936818fced6309c948974fa83ce733dd16a8d83322744a7ada1fbf6c502`.
These checks do not measure voluntary use or retrieval benefit.


## First live pair

After separate user authorization on 2026-09-19, one fresh pair ran on the Django
middleware task `ccx-crossorg-217`, with `gpt-5.6-luna`, high effort, seed 0 and
one repetition. Updated wording ran first, then baseline, with the same current
binary and default resource limits. Grepglint remained optional.

| Observation | Updated wording, Grepglint available | Baseline |
| --- | ---: | ---: |
| Wall seconds | 60.90 | 78.69 |
| Input tokens, including cached input | 76,821 | 169,935 |
| Output tokens, including reasoning | 2,332 | 2,000 |
| Total tokens | 79,153 | 171,935 |
| Grepglint calls | 0 | 0 |

Both trials completed with valid answer structure and citations. The offline
scorer replayed their audits successfully. Factual correctness remains ungraded;
citation coverage does not establish correctness. No Grepglint call ran, so
indexing and retrieval were not exercised. The lower treatment time and token
count cannot be attributed to successful retrieval. One non-use observation
does not establish whether the wording changes adoption.

This compares optional availability against baseline, not old wording against
new wording. Live run `run-dc3c6845946526c7cf2ef0c7`, the ungraded report and a
prepared grading form remain in the private `retrieval-wording-validation`
directory. No retries or additional model trials ran.


## Persistent exploration instruction experiment

The separately selected `--guidance prefer-search-v1` experiment adds a fixed
paragraph to the Grepglint trial's developer instructions. The baseline retains
the common instructions, and `description-only` remains the default. Source,
question, binary, budgets and optional tool availability otherwise stay the same.
This tests availability plus a stated exploration preference against baseline;
it does not isolate the paragraph's effect from tool availability.

The instruction recommends starting with `grepglint_search` for exploratory
implementation questions when no relevant file is known. It gives the generic
example `migration dependency graph`, directs agents to verify result ranges,
and explicitly allows skipping Grepglint for focused reads or exact searches.
It retains lexical limitations, cold-index cost, failure fallback and local-only
operation. The full text is printed in the plan and recorded in session settings.
It contains no terms chosen from the calibration question.

This borrows the persistent task guidance used in Graphify's
[agent instructions](https://github.com/Graphify-Labs/graphify/blob/b9cd9570728a5ff3485d2a1e36fe9a1272a368ae/graphify/always_on/agents-md.md).
Graphify's instructions direct agents to query first when a graph exists. Its
[installer](https://github.com/Graphify-Labs/graphify/blob/b9cd9570728a5ff3485d2a1e36fe9a1272a368ae/graphify/install.py)
also supports Claude search/read hooks and an optional strict read restriction.
This experiment uses no hooks or forced calls and does not copy Graphify's graph
capabilities or claims of benefit.

A fresh offline proof is required for this experiment. Reports identify its
selection, and validation allows only the recorded paragraph to differ between
configurations. Existing saved plans and trials are preserved.


### Persistent-instruction trial result

On 2026-09-19, the authorized `prefer-search-v1` pair completed on
`ccx-crossorg-217` with Luna/high, seed 0, treatment first and one repetition.
The binary and resource limits matched the earlier description-only pair.
The new paragraph reached the treatment developer instructions; the baseline
retained the common instructions.

| Observation | Persistent instruction, Grepglint available | Baseline |
| --- | ---: | ---: |
| Wall seconds | 60.33 | 66.14 |
| Input tokens, including cached input | 121,441 | 123,945 |
| Output tokens, including reasoning | 1,919 | 2,597 |
| Total tokens | 123,360 | 126,542 |
| Grepglint calls | 0 | 0 |

Both records and the offline scoring audit passed. Answer structure and citations
were valid, but factual correctness remains ungraded. The model did not call
Grepglint, so neither indexing nor retrieval ran. This pair does not demonstrate
retrieval benefit or establish a general effect on adoption. No model retries,
forced calls, additional tasks or rubric changes were made.

The benchmark suite ran 160 tests with 12 opt-in skips. Four global-lock tests
conflicted with concurrently running offline proofs; all four passed when rerun
alone. Focused native-client checks passed for both instruction modes, including
optional non-use. Re-scoring the prior live pair preserved its input identity
and measurements. Whitespace and local documentation-link checks passed.

The offline proof is `run-4fea9ec3c3d10dd6c7087331` and the live run is
`run-e2c9260a3d1a40c709e2596b`. The private
`exploration-guidance-validation` directory contains plans, protocol evidence,
the ungraded report and a prepared grading form. Runner implementation SHA-256
is `54a879c9cbc273e912e0ae17cf99894f5e21c6601a70cb206a6f4a484ff458ed`;
the binary SHA-256 remains
`d7694936818fced6309c948974fa83ce733dd16a8d83322744a7ada1fbf6c502`.


## Final skill experiment

The user authorized one final Django pair using an actual skill with similar
exploration guidance. `--guidance skill-v1` installs the
[grepglint-explore skill](../benchmarks/skills/grepglint-explore/SKILL.md) only in
the treatment's disposable Codex skill directory. It is not installed globally,
and neither checkout nor personal configuration is modified. The baseline has
no skill. Both retain the original question and common developer instructions,
with no explicit skill invocation and no `prefer-search-v1` paragraph.

The skill metadata describes exploratory implementation questions. Its body
recommends a compact lexical query and targeted verification, permits direct
reads and exact searches instead, and retains cold-index and failure guidance.
No query examples come from the Django calibration question. Tool use remains
optional. The model must choose whether to load the skill and call Grepglint.

[Official skill documentation](https://developers.openai.com/codex/skills/)
describes initial metadata discovery and later body loading. For the pinned
client, the offline protocol proof checks the actual emitted catalog and a
complete native file read, plus denial of writes and access to sibling private
files. The whole skill text and its SHA-256 are included in the selected plan.
A full-file read is recorded separately from Grepglint calls and successes.

The final live scope remains `ccx-crossorg-217`, Luna/high, seed 0, one repetition
per configuration and the existing binary and resource limits. A non-use result
will be preserved without further wording or model retries. The next investigation
would examine transparent interception, without installing interception hooks as
part of this trial.


### Final skill trial result

On 2026-09-19, the authorized `skill-v1` pair completed with Luna/high, seed 0,
`ccx-crossorg-217`, treatment first and one repetition. The binary and resource
limits matched both earlier wording pairs. The baseline had no skill.

This time the treatment read the complete skill file and made one successful
Grepglint call. The query was `MIDDLEWARE middleware chain legacy request response
hooks`. It returned five regions, all from `docs/ref/request-response.txt`.
No implementation file appeared in those search results. The search including
cold indexing took 6.17 seconds. There were no indexing errors or repeated
Grepglint calls. Full-file skill reading was independently checked against
completed native-command output in the saved transcript.

| Observation | Skill and Grepglint available | Baseline |
| --- | ---: | ---: |
| Wall seconds | 92.49 | 49.49 |
| Input tokens, including cached input | 193,522 | 117,135 |
| Output tokens, including reasoning | 3,347 | 1,714 |
| Total tokens | 196,869 | 118,849 |
| Grepglint calls | 1 | 0 |
| Successful Grepglint calls | 1 | 0 |

Both records and offline scoring validation passed. Answer structure and
citations were valid; factual correctness remains ungraded. This is a successful
observation of voluntary skill loading and tool use, not evidence of a general
adoption improvement or retrieval benefit. The treatment used more time and
tokens, and the returned regions were all documentation. One pair cannot isolate
why those measurements differ.

The full benchmark suite passed 165 tests with 13 opt-in skips. The selected
offline proof additionally verified the sole skill catalog, complete skill read,
write denial, private-sibling denial and existing source/credential isolation.
The previous live pair still replayed with unchanged input identity and
measurements. The skill validator and documentation checks passed.

No additional model trial ran. Since the no-use result did not recur, transparent
interception was not investigated or installed in this step.

The selected proof is `run-5924c8f328b550cc7e183ea6` and the live run is
`run-07baad1543c7762708c73b1f`. The private `skill-guidance-validation`
directory retains plans, audits, the ungraded report, grading form and search
observations. Skill SHA-256 is
`87a57722b5fcb418623e2d65a91d8a066a342b265a899cbeb0506eaed669a52a`; runner implementation SHA-256 is
`f8773c1eea1789ee82aa35da94832acbbbcf8369ea830dd88cd3ef821d3eac41`.
