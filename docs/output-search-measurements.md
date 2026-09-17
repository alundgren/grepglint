# Retained output search measurements

Observed on Linux on 2026-09-17 with Rust 1.89.0 and a release build. Run
`cargo build --release --locked` and `python3 scripts/output-search-measure.py`.
The [raw observations](output-search-measurements.json) include every response,
latency, sampled daemon RSS/high-water mark, allocated cache disk bytes, and
reported daemon limits. Fixtures and caches are disposable. No model calls or
agent-effectiveness claims are involved.

| Scenario | Original bytes | Preview bytes | Search bytes | Region page bytes | Full exact pages | Full page response bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 4,000 noisy test lines and one middle failure | 88,042 | 906 | 1,401 | 4,514 | 22 | 97,188 |
| 4,000 compiler warnings and one meaningful error | 136,046 | 907 | 1,530 | 4,452 | 34 | 148,094 |
| Version/path information only in the middle | 116,029 | 907 | 1,444 | 4,470 | 29 | 126,877 |

Each intended diagnostic appeared in the first result for the scripted query.
A search plus its printed region-page command took two retrievals after the
preview. A deliberately absent query returned no results and a paging command,
using 165 or 166 response bytes. Subsequent full decoded paging reconstructed
every original byte in all three scenarios. The integration suite also uses a
poor query that returns plausible test lines while omitting the real failure,
then reconstructs the original successfully.

The first output search, including daemon startup, took 147.94 ms. Twenty
alternating output/repository cycles gave output-search latency of 16.42 to
23.74 ms, median 20.42 ms. Repository queries took 11.77 to 40.28 ms, median
15.46 ms, including their first registration. The temporary output index is
rebuilt each time; no retained ranking cache improves repeated queries.

Before the large-limit fixtures, the sampled daemon high-water mark was
9,004 KiB and peak allocated cache disk was 667,648 bytes. Including an accepted
8 MiB single-line output and a 600,000-newline chunk-limit fixture, high-water
memory reached 26,020 KiB and peak allocated disk reached 21,893,120 bytes.
The maximum-size output's middle `constructEvent` diagnostic was found.
The newline fixture returned the explicit 8,192-chunk error and its exact page
was unchanged afterward. Cache file inspection found only existing daemon,
repository, and owned output files, with no temporary ranking index.

Sampling every 10 ms can miss short peaks. RSS measurements do not establish
an operating-system-independent memory guarantee or prove the absence of leaks.
The daemon reported the unchanged 64 MiB SQLite heap, 512 MiB Linux address-space,
30-second work, and 64 KiB response limits. Code additionally caps the temporary
database at 32 MiB, expanded corpus at 32 MiB, chunks at 8,192, and result count
at 20. SQLite allocation, chunk or expansion failures return errors rather than
successful partial searches. The normal tests verify encoded response limits,
controls, cancellation, expiry, eviction, purge and retained-data corruption.

These observations cover selected logs and a short sustained run. They do not
show that arbitrary queries find the answer or that every accepted 8 MiB log
fits the search budget. Exact paging remains the complete retrieval path.
