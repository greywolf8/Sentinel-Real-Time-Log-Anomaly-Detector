#!/usr/bin/env python3
"""Replay benchmark for both parsers (docs/sentinel-plan.md section 8.4 and section 11.1).

Replays a dump file as fast as possible and prints lines per second for both the reference
parser and the batch parser. This is how we measure the speed advantage of the vectorised
batch parser over the per-line reference parser.

The script makes no claims about what a production deployment would achieve: it measures
only raw parsing throughput on a single file, with no network, no detection, and no UI.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Add the repo root to the path so imports work.
sys.path.insert(0, str(Path(__file__).parent.parent))

from detector.parse_batch import BatchPipeline, parse_chunk, VocabularyIndex
from detector.parse_ref import Vocabulary, load_vocabulary, parse_line


def benchmark_reference(lines: list[bytes], vocab: Vocabulary) -> dict[str, float]:
    """Parse every line with the reference parser and return timing."""
    start = time.perf_counter()
    for line in lines:
        parse_line(line, vocab)
    elapsed = time.perf_counter() - start
    return {"lines": len(lines), "seconds": elapsed, "lines_per_second": len(lines) / elapsed if elapsed > 0 else 0.0}


def benchmark_batch(data: bytes, vocab: Vocabulary, n_cap: int, n_code: int, n_bins: int) -> dict[str, float]:
    """Parse the whole chunk with the batch parser and return timing."""
    index = VocabularyIndex(vocab)
    start = time.perf_counter()
    result = parse_chunk(data, index, n_cap, n_code, n_bins)
    elapsed = time.perf_counter() - start
    return {
        "lines": result.n_lines,
        "seconds": elapsed,
        "lines_per_second": result.n_lines / elapsed if elapsed > 0 else 0.0,
        "unidentified": result.unk_lines,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay benchmark: compare reference parser vs batch parser throughput."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to a log file or dump file to replay.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("sim/catalog.yaml"),
        help="Path to catalog.yaml (default: sim/catalog.yaml).",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=None,
        help="Stop after this many lines (useful for quick checks).",
    )
    parser.add_argument(
        "--reference-only",
        action="store_true",
        help="Only benchmark the reference parser.",
    )
    parser.add_argument(
        "--batch-only",
        action="store_true",
        help="Only benchmark the batch parser.",
    )
    args = parser.parse_args()

    if not args.path.exists():
        print(f"Error: {args.path} does not exist", file=sys.stderr)
        return 1

    if not args.catalog.exists():
        print(f"Error: catalog {args.catalog} does not exist", file=sys.stderr)
        return 1

    print(f"Loading catalog from {args.catalog}...")
    vocab = load_vocabulary(args.catalog)
    print(f"Loaded {len(vocab.keys)} components, {len(vocab.codes)} codes")

    print(f"Reading {args.path}...")
    data = args.path.read_bytes()
    lines = data.split(b"\n")
    # Remove the trailing empty line if present
    if lines and not lines[-1]:
        lines = lines[:-1]

    if args.max_lines:
        lines = lines[: args.max_lines]
        data = b"\n".join(lines) + b"\n"

    print(f"Read {len(lines)} lines ({len(data)} bytes)")

    from detector.rings import NCAP, NCODE, NB

    results = {}

    if not args.batch_only:
        print("\nBenchmarking reference parser...")
        ref_result = benchmark_reference(lines, vocab)
        results["reference"] = ref_result
        print(f"  Reference: {ref_result['lines_per_second']:.0f} lines/sec ({ref_result['lines']} lines in {ref_result['seconds']:.3f}s)")

    if not args.reference_only:
        print("\nBenchmarking batch parser...")
        batch_result = benchmark_batch(data, vocab, NCAP, NCODE, NB)
        results["batch"] = batch_result
        print(f"  Batch: {batch_result['lines_per_second']:.0f} lines/sec ({batch_result['lines']} lines in {batch_result['seconds']:.3f}s)")
        if batch_result["unidentified"]:
            print(f"  Unidentified: {batch_result['unidentified']} ({batch_result['unidentified'] / batch_result['lines'] * 100:.1f}%)")

    if "reference" in results and "batch" in results:
        speedup = results["batch"]["lines_per_second"] / results["reference"]["lines_per_second"]
        print(f"\nSpeedup: {speedup:.1f}x")

    return 0


if __name__ == "__main__":
    sys.exit(main())
