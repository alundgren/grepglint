# Retained output observations

Measured on Linux with the release build using
`python3 scripts/output-measure.py`. The script makes no network or model calls.
It creates disposable repositories and alternates capture, paging, and repository
search for 20 cycles. Each log contains 2,079,000 bytes of repeated test results
and compiler warnings with CRLF and LF endings. It then reconstructs one complete
log, confirms pressure eviction of the oldest handle, and purges output.
[Raw observations](output-measurements.json) include each invocation.

| Operation | Cold elapsed | Warm median | Maximum elapsed | Maximum CLI peak RSS | Maximum encoded response |
| --- | ---: | ---: | ---: | ---: | ---: |
| Capture | 2.251 s | 1.0884 s | 2.4714 s | 7,140 KiB | 830 bytes |
| First page | 45.6 ms | 24.7 ms | 61.6 ms | 7,116 KiB | 4,482 bytes |
| Repository query | 568.4 ms | 23.4 ms | 568.4 ms | 4,924 KiB | 787 bytes |

The daemon had one thread and 7,872 KiB peak RSS after these alternating queries.
A 10 ms sampler observed 63,016,960 allocated disk bytes at peak, including the
repository cache, output database, rollback journal, ownership, and locks.
After purge, 31,506,432 allocated bytes remained, mostly reusable empty output
database pages. The journal was truncated; source contents were erased with
SQLite secure deletion. The sampler can miss shorter peaks, so the 81 MiB output
file allowance, plus the repository's existing budget, is the enforced bound.
RSS comes from `/usr/bin/time`; daemon RSS comes from `/proc`. These results are
observations on a shared Linux development machine, not latency guarantees.

The 4 KiB pass-through threshold keeps a short diagnostic unchanged. The 256-byte
head and tail give a brief view of startup and completion while leaving the
middle available through exact pages. A 4 KiB page stays below 64 KiB even with
worst-case JSON or human control escaping and continuation metadata. Captures
use 28 KiB write batches to avoid a transaction per short producer write.
An 8 MiB per-output maximum and two full reservations allow simultaneous test
and build captures within a 32 MiB payload allowance. A 40 MiB database leaves
room for chunk records and indexes without changing repository storage limits.
One-hour expiry and 64 entries bound quiet accumulation of smaller logs.

The tests separately exercise invalid late bytes, size and entry limits,
reservation contention, fixed expiry, corrupted content, full SQLite storage,
insufficient reserve, interrupted captures, stalled stdin, broken output pipes,
private files, unsafe replacements, purge, legacy daemon sockets, and repository
search during capture. Linux and macOS run the same tests in CI. Local failure
injection lowers SQLite's page limit and raises the requested free-space reserve;
it does not fill the development machine's disk. The overall capture deadline
is also checked with an expired clock value rather than a two-minute test wait.

These fixture results do not establish agent effectiveness or saved model
context. That evaluation remains separate from capture and paging correctness.
