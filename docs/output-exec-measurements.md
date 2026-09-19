# Command execution observations

Measured on Linux with Rust 1.89.0 and an optimized release build, using
`python3 scripts/output-exec-measure.py`. The script uses disposable local caches,
requires no model or network calls, and saves no command contents in accounting.
[Raw measurements](output-exec-measurements.json) include all 18 command records.
Timing samples precede the SIGXFSZ handling correction; the subsequent regression
tests cover that failure path and producer signal inheritance.
Each command appends one byte to a counter, emits the fixture and exits 7.
The final counter proves one execution per invocation, including the unchanged
and both preview profiles. Every wrapper returned exit 7.

| Original bytes | Profile | First call | Warm median, two calls | Peak CLI RSS | Returned stdout bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| 1,024 | unchanged | 25.7 ms | 16.8 ms | 7,680 KiB | 1,024 |
| 1,024 | preview16k | 14.1 ms | 20.0 ms | 7,688 KiB | 1,024 |
| 1,024 | preview32k | 22.5 ms | 34.6 ms | 7,672 KiB | 1,024 |
| 8,388,608 | unchanged | 47.6 ms | 66.1 ms | 7,592 KiB | 8,388,608 |
| 8,388,608 | preview16k | 6.197 s | 3.368 s | 7,532 KiB | 918 |
| 8,388,608 | preview32k | 3.042 s | 3.301 s | 7,972 KiB | 918 |

The first large preview16k call initializes storage. Preview32k reuses an emptied
store after purge. Small calls do not open storage. These first/warm observations
include process startup, the disposable producer and `/usr/bin/time`; they are
not isolated CPU costs or filesystem-cache-controlled trials. Other validation
ran on this shared machine during measurement. The two preview profiles use
the same implementation with different thresholds, so timing differences here
do not establish a performance advantage for either profile.

The omitted middle diagnostic was found by output search in 0.743 s. A rejected
2,001-byte query left paging available. Full recovery made 2,061 page calls,
returned 9,060,219 encoded bytes, and reconstructed all 8,388,608 original bytes
with SHA-256 `98e255bbcfbb8cb7922f82fb80433a0f876187f77293c6b28bb89dadd26609f7`.
Repeating the initial cursor returned the identical page. Recovery took
85.281 s in total, with a 39.3 ms median page and 7,940 KiB maximum page-process
RSS. Every page verifies the entire retained output, so recovering 8 MiB reads
roughly 16 GiB through SQLite. This existing integrity policy makes complete
recovery costly even though the initial preview is small.

A 10 ms sampler observed 58,691,584 allocated bytes across the output store,
journal, anonymous spool, ownership and coordination files. It observed the
spool reaching exactly 8,388,608 bytes. The spool uses an unlinked file and is
counted through the executing process's `/proc` descriptors, not just directory
entries. Process-exit races can make a sample unavailable; shorter peaks can
also fall between samples. The enforced output allowance is 97 MiB across the
40 MiB database, 41 MiB journal and two 8 MiB spools. This is additional to the
repository reserve. Purge took 0.995 s and left 29,343,744 allocated bytes in
reusable empty database pages and bounded metadata, with no named spool files.

The [execution integration tests](../tests/output_exec.rs) separately exercise:

- Exact small and unchanged output, both preview thresholds, combined stdout and
  stderr, nonzero status, shell quoting, pipelines, redirects, environment and cwd.
- Omitted-middle search, repeated cursors, failed search followed by full exact
  Unicode/control/CRLF reconstruction, and an execution-once counter.
- Invalid late UTF-8/NUL, oversized output, unavailable storage, invalid cache
  configuration, a failed retained-store write, fixed expiry and corruption.
- A real inherited 256 KiB file-size limit with default SIGXFSZ disposition
  that forces partial spool-write failure. The prefix and remaining bytes are forwarded in order, the producer
  exits 13, and its side-effect counter contains exactly one byte. A separate
  check proves the producer retains both its file-size limit and the inherited
  default or ignored signal disposition.
- A 10.2-second quiet command, waiting without premature preview delivery,
  two simultaneous captures and native forwarding for a third command.
- Deadline, signal and disconnected-consumer cancellation. Descendants cannot
  perform their delayed side effect, and uncommitted rows are removed.
- On Linux, an anonymous 8 MiB spool is observed while the producer waits;
  cancellation removes it and leaves only the five recorded store files.

The existing output tests cover aggregate/entry pressure, free-space reserve,
SQLite capacity, paging/search limits, crash cleanup and owned-file protection.
These deterministic checks establish the tested execution/retrieval behavior,
not model effectiveness or savings in paid agent sessions. No paid trials ran.
