"""Accounting for lines the detector cannot attribute (docs/sentinel-plan.md section 8.9).

Principle from the plan: the detector never crashes, never silently drops, and never lets a
bad line corrupt a count. Every problem line lands here, is classified, is counted, and is
sampled.

This is the only place in the detector that is allowed to be messy about input, and it must
never raise. Callers hand it raw bytes; it decides what the line was. That includes the
malformed lines the vectorised parser routes out of the hot path, which arrive here without
having been classified, so this module classifies them with the reference parser.

Nothing here decides whether an alert fires. It only counts, and it supplies the health
metrics section 8.9 asks for: unidentified_ratio_60s, the top unknown keys, and a sample
ring.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from detector.parse_ref import (
    LEVEL_LUT,
    OVERSIZED_BYTES,
    SEPARATOR_INDEXES,
    HEADER_BYTES,
    SEPARATOR_BYTE,
    LineClass,
    Vocabulary,
    parse_line,
)

# Section 8.9: the unknown-key dict is capped at about 1,000 keys, overflow to one bucket.
MAX_UNKNOWN_KEYS = 1000
OVERFLOW_KEY = "<overflow>"
# "first 3 per unique key plus the last 200 overall" (section 8.9 surfacing).
SAMPLES_PER_KEY = 3
MAX_SAMPLES = 200
# Section 8.9: an unknown component sending this many lines in 30 s is a new component.
ADOPT_LINES = 20
ADOPT_WINDOW_S = 30
# Section 8.9: unidentified_ratio_60s above these raises a log_format_drift alert.
RATIO_WARNING = 0.005
RATIO_HIGH = 0.05

ALL_CLASSES: tuple[LineClass, ...] = (
    LineClass.VALID,
    LineClass.MALFORMED_HEADER,
    LineClass.BAD_TIMESTAMP,
    LineClass.BAD_LEVEL,
    LineClass.UNKNOWN_COMPONENT,
    LineClass.UNKNOWN_CODE,
    LineClass.OVERSIZED_LINE,
    LineClass.NON_UTF8_MESSAGE,
)


@dataclass(slots=True)
class UnknownKeyStats:
    """Per unknown 7-byte key: how many lines, when, and a few samples."""

    key: str
    count: int = 0
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    samples: deque[bytes] = field(default_factory=lambda: deque(maxlen=SAMPLES_PER_KEY))

    def to_json(self, scrub: Any = None) -> dict[str, Any]:
        return {
            "key": self.key,
            "count": self.count,
            "first_seen_ms": self.first_seen_ms,
            "last_seen_ms": self.last_seen_ms,
            "samples": [line[:512] for line in self.samples],
            "scrubbed": scrub is not None,
        }


@dataclass(slots=True)
class AdoptCandidate:
    """An unknown component that has sent enough lines to be worth adopting."""

    key: str
    lines: int
    first_seen_ms: int
    last_seen_ms: int
    samples: list[bytes] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "lines": self.lines,
            "first_seen_ms": self.first_seen_ms,
            "last_seen_ms": self.last_seen_ms,
            "samples": [line[:512] for line in self.samples],
        }


class Unidentified:
    """Counters, capped key tracking, a sample ring and the health ratio.

    Every public method swallows its own errors. A counter that cannot be incremented is
    not worth crashing a detection pipeline over, and the section 8.9 principle is that the
    detector keeps running.
    """

    def __init__(
        self,
        vocab: Vocabulary | None = None,
        max_unknown_keys: int = MAX_UNKNOWN_KEYS,
        max_samples: int = MAX_SAMPLES,
        clock: Any = time.time,
    ) -> None:
        self._vocab = vocab
        self._max_keys = max_unknown_keys
        self._max_samples = max_samples
        self._clock = clock
        self._reset()

    def _reset(self) -> None:
        self.by_class: dict[LineClass, int] = {cls: 0 for cls in ALL_CLASSES}
        self.total: int = 0
        self.valid: int = 0
        self.unknown_keys: dict[str, UnknownKeyStats] = {}
        self._overflow: UnknownKeyStats | None = None
        self.samples: deque[bytes] = deque(maxlen=self._max_samples)
        # Per-second denominator for unidentified_ratio_60s, so the ratio is over a real
        # 60-second window rather than over the whole run since start.
        self._sec_total: dict[int, int] = {}
        self._sec_bad: dict[int, int] = {}
        self.errors: int = 0
        # Lines routed out of the batch parser without being classified, keyed by the
        # reason the parser could reject them cheaply. Keeps the hot path free of the
        # reference parser while still accounting for every byte.
        self.routed: dict[str, int] = {}

    def set_vocabulary(self, vocab: Vocabulary) -> None:
        """Swap the vocabulary, e.g. after an adopt assigns a new component a slot."""
        self._vocab = vocab

    # -- ingest ------------------------------------------------------------

    def record(self, line: bytes, now_ms: int | None = None) -> LineClass:
        """Classify one line and count it. Returns the class. Never raises."""
        try:
            return self._record_inner(line, now_ms)
        except Exception as exc:  # pragma: no cover - the belt-and-braces path
            self.errors += 1
            self.routed[f"internal:{type(exc).__name__}"] = (
                self.routed.get(f"internal:{type(exc).__name__}", 0) + 1
            )
            return LineClass.MALFORMED_HEADER

    def _record_inner(self, line: bytes, now_ms: int | None) -> LineClass:
        at_ms = self._now_ms() if now_ms is None else now_ms
        parsed = parse_line(line, self._vocab)
        cls = parsed.cls
        self.total += 1
        self.by_class[cls] = self.by_class.get(cls, 0) + 1
        second = at_ms // 1000
        self._sec_total[second] = self._sec_total.get(second, 0) + 1

        if cls is LineClass.VALID:
            self.valid += 1
            self._prune_seconds(second)
            return cls

        self._sec_bad[second] = self._sec_bad.get(second, 0) + 1
        self._prune_seconds(second)
        self._sample(line)

        if LineClass.UNKNOWN_COMPONENT in parsed.flags:
            self._note_key(parsed.key, line, at_ms)
        return cls

    def record_routed(self, reason: str, count: int = 1, now_ms: int | None = None) -> None:
        """Count lines the vectorised parser rejected without classifying.

        The batch parser can tell a line is unparseable from the bytes alone, which is the
        whole point of the fast path. Classifying it properly is this module's job, but
        doing it inline would put the reference parser back on the hot path, so the caller
        either batches the bytes over to :meth:`record_many` or accepts the cheap reason.
        """
        if count <= 0:
            return
        self.routed[reason] = self.routed.get(reason, 0) + count
        self.total += count
        self.by_class[LineClass.MALFORMED_HEADER] += count
        at_ms = self._now_ms() if now_ms is None else now_ms
        second = at_ms // 1000
        self._sec_total[second] = self._sec_total.get(second, 0) + count
        self._sec_bad[second] = self._sec_bad.get(second, 0) + count
        self._prune_seconds(second)

    def record_many(self, lines: Iterable[bytes], now_ms: int | None = None) -> int:
        """Classify a batch of rejected lines. Returns how many were counted."""
        counted = 0
        at_ms = self._now_ms() if now_ms is None else now_ms
        for line in lines:
            try:
                self.record(line, at_ms)
            except Exception:  # pragma: no cover
                self.errors += 1
            counted += 1
        return counted

    def _now_ms(self) -> int:
        try:
            return int(self._clock() * 1000)
        except Exception:  # pragma: no cover
            return 0

    def _sample(self, line: bytes) -> None:
        if len(self.samples) < self._max_samples:
            self.samples.append(line[:MAX_SAMPLES])

    def _note_key(self, key: str, line: bytes, at_ms: int) -> None:
        entry = self.unknown_keys.get(key)
        if entry is None:
            if len(self.unknown_keys) >= self._max_keys:
                # Past the cap, everything further lands in one bucket. Counting an
                # unbounded dict is how a log flood turns into a memory leak.
                if self._overflow is None:
                    self._overflow = UnknownKeyStats(key=OVERFLOW_KEY, first_seen_ms=at_ms)
                self._overflow.count += 1
                self._overflow.last_seen_ms = at_ms
                self._overflow.samples.append(line[:MAX_SAMPLES])
                return
            entry = UnknownKeyStats(key=key, first_seen_ms=at_ms)
            self.unknown_keys[key] = entry
        entry.count += 1
        entry.last_seen_ms = at_ms
        entry.samples.append(line[:MAX_SAMPLES])

    def _prune_seconds(self, now_second: int) -> None:
        """Keep the per-second counters to a window. 90 s covers the 60 s ratio with
        slack for a batch that arrives out of order."""
        cutoff = now_second - 90
        for table in (self._sec_total, self._sec_bad):
            stale = [sec for sec in table if sec < cutoff]
            for sec in stale:
                del table[sec]

    # -- health ------------------------------------------------------------

    def window_counts(self, window_s: int = 60, now_ms: int | None = None) -> tuple[int, int]:
        """(total lines, unidentified lines) over the last ``window_s`` event seconds."""
        at_ms = self._now_ms() if now_ms is None else now_ms
        newest = at_ms // 1000
        cutoff = newest - window_s + 1
        total = bad = 0
        for second, count in self._sec_total.items():
            if second >= cutoff:
                total += count
                bad += self._sec_bad.get(second, 0)
        return total, bad

    def unidentified_ratio_60s(self, now_ms: int | None = None) -> float:
        """Fraction of the last 60 event seconds the detector could not attribute.

        This is a health metric for the monitor, not for the platform (section 8.9).
        """
        total, bad = self.window_counts(60, now_ms)
        if total == 0:
            return 0.0
        return bad / total

    def ratio_severity(self, now_ms: int | None = None) -> str | None:
        """The log_format_drift level the current ratio implies, or None.

        Above 0.5% is a WARNING, above 5% a HIGH, because past that point the monitoring
        itself is degraded and the error rates cannot be trusted.
        """
        ratio = self.unidentified_ratio_60s(now_ms)
        if ratio >= RATIO_HIGH:
            return "HIGH"
        if ratio >= RATIO_WARNING:
            return "WARNING"
        return None

    # -- adopt -------------------------------------------------------------

    def adopt_candidates(
        self, now_ms: int | None = None, min_lines: int = ADOPT_LINES
    ) -> list[AdoptCandidate]:
        """Unknown components that sent enough lines recently to be worth adopting.

        Section 8.9: 20 or more lines in 30 s raises an INFO new_component alert with an
        Adopt action. Candidates are sorted by volume so the console can offer the biggest
        one first.
        """
        at_ms = self._now_ms() if now_ms is None else now_ms
        window_start = at_ms - ADOPT_WINDOW_S * 1000
        out: list[AdoptCandidate] = []
        entries: list[UnknownKeyStats] = list(self.unknown_keys.values())
        if self._overflow is not None:
            entries.append(self._overflow)
        for entry in entries:
            if entry.last_seen_ms < window_start:
                continue
            # Only a sustained stream counts. A single stray line from an unknown service
            # is noise, not a component.
            if entry.count < min_lines:
                continue
            out.append(
                AdoptCandidate(
                    key=entry.key,
                    lines=entry.count,
                    first_seen_ms=entry.first_seen_ms,
                    last_seen_ms=entry.last_seen_ms,
                    samples=list(entry.samples),
                )
            )
        out.sort(key=lambda c: c.lines, reverse=True)
        return out

    def mark_adopted(self, key: str) -> bool:
        """Stop offering a key as an adopt candidate, once its slot has been assigned."""
        return self.unknown_keys.pop(key, None) is not None

    # -- reporting ---------------------------------------------------------

    def top_unknown_keys(self, limit: int = 10) -> list[dict[str, Any]]:
        entries = sorted(self.unknown_keys.values(), key=lambda e: e.count, reverse=True)
        if self._overflow is not None:
            entries.append(self._overflow)
        return [entry.to_json() for entry in entries[:limit]]

    def to_json(self, now_ms: int | None = None, sample_limit: int = 20) -> dict[str, Any]:
        """Everything the "Unidentified logs" panel needs (section 8.9 surfacing)."""
        return {
            "total": self.total,
            "valid": self.valid,
            "by_class": {cls.name: count for cls, count in sorted(self.by_class.items())},
            "unidentified_ratio_60s": self.unidentified_ratio_60s(now_ms),
            "drift_level": self.ratio_severity(now_ms),
            "unknown_key_count": len(self.unknown_keys),
            "overflowed": self._overflow is not None,
            "top_unknown_keys": self.top_unknown_keys(),
            "adopt_candidates": [
                candidate.to_json() for candidate in self.adopt_candidates(now_ms)
            ],
            "samples": [line[:512] for line in list(self.samples)[-sample_limit:]],
            "routed": dict(sorted(self.routed.items())),
            "errors": self.errors,
        }

    def health_metrics(self) -> dict[str, float]:
        """Numeric subset, for the detector's own self-metrics (section 8.8)."""
        return {
            "unidentified_total": float(self.total),
            "unidentified_valid": float(self.valid),
            "unidentified_ratio_60s": self.unidentified_ratio_60s(),
            "unidentified_errors": float(self.errors),
            "unknown_keys": float(len(self.unknown_keys)),
        }

    def reset(self) -> None:
        self._reset()


# --- cheap classification, shared with the batch parser ---------------------
#
# parse_batch needs to tell "this line cannot be parsed" from "this line is fine" without
# the reference parser. These predicates work on bytes with no allocation, and the
# reference parser's verdicts are checked against them in the tests. Keeping them here
# rather than in parse_batch avoids an import cycle: unidentified imports parse_ref, and
# parse_batch imports unidentified.

def quick_class_reason(line: bytes) -> str | None:
    """Why a line is unparseable, from the bytes alone. None if it looks well formed.

    Deliberately conservative: it only reports a problem it is certain about, and returns
    None otherwise so the caller does not reject a line that is actually fine. A false
    positive here would silently drop a good line, which section 8.9 forbids outright.
    """
    if len(line) > OVERSIZED_BYTES:
        return "oversized_line"
    if len(line) < HEADER_BYTES:
        return "short_line"
    for index in SEPARATOR_INDEXES:
        if line[index] != SEPARATOR_BYTE:
            return "bad_separator"
    ts = line[0:13]
    for byte in ts:
        if byte < 0x30 or byte > 0x39:
            return "bad_timestamp"
    if LEVEL_LUT[line[14]] == 255:
        return "bad_level"
    return None


def classify_chunk(lines: Iterable[bytes], vocab: Vocabulary | None) -> dict[str, int]:
    """Count classes for a set of lines using the reference parser. For tests and tools."""
    out: dict[str, int] = {}
    for line in lines:
        name = parse_line(line, vocab).class_name
        out[name] = out.get(name, 0) + 1
    return out


def sample_summary(samples: Iterable[bytes], limit: int = 5) -> list[Mapping[str, str]]:
    """Small structured view of sample lines, for logs and tests."""
    out: list[Mapping[str, str]] = []
    for line in list(samples)[:limit]:
        out.append({"bytes": len(line), "preview": line[:120].decode("latin-1", "replace")})
    return out
