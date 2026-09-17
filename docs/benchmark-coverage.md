# Corpus coverage

These measurements use Grepglint at commit `85030ca1a7c77a9185a63c6ceb99a8208e7df564`, a release build, default limits, and a fresh private cache per source. No agent or paid benchmark trial ran. The query was the fixed text `corpus coverage`; its search hits did not affect task selection.

| Source | Git paths | Source bytes | File lines | Indexed files | Cold seconds | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| django-674eda1c | 7,013 | 45,732,462 | 1,120,268 | 0 | 11.384 | failed |
| envoy-d7809ba2 | 11,700 | 90,089,286 | 1,835,265 | 0 | 5.752 | failed |
| flipt-3d5a345f | 997 | 12,352,278 | 229,095 | 908 | 1.285 | success |
| numpy-v2.2.2 | 2,211 | 34,990,979 | 908,039 | 2,113 | 5.065 | success |
| pandas-41968da5 | 2,629 | 52,845,599 | 929,362 | 2,090 | 5.674 | success |
| scikit-learn-cb7e82dd | 1,748 | 23,330,273 | 561,546 | 1,449 | 3.677 | success |
| vscode-138f619c | 7,795 | 127,352,560 | 2,501,876 | 0 | 15.15 | failed |
| vscode-17baf841 | 8,177 | 123,976,327 | 2,614,001 | 0 | 13.295 | failed |
| eshop-b4a40872 | 1,144 | 18,179,447 | 134,384 | 891 | 0.946 | success |

Five sources indexed successfully. Django, Envoy and both VS Code sources returned `SQLITE_FULL` with the diagnostic "Index update failed; no partial search view was published: database or disk is full: Error code 13: database or disk is full". Their transactions rolled back and no source files were indexed. This was observed with the default 128 MiB database limit; the diagnostic does not distinguish every possible cause of SQLite FULL. These sources and their tasks remain in the corpus.

The machine had over 70 GiB free during preparation. All nine prepared trees together, including local Git data, remained below the 8 GiB disposable-data limit. Source byte counts include every tracked blob, including links and binary assets. File lines count newline-delimited records in regular files, including binary files; this is an inventory count, not a claim about executable lines of code.

The per-source JSON files in [benchmarks/coverage](../benchmarks/coverage) retain every tracked path, Git object ID, bytes, line count, default static skip reason and actual indexed chunk count. They also retain stdout size, elapsed time, cache bytes after idle exit, and Linux daemon high-water memory where available. Static skip counts count paths; Grepglint refresh counters may count shared blobs differently. The static classifier does not predict syntax-parser chunk limits. Actual chunk counts come from the completed cache, not from filename eligibility.

[Required task evidence](../benchmarks/coverage/task-evidence.json) lists every required region group and whether any alternative file has indexed chunks. A failed index leaves all its task evidence outside coverage. File availability does not prove retrieval of every region or factual answer correctness. Excluded or unsupported files stay in the source supplied to both configurations, so ordinary file reads and grep remain available.

The stress inventory records Kubernetes at 26,242 paths. The TypeScript API tree response was truncated after at least 53,464 paths, already above the limit; its size and count are explicitly lower bounds. Neither was prepared. Other candidate exclusions are listed separately in [candidates.json](../benchmarks/candidates.json), including unresolved source identity and a multi-repository task.

Reproduce after preparing all sources:

```sh
cargo build --release --locked
python3 benchmarks/coverage.py --snapshots /tmp/my-corpus-sources --output /tmp/my-corpus-coverage --binary target/release/grepglint
```

Coverage processes one source at a time. The client timeout is 45 seconds, allowing the daemon its documented 30-second work budget. Each cache requests one-second idle shutdown, waits at most eight seconds for the socket to disappear, then removes its private cache. If the daemon does not exit, the cache is retained and its path is reported. A coverage failure is retained in JSON; it does not remove a task. The script exits nonzero for missing or modified sources and preparation errors. A recorded index failure is a measurement, so completing the report still exits zero.

The frozen calibration task is the Django middleware question `ccx-crossorg-217`. A later runner must record its cold-index failure and any fallback use. It must not silently pick a more favorable calibration task.
