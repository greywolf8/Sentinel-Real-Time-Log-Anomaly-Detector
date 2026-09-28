"""Severity scoring (docs/sentinel-plan.md section 8.7).

Score combines five inputs from the plan:

- deviation strength: the z-score and the rate ratio against baseline
- duration: seconds in breach
- criticality weight: from the catalog
- burn rate: window rate against an SLO target, with the plan's fast and slow pairs
- payment-cycle proximity: configurable cutoff (accelerated in demo)

Levels are INFO, WARNING, HIGH, CRITICAL with hysteresis, so severity does not flap. A score
that sits on a boundary must not produce an alert that oscillates between two levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from detector.detectors import Alert, Rule, State
from detector.rings import Rings

# Section 8.7 levels.
SLO_TARGET = 0.01  # 1% of requests failing is the target
# Section 8.7 burn rate pairs. 14.4x over both 1 h and 5 min pages; 6x over 6 h and 30 min
# tickets. The demo scales the windows down proportionally, which is why these are
# configurable rather than baked in.
FAST_BURN_RATE = 14.4
FAST_BURN_WINDOWS = (300, 60)
SLOW_BURN_RATE = 6.0
SLOW_BURN_WINDOWS = (21600, 1800)
# Section 8.7: page on fast burn, ticket on slow burn.
PAGE_BURN = FAST_BURN_RATE
TICKET_BURN = SLOW_BURN_RATE
# Score weights. They sum to 1 so the score reads as a 0-1 quality, then is mapped to a level.
W_DEVIATION = 0.40
W_DURATION = 0.18
W_CRITICALITY = 0.22
W_BURN = 0.10
W_CYCLE = 0.10  # Payment-cycle proximity weight
# Duration saturates at this many seconds in breach. A ten-minute incident is not ten times
# worse than a one-minute one for the demo's purposes.
DURATION_SATURATION_S = 120.0
# Section 8.7 hysteresis: a level must be beaten by this margin to drop.
LEVEL_MARGIN = 0.08
# Number of consecutive seconds at a lower level before the level is allowed to drop.
LEVEL_HOLD_S = 15
# Section 8.7 payment-cycle proximity: cutoff in minutes. Configurable, accelerated in demo.
# This is a default; real deployments would load this from a business schedule.
DEFAULT_CYCLE_CUTOFF_MIN = 480.0  # 8 hours
# Demo acceleration: scale down the cutoff for faster testing.
DEMO_CYCLE_SCALE = 0.1  # Demo uses 10% of the real cutoff


class Severity(str, Enum):
    """Section 8.7 levels, ordered."""

    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}
# Score cut points for each level's floor.
LEVEL_CUTS: tuple[tuple[float, Severity], ...] = (
    (0.55, Severity.CRITICAL),
    (0.35, Severity.HIGH),
    (0.18, Severity.WARNING),
    (0.0, Severity.INFO),
)


@dataclass(slots=True)
class SeverityInputs:
    """The five section 8.7 inputs, so a caller can see exactly what a score was made of."""

    z: float
    rate: float
    baseline: float
    seconds_in_breach: int
    criticality: float
    burn_rate: float
    cycle_cutoff_in_min: float | None = None
    cycle_proximity: float = 0.0  # 0-1 score based on how close to payment cycle

    def to_json(self) -> dict[str, float | int | None]:
        return {
            "z": round(self.z, 3),
            "rate": round(self.rate, 5),
            "baseline": round(self.baseline, 5),
            "seconds_in_breach": self.seconds_in_breach,
            "criticality": self.criticality,
            "burn_rate": round(self.burn_rate, 2),
            "cycle_cutoff_in_min": self.cycle_cutoff_in_min,
            "cycle_proximity": round(self.cycle_proximity, 3),
        }


@dataclass(slots=True)
class ScoredAlert:
    """An alert plus its severity, which is what the section 9 schema wants."""

    alert: Alert
    severity: Severity
    score: float
    inputs: SeverityInputs
    reasons: list[str] = field(default_factory=list)
    cycle_cutoff_in_min: float | None = None
    # Section 9 additional fields
    incident_id: str = ""
    burn_rate: float = 0.0

    def to_json(self) -> dict[str, object]:
        payload = self.alert.to_json()
        payload.update(
            {
                "severity": self.severity.value,
                "score": round(self.score, 4),
                "severity_inputs": self.inputs.to_json(),
                "reasons": list(self.reasons),
                "incident_id": self.incident_id,
                "burn_rate": round(self.burn_rate, 2),
            }
        )
        if self.cycle_cutoff_in_min is not None:
            payload["cycle_cutoff_in_min"] = self.cycle_cutoff_in_min
        # Section 9: baseline band as object
        payload["baseline"] = {
            "mean": self.alert.baseline,
            "band": [0.0, self.alert.baseline * 1.5],  # Simplified band
        }
        return payload


def burn_rate(window_rate: float, slo_target: float = SLO_TARGET) -> float:
    """Section 8.7: ``burn = window_err_rate / (1 - slo_target)``.

    A rate below the target gives a negative burn, which reads as "burning budget faster than
    target" and is the sign the plan intends: the closer to the target, the worse.
    """
    denominator = max(1.0 - slo_target, 1e-6)
    return window_rate / denominator


def deviation_score(z: float, rate: float, baseline: float) -> float:
    """Statistical strength in [0, 1].

    Two parts, because either alone is misleading: a large z on a tiny rate ratio is a blip,
    and a moderate z on a hundredfold rate ratio is serious.
    """
    # z saturates: a z of 30 is not three times as bad as a z of 10, it is already certain.
    z_part = min(max(z, 0.0) / 30.0, 1.0)
    if baseline <= 0:
        ratio_part = 1.0 if rate > 0 else 0.0
    else:
        ratio = rate / baseline
        # log10 so a 10x jump is half the score, a 100x jump is all of it.
        ratio_part = min(max((ratio - 1.0) / 99.0, 0.0), 1.0)
    return 0.5 * z_part + 0.5 * ratio_part


def duration_score(seconds_in_breach: int) -> float:
    """How long the breach has lasted, saturating at DURATION_SATURATION_S."""
    if seconds_in_breach <= 0:
        return 0.0
    return min(seconds_in_breach / DURATION_SATURATION_S, 1.0)


def cycle_proximity_score(
    cycle_cutoff_in_min: float | None, demo_mode: bool = False
) -> float:
    """Payment-cycle proximity score (section 8.7).

    Closer to the payment cycle cutoff = higher severity. The score is 0-1, where
    1 means "at or past the cutoff" and 0 means "far from the cutoff".

    In demo mode, the cutoff is scaled down to make the effect visible during testing.
    """
    if cycle_cutoff_in_min is None:
        return 0.0
    cutoff = cycle_cutoff_in_min * (DEMO_CYCLE_SCALE if demo_mode else 1.0)
    if cutoff <= 0:
        return 0.0
    # Linear ramp: 0 at 2x cutoff, 1 at cutoff
    normalized = max(0.0, (2.0 * cutoff - cycle_cutoff_in_min) / cutoff)
    return min(normalized, 1.0)


def severity_score(inputs: SeverityInputs, demo_mode: bool = False) -> float:
    """The weighted 0-1 score. Weights are the section 8.7 inputs, weighted by how much
    each one should move a responder."""
    dev = deviation_score(inputs.z, inputs.rate, inputs.baseline)
    dur = duration_score(inputs.seconds_in_breach)
    burn = min(max(inputs.burn_rate, 0.0) / FAST_BURN_RATE, 1.0)
    cycle = cycle_proximity_score(inputs.cycle_cutoff_in_min, demo_mode)
    score = (
        W_DEVIATION * dev
        + W_DURATION * dur
        + W_CRITICALITY * inputs.criticality
        + W_BURN * burn
        + W_CYCLE * cycle
    )
    return min(max(score, 0.0), 1.0)


def level_for_score(score: float) -> Severity:
    for cut, level in LEVEL_CUTS:
        if score >= cut:
            return level
    return Severity.INFO


class SeverityEngine:
    """Scores alerts and holds the level steady (section 8.7 hysteresis)."""

    def __init__(
        self,
        rings: Rings,
        criticality: dict[str, float] | None = None,
        slo_target: float = SLO_TARGET,
        hold_s: int = LEVEL_HOLD_S,
        margin: float = LEVEL_MARGIN,
        cycle_cutoff_min: float = DEFAULT_CYCLE_CUTOFF_MIN,
        demo_mode: bool = False,
    ) -> None:
        self.rings = rings
        self.criticality = dict(criticality or {})
        self.slo_target = slo_target
        self.hold_s = hold_s
        self.margin = margin
        self.cycle_cutoff_min = cycle_cutoff_min
        self.demo_mode = demo_mode
        # Current level per component key, plus how long it has been held at a lower one.
        self._level: dict[str, Severity] = {}
        self._below_for: dict[str, int] = {}

    def criticality_of(self, key: str) -> float:
        """Criticality for a component key, defaulting to a middling value.

        Scope roll-ups (service, system) get the maximum of their members, so a service
        alert is not quieter than its worst component.
        """
        if key in self.criticality:
            return self.criticality[key]
        prefix = key.split(".")[0] + "."
        members = [v for k, v in self.criticality.items() if k.startswith(prefix)]
        if members:
            return max(members)
        if key == "SYSTEM":
            return max(self.criticality.values(), default=0.5)
        return 0.5

    def apply_hysteresis(self, key: str, proposed: Severity) -> Severity:
        """Hold a level up briefly before allowing a drop (section 8.7)."""
        current = self._level.get(key, proposed)
        if proposed.rank > current.rank:
            self._level[key] = proposed
            self._below_for.pop(key, None)
            return proposed
        if proposed.rank == current.rank:
            self._below_for.pop(key, None)
            return current
        # Proposed is lower. Require it to persist before dropping.
        count = self._below_for.get(key, 0) + 1
        if count >= self.hold_s:
            self._level[key] = proposed
            self._below_for.pop(key, None)
            return proposed
        self._below_for[key] = count
        return current

    def score_alert(
        self,
        alert: Alert,
        seconds_in_breach: int = 0,
        window_rate: float | None = None,
    ) -> ScoredAlert:
        """Score one alert. ``window_rate`` lets the caller pass a rate from a different
        window than the alert's, which is how the burn-rate pairs are evaluated."""
        rate = alert.observed if window_rate is None else window_rate
        burn = burn_rate(rate, self.slo_target)
        cycle_prox = cycle_proximity_score(self.cycle_cutoff_min, self.demo_mode)
        inputs = SeverityInputs(
            z=alert.z,
            rate=alert.observed,
            baseline=alert.baseline,
            seconds_in_breach=seconds_in_breach,
            criticality=self.criticality_of(alert.key),
            burn_rate=burn,
            cycle_cutoff_in_min=self.cycle_cutoff_min,
            cycle_proximity=cycle_prox,
        )
        score = severity_score(inputs, self.demo_mode)
        level = level_for_score(score)
        level = self.apply_hysteresis(alert.key, level)
        reasons = self.explain(alert, inputs, score, level)
        return ScoredAlert(
            alert=alert, severity=level, score=score, inputs=inputs, reasons=reasons
        )

    def explain(
        self, alert: Alert, inputs: SeverityInputs, score: float, level: Severity
    ) -> list[str]:
        """Human-readable reasons. Section 8.7 and the section 9 "reason" field.

        Every alert carries a reason (section 2: detection is explainable and statistical
        first), so this is not optional decoration.
        """
        reasons: list[str] = []
        if inputs.z > 0:
            reasons.append(f"z={inputs.z:.1f} over {alert.window_s}s")
        if inputs.baseline > 0:
            reasons.append(
                f"{alert.key} error rate {inputs.rate:.1%} vs baseline "
                f"{inputs.baseline:.1%} ({inputs.rate / inputs.baseline:.0f}x)"
            )
        else:
            reasons.append(f"{alert.key} error rate {inputs.rate:.1%} with no baseline yet")
        if inputs.seconds_in_breach > 0:
            reasons.append(f"breaching for {inputs.seconds_in_breach}s")
        if inputs.criticality >= 0.9:
            reasons.append("component is critical to the payment cycle")
        if inputs.burn_rate >= PAGE_BURN:
            reasons.append(f"burn rate {inputs.burn_rate:.1f}x: page")
        elif inputs.burn_rate >= TICKET_BURN:
            reasons.append(f"burn rate {inputs.burn_rate:.1f}x: ticket")
        if inputs.cycle_proximity > 0.5:
            reasons.append(f"payment cycle proximity {inputs.cycle_proximity:.0%}: high impact")
        elif inputs.cycle_proximity > 0.2:
            reasons.append(f"payment cycle proximity {inputs.cycle_proximity:.0%}: moderate impact")
        if alert.rule is Rule.CUSUM:
            reasons.append("slow drift caught by CUSUM rather than a single z-score")
        if alert.scope != "component":
            reasons.append(f"{alert.scope} level roll-up, not one component")
        reasons.append(f"score {score:.2f} -> {level.value}")
        return reasons

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._level.clear()
            self._below_for.clear()
        else:
            self._level.pop(key, None)
            self._below_for.pop(key, None)

    def to_json(self) -> dict[str, object]:
        return {key: level.value for key, level in sorted(self._level.items())}


def criticality_from_catalog(catalog: object) -> dict[str, float]:
    """Pull the criticality map out of a loaded catalog.

    Takes the catalog object rather than the file so the caller has already validated it,
    and so the detector is not reading YAML itself.
    """
    out: dict[str, float] = {}
    components = getattr(catalog, "components", None)
    if not isinstance(components, dict):
        return out
    for code, spec in components.items():
        service = getattr(spec, "service", None)
        if service is None:
            continue
        out[f"{service}.{code}"] = float(getattr(spec, "criticality", 0.5))
    return out


def state_to_seconds(state: State, second: int) -> int:
    """Seconds an incident has been open, for the duration input."""
    if state is State.OK or state.open_since <= 0:
        return 0
    return max(0, second - state.open_since)
