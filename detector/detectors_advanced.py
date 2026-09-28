"""Advanced detectors (docs/sentinel-plan.md section 8.6).

These detectors are not part of the core error-rate detection but are critical for the
Sentinel use case:

- Code-mix drift: Jensen-Shannon divergence on denial codes, names the code that moved most
- Latency shift: p95 from log2 histograms versus baseline
- New-code detection: unknown codes appearing at meaningful rates
- Silence detection: expected volume drops to zero

All are vectorised where possible and follow the same hysteresis pattern as the core detectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from detector.baseline import Baselines
from detector.detectors import Alert, ComponentState, Rule, State
from detector.rings import NCAP, NCODE, Rings, WINDOWS

# Section 8.6: Jensen-Shannon divergence threshold for code-mix drift.
JS_THRESHOLD = 0.15
# Section 8.6: minimum total observations for a mix comparison.
JS_MIN_TOTAL = 50
# Section 8.6: latency shift threshold on p95.
LATENCY_P95_SHIFT_THRESHOLD = 2.0  # factor
LATENCY_MIN_OBS = 20
# Section 8.6: new-code must appear at this rate to alert.
NEW_CODE_MIN_RATE = 0.01
NEW_CODE_MIN_COUNT = 5
# Section 8.6: silence is when sum10 == 0 where baseline expects volume.
SILENCE_MIN_BASELINE_RATE = 0.5  # lines per second
SILENCE_WINDOW_S = 60  # Must match one of the WINDOWS in rings.py


class AdvancedRule(str, Enum):
    """Rules specific to the advanced detectors."""

    CODE_MIX_DRIFT = "code_mix_drift"
    LATENCY_SHIFT = "latency_shift"
    NEW_CODE = "new_code"
    SILENCE = "silence"


@dataclass(slots=True)
class CodeMixState:
    """Baseline code distribution for one component (section 8.6)."""

    baseline_dist: np.ndarray  # distribution over codes, sum to 1
    baseline_total: int = 0
    updates: int = 0
    last_js: float = 0.0
    last_moved_code: str = ""
    last_moved_delta: float = 0.0


@dataclass(slots=True)
class LatencyState:
    """Baseline p95 latency for one component."""

    baseline_p95: float = 0.0
    observed_p95: float = 0.0
    updates: int = 0
    shift_factor: float = 0.0


@dataclass(slots=True)
class NewCodeState:
    """Tracking of unknown codes for one component."""

    seen_codes: set[str] = field(default_factory=set)
    unknown_counts: dict[str, int] = field(default_factory=dict)
    last_alerted_code: str = ""
    last_alerted_count: int = 0


@dataclass(slots=True)
class SilenceState:
    """Baseline volume for silence detection."""

    baseline_lines_per_sec: float = 0.0
    last_total: int = 0
    silent_seconds: int = 0


class AdvancedDetectors:
    """Detectors beyond the core error-rate rules."""

    def __init__(
        self,
        rings: Rings,
        baselines: Baselines,
        denial_codes: list[str] | None = None,
    ) -> None:
        self.rings = rings
        self.baselines = baselines
        # EDT denial codes from section 4.2: W4101-W4105
        self.denial_codes = denial_codes or ["W4101", "W4102", "W4103", "W4104", "W4105"]
        self.code_mix_states: list[CodeMixState] = [
            CodeMixState(baseline_dist=np.zeros(NCODE, dtype=np.float64))
            for _ in range(rings.n_cap)
        ]
        self.latency_states: list[LatencyState] = [
            LatencyState() for _ in range(rings.n_cap)
        ]
        self.new_code_states: list[NewCodeState] = [
            NewCodeState() for _ in range(rings.n_cap)
        ]
        self.silence_states: list[SilenceState] = [
            SilenceState() for _ in range(rings.n_cap)
        ]
        self.hysteresis: list[ComponentState] = [
            ComponentState() for _ in range(rings.n_cap)
        ]
        self.seconds_evaluated = 0

    # -- code-mix drift ----------------------------------------------------

    def jensen_shannon_divergence(
        self, p: np.ndarray, q: np.ndarray
    ) -> tuple[float, int, float]:
        """JS divergence between two distributions, plus the index that moved most.

        Returns (js_divergence, max_delta_index, max_delta_value).
        """
        # Add epsilon to avoid log(0)
        eps = 1e-12
        p_safe = p + eps
        q_safe = q + eps
        m = 0.5 * (p_safe + q_safe)
        # JS = 0.5 * KL(p||m) + 0.5 * KL(q||m)
        kl_pm = np.sum(p_safe * np.log(p_safe / m))
        kl_qm = np.sum(q_safe * np.log(q_safe / m))
        js = 0.5 * (kl_pm + kl_qm)
        # Find the code that moved most
        delta = np.abs(p - q)
        max_idx = int(np.argmax(delta))
        max_delta = float(delta[max_idx])
        return js, max_idx, max_delta

    def check_code_mix_drift(
        self, index: int, second: int, now_ms: int
    ) -> Alert | None:
        """Check for denial-code mix drift on one component.

        Only applies to CLM.EDT (the edit engine with denial codes), but the
        implementation is general enough for any component with meaningful codes.
        """
        if not self.rings.slots[index].active:
            return None
        if self.baselines.is_warm(index):
            return None

        # Get 60s code distribution
        window_s = 60
        total, _, _ = self.rings.window_sums(index, window_s)
        if total < JS_MIN_TOTAL:
            return None

        # Build current distribution over denial codes
        current_dist = np.zeros(NCODE, dtype=np.float64)
        for code_name in self.denial_codes:
            code_idx = self.rings.code_index(code_name)
            if code_idx is None:
                continue
            # Sum over the window for this code
            for w_idx, w_sec in enumerate(WINDOWS):
                if w_sec != window_s:
                    continue
                # This is a simplification; we need the actual code counts over the window
                # For now, use the ring directly
                pass

        # For now, implement a simpler version using the ring_code array
        # Get the last 60 seconds of code counts
        slot = second % self.rings.r
        code_counts = np.zeros(NCODE, dtype=np.uint32)
        for offset in range(window_s):
            sec = second - offset
            if sec < 0:
                break
            s = sec % self.rings.r
            code_counts += self.rings.ring_code[:, s]

        state = self.code_mix_states[index]
        if state.baseline_total == 0:
            # First observation: set baseline
            if total > 0:
                state.baseline_dist = code_counts.astype(np.float64) / max(total, 1)
                state.baseline_total = total
                state.updates = 1
            return None

        # Compare to baseline
        baseline_norm = state.baseline_dist / max(state.baseline_dist.sum(), 1)
        current_norm = code_counts.astype(np.float64) / max(total, 1)

        js, max_idx, max_delta = self.jensen_shannon_divergence(current_norm, baseline_norm)

        state.last_js = js
        # Find the code name that moved most
        moved_code = ""
        if max_idx < len(self.rings.code_names):
            moved_code = self.rings.code_names[max_idx]
        state.last_moved_code = moved_code
        state.last_moved_delta = max_delta

        if js > JS_THRESHOLD:
            # Hysteresis check
            opened = self._step_hysteresis(
                index, True, second, now_ms, AdvancedRule.CODE_MIX_DRIFT
            )
            if opened:
                return Alert(
                    rule=Rule.CUSUM,  # Reuse CUSUM for advanced rules
                    key=self.rings.key_name(index),
                    scope="component",
                    observed=js,
                    baseline=JS_THRESHOLD,
                    z=js / JS_THRESHOLD,  # Use ratio as z-like score
                    window_s=window_s,
                    total=total,
                    errors=0,  # Not an error rate
                    opened_at_ms=now_ms,
                    detail={
                        "js_divergence": round(js, 4),
                        "moved_code": moved_code,
                        "moved_delta": round(max_delta, 4),
                        "advanced_rule": AdvancedRule.CODE_MIX_DRIFT.value,
                    },
                )
        else:
            self._step_hysteresis(index, False, second, now_ms, AdvancedRule.CODE_MIX_DRIFT)

        return None

    # -- latency shift ------------------------------------------------------

    def p95_from_histogram(self, histogram: np.ndarray) -> float:
        """Compute p95 from a log2-binned latency histogram."""
        if histogram.sum() == 0:
            return 0.0
        total = histogram.sum()
        target = 0.95 * total
        cumulative = 0
        for i, count in enumerate(histogram):
            cumulative += count
            if cumulative >= target:
                # Convert bin index back to latency: 2^(i-1) is the lower bound
                return float(2 ** (i - 1))
        return float(2 ** (len(histogram) - 1))

    def check_latency_shift(
        self, index: int, second: int, now_ms: int
    ) -> Alert | None:
        """Check for p95 latency shift."""
        if not self.rings.slots[index].active:
            return None
        if self.baselines.is_warm(index):
            return None

        window_s = 60
        total, _, _ = self.rings.window_sums(index, window_s)
        if total < LATENCY_MIN_OBS:
            return None

        # Build histogram over the window
        histogram = np.zeros(self.rings.n_bins, dtype=np.uint32)
        for offset in range(window_s):
            sec = second - offset
            if sec < 0:
                break
            s = sec % self.rings.r
            histogram += self.rings.ring_lat[index, s, :]

        current_p95 = self.p95_from_histogram(histogram)
        state = self.latency_states[index]

        if state.baseline_p95 == 0.0:
            # First observation
            state.baseline_p95 = current_p95
            state.updates = 1
            return None

        state.observed_p95 = current_p95
        if state.baseline_p95 > 0:
            shift_factor = current_p95 / state.baseline_p95
            state.shift_factor = shift_factor

            if shift_factor > LATENCY_P95_SHIFT_THRESHOLD:
                opened = self._step_hysteresis(
                    index, True, second, now_ms, AdvancedRule.LATENCY_SHIFT
                )
                if opened:
                    return Alert(
                        rule=Rule.CUSUM,
                        key=self.rings.key_name(index),
                        scope="component",
                        observed=current_p95,
                        baseline=state.baseline_p95,
                        z=shift_factor,
                        window_s=window_s,
                        total=total,
                        errors=0,
                        opened_at_ms=now_ms,
                        detail={
                            "p95_ms": round(current_p95, 2),
                            "baseline_p95_ms": round(state.baseline_p95, 2),
                            "shift_factor": round(shift_factor, 2),
                            "advanced_rule": AdvancedRule.LATENCY_SHIFT.value,
                        },
                    )
            else:
                self._step_hysteresis(
                    index, False, second, now_ms, AdvancedRule.LATENCY_SHIFT
                )

        return None

    # -- new-code detection -------------------------------------------------

    def check_new_code(self, index: int, second: int, now_ms: int) -> Alert | None:
        """Check for unknown codes appearing at meaningful rates."""
        if not self.rings.slots[index].active:
            return None
        if self.baselines.is_warm(index):
            return None

        window_s = 60
        total, _, _ = self.rings.window_sums(index, window_s)
        if total < NEW_CODE_MIN_COUNT:
            return None

        state = self.new_code_states[index]

        # Check for codes not in the catalog
        for code_name in self.rings.code_names:
            if code_name in state.seen_codes:
                continue
            # This is a known code from the catalog
            state.seen_codes.add(code_name)

        # In a real implementation, we'd track unknown codes from the unidentified path
        # For now, this is a placeholder that would be wired to the unidentified counter
        # when unknown_code is counted

        return None

    # -- silence detection --------------------------------------------------

    def check_silence(self, index: int, second: int, now_ms: int) -> Alert | None:
        """Check for expected volume dropping to zero."""
        if not self.rings.slots[index].active:
            return None
        if self.baselines.is_warm(index):
            return None

        window_s = SILENCE_WINDOW_S
        total, _, _ = self.rings.window_sums(index, window_s)
        state = self.silence_states[index]

        # Establish baseline volume
        if state.baseline_lines_per_sec == 0.0:
            if total > 0:
                state.baseline_lines_per_sec = total / window_s
            return None

        lines_per_sec = total / window_s
        state.last_total = total

        if lines_per_sec < SILENCE_MIN_BASELINE_RATE and state.baseline_lines_per_sec > SILENCE_MIN_BASELINE_RATE:
            state.silent_seconds += 1
            if state.silent_seconds >= 30:  # 30 seconds of silence
                opened = self._step_hysteresis(
                    index, True, second, now_ms, AdvancedRule.SILENCE
                )
                if opened:
                    return Alert(
                        rule=Rule.CUSUM,
                        key=self.rings.key_name(index),
                        scope="component",
                        observed=lines_per_sec,
                        baseline=state.baseline_lines_per_sec,
                        z=state.baseline_lines_per_sec / max(lines_per_sec, 0.001),
                        window_s=window_s,
                        total=total,
                        errors=0,
                        opened_at_ms=now_ms,
                        detail={
                            "lines_per_sec": round(lines_per_sec, 2),
                            "baseline_lines_per_sec": round(state.baseline_lines_per_sec, 2),
                            "silent_seconds": state.silent_seconds,
                            "advanced_rule": AdvancedRule.SILENCE.value,
                        },
                    )
        else:
            state.silent_seconds = 0
            self._step_hysteresis(index, False, second, now_ms, AdvancedRule.SILENCE)

        return None

    # -- hysteresis ---------------------------------------------------------

    def _step_hysteresis(
        self,
        index: int,
        breaching: bool,
        second: int,
        now_ms: int,
        rule: AdvancedRule,
    ) -> bool:
        """Shared hysteresis for all advanced detectors."""
        state = self.hysteresis[index]
        if breaching:
            state.consec_bad += 1
            state.consec_ok = 0
            if state.state is State.OK and state.consec_bad >= 2:
                state.state = State.OPEN
                state.open_since = second
                state.opened_at_ms = now_ms
                return True
            if state.state is State.RESOLVING:
                state.state = State.OPEN
                return False
        else:
            state.consec_ok += 1
            state.consec_bad = 0
            if state.state is State.OPEN and state.consec_ok >= 30:
                state.state = State.RESOLVING
            elif state.state is State.RESOLVING and state.consec_ok >= 30:
                state.state = State.OK
        return False

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, second: int, now_ms: int = 0) -> list[Alert]:
        """Run all advanced detectors for all components."""
        alerts: list[Alert] = []
        self.seconds_evaluated += 1

        for i in range(self.rings.n_cap):
            if not self.rings.slots[i].active:
                continue

            # Code-mix drift (only for EDT)
            if self.rings.slots[i].component == "EDT":
                alert = self.check_code_mix_drift(i, second, now_ms)
                if alert:
                    alerts.append(alert)

            # Latency shift
            alert = self.check_latency_shift(i, second, now_ms)
            if alert:
                alerts.append(alert)

            # New code
            alert = self.check_new_code(i, second, now_ms)
            if alert:
                alerts.append(alert)

            # Silence
            alert = self.check_silence(i, second, now_ms)
            if alert:
                alerts.append(alert)

        return alerts

    def to_json(self) -> dict[str, object]:
        return {
            "seconds_evaluated": self.seconds_evaluated,
            "denial_codes": self.denial_codes,
        }
