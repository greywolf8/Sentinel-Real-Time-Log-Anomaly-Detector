"""Sentinel detector: reads platform.log and nothing else.

Key rule from docs/sentinel-plan.md section 3: the detector never imports simulator code
and never reads ground_truth.jsonl. The only thing it shares with the simulator is
``sim/catalog.yaml``, read as a data file by ``parse_ref.load_vocabulary``.

Modules, in build order:
    parse_ref       per-line reference parser, the correctness oracle (sections 8.3, 8.4)
    parse_batch     numpy batch parser, the fast path (section 8.4)
    tailer          chunked reader with rotation and carry handling (section 8.3)
    rings           preallocated arrays and window sums (sections 8.1, 8.5)
    baseline        EWMA and seasonal baselines (section 8.5)
    detectors       rate, threshold, CUSUM, mix drift, latency, new code, silence (8.6)
    severity        score and level with hysteresis (section 8.7)
    incidents       grouping and lifecycle (section 8.8)
    unidentified    classification counters and samples (section 8.9)
    redact          PHI scrubbing before anything is stored or shipped (section 12)
"""

from __future__ import annotations

__all__ = ["parse_ref"]
