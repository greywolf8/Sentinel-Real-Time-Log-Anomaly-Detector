"""End-to-end pipeline: tailer or file -> batch parser -> rings -> detectors -> severity.

The wiring that section 7 describes, kept separate from the parts so each can be tested on
its own and benchmarked. :class:`Pipeline` is what the API layer and the eval runner drive.

One asyncio loop, per section 8.10: a reader task, the cold-path evaluation on the same
loop, and a bounded queue for the UI. The reader never blocks on the UI, because a slow
dashboard must not slow detection.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from detector.baseline import Baselines
from detector.detectors import DetectorEngine, State
from detector.parse_batch import BatchPipeline, BatchResult
from detector.parse_ref import Vocabulary
from detector.rings import NCAP, REORDER_SLACK_S, Rings, WINDOWS
from detector.severity import ScoredAlert, Severity, SeverityEngine, criticality_from_catalog
from detector.tailer import DEFAULT_CHUNK, Tailer
from detector.unidentified import Unidentified

# Section 8.10: bounded queue to the WebSocket broadcaster. Alerts always go to the outbox
# first; the UI is the thing that may be dropped.
UI_QUEUE_MAX = 1024


@dataclass(slots=True)
class PipelineOptions:
    """Everything tunable, in one place so the bench and the tests share a shape."""

    log_path: Path = Path("platform.log")
    catalog: object | None = None
    vocab: Vocabulary | None = None
    chunk_size: int = DEFAULT_CHUNK
    follow: bool = True
    start_at_end: bool = False
    warmup_s: float = 120.0
    # Replay speed control. 0 replays as fast as the file can be read, 1 is real time.
    speed: float = 0.0
    max_seconds: int | None = None
    evaluate_every: int = 1
    keep_rejected: bool = False


@dataclass(slots=True)
class PipelineStats:
    """The detector's own health metrics (section 8.8)."""

    chunks: int = 0
    lines: int = 0
    bytes_read: int = 0
    lines_rejected: int = 0
    seconds_evaluated: int = 0
    alerts: int = 0
    ui_dropped: int = 0
    wall_seconds: float = 0.0
    event_first_sec: int | None = None
    event_last_sec: int | None = None
    lag_seconds: float = 0.0

    def lines_per_second(self) -> float:
        if self.wall_seconds <= 0:
            return 0.0
        return self.lines / self.wall_seconds

    def to_json(self) -> dict[str, object]:
        return {
            "chunks": self.chunks,
            "lines": self.lines,
            "lines_rejected": self.lines_rejected,
            "seconds_evaluated": self.seconds_evaluated,
            "alerts": self.alerts,
            "ui_dropped": self.ui_dropped,
            "wall_seconds": round(self.wall_seconds, 3),
            "lines_per_second": round(self.lines_per_second(), 1),
            "event_first_sec": self.event_first_sec,
            "event_last_sec": self.event_last_sec,
            "lag_seconds": round(self.lag_seconds, 3),
        }


class Pipeline:
    """The whole detector, minus delivery and the UI."""

    def __init__(self, options: PipelineOptions) -> None:
        self.options = options
        vocab = options.vocab
        if vocab is None:
            from detector.parse_ref import load_vocabulary

            vocab = load_vocabulary()
        self.vocab = vocab

        self._slots: dict[str, int] = {}
        self.rings = Rings()
        self.rings.watermark.slack_s = REORDER_SLACK_S

        self.unk = Unidentified(vocab)

        # Create a temporary VocabularyIndex to determine component slot assignments
        from detector.parse_batch import VocabularyIndex

        temp_index = VocabularyIndex(vocab)

        # Register components using the parser's slot numbering
        for key in vocab.keys:
            service, _, component = key.partition("|")
            parser_slot = temp_index.key_index(service, component)
            if parser_slot is None:
                continue
            if parser_slot >= self.rings.n_cap:
                break
            slot = self.rings.register(key, service, component, parser_slot)
            self._slots[key] = slot.index

        # Now create batch pipeline with the correct component slots
        self.batch = BatchPipeline(
            vocab,
            n_cap=self.rings.n_cap,
            n_code=self.rings.n_code,
            n_bins=self.rings.n_bins,
            component_slots=dict(self._slots),
            unidentified=self.unk,
            keep_rejected=options.keep_rejected,
        )

        self.baselines = Baselines(self.rings, warmup_s=options.warmup_s)
        self.detectors = DetectorEngine(self.rings, self.baselines)
        criticality = criticality_from_catalog(options.catalog) if options.catalog else {}
        self.severity = SeverityEngine(self.rings, criticality)
        self.stats = PipelineStats()
        self.ui_queue: asyncio.Queue[ScoredAlert] = asyncio.Queue(maxsize=UI_QUEUE_MAX)
        self.alerts: list[ScoredAlert] = []
        self._closed = False

    def _register_components(self, vocab: Vocabulary) -> None:
        """Give every catalog component a preallocated slot (section 8.1, section 8.9 adopt).

        Assigning every component a slot up front is what lets the hot path write counts
        without a lookup that can fail or grow.

        This method is no longer used; registration is done inline during __init__.
        """
        pass

    def _register_codes(self) -> None:
        """Give every catalog code a ring slot, in the batch parser's slot numbering.

        The two must agree: parse_chunk writes code counts to a slot derived from its
        searchsorted lookup, and code_counts reads them back by name. If the ring numbered
        them differently, every code's count would be attributed to a different code.

        Register codes in catalog order, which is the order code_names uses.
        """
        for code in self.batch.index.code_names:
            try:
                self.rings.register_code(code)
            except Exception:
                # More codes than NCODE slots. Section 8.1 sizes the table at 256, which is
                # well above the catalog; running out means something is wrong upstream.
                break

    def ring_code_slot(self, code: str) -> int | None:
        """The ring slot for a code, for callers reading code counts by name."""
        return self.rings.code_index(code)

    # -- ingest ------------------------------------------------------------

    def feed(self, data: bytes, now_ms: int | None = None) -> BatchResult:
        """Parse a chunk and fold it into the ring. The hot path."""
        result = self.batch.feed(data, now_ms)
        if result.n_lines:
            self.stats.chunks += 1
            self.stats.lines += result.n_lines
            self.stats.bytes_read += len(data)
            self.stats.lines_rejected += result.unk_lines
            if result.first_sec is not None:
                if self.stats.event_first_sec is None:
                    self.stats.event_first_sec = result.first_sec
                self.stats.event_last_sec = result.last_sec
        self.rings.observe_seconds(result)
        lost = self.rings.add_batch(result)
        if lost:
            self.stats.lines_rejected += 0  # already counted by the batch pipeline
        return result

    def feed_bytes(self, data: bytes) -> list[ScoredAlert]:
        """Feed a chunk and evaluate any seconds the watermark has settled."""
        self.feed(data)
        return self.drain()

    def drain(self) -> list[ScoredAlert]:
        """Evaluate every settled second. The cold path.

        A second is evaluated once the watermark is REORDER_SLACK_S past it, so a line that
        arrives slightly out of order still lands in its own second before it is scored.
        """
        fired: list[ScoredAlert] = []
        settled = self.rings.watermark.settled_through()
        if settled < 0:
            return fired
        if self.rings.head < 0:
            return fired
        head_second = int(self.rings.slot_second[self.rings.head])
        if settled >= head_second:
            return fired
        seconds = self.rings.advance_to(settled)
        self.stats.seconds_evaluated += len(seconds)
        for second in seconds:
            self.detectors.step_baselines(second)
            now_ms = second * 1000
            for alert in self.detectors.evaluate(second, now_ms):
                scored = self.severity.score_alert(
                    alert, seconds_in_breach=self._breach_seconds(alert.key, second)
                )
                fired.append(scored)
                self.alerts.append(scored)
                self.stats.alerts += 1
                self._publish(scored)
        # The watermark is the detector's lag: how far behind event time we are.
        self.stats.lag_seconds = max(0.0, float(settled - head_second + 1))
        return fired

    def _breach_seconds(self, key: str, second: int) -> int:
        for state in self.detectors.states:
            if self.rings.key_name(state.index) == key:
                if state.state is State.OK or state.open_since <= 0:
                    return 0
                return max(0, second - state.open_since)
        return 0

    def _publish(self, alert: ScoredAlert) -> None:
        """Hand an alert to the UI queue, dropping the oldest if it is full.

        Section 8.10: the reader never blocks on the UI. Alerts go to the outbox first
        (that is a later phase); this queue is only the dashboard feed.
        """
        try:
            self.ui_queue.put_nowait(alert)
        except asyncio.QueueFull:
            try:
                self.ui_queue.get_nowait()
                self.ui_queue.put_nowait(alert)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass
            self.stats.ui_dropped += 1

    # -- running -----------------------------------------------------------

    def run_file(self, path: Path | None = None, max_lines: int | None = None) -> None:
        """Replay a file as fast as possible. Used by the bench and the offline replay."""
        target = Path(path or self.options.log_path)
        started = time.perf_counter()
        tailer = Tailer(path=target, chunk_size=self.options.chunk_size, start_at_end=False)
        with tailer:
            seen = 0
            for chunk in tailer.iter_chunks(follow=False):
                self.feed(chunk)
                seen += chunk.count(b"\n")
                if max_lines is not None and seen >= max_lines:
                    break
            # Fold whatever the watermark settles at the end of the file.
            self.drain()
            self.drain()
        self.stats.wall_seconds = time.perf_counter() - started

    async def follow(self, stop: asyncio.Event | None = None) -> None:
        """Follow a live log until stopped. One asyncio loop (section 8.10).

        ``await`` means this is the asyncio entry point. The event loop carries the reader,
        the cold-path evaluation and the UI queue, and nothing blocking runs on it.
        """
        tailer = Tailer(
            path=self.options.log_path,
            chunk_size=self.options.chunk_size,
            start_at_end=self.options.start_at_end,
        )
        started = time.perf_counter()
        with tailer:
            while not self._closed:
                if stop is not None and stop.is_set():
                    break
                chunk = tailer.read_chunk()
                if chunk:
                    self.feed(chunk)
                    continue
                self.drain()
                if self._reached_max_seconds():
                    break
                if stop is None:
                    # Nothing to wait for, so this was a one-shot drain of an idle file.
                    break
                # Short wait, but never zero: section 8.3 forbids busy-spinning.
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.01)
                except TimeoutError:
                    pass
            self.drain()
        self.stats.wall_seconds = time.perf_counter() - started

    def _reached_max_seconds(self) -> bool:
        if self.options.max_seconds is None or self.stats.event_last_sec is None:
            return False
        first = self.stats.event_first_sec or self.stats.event_last_sec
        return (self.stats.event_last_sec - first) >= self.options.max_seconds

    def close(self) -> None:
        self._closed = True

    # -- reporting ---------------------------------------------------------

    def health(self) -> dict[str, object]:
        """Everything the detector says about itself (section 8.8, section 8.9)."""
        return {
            "pipeline": self.stats.to_json(),
            "rings": self.rings.health(),
            "unidentified": self.unk.to_json(),
            "detector": self.detectors.state_json(),
            "severity": self.severity.to_json(),
        }

    def component_table(self) -> list[dict[str, object]]:
        """Per component rate, baseline and state, for the dashboard grid."""
        rows: list[dict[str, object]] = []
        for slot in self.rings.active_components():
            i = slot.index
            rate10 = self.rings.rate(i, 10)
            rate60 = self.rings.rate(i, 60)
            baseline = self.baselines.rate(i)
            rows.append(
                {
                    "key": slot.key,
                    "service": slot.service,
                    "component": slot.component,
                    "rate_10s": round(rate10, 5),
                    "rate_60s": round(rate60, 5),
                    "baseline": round(baseline, 5),
                    "z": round(_z_now(self, i), 2),
                    "state": self.detectors.states[i].state.value,
                    "warm": self.baselines.is_warm(i),
                    "criticality": self.severity.criticality_of(slot.key),
                }
            )
        return rows


def _z_now(pipeline: Pipeline, index: int) -> float:
    total, errors, _ = pipeline.rings.window_sums(index, 10)
    from detector.baseline import binomial_z

    if total <= 0:
        return 0.0
    return binomial_z(errors, total, pipeline.baselines.rate(index))


def build_pipeline(
    log_path: Path,
    catalog: object | None = None,
    vocab: Vocabulary | None = None,
    **overrides: object,
) -> Pipeline:
    """Convenience constructor for the tools, so they do not each build options by hand."""
    options = PipelineOptions(log_path=log_path, catalog=catalog, vocab=vocab, **overrides)  # type: ignore[arg-type]
    pipeline = Pipeline(options)
    pipeline._register_codes()
    return pipeline
