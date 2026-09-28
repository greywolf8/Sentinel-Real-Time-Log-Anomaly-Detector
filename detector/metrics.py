"""Detector self-metrics (docs/sentinel-plan.md section 8.9).

Tracks lag, lines per second, queue depth, dropped counts, and heartbeat so the monitor's
own health is visible. This is critical for production monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import time

# Section 8.9: heartbeat interval in seconds
HEARTBEAT_INTERVAL_S = 5.0
# Section 8.9: metrics window size for averaging
METRICS_WINDOW_S = 60.0


@dataclass(slots=True)
class MetricsSnapshot:
    """A snapshot of detector metrics at a point in time."""

    timestamp_ms: int
    lag_s: float  # Event time lag behind wall clock
    lines_per_second: float
    queue_depth: int
    dropped_lines: int
    dropped_late: int
    dropped_future: int
    total_lines_processed: int
    total_alerts_generated: int
    uptime_s: float
    heartbeat_active: bool
    seconds_evaluated: int

    def to_json(self) -> dict[str, object]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "timestamp_iso": datetime.fromtimestamp(
                self.timestamp_ms / 1000, tz=timezone.utc
            ).isoformat(),
            "lag_s": round(self.lag_s, 3),
            "lines_per_second": round(self.lines_per_second, 2),
            "queue_depth": self.queue_depth,
            "dropped_lines": self.dropped_lines,
            "dropped_late": self.dropped_late,
            "dropped_future": self.dropped_future,
            "total_lines_processed": self.total_lines_processed,
            "total_alerts_generated": self.total_alerts_generated,
            "uptime_s": round(self.uptime_s, 2),
            "heartbeat_active": self.heartbeat_active,
            "seconds_evaluated": self.seconds_evaluated,
        }


@dataclass(slots=True)
class RollingCounter:
    """A rolling counter for rate calculations."""

    window_s: float = METRICS_WINDOW_S
    _samples: list[tuple[float, int]] = field(default_factory=list)

    def add(self, value: int, now: float | None = None) -> None:
        """Add a sample at the current time."""
        if now is None:
            now = time()
        self._samples.append((now, value))
        self._prune(now)

    def _prune(self, now: float) -> None:
        """Remove samples older than the window."""
        cutoff = now - self.window_s
        self._samples = [(t, v) for t, v in self._samples if t >= cutoff]

    def rate(self, now: float | None = None) -> float:
        """Calculate the rate per second over the window."""
        if now is None:
            now = time()
        self._prune(now)
        if not self._samples:
            return 0.0
        total = sum(v for _, v in self._samples)
        duration = max(self._samples[-1][0] - self._samples[0][0], 1.0)
        return total / duration

    def total(self) -> int:
        """Get the total count in the window."""
        return sum(v for _, v in self._samples)


class DetectorMetrics:
    """Self-metrics for the detector pipeline."""

    def __init__(
        self,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        metrics_window_s: float = METRICS_WINDOW_S,
    ) -> None:
        self.heartbeat_interval_s = heartbeat_interval_s
        self.metrics_window_s = metrics_window_s
        self.start_time = time()
        self.last_heartbeat = 0.0
        self.heartbeat_active = False

        # Counters
        self.total_lines_processed = 0
        self.total_alerts_generated = 0
        self.dropped_lines = 0
        self.dropped_late = 0
        self.dropped_future = 0

        # Rolling counters for rates
        self.lines_counter = RollingCounter(window_s=metrics_window_s)
        self.alerts_counter = RollingCounter(window_s=metrics_window_s)

        # Lag tracking (event time vs wall clock)
        self.current_lag_s = 0.0
        self.max_lag_s = 0.0

        # Queue depth
        self.queue_depth = 0

        # Seconds evaluated (from rings)
        self.seconds_evaluated = 0

    def record_lines(self, count: int) -> None:
        """Record lines processed."""
        self.total_lines_processed += count
        self.lines_counter.add(count)

    def record_alert(self, count: int = 1) -> None:
        """Record alerts generated."""
        self.total_alerts_generated += count
        self.alerts_counter.add(count)

    def record_dropped(self, count: int = 1, reason: str = "unknown") -> None:
        """Record dropped lines."""
        self.dropped_lines += count
        if reason == "late":
            self.dropped_late += count
        elif reason == "future":
            self.dropped_future += count

    def update_lag(self, lag_s: float) -> None:
        """Update the current lag."""
        self.current_lag_s = lag_s
        self.max_lag_s = max(self.max_lag_s, lag_s)

    def update_queue_depth(self, depth: int) -> None:
        """Update the queue depth."""
        self.queue_depth = depth

    def update_seconds_evaluated(self, count: int) -> None:
        """Update the seconds evaluated count."""
        self.seconds_evaluated = count

    def heartbeat(self) -> bool:
        """Check if heartbeat should be sent and update state."""
        now = time()
        if now - self.last_heartbeat >= self.heartbeat_interval_s:
            self.last_heartbeat = now
            self.heartbeat_active = True
            return True
        self.heartbeat_active = False
        return False

    def snapshot(self) -> MetricsSnapshot:
        """Create a snapshot of current metrics."""
        now = time()
        return MetricsSnapshot(
            timestamp_ms=int(now * 1000),
            lag_s=self.current_lag_s,
            lines_per_second=self.lines_counter.rate(now),
            queue_depth=self.queue_depth,
            dropped_lines=self.dropped_lines,
            dropped_late=self.dropped_late,
            dropped_future=self.dropped_future,
            total_lines_processed=self.total_lines_processed,
            total_alerts_generated=self.total_alerts_generated,
            uptime_s=now - self.start_time,
            heartbeat_active=self.heartbeat_active,
            seconds_evaluated=self.seconds_evaluated,
        )

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        self.start_time = time()
        self.last_heartbeat = 0.0
        self.heartbeat_active = False
        self.total_lines_processed = 0
        self.total_alerts_generated = 0
        self.dropped_lines = 0
        self.dropped_late = 0
        self.dropped_future = 0
        self.lines_counter = RollingCounter(window_s=self.metrics_window_s)
        self.alerts_counter = RollingCounter(window_s=self.metrics_window_s)
        self.current_lag_s = 0.0
        self.max_lag_s = 0.0
        self.queue_depth = 0
        self.seconds_evaluated = 0

    def to_json(self) -> dict[str, object]:
        """Full metrics as JSON."""
        snapshot = self.snapshot()
        base = snapshot.to_json()
        base.update(
            {
                "max_lag_s": round(self.max_lag_s, 3),
                "alerts_per_second": round(self.alerts_counter.rate(), 2),
                "heartbeat_interval_s": self.heartbeat_interval_s,
                "metrics_window_s": self.metrics_window_s,
            }
        )
        return base


# Global metrics instance
_global_metrics: DetectorMetrics | None = None


def get_metrics() -> DetectorMetrics:
    """Get the global metrics instance."""
    global _global_metrics
    if _global_metrics is None:
        _global_metrics = DetectorMetrics()
    return _global_metrics


def reset_metrics() -> None:
    """Reset the global metrics instance (for testing)."""
    global _global_metrics
    _global_metrics = None
