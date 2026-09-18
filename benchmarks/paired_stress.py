#!/usr/bin/env python3
"""Measure bounded controller buffers across disposable queries and edits, without inference."""
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from unittest.mock import patch

from _codex_capture import Budget
import _codex_handler as handler
from _paired_trial import TrialAudit


def measure(epochs=12, queries=80):
    if not 1 <= epochs <= 20 or not 1 <= queries <= 100:
        raise ValueError('At most 20 epochs and 100 queries per epoch')
    started = time.monotonic()
    tracemalloc.start()
    sizes = []
    retained = []
    with tempfile.TemporaryDirectory(prefix='paired-stress-') as directory:
        root = Path(directory)
        source = root / 'source'
        source.mkdir()
        for epoch in range(epochs):
            path = root / 'audit.jsonl'
            path.touch(mode=0o600)
            audit = TrialAudit(path, Budget())
            try:
                for query in range(queries):
                    (source / 'sample.py').write_text(f'def example():\n    return {epoch * queries + query}\n')
                    with patch.object(handler, 'SOURCE', source):
                        value = handler.read_range({'path': 'sample.py', 'start': 1, 'end': 2})
                    audit.record('stress.read', value, 'simulation')
                sizes.append(path.stat().st_size)
            finally:
                audit.close()
            path.unlink()
            del audit
            retained.append(tracemalloc.get_traced_memory()[0])
        _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'simulation': True, 'inference_performed': False, 'epochs': epochs,
            'queries_and_edits': epochs * queries, 'seconds': time.monotonic() - started,
            'controller_python_peak_bytes': peak, 'retained_python_bytes_by_epoch': retained,
            'maximum_epoch_disk_bytes': max(sizes), 'final_fixture_disk_bytes': 0,
            'method': 'tracemalloc Python allocations; exact streamed audit sizes; disposable source edits outside immutable trials'}


if __name__ == '__main__':
    print(json.dumps(measure(), indent=2))
