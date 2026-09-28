"""Per-component baseline (docs/sentinel-plan.md section 8.5).

The baseline is an EWMA of the error rate, updated only while the component is in the OK
state. Freezing it during an incident is the whole point: if the baseline learned from a
spike, the spike would become normal within a few windows and the alert would resolve
itself, which is the classic way a rate detector stops working after its first incident.

A floor prevents a zero or near-zero baseline from producing an infinite z-score, so a
component that has seen no errors at all does not alert on its first one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from detector.rings import NCAP, P_FLOOR, Rings

# Section 8.5: the EWMA is over the 300 s window, so alpha is the weight of one second in a
# 300 second window. 2/(300+1) is the standard smoothing choice for that span.
ALPHA_SLOW = 2.0 / 301.0
# The warm-up period. Section 8.5 suggests 2 minutes in the demo, longer in production.
WARMUP_S = 120.0
# Section 8.5 optional seasonal layer: per hour-of-week median and MAD. Not wired in yet;
# the constants are here so the interface is stable when it is.
HOURS_PER_WEEK = 168


@dataclass(slots=True)
class ComponentBaseline:
    """Baseline state for one component."""

    index: int
    ewma: float = 0.0
    variance: float = 0.0
    # Seconds of observed data, used for warm-up and for the variance estimate.
    observed_s: int = 0
    warmup_s: float = WARMUP_S
    frozen: bool = False
    updates: int = 0
    floor: float = P_FLOOR

    @property
    def warm(self) -> bool:
        return self.observed_s < self.warmup_s

    def rate(self) -> float:
        """The baseline rate, floored. Never zero, never negative."""
        return max(self.ewma, self.floor)

    def to_json(self) -> dict[str, float | int | bool]:
        return {
            "ewma": self.ewma,
            "rate": self.rate(),
            "variance": self.variance,
            "observed_s": self.observed_s,
            "warm": self.warm,
            "frozen": self.frozen,
            "updates": self.updates,
        }


@dataclass(slots=True)
class Baselines:
    """EWMA baselines for every component, updated once per completed second.

    Section 8.2: the cold path is vectorised over all components at once, so this is one
    pass over an (NCAP,) array per second rather than a loop.
    """

    rings: Rings
    alpha: float = ALPHA_SLOW
    warmup_s: float = WARMUP_S
    floor: float = P_FLOOR
    ewma: np.ndarray = field(init=False)
    variance: np.ndarray = field(init=False)
    observed_s: np.ndarray = field(init=False)
    frozen: np.ndarray = field(init=False)
    updates: np.ndarray = field(init=False)
    window_s: int = 300

    def __post_init__(self) -> None:
        n = self.rings.n_cap
        self.ewma = np.zeros(n, dtype=np.float64)
        self.variance = np.full(n, P_FLOOR**2, dtype=np.float64)
        self.observed_s = np.zeros(n, dtype=np.int64)
        self.frozen = np.zeros(n, dtype=bool)
        self.updates = np.zeros(n, dtype=np.int64)

    # -- access ------------------------------------------------------------

    def rate(self, index: int) -> float:
        return max(float(self.ewma[index]), self.floor)

    def rates(self) -> np.ndarray:
        return np.maximum(self.ewma, self.floor)

    def is_warm(self, index: int) -> bool:
        return int(self.observed_s[index]) < self.warmup_s

    def warm_mask(self) -> np.ndarray:
        return self.observed_s < self.warmup_s

    def active_mask(self) -> np.ndarray:
        """Slots that are in use. Unused slots must never be evaluated or alerted on."""
        return np.array([slot.active for slot in self.rings.slots], dtype=bool)

    def of(self, index: int) -> ComponentBaseline:
        return ComponentBaseline(
            index=index,
            ewma=float(self.ewma[index]),
            variance=float(self.variance[index]),
            observed_s=int(self.observed_s[index]),
            warmup_s=self.warmup_s,
            frozen=bool(self.frozen[index]),
            updates=int(self.updates[index]),
            floor=self.floor,
        )

    # -- update ------------------------------------------------------------

    def update(
        self,
        second: int,
        freeze_mask: np.ndarray | None = None,
        window_s: int | None = None,
    ) -> None:
        """Fold one completed second into the baselines.

        ``freeze_mask`` marks components that are in an incident: their baseline is not
        touched, so the anomaly does not become the new normal. This is the section 8.5
        requirement stated as an argument rather than as hidden state, because the detector
        engine owns the incident state and the baseline must not also own it.
        """
        size = window_s or self.window_s
        sums = self.rings._window_sums_all(size)
        totals = np.maximum(sums[:, 0], 1).astype(np.float64)
        rates = sums[:, 1] / totals
        active = self.active_mask()

        if freeze_mask is not None:
            frozen = np.asarray(freeze_mask, dtype=bool)
        else:
            frozen = np.zeros(self.rings.n_cap, dtype=bool)
        # Warm-up and frozen components are excluded, plus any slot that saw no traffic: an
        # empty second must not pull the baseline to zero.
        saw_traffic = sums[:, 0] > 0
        eligible = active & saw_traffic & ~frozen & ~self.warm_mask()

        if not eligible.any():
            # Still count the seconds, so warm-up eventually ends for a component that is
            # quiet. A component with genuinely no traffic never leaves warm-up, which is
            # correct: it has no baseline yet.
            self.observed_s[active & saw_traffic] += 1
            return

        idx = np.flatnonzero(eligible)
        current = rates[idx]
        previous = self.ewma[idx]
        updated = previous + self.alpha * (current - previous)
        self.ewma[idx] = updated
        # Variance of the observed rate around the baseline, EWMA smoothed the same way.
        # It is a rough scale, not a rigorous estimator, and is only used to report a band.
        self.variance[idx] += self.alpha * ((current - previous) ** 2 - self.variance[idx])
        self.updates[idx] += 1
        self.observed_s[idx] += 1
        self.observed_s[active & saw_traffic] += 1

    def set_frozen(self, mask: np.ndarray) -> None:
        self.frozen = np.asarray(mask, dtype=bool).copy()

    def seed(self, index: int, rate: float) -> None:
        """Start a component's baseline at a known rate.

        Section 17: give the demo a warm-up period or a pre-seeded baseline. Seeding skips
        the cold start where every component looks anomalous.
        """
        self.ewma[index] = rate
        self.observed_s[index] = int(self.warmup_s)
        self.updates[index] = 1

    def reset(self) -> None:
        self.ewma[:] = 0.0
        self.variance[:] = self.floor**2
        self.observed_s[:] = 0
        self.frozen[:] = False
        self.updates[:] = 0

    # -- reporting ---------------------------------------------------------

    def band(self, index: int, sigma: float = 3.0) -> tuple[float, float]:
        """A (low, high) band around the baseline, for the dashboard (section 11.1)."""
        rate = self.rate(index)
        spread = sigma * float(np.sqrt(max(self.variance[index], 0.0)))
        return (max(rate - spread, 0.0), rate + spread)

    def to_json(self, limit: int | None = None) -> dict[str, object]:
        indices = [s.index for s in self.rings.active_components()]
        if limit is not None:
            indices = indices[:limit]
        return {
            "alpha": self.alpha,
            "warmup_s": self.warmup_s,
            "floor": self.floor,
            "components": {
                self.rings.key_name(i): self.of(i).to_json() for i in indices
            },
        }


def wilson_lower_bound(errors: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion.

    Section 8.6: small-volume components use a Wilson lower bound so a handful of errors
    does not page anyone. A normal approximation on three errors out of four is
    meaningless; the Wilson bound is bounded in [0, 1] by construction, which is the point.

    z=1.96 is the 95% two-sided value, the usual default for a floor on a rate.
    """
    if total <= 0:
        return 0.0
    if errors < 0 or errors > total:
        raise ValueError(f"errors {errors} out of range for total {total}")
    phat = errors / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = phat + z2 / (2 * total)
    margin = z * np.sqrt(phat * (1.0 - phat) / total + z2 / (4 * total * total))
    return float(max((centre - margin) / denominator, 0.0))


def binomial_z(errors: int, total: int, baseline: float) -> float:
    """The z-score of an observed error count against a baseline rate.

    Section 8.6 gives the formula: (err - tot*p0) / sqrt(tot*p0*(1-p0)), with the variance
    floored so a zero baseline does not divide by zero. Returns 0.0 when there is no data,
    which reads as "nothing to say" rather than as a breach.
    """
    if total <= 0:
        return 0.0
    p0 = min(max(baseline, 0.0), 1.0)
    variance = total * p0 * (1.0 - p0)
    if variance < 1e-9:
        # A zero baseline: the count is either exactly as expected (nothing to say) or above
        # it, in which case the deviation is real and infinite. Report a large finite
        # number so downstream comparisons and severities stay well defined.
        if errors > 0:
            return float(NCAP)  # a stand-in for "infinitely significant"
        return 0.0
    return float((errors - total * p0) / np.sqrt(variance))
