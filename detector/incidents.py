"""Incident engine (docs/sentinel-plan.md section 8.8).

Groups alerts by time window, dependency edge and shared deploy marker into one incident.
Suspected origin is the earliest-breaching component in the dependency chain. Lifecycle:
open, acknowledged, resolved with cooldown and deduplication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from detector.detectors import Alert
from detector.severity import ScoredAlert, Severity

# Section 8.8: time window for grouping alerts into one incident.
GROUPING_WINDOW_S = 120  # 2 minutes
# Section 8.8: cooldown before an incident can be reopened with the same key.
COOLDOWN_S = 300  # 5 minutes
# Section 8.8: deduplication - same origin and type within window is one incident.
DEDUP_WINDOW_S = 60


class IncidentStatus(str, Enum):
    """Incident lifecycle (section 8.8)."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


@dataclass(slots=True)
class DependencyEdge:
    """One propagation edge from platform.yaml (section 4.6)."""

    from_component: str
    to_component: str
    coupling: float
    lag_s: int


@dataclass(slots=True)
class Incident:
    """One incident grouping related alerts."""

    incident_id: str
    suspected_origin: str
    status: IncidentStatus = IncidentStatus.OPEN
    opened_at_ms: int = 0
    acknowledged_at_ms: int = 0
    resolved_at_ms: int = 0
    alerts: list[ScoredAlert] = field(default_factory=list)
    dependency_chain: list[str] = field(default_factory=list)
    dedupe_key: str = ""
    last_alert_at_ms: int = 0
    cooldown_until_ms: int = 0

    def add_alert(self, alert: ScoredAlert) -> None:
        """Add an alert to this incident, updating timing."""
        self.alerts.append(alert)
        self.last_alert_at_ms = max(self.last_alert_at_ms, alert.alert.opened_at_ms)

    def acknowledge(self, now_ms: int) -> None:
        """Mark the incident as acknowledged."""
        if self.status is IncidentStatus.OPEN:
            self.status = IncidentStatus.ACKNOWLEDGED
            self.acknowledged_at_ms = now_ms

    def resolve(self, now_ms: int) -> None:
        """Mark the incident as resolved and start cooldown."""
        if self.status in (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED):
            self.status = IncidentStatus.RESOLVED
            self.resolved_at_ms = now_ms
            self.cooldown_until_ms = now_ms + COOLDOWN_S * 1000

    def is_in_cooldown(self, now_ms: int) -> bool:
        """Check if the incident is in cooldown and cannot be reopened."""
        return now_ms < self.cooldown_until_ms

    def to_json(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "suspected_origin": self.suspected_origin,
            "status": self.status.value,
            "opened_at_ms": self.opened_at_ms,
            "acknowledged_at_ms": self.acknowledged_at_ms,
            "resolved_at_ms": self.resolved_at_ms,
            "resolved_at_iso": (
                datetime.fromtimestamp(self.resolved_at_ms / 1000, tz=timezone.utc).isoformat()
                if self.resolved_at_ms
                else None
            ),
            "alert_count": len(self.alerts),
            "dependency_chain": list(self.dependency_chain),
            "last_alert_at_ms": self.last_alert_at_ms,
            "cooldown_until_ms": self.cooldown_until_ms,
            "max_severity": max((a.severity.value for a in self.alerts), default="INFO"),
        }


class IncidentEngine:
    """Groups alerts into incidents based on propagation and time."""

    def __init__(
        self,
        propagation_edges: list[dict[str, Any]] | None = None,
        grouping_window_s: int = GROUPING_WINDOW_S,
        dedup_window_s: int = DEDUP_WINDOW_S,
    ) -> None:
        self.grouping_window_s = grouping_window_s
        self.dedup_window_s = dedup_window_s
        self.edges: list[DependencyEdge] = []
        if propagation_edges:
            for edge in propagation_edges:
                self.edges.append(
                    DependencyEdge(
                        from_component=edge["from"],
                        to_component=edge["to"],
                        coupling=edge["coupling"],
                        lag_s=edge["lag_s"],
                    )
                )
        self.incidents: dict[str, Incident] = {}
        self._incident_counter = 0
        self._resolved_incidents: list[Incident] = []
        self._max_resolved_history = 100

    def _next_incident_id(self) -> str:
        self._incident_counter += 1
        return f"inc_{self._incident_counter:04d}"

    def _dedupe_key(self, alert: ScoredAlert) -> str:
        """Key for deduplication: origin + type."""
        return f"{alert.alert.key}:{alert.alert.rule.value}"

    def _find_suspected_origin(
        self, alert: ScoredAlert, all_alerts: list[ScoredAlert]
    ) -> str:
        """Find the earliest-breaching component in the dependency chain.

        Walks the propagation graph backwards from the alert's component to find
        the root cause.
        """
        component = alert.alert.key
        # Build a map of upstream dependencies
        upstream: dict[str, list[str]] = {}
        for edge in self.edges:
            if edge.to_component not in upstream:
                upstream[edge.to_component] = []
            upstream[edge.to_component].append(edge.from_component)

        # Find all components that have breached
        breached = {a.alert.key for a in all_alerts}

        # Walk upstream from the alert's component
        visited = set()
        queue = [component]
        candidates = []

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            if current in breached:
                candidates.append(current)
            if current in upstream:
                queue.extend(upstream[current])

        # Return the earliest-breaching candidate (by opened_at_ms)
        if not candidates:
            return component

        earliest = component
        earliest_time = alert.alert.opened_at_ms

        for cand in candidates:
            for a in all_alerts:
                if a.alert.key == cand and a.alert.opened_at_ms < earliest_time:
                    earliest = cand
                    earliest_time = a.alert.opened_at_ms

        return earliest

    def _build_dependency_chain(self, origin: str) -> list[str]:
        """Build the dependency chain from origin to downstream components."""
        chain = [origin]
        # Build downstream map
        downstream: dict[str, list[str]] = {}
        for edge in self.edges:
            if edge.from_component not in downstream:
                downstream[edge.from_component] = []
            downstream[edge.from_component].append(edge.to_component)

        # BFS to find all downstream components
        visited = set([origin])
        queue = list(downstream.get(origin, []))

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            chain.append(current)
            if current in downstream:
                queue.extend(downstream[current])

        return chain

    def _find_existing_incident(
        self, alert: ScoredAlert, now_ms: int
    ) -> Incident | None:
        """Find an existing incident for this alert, respecting cooldown."""
        dedupe_key = self._dedupe_key(alert)

        # Check open incidents
        for inc in self.incidents.values():
            if inc.status is IncidentStatus.RESOLVED:
                continue
            if inc.dedupe_key == dedupe_key:
                # Check if within dedup window
                if now_ms - inc.last_alert_at_ms <= self.dedup_window_s * 1000:
                    return inc

        # Check resolved incidents in cooldown
        for inc in self._resolved_incidents:
            if inc.dedupe_key == dedupe_key and inc.is_in_cooldown(now_ms):
                # Reopen
                inc.status = IncidentStatus.OPEN
                inc.resolved_at_ms = 0
                self.incidents[inc.incident_id] = inc
                self._resolved_incidents.remove(inc)
                return inc

        return None

    def process_alert(self, alert: ScoredAlert, now_ms: int = 0) -> Incident:
        """Process one alert, grouping it into an incident."""
        if now_ms == 0:
            now_ms = alert.alert.opened_at_ms

        # Check for existing incident
        existing = self._find_existing_incident(alert, now_ms)
        if existing:
            existing.add_alert(alert)
            return existing

        # Create new incident
        incident_id = self._next_incident_id()
        dedupe_key = self._dedupe_key(alert)

        # Find suspected origin from all recent alerts
        recent_alerts = [
            a
            for inc in self.incidents.values()
            for a in inc.alerts
            if now_ms - a.alert.opened_at_ms <= self.grouping_window_s * 1000
        ]
        recent_alerts.append(alert)

        origin = self._find_suspected_origin(alert, recent_alerts)
        chain = self._build_dependency_chain(origin)

        incident = Incident(
            incident_id=incident_id,
            suspected_origin=origin,
            status=IncidentStatus.OPEN,
            opened_at_ms=now_ms,
            alerts=[alert],
            dependency_chain=chain,
            dedupe_key=dedupe_key,
            last_alert_at_ms=now_ms,
        )

        self.incidents[incident_id] = incident
        return incident

    def acknowledge(self, incident_id: str, now_ms: int = 0) -> bool:
        """Acknowledge an incident."""
        if incident_id not in self.incidents:
            return False
        if now_ms == 0:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.incidents[incident_id].acknowledge(now_ms)
        return True

    def resolve(self, incident_id: str, now_ms: int = 0) -> bool:
        """Resolve an incident."""
        if incident_id not in self.incidents:
            return False
        if now_ms == 0:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        incident = self.incidents[incident_id]
        incident.resolve(now_ms)
        # Move to resolved history
        self._resolved_incidents.append(incident)
        del self.incidents[incident_id]
        # Trim history
        if len(self._resolved_incidents) > self._max_resolved_history:
            self._resolved_incidents.pop(0)
        return True

    def auto_resolve_stale(self, now_ms: int = 0) -> list[str]:
        """Auto-resolve incidents that have had no alerts for the grouping window."""
        if now_ms == 0:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        resolved_ids = []
        for incident_id, incident in list(self.incidents.items()):
            if (
                incident.status is IncidentStatus.OPEN
                and now_ms - incident.last_alert_at_ms > self.grouping_window_s * 1000
            ):
                self.resolve(incident_id, now_ms)
                resolved_ids.append(incident_id)

        return resolved_ids

    def get_incident(self, incident_id: str) -> Incident | None:
        """Get an incident by ID."""
        if incident_id in self.incidents:
            return self.incidents[incident_id]
        for inc in self._resolved_incidents:
            if inc.incident_id == incident_id:
                return inc
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "grouping_window_s": self.grouping_window_s,
            "dedup_window_s": self.dedup_window_s,
            "open_incidents": {
                inc_id: inc.to_json() for inc_id, inc in self.incidents.items()
            },
            "resolved_incident_count": len(self._resolved_incidents),
            "propagation_edges": [
                {
                    "from": e.from_component,
                    "to": e.to_component,
                    "coupling": e.coupling,
                    "lag_s": e.lag_s,
                }
                for e in self.edges
            ],
        }
