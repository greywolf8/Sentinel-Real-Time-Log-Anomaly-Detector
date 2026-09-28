"""Fault layer for the simulator (docs/sentinel-plan.md sections 6.1 and 6.2).

A fault is a time-boxed override on one component: error rate, error code mix, volume, or
silence. Overrides support a ramp and a hold, after which the component returns to its
configured baseline.

Two invariants this module exists to protect:

1. Fault state is NEVER written to ``platform.log``. The generator reads overrides from
   here and draws a different error distribution; the log carries no marker, hint or
   annotation about rates. The detector has to infer the fault from the log alone.
2. Every control action is appended to ``ground_truth.jsonl`` with its timestamp, target
   and new setting. That file is the evaluation oracle and is never read by the detector.

The store is asyncio-safe: it is mutated by the control API and read by the per-component
generator tasks, all on the same event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from sim.config import Catalog, Platform

GroundTruthPath = Path

FaultKind = Literal["rate", "mix", "silence", "volume", "deploy"]

TimeSource = Callable[[], int]


def real_now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class FaultWindow:
    """A scheduled override on one component, valid over a virtual-time interval.

    ``start_ms`` is when the override begins to take effect (virtual epoch ms). ``ramp_s``
    interpolates from the value in force at ``start_ms`` to ``target`` over that many
    seconds; ``hold_s`` counts from the end of the ramp. After ``end_ms`` the component
    reverts to its configured baseline with no further action, so the log shows recovery
    without any fault marker.
    """

    kind: FaultKind
    key: str
    target: dict[str, Any]
    start_ms: int
    ramp_s: float = 0.0
    hold_s: float | None = None
    code: str = "manual"
    base_value: float | None = None
    end_ms: int | None = None
    applied_at_ms: int | None = None

    def progress(self, at_ms: int) -> float:
        """Interpolation factor in [0, 1] for the ramp, 0 before the start, 1 after."""
        if at_ms <= self.start_ms:
            return 0.0
        if self.ramp_s <= 0.0:
            return 1.0
        return min(1.0, (at_ms - self.start_ms) / (self.ramp_s * 1000.0))

    def active(self, at_ms: int) -> bool:
        if at_ms < self.start_ms:
            return False
        return self.end_ms is None or at_ms < self.end_ms

    def value_at(self, at_ms: int) -> float:
        """Target value blended from ``base_value`` to ``target`` along the ramp."""
        target = float(self.target.get("rate", 0.0))
        base = self.base_value if self.base_value is not None else 0.0
        return base + (target - base) * self.progress(at_ms)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "target": self.target,
            "start_ms": self.start_ms,
            "ramp_s": self.ramp_s,
            "hold_s": self.hold_s,
            "code": self.code,
        }


@dataclass(slots=True)
class ComponentFaults:
    """All live windows for one component, newest last."""

    key: str
    windows: list[FaultWindow] = field(default_factory=list)


class FaultStore:
    """Live fault state plus the append-only ground-truth log.

    The store is the only mutable fault state in the simulator. The generator asks it for
    the current effective rate, mix, volume scale and silence flag per component tick; the
    control API mutates it. Nothing here writes to ``platform.log``.
    """

    def __init__(
        self,
        catalog: Catalog,
        platform: Platform,
        truth_path: Path | None = None,
        token: str | None = None,
        time_source: TimeSource = real_now_ms,
    ) -> None:
        self._catalog = catalog
        self._platform = platform
        self._faults: dict[str, ComponentFaults] = {}
        self._lock = asyncio.Lock()
        self._truth_path = truth_path
        self._token = token
        self._seq = 0
        # The generator injects the virtual clock here, so fault windows are scheduled in
        # the same event-time base the log uses. With --speed N a 60 s hold is 60 s of
        # log time, not 60/N s of wall time.
        self._now_ms = time_source

    @property
    def truth_path(self) -> Path | None:
        return self._truth_path

    def now_ms(self) -> int:
        """The store's time base: the generator's virtual clock when injected, wall time
        otherwise. The console reads this so it agrees with the log's event time."""
        return self._now_ms()

    def base_rate(self, key: str) -> float:
        """Configured baseline error rate for a ``SVC.CMP`` key."""
        service, _, component = key.partition(".")
        if component not in self._catalog.components:
            raise KeyError(key)
        if self._catalog.components[component].service != service:
            raise KeyError(key)
        cfg = self._platform.components[component]
        if cfg.err >= 1.0:
            return 0.0
        return cfg.err

    def known_key(self, key: str) -> bool:
        try:
            self.base_rate(key)
        except KeyError:
            return False
        return True

    def authorize(self, supplied: str | None) -> bool:
        """Token check. A store with no token is localhost-only and always open."""
        if self._token is None:
            return True
        return supplied == self._token

    def effective_rate(self, key: str, at_ms: int) -> float | None:
        """The overridden error rate for a key, or None when no rate window is live.

        None means "no override", which is not the same as "the baseline": the caller
        composes this with daily and propagated rates, and a silent fallback to the
        baseline here would wipe out any propagation the caller had just computed.
        """
        windows = self._live(key, at_ms, "rate")
        if not windows:
            return None
        return windows[-1].value_at(at_ms)

    def effective_volume(self, key: str, at_ms: int) -> float:
        """Volume multiplier from baseline (1.0) plus any volume windows."""
        volume = 1.0
        for window in self._live(key, at_ms, "volume"):
            volume = window.value_at(at_ms)
        return volume

    def is_silent(self, key: str, at_ms: int) -> bool:
        return bool(self._live(key, at_ms, "silence"))

    def code_weights(self, key: str, at_ms: int) -> dict[str, float] | None:
        """Last active mix window's code weights, or None to use the catalog weights."""
        result: dict[str, float] | None = None
        for window in self._live(key, at_ms, "mix"):
            result = {str(k): float(v) for k, v in window.target.get("code_weights", {}).items()}
        return result

    def _live(self, key: str, at_ms: int, kind: FaultKind) -> list[FaultWindow]:
        entry = self._faults.get(key)
        if entry is None:
            return []
        return [w for w in entry.windows if w.kind == kind and w.active(at_ms)]

    def active_summary(self, at_ms: int) -> list[dict[str, Any]]:
        """What the Fault Console grid shows as recently applied settings."""
        out: list[dict[str, Any]] = []
        for key, entry in sorted(self._faults.items()):
            for window in entry.windows:
                if window.active(at_ms):
                    out.append({**window.to_json(), "applied_at_ms": window.applied_at_ms})
        return out

    def expire(self, at_ms: int) -> None:
        """Drop windows that have run out. Called by the generator each tick."""
        for entry in self._faults.values():
            entry.windows = [w for w in entry.windows if w.active(at_ms)]

    async def set_rate(
        self,
        key: str,
        rate: float,
        ramp_s: float = 0.0,
        hold_s: float | None = None,
        at_ms: int | None = None,
        code: str = "manual",
    ) -> FaultWindow:
        """Override the error rate of one component, optionally ramping in and holding."""
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"rate out of range: {rate}")
        if ramp_s < 0.0:
            raise ValueError(f"ramp_s must be >= 0, got {ramp_s}")
        start = self._now_ms() if at_ms is None else at_ms
        end: int | None = None
        if hold_s is not None:
            end = start + int((ramp_s + hold_s) * 1000)
        async with self._lock:
            # The ramp starts from whatever is in force now, which is the configured
            # baseline unless an earlier window is still live. A ramp that started from
            # zero would under-report the rate for its whole duration.
            base = self.effective_rate(key, start)
            window = FaultWindow(
                kind="rate",
                key=key,
                target={"rate": rate},
                start_ms=start,
                ramp_s=ramp_s,
                hold_s=hold_s,
                code=code,
                base_value=self.base_rate(key) if base is None else base,
                end_ms=end,
                applied_at_ms=start,
            )
            self._append(key, window)
        self.record_truth("rate_change", key, window.to_json())
        return window

    async def set_code_weights(
        self,
        key: str,
        code_weights: dict[str, float],
        hold_s: float | None = None,
        at_ms: int | None = None,
        code: str = "manual",
    ) -> FaultWindow:
        """Shift the code mix of a component without moving the overall error rate."""
        if not code_weights:
            raise ValueError("code_weights must not be empty")
        if any(w < 0.0 for w in code_weights.values()):
            raise ValueError("code weights must be non-negative")
        start = self._now_ms() if at_ms is None else at_ms
        end: int | None = None
        if hold_s is not None:
            end = start + int(hold_s * 1000)
        async with self._lock:
            window = FaultWindow(
                kind="mix",
                key=key,
                target={"code_weights": dict(code_weights)},
                start_ms=start,
                hold_s=hold_s,
                code=code,
                end_ms=end,
                applied_at_ms=start,
            )
            self._append(key, window)
        self.record_truth("mix_change", key, window.to_json())
        return window

    async def silence(
        self,
        key: str,
        hold_s: float = 60.0,
        at_ms: int | None = None,
        code: str = "manual",
    ) -> FaultWindow:
        """Stop a component logging entirely. Only detectable as silence, not as a rate."""
        if hold_s <= 0.0:
            raise ValueError(f"hold_s must be positive, got {hold_s}")
        start = self._now_ms() if at_ms is None else at_ms
        async with self._lock:
            window = FaultWindow(
                kind="silence",
                key=key,
                target={"silenced": True},
                start_ms=start,
                hold_s=hold_s,
                code=code,
                end_ms=start + int(hold_s * 1000),
                applied_at_ms=start,
            )
            self._append(key, window)
        self.record_truth("silence", key, window.to_json())
        return window

    async def set_volume(
        self,
        key: str,
        multiplier: float,
        hold_s: float | None = None,
        at_ms: int | None = None,
        code: str = "manual",
    ) -> FaultWindow:
        """Scale a component's traffic (nightly_batch_surge), leaving its rate alone."""
        if multiplier <= 0.0:
            raise ValueError(f"volume multiplier must be positive, got {multiplier}")
        start = self._now_ms() if at_ms is None else at_ms
        end: int | None = None
        if hold_s is not None:
            end = start + int(hold_s * 1000)
        async with self._lock:
            window = FaultWindow(
                kind="volume",
                key=key,
                target={"multiplier": multiplier},
                start_ms=start,
                hold_s=hold_s,
                code=code,
                end_ms=end,
                applied_at_ms=start,
            )
            self._append(key, window)
        self.record_truth("volume_change", key, window.to_json())
        return window

    async def reset(self) -> None:
        """Clear every fault. The log shows the return to baseline, nothing else."""
        async with self._lock:
            self._faults.clear()
        self.record_truth("reset", "*", {})

    def _append(self, key: str, window: FaultWindow) -> None:
        entry = self._faults.setdefault(key, ComponentFaults(key=key))
        # A newer window of the same kind supersedes the older one, which is what a slider
        # drag means. Different kinds coexist, so silence + rate can be combined.
        entry.windows = [w for w in entry.windows if w.kind != window.kind or w.active(window.start_ms)]
        entry.windows.append(window)

    def record_truth(self, event: str, target: str, detail: dict[str, Any]) -> dict[str, Any]:
        """Append one ground-truth record. Never read by the detector."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "event": event,
            "target": target,
            "at_ms": self._now_ms(),
            "detail": detail,
        }
        if self._truth_path is None:
            return record
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(self._truth_path, flags, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return record

    def truth_records(self) -> list[dict[str, Any]]:
        """Read the ground-truth file back. For the eval runner and the console only."""
        if self._truth_path is None or not self._truth_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for raw in self._truth_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                out.append(json.loads(raw))
        return out
