"""Tests for incident engine (alert grouping and lifecycle)."""

import pytest

from detector.detectors import Alert, Rule
from detector.incidents import (
    DependencyEdge,
    Incident,
    IncidentEngine,
    IncidentStatus,
)
from detector.severity import ScoredAlert, Severity, SeverityInputs


@pytest.fixture
def sample_alert() -> Alert:
    """Create a sample alert for testing."""
    return Alert(
        rule=Rule.FAST,
        key="CLM.STR",
        scope="component",
        observed=0.27,
        baseline=0.002,
        z=38.1,
        window_s=10,
        total=1000,
        errors=270,
        opened_at_ms=1790603432000,
    )


@pytest.fixture
def sample_scored_alert(sample_alert: Alert) -> ScoredAlert:
    """Create a sample scored alert for testing."""
    inputs = SeverityInputs(
        z=38.1,
        rate=0.27,
        baseline=0.002,
        seconds_in_breach=6,
        criticality=1.0,
        burn_rate=21.4,
    )
    return ScoredAlert(
        alert=sample_alert,
        severity=Severity.HIGH,
        score=0.6,
        inputs=inputs,
        reasons=["test reason"],
    )


@pytest.fixture
def propagation_edges() -> list[dict]:
    """Sample propagation edges from platform.yaml."""
    return [
        {"from": "ELG.CCH", "to": "ELG.MBR", "coupling": 0.5, "lag_s": 2},
        {"from": "ELG.MBR", "to": "CLM.EDT", "coupling": 0.6, "lag_s": 3},
        {"from": "CLM.STR", "to": "PAY.RMT", "coupling": 0.5, "lag_s": 4},
    ]


@pytest.fixture
def incident_engine(propagation_edges: list[dict]) -> IncidentEngine:
    """Create an incident engine for testing."""
    return IncidentEngine(propagation_edges=propagation_edges)


@pytest.fixture
def incident() -> Incident:
    """Create a sample incident for testing."""
    return Incident(
        incident_id="inc_0001",
        suspected_origin="CLM.STR",
        status=IncidentStatus.OPEN,
        opened_at_ms=1790603432000,
    )


def test_dependency_edge_creation() -> None:
    """Test dependency edge creation."""
    edge = DependencyEdge(
        from_component="CLM.STR", to_component="PAY.RMT", coupling=0.5, lag_s=4
    )
    assert edge.from_component == "CLM.STR"
    assert edge.to_component == "PAY.RMT"
    assert edge.coupling == 0.5
    assert edge.lag_s == 4


def test_incident_initialization() -> None:
    """Test incident initialization."""
    incident = Incident(
        incident_id="inc_0001",
        suspected_origin="CLM.STR",
        status=IncidentStatus.OPEN,
        opened_at_ms=1790603432000,
    )
    assert incident.incident_id == "inc_0001"
    assert incident.suspected_origin == "CLM.STR"
    assert incident.status is IncidentStatus.OPEN
    assert incident.opened_at_ms == 1790603432000
    assert len(incident.alerts) == 0


def test_incident_add_alert(incident: Incident, sample_scored_alert: ScoredAlert) -> None:
    """Test adding an alert to an incident."""
    incident.add_alert(sample_scored_alert)
    assert len(incident.alerts) == 1
    assert incident.last_alert_at_ms == sample_scored_alert.alert.opened_at_ms


def test_incident_acknowledge(incident: Incident) -> None:
    """Test acknowledging an incident."""
    incident.acknowledge(1790603500000)
    assert incident.status is IncidentStatus.ACKNOWLEDGED
    assert incident.acknowledged_at_ms == 1790603500000


def test_incident_resolve(incident: Incident) -> None:
    """Test resolving an incident."""
    incident.resolve(1790603600000)
    assert incident.status is IncidentStatus.RESOLVED
    assert incident.resolved_at_ms == 1790603600000
    assert incident.cooldown_until_ms == 1790603600000 + 300000  # 5 minutes


def test_incident_is_in_cooldown(incident: Incident) -> None:
    """Test cooldown check."""
    incident.resolve(1790603600000)
    assert incident.is_in_cooldown(1790603650000)  # 50 seconds later
    assert not incident.is_in_cooldown(1790604000000)  # 400 seconds later


def test_incident_to_json(incident: Incident) -> None:
    """Test incident JSON serialization."""
    json_data = incident.to_json()
    assert json_data["incident_id"] == incident.incident_id
    assert json_data["suspected_origin"] == incident.suspected_origin
    assert json_data["status"] == incident.status.value
    assert json_data["alert_count"] == 0


def test_incident_engine_initialization(propagation_edges: list[dict]) -> None:
    """Test incident engine initialization."""
    engine = IncidentEngine(propagation_edges=propagation_edges)
    assert len(engine.edges) == 3
    assert engine.grouping_window_s == 120
    assert engine.dedup_window_s == 60


def test_incident_engine_next_incident_id(incident_engine: IncidentEngine) -> None:
    """Test incident ID generation."""
    id1 = incident_engine._next_incident_id()
    id2 = incident_engine._next_incident_id()
    assert id1 == "inc_0001"
    assert id2 == "inc_0002"


def test_incident_engine_dedupe_key(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test dedupe key generation."""
    key = incident_engine._dedupe_key(sample_scored_alert)
    assert key == "CLM.STR:fast"


def test_incident_engine_process_new_incident(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test processing a new alert creates a new incident."""
    incident = incident_engine.process_alert(sample_scored_alert)
    assert incident.incident_id == "inc_0001"
    assert incident.suspected_origin == "CLM.STR"
    assert incident.status is IncidentStatus.OPEN
    assert len(incident.alerts) == 1


def test_incident_engine_process_existing_incident(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test processing an alert adds to existing incident within dedup window."""
    # First alert
    incident1 = incident_engine.process_alert(sample_scored_alert)
    # Second alert within dedup window
    sample_scored_alert.alert.opened_at_ms += 30000  # 30 seconds later
    incident2 = incident_engine.process_alert(sample_scored_alert)
    assert incident1.incident_id == incident2.incident_id
    assert len(incident2.alerts) == 2


def test_incident_engine_acknowledge(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test acknowledging an incident."""
    incident = incident_engine.process_alert(sample_scored_alert)
    result = incident_engine.acknowledge(incident.incident_id, 1790603500000)
    assert result is True
    updated = incident_engine.get_incident(incident.incident_id)
    assert updated.status is IncidentStatus.ACKNOWLEDGED


def test_incident_engine_resolve(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test resolving an incident."""
    incident = incident_engine.process_alert(sample_scored_alert)
    result = incident_engine.resolve(incident.incident_id, 1790603600000)
    assert result is True
    updated = incident_engine.get_incident(incident.incident_id)
    assert updated.status is IncidentStatus.RESOLVED


def test_incident_engine_auto_resolve_stale(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test auto-resolving stale incidents."""
    incident = incident_engine.process_alert(sample_scored_alert)
    # Advance time past grouping window
    resolved_ids = incident_engine.auto_resolve_stale(
        sample_scored_alert.alert.opened_at_ms + 130000
    )
    assert incident.incident_id in resolved_ids
    updated = incident_engine.get_incident(incident.incident_id)
    assert updated.status is IncidentStatus.RESOLVED


def test_incident_engine_get_incident(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test getting an incident by ID."""
    incident = incident_engine.process_alert(sample_scored_alert)
    retrieved = incident_engine.get_incident(incident.incident_id)
    assert retrieved is not None
    assert retrieved.incident_id == incident.incident_id


def test_incident_engine_get_nonexistent_incident(incident_engine: IncidentEngine) -> None:
    """Test getting a nonexistent incident returns None."""
    retrieved = incident_engine.get_incident("inc_9999")
    assert retrieved is None


def test_incident_engine_to_json(incident_engine: IncidentEngine) -> None:
    """Test incident engine JSON serialization."""
    json_data = incident_engine.to_json()
    assert "grouping_window_s" in json_data
    assert "dedup_window_s" in json_data
    assert "open_incidents" in json_data
    assert "propagation_edges" in json_data
    assert len(json_data["propagation_edges"]) == 3


def test_incident_engine_build_dependency_chain(incident_engine: IncidentEngine) -> None:
    """Test building dependency chain from origin."""
    chain = incident_engine._build_dependency_chain("CLM.STR")
    assert "CLM.STR" in chain
    # Should include downstream components
    assert len(chain) >= 1


def test_incident_reopen_after_cooldown(
    incident_engine: IncidentEngine, sample_scored_alert: ScoredAlert
) -> None:
    """Test that an incident can be reopened after cooldown."""
    incident = incident_engine.process_alert(sample_scored_alert)
    incident_engine.resolve(incident.incident_id, 1790603600000)
    # Try to reopen before cooldown (5 seconds later, still in cooldown)
    sample_scored_alert.alert.opened_at_ms = 1790603650000
    incident2 = incident_engine.process_alert(sample_scored_alert)
    # Should reopen the same incident (it's in cooldown, so it gets reopened)
    assert incident2.incident_id == incident.incident_id
    assert incident2.status is IncidentStatus.OPEN
