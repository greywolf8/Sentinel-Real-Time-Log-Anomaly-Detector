"""Detectors (docs/sentinel-plan.md section 8.6).

All vectorised over components. One call to :meth:`DetectorEngine.evaluate` handles every
component, every service and the system as a whole, because that is what makes the cold
path affordable at 2,800 lines per second.

Four rules carry the implementation:

1. Minimum sample size. A handful of errors out of a handful of lines is not an incident.
2. A Wilson lower bound for low-volume components, so the floor on a rate is honest.
3. Two thresholds, fast and slow, plus an absolute threshold from config that works with
   no baseline at all.
4. Hysteresis on both open and resolve, so an alert does not flap.

Silence is deliberately absent here: zero errors from a silent component looks perfectly
healthy to a rate detector. It is a separate detector and comes with the other phase-2 work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from detector.baseline import Baselines, binomial_z, wilson_lower_bound
from detector.rings import NCAP, NS, Rings, WINDOWS

# Section 8.6 thresholds. The z values are what the plan calls "high threshold, short
# window" and "lower threshold, longer window".
Z_FAST = 6.0
Z_SLOW = 4.0
MIN_DELTA = 0.01  # the rate must exceed the baseline by this much as well as by z
MIN_N_DEFAULT = 20
# Section 8.6 hysteresis: open after K consecutive breaching seconds, resolve after M quiet.
K_FAST = 2
K_SLOW = 5
M_QUIET = 30
# Section 8.6 low-volume components (PAY.BNK is the plan's example) use a Wilson floor.
WILSON_COMPONENTS = ("PAY.BNK",)
WILSON_MIN_N = 5
WILSON_FLOOR = 0.05
# Section 8.6 CUSUM on the 60 s rate, for slow drift.
CUSUM_K = 0.5  # slack, in units of the baseline's own sigma
CUSUM_H = 5.0  # decision threshold


class State(str, Enum):
    """The hysteresis state machine per component (section 8.1, section 8.6)."""

    OK = "ok"
    OPEN = "open"
    RESOLVING = "resolving"


class Rule(str, Enum):
    """Which rule fired. Section 9 alert types follow from this."""

    FAST = "fast"
    SLOW = "slow"
    THRESHOLD = "threshold"
    CUSUM = "cusum"
    ROLLUP = "rollup"


@dataclass(frozen=True, slots=True)
class Threshold:
    """One component's absolute threshold, from the config block in section 8.6."""

    err_rate: float
    window_s: int
    min_n: int


DEFAULT_THRESHOLD = Threshold(err_rate=0.05, window_s=10, min_n=MIN_N_DEFAULT)

# Section 8.6, verbatim. Compliance-critical components get their own tighter bound.
THRESHOLDS: dict[str, Threshold] = {
    "PAY.BNK": Threshold(err_rate=0.10, window_s=30, min_n=5),
    "ADM.AUD": Threshold(err_rate=0.01, window_s=10, min_n=MIN_N_DEFAULT),
    "PAY.EXP": Threshold(err_rate=0.05, window_s=10, min_n=MIN_N_DEFAULT),
}


@dataclass(slots=True)
class Alert:
    """One detector firing. Shaped to become a section 9 alert payload."""

    rule: Rule
    key: str
    scope: str  # "component", "service", "system"
    observed: float
    baseline: float
    z: float
    window_s: int
    total: int
    errors: int
    opened_at_ms: int
    seconds_in_breach: int = 0
    detail: dict[str, float | int | str] = field(default_factory=dict)
    # Section 9 fields
    alert_id: str = ""
    type: str = ""  # Mapped from rule
    service: str = ""
    reason: str = ""
    evidence: list[dict[str, str]] = field(default_factory=list)
    suspected_origin: str = ""
    status: str = "open"

    def to_json(self) -> dict[str, object]:
        return {
            "alert_id": self.alert_id,
            "type": self.type or self.rule.value,
            "key": self.key,
            "service": self.service,
            "scope": self.scope,
            "observed": self.observed,
            "baseline": self.baseline,
            "z": self.z,
            "window_s": self.window_s,
            "total": self.total,
            "errors": self.errors,
            "opened_at_ms": self.opened_at_ms,
            "seconds_in_breach": self.seconds_in_breach,
            "detail": dict(self.detail),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "suspected_origin": self.suspected_origin,
            "status": self.status,
        }


@dataclass(slots=True)
class ComponentState:
    """Hysteresis and timing state for one component (section 8.1)."""

    state: State = State.OK
    consec_bad: int = 0
    consec_ok: int = 0
    open_since: int = 0  # event second
    opened_at_ms: int = 0
    peak_z: float = 0.0
    peak_rate: float = 0.0
    last_rule: Rule | None = None
    cusum: float = 0.0
    breaches: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "consec_bad": self.consec_bad,
            "consec_ok": self.consec_ok,
            "open_since": self.open_since,
            "peak_z": self.peak_z,
            "peak_rate": self.peak_rate,
            "last_rule": self.last_rule.value if self.last_rule else None,
            "cusum": self.cusum,
            "breaches": self.breaches,
        }


class DetectorEngine:
    """Every rate rule, evaluated together for all components.

    The engine owns the hysteresis state. The baseline is frozen while any component is
    open, which is the section 8.5 requirement expressed where the incident state lives.
    """

    def __init__(
        self,
        rings: Rings,
        baselines: Baselines,
        thresholds: dict[str, Threshold] | None = None,
        z_fast: float = Z_FAST,
        z_slow: float = Z_SLOW,
        min_delta: float = MIN_DELTA,
        k_fast: int = K_FAST,
        k_slow: int = K_SLOW,
        m_quiet: int = M_QUIET,
        use_cusum: bool = True,
    ) -> None:
        self.rings = rings
        self.baselines = baselines
        self.thresholds = dict(thresholds if thresholds is not None else THRESHOLDS)
        self.z_fast = z_fast
        self.z_slow = z_slow
        self.min_delta = min_delta
        self.k_fast = k_fast
        self.k_slow = k_slow
        self.m_quiet = m_quiet
        self.use_cusum = use_cusum
        self.states: list[ComponentState] = [
            ComponentState() for _ in range(rings.n_cap)
        ]
        self.service_states: list[ComponentState] = [
            ComponentState() for _ in range(NS)
        ]
        self.system_state = ComponentState()
        self.seconds_evaluated = 0

    # -- helpers -----------------------------------------------------------

    def threshold_for(self, index: int) -> Threshold:
        key = self.rings.key_name(index)
        return self.thresholds.get(key, DEFAULT_THRESHOLD)

    def min_n_for(self, index: int) -> int:
        return self.threshold_for(index).min_n

    def uses_wilson(self, index: int) -> bool:
        return self.rings.key_name(index) in WILSON_COMPONENTS

    def open_mask(self) -> np.ndarray:
        """Components currently in an open incident, for freezing the baseline."""
        return np.array([s.state is not State.OK for s in self.states], dtype=bool)

    # -- the rules ---------------------------------------------------------

    def fast_mask(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(breaching, rate, z, total) for the 10 s rule."""
        return self._z_rule(10, self.z_fast, self.k_fast)

    def slow_mask(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(breaching, rate, z, total) for the 60 s rule."""
        return self._z_rule(60, self.z_slow, self.k_slow)

    def _z_rule(
        self, window_s: int, threshold: float, _k: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        sums = self.rings._window_sums_all(window_s)
        total = sums[:, 0]
        errors = sums[:, 1]
        baseline = self.baselines.rates()
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(total > 0, errors / np.maximum(total, 1), 0.0)
        z = np.zeros(self.rings.n_cap, dtype=np.float64)
        for i in np.flatnonzero(total > 0):
            z[i] = binomial_z(int(errors[i]), int(total[i]), float(baseline[i]))

        min_n = np.array([self.min_n_for(i) for i in range(self.rings.n_cap)], dtype=np.float64)
        eligible = total >= min_n
        breach = (
            eligible
            & (z > threshold)
            & (rate > baseline + self.min_delta)
            & self.baselines.active_mask()
            & ~self.baselines.warm_mask()
        )
        return breach, rate, z, total

    def wilson_mask(self) -> tuple[np.ndarray, np.ndarray]:
        """(breaching, lower bound) for the low-volume components.

        Section 8.6: a handful of errors on a small-volume component should not page anyone,
        so the rule is on the Wilson lower bound of the observed rate, not the rate itself.
        """
        breaching = np.zeros(self.rings.n_cap, dtype=bool)
        bounds = np.zeros(self.rings.n_cap, dtype=np.float64)
        for i in range(self.rings.n_cap):
            if not self.uses_wilson(i):
                continue
            total, errors, _ = self.rings.window_sums(i, 30)
            if total < WILSON_MIN_N:
                continue
            bound = wilson_lower_bound(errors, total)
            bounds[i] = bound
            breaching[i] = bound > WILSON_FLOOR
        return breaching, bounds

    def threshold_mask(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(breaching, rate, threshold) for the absolute rule, which needs no baseline.

        The config block in section 8.6 gives each component its own window, so this reads
        the per-component window rather than a fixed one.
        """
        breaching = np.zeros(self.rings.n_cap, dtype=bool)
        rates = np.zeros(self.rings.n_cap, dtype=np.float64)
        limits = np.zeros(self.rings.n_cap, dtype=np.float64)
        for i in range(self.rings.n_cap):
            spec = self.threshold_for(i)
            total, errors, _ = self.rings.window_sums(i, spec.window_s)
            rate = errors / max(total, 1)
            rates[i] = rate
            limits[i] = spec.err_rate
            breaching[i] = (
                total >= spec.min_n
                and rate > spec.err_rate
                and self.rings.slots[i].active
                and not self.baselines.is_warm(i)
            )
        return breaching, rates, limits

    def cusum_mask(self) -> tuple[np.ndarray, np.ndarray]:
        """(breaching, cusum value) on the 60 s rate, for slow drift.

        A CUSUM accumulates small persistent deviations, so a rate that creeps from 0.2% to
        2% over ten minutes eventually crosses H even though no single z-score is large.
        That is exactly the slow_degradation scenario.
        """
        breach = np.zeros(self.rings.n_cap, dtype=bool)
        values = np.zeros(self.rings.n_cap, dtype=np.float64)
        if not self.use_cusum:
            return breach, values
        sums = self.rings._window_sums_all(60)
        total = sums[:, 0]
        errors = sums[:, 1]
        baseline = self.baselines.rates()
        sigma = np.sqrt(np.maximum(self.baselines.variance, 1e-12))
        for i in range(self.rings.n_cap):
            if not self.rings.slots[i].active or total[i] < 20:
                self.states[i].cusum = 0.0
                continue
            observed = errors[i] / max(total[i], 1)
            # Scale the slack to the baseline's own noise, so the CUSUM is not tuned to one
            # component's volume and meaningless on another's.
            slack = CUSUM_K * sigma[i] if sigma[i] > 0 else CUSUM_K * max(baseline[i], 1e-3)
            increment = observed - baseline[i] - slack
            self.states[i].cusum = max(self.states[i].cusum + increment, 0.0)
            values[i] = self.states[i].cusum
            breach[i] = self.states[i].cusum > CUSUM_H
        return breach, values

    # -- hysteresis --------------------------------------------------------

    def _step(
        self,
        state: ComponentState,
        breaching: bool,
        required: int,
        second: int,
        now_ms: int,
    ) -> bool:
        """Advance one component's hysteresis machine. Returns True if it opened now.

        Open after K consecutive breaching seconds; resolve after M quiet ones. Both
        directions need persistence, otherwise a rate sitting exactly on the threshold
        produces an alert every second.
        """
        if breaching:
            state.consec_bad += 1
            state.consec_ok = 0
            state.breaches += 1
            if state.state is State.OK and state.consec_bad >= required:
                state.state = State.OPEN
                state.open_since = second
                state.opened_at_ms = now_ms
                state.peak_z = 0.0
                state.peak_rate = 0.0
                return True
            if state.state is State.RESOLVING:
                # Breach during the resolve countdown: back to open, keeping the original
                # open_since so the duration an alert reports is not reset by a wobble.
                state.state = State.OPEN
                return False
        else:
            state.consec_ok += 1
            state.consec_bad = 0
            if state.state is State.OPEN and state.consec_ok >= self.m_quiet:
                state.state = State.RESOLVING
            elif state.state is State.RESOLVING and state.consec_ok >= self.m_quiet:
                state.state = State.OK
        return False

    def _reset_after_resolve(self, state: ComponentState) -> None:
        if state.state is State.OK and state.consec_bad == 0:
            state.open_since = 0
            state.opened_at_ms = 0
            state.peak_z = 0.0
            state.peak_rate = 0.0
            state.cusum = 0.0
            state.last_rule = None

    # -- evaluation --------------------------------------------------------

    def evaluate(self, second: int, now_ms: int = 0) -> list[Alert]:
        """Run every rule for every component, plus the service and system roll-ups."""
        alerts: list[Alert] = []
        self.seconds_evaluated += 1

        fast, fast_rate, fast_z, fast_total = self.fast_mask()
        slow, slow_rate, slow_z, slow_total = self.slow_mask()
        thresh, thresh_rate, _ = self.threshold_mask()
        wilson, wilson_bounds = self.wilson_mask()
        cusum, cusum_values = self.cusum_mask()

        for i in range(self.rings.n_cap):
            state = self.states[i]
            if not self.rings.slots[i].active:
                continue
            # A component still in warm-up must not alert, and must not be frozen either:
            # its baseline is still learning, which is the definition of warm-up.
            if self.baselines.is_warm(i):
                state.consec_bad = 0
                state.consec_ok = 0
                continue

            rule: Rule | None = None
            if fast[i]:
                rule = Rule.FAST
            elif slow[i]:
                rule = Rule.SLOW
            elif wilson[i]:
                rule = Rule.THRESHOLD
            elif thresh[i]:
                rule = Rule.THRESHOLD
            elif cusum[i]:
                rule = Rule.CUSUM

            required = self.k_fast if rule in (Rule.FAST, Rule.THRESHOLD) else self.k_slow
            opened = self._step(state, rule is not None, required, second, now_ms)

            if rule is not None:
                rate = float(fast_rate[i] if rule is Rule.FAST else slow_rate[i])
                if rule is Rule.THRESHOLD and thresh[i]:
                    rate = float(thresh_rate[i])
                z = float(fast_z[i] if rule is Rule.FAST else slow_z[i])
                if rule is Rule.CUSUM:
                    rate = float(slow_rate[i])
                    z = 0.0
                state.peak_z = max(state.peak_z, z)
                state.peak_rate = max(state.peak_rate, rate)
                state.last_rule = rule
                if opened:
                    window = 10 if rule is Rule.FAST else 60
                    total = int(fast_total[i] if rule is Rule.FAST else slow_total[i])
                    _, errors, _ = self.rings.window_sums(i, window)
                    alerts.append(
                        Alert(
                            rule=rule,
                            key=self.rings.key_name(i),
                            scope="component",
                            observed=rate,
                            baseline=self.baselines.rate(i),
                            z=z,
                            window_s=window,
                            total=total,
                            errors=errors,
                            opened_at_ms=now_ms,
                            detail={
                                "cusum": round(float(cusum_values[i]), 4),
                                "wilson_lower": round(float(wilson_bounds[i]), 4),
                                "min_delta": self.min_delta,
                            },
                        )
                    )
            else:
                self._reset_after_resolve(state)

        alerts.extend(self._evaluate_rollups(second, now_ms))
        return alerts

    def _evaluate_rollups(self, second: int, now_ms: int) -> list[Alert]:
        """Service and system level checks.

        A service can be breaching while no single component is, and the plan wants the
        roll-up visible: twenty components at 4% each is a 4% service, which matters.
        """
        alerts: list[Alert] = []
        for window in (60,):
            totals, errors, _ = self.rings.service_rollup(window)
            for row in range(NS):
                total = int(totals[row])
                if total < MIN_N_DEFAULT * 4:
                    continue
                rate = errors[row] / max(total, 1)
                if rate <= DEFAULT_THRESHOLD.err_rate:
                    self._step(self.service_states[row], False, self.k_slow, second, now_ms)
                    continue
                opened = self._step(
                    self.service_states[row], True, self.k_slow, second, now_ms
                )
                if opened:
                    alerts.append(
                        Alert(
                            rule=Rule.ROLLUP,
                            key=self.rings.service_name_for_row(row) or f"svc{row}",  # noqa: E501
                            scope="service",
                            observed=float(rate),
                            baseline=DEFAULT_THRESHOLD.err_rate,
                            z=0.0,
                            window_s=window,
                            total=total,
                            errors=int(errors[row]),
                            opened_at_ms=now_ms,
                        )
                    )
        total, errors, _ = self.rings.system_rollup(60)
        if total >= MIN_N_DEFAULT * 20:
            rate = errors / max(total, 1)
            opened = self._step(
                self.system_state, rate > DEFAULT_THRESHOLD.err_rate, self.k_slow, second, now_ms
            )
            if opened:
                alerts.append(
                    Alert(
                        rule=Rule.ROLLUP,
                        key="SYSTEM",
                        scope="system",
                        observed=float(rate),
                        baseline=DEFAULT_THRESHOLD.err_rate,
                        z=0.0,
                        window_s=60,
                        total=int(total),
                        errors=int(errors),
                        opened_at_ms=now_ms,
                    )
                )
        return alerts

    def step_baselines(self, second: int) -> None:
        """Update the baselines, frozen for anything currently open."""
        self.baselines.update(second, freeze_mask=self.open_mask())

    # -- reporting ---------------------------------------------------------

    def state_json(self) -> dict[str, object]:
        return {
            "seconds_evaluated": self.seconds_evaluated,
            "components": {
                self.rings.key_name(i): self.states[i].to_json()
                for i in range(self.rings.n_cap)
                if self.rings.slots[i].active
            },
            "open_count": int(self.open_mask().sum()),
        }

    def open_components(self) -> list[str]:
        return [
            self.rings.key_name(s.index)
            for s in self.states
            if s.state is not State.OK and self.rings.slots[s.index].active
        ]
