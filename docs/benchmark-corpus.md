# Discovery corpus

The corpus in [benchmarks](../benchmarks/manifest.json) contains 40 discovery
questions, 30 adapted from CodeScaleBench and ten authored for eShop. These
are a small pilot with explicit coverage requirements, not a representative
sample of software work. None requires editing or running the source project.

The questions cover ten identifier lookups, eleven descriptions of behavior
without answer paths, and nineteen explanations across files. Required
evidence is Python for 21 tasks, Go for five, TypeScript for three, C++ for one,
and C# for ten. Every task includes the original wording when published, its
adaptation, required factual claims, a reference answer and source regions.

## Validate without network access

Run from the Grepglint checkout with Python 3.10 or later:

```sh
python3 benchmarks/validate.py
python3 -m unittest discover -s benchmarks/tests -v
```

The validator needs no downloaded repository, daemon, trial credentials or
third-party Python package. It checks the retained source evidence against
Git blob IDs, reconstructs each full tree from the coverage inventory, checks
commit evidence, notices, citations, partition and task-specific review
receipts. It cannot decide whether prose is factually correct. That requires
the independent source audit recorded in `benchmarks/reviews.json`.

`--allow-pending-review` is an authoring check. It does not accept tasks.
`--snapshots /absolute/disposable/path` also compares cited files with prepared
source bytes. A missing file, changed evidence, pending review or malformed
record exits nonzero with a diagnostic. Run only the benchmark tests shown
above; retained upstream source and original task files are data.

## Prepare source

Preparation needs Git with `clone --revision` support, GitHub CLI and network
access. The implementation was checked with Git 2.53.0 and gh 2.100.0. Use an
owned disposable directory outside the benchmark checkout:

```sh
python3 benchmarks/prepare.py eshop-b4a40872 --output /tmp/my-corpus-sources
python3 benchmarks/validate.py --snapshots /tmp/my-corpus-sources
```

The second command requires all selected sources when validating every task.
Use the IDs in `benchmarks/sources.json` to prepare them sequentially. An
existing source destination is refused, never replaced. A process lock allows
one preparation or coverage operation per disposable directory at a time.

Preparation fetches only the locked revision with no checkout, disables Git
hooks and external Git configuration, reads raw objects, validates paths and
links in a local archive, and creates a fresh one-commit repository whose tree
must equal the source lock. Source bytes, executable flags, links and notices
remain intact. It does not run repository code, dependencies, applications,
hooks or Actions. The upstream commit and any mirror differences stay in the
benchmark metadata, outside the agent checkout. GitHub-generated archives
were unsuitable for several sources because their output differed from the
tracked objects; the retained preparation report records those failures.

Each prepared source, including its local Git storage, must fit within 1 GiB.
The shared directory has an 8 GiB budget. Space checks require 1 GiB free
beyond Grepglint's 576 MiB disk reserve before preparation, during fetch,
before export and after local Git initialization. These checks sample disk use; another process
can consume disk between checks.
Fetching and Git-object export each have a 300-second deadline. Child output, archive entries and paths are bounded.
Temporary fetch data is removed on success or failure. After using the corpus,
remove only the disposable directory you created; no global Grepglint cache
or unrelated checkout needs cleanup.

Give both trial configurations the same complete prepared tree and question.
Keep `benchmarks/`, reference answers, review receipts, original verifiers and
later Git history outside their accessible filesystem. The source checkout
has no remote and contains a local commit for the pinned tree. The later
runner must enforce isolation and read-only access; this preparation tool is
not an agent sandbox.

## Selection and partition

The suite lock is `csb-v2-full-validated` at CodeScaleBench commit
`8d5f3c876c28a5033634facf42c21da9ebc6fcd6`. Its actual manifest has 275 entries.
The inspected candidates and exclusions are in
[the candidate inventory](../benchmarks/candidates.json). The 30 selected
published task directories exist in that pinned suite. Original code-edit
requests have separately written discovery questions and reference answers;
there is no claim that their original editing scores still apply.

Mirror names are not pins. `sources.json` records full mirror and upstream
commit/tree IDs and every differing path. The complete pinned mirror tree is
the selected source where a mirror is used. Some mirrors contain changed CI
files or omit upstream files; those differences are explicit. No files were
removed during preparation to improve Grepglint results. eShop uses upstream
commit `b4a40872005d4bb29e5b1fa1ff7e244143d39215` directly.

The partition is frozen before trials: six published and two eShop tasks are
for development; 24 published and eight eShop tasks are held out. Near
variants share `duplicate_cluster`. Clusters are sorted by the SHA-256 of
`corpus-v1:` followed by cluster ID. The first subset reaching each possible
count is retained until the required count is reached. The remaining clusters
are held out. The validator recomputes this rule.

Calibration uses the development published task with the smallest SHA-256 of
`calibration-v1:` followed by task ID. This selects `ccx-crossorg-217`. Its
Django source fails the recorded default cold-index check. That outcome does
not change the partition or calibration task. No agent trial was run here.

## Source audits and scoring

Published file lists and line ranges are not accepted without inspection.
The adaptations document corrections, including the conflicting Django
middleware oracles, placeholder lines 1–50, `check_is_fitted` raising an error,
and FastICA computing sources during unit-variance normalization even when
its source-computation flag is false. The eShop claims include observed edge
cases such as missing products during stock checks and differing quantity
checks for new and existing order items.

The independent reviewer checks every task against cited source, including all
ten authored answers and every published oracle correction. Review receipts
bind the accepted task JSON to a digest. A changed question or rubric requires
a new audit. Read the [scoring contract](benchmark-scoring.md) before building
the runner or scorer, and the [coverage report](benchmark-coverage.md) before
calibration.

The original CodeScaleBench material retains Apache-2.0 terms and attribution.
RepoQA-derived descriptions retain the additional RepoQA notice. Evidence
files retain their upstream terms and headers, including Flipt's GPLv3 terms;
they are not relicensed as benchmark code. Each source lock lists retained
licenses and notices. Full snapshots preserve all tracked notices, including
notices outside the root. Only cited files and metadata are checked in here,
not complete source repositories.
