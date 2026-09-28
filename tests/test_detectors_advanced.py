"""Tests for advanced detectors (code-mix drift, latency shift, new-code, silence)."""

import pytest

from detector.baseline import Baselines
from detector.detectors_advanced import (
    AdvancedDetectors,
    AdvancedRule,
    CodeMixState,
    LatencyState,
    NewCodeState,
    SilenceState,
)
from detector.detectors import Alert, Rule
from detector.rings import Rings


@pytest.fixture
def rings() -> Rings:
    """Create a rings instance for testing."""
    rings = Rings()
    # Register a test component
    rings.register("CLM|EDT", "CLM", "EDT", 0)
    rings.register("PAY|BNK", "PAY", "BNK", 1)
    return rings


@pytest.fixture
def baselines(rings: Rings) -> Baselines:
    """Create a baselines instance for testing."""
    baselines = Baselines(rings)
    # Seed baselines to avoid warm-up
    baselines.seed(0, 0.002)
    baselines.seed(1, 0.01)
    return baselines


@pytest.fixture
def advanced_detectors(rings: Rings, baselines: Baselines) -> AdvancedDetectors:
    """Create an advanced detectors instance for testing."""
    denial_codes = ["W4101", "W4102", "W4103", "W4104", "W4105"]
    return AdvancedDetectors(rings, baselines, denial_codes)


def test_code_mix_state_initialization() -> None:
    """Test that code mix state initializes correctly."""
    state = CodeMixState(baseline_dist=__import__("numpy").zeros(256))
    assert state.baseline_total == 0
    assert state.updates == 0
    assert state.last_js == 0.0
    assert state.last_moved_code == ""


def test_latency_state_initialization() -> None:
    """Test that latency state initializes correctly."""
    state = LatencyState()
    assert state.baseline_p95 == 0.0
    assert state.observed_p95 == 0.0
    assert state.updates == 0
    assert state.shift_factor == 0.0


def test_new_code_state_initialization() -> None:
    """Test that new code state initializes correctly."""
    state = NewCodeState()
    assert len(state.seen_codes) == 0
    assert len(state.unknown_counts) == 0
    assert state.last_alerted_code == ""
    assert state.last_alerted_count == 0


def test_silence_state_initialization() -> None:
    """Test that silence state initializes correctly."""
    state = SilenceState()
    assert state.baseline_lines_per_sec == 0.0
    assert state.last_total == 0
    assert state.silent_seconds == 0


def test_jensen_shannon_divergence_identical() -> None:
    """Test JS divergence with identical distributions."""
    np = __import__("numpy")
    p = np.array([0.25, 0.25, 0.25, 0.25])
    q = np.array([0.25, 0.25, 0.25, 0.25])
    detectors = AdvancedDetectors(Rings(), Baselines(Rings()))
    js, max_idx, max_delta = detectors.jensen_shannon_divergence(p, q)
    assert js < 0.01  # Should be near zero


def test_jensen_shannon_divergence_different() -> None:
    """Test JS divergence with different distributions."""
    np = __import__("numpy")
    p = np.array([0.5, 0.3, 0.1, 0.1])
    q = np.array([0.1, 0.1, 0.3, 0.5])
    detectors = AdvancedDetectors(Rings(), Baselines(Rings()))
    js, max_idx, max_delta = detectors.jensen_shannon_divergence(p, q)
    assert js > 0.1  # Should be significant
    assert max_delta > 0.3


def test_p95_from_histogram() -> None:
    """Test p95 calculation from histogram."""
    np = __import__("numpy")
    detectors = AdvancedDetectors(Rings(), Baselines(Rings()))
    # Create a histogram where 95% of values are in bin 5
    histogram = np.zeros(20, dtype=np.uint32)
    histogram[5] = 95
    histogram[10] = 5
    p95 = detectors.p95_from_histogram(histogram)
    assert p95 == 2 ** 4  # bin 5 maps to 2^(5-1) = 16


def test_p95_empty_histogram() -> None:
    """Test p95 with empty histogram."""
    np = __import__("numpy")
    detectors = AdvancedDetectors(Rings(), Baselines(Rings()))
    histogram = np.zeros(20, dtype=np.uint32)
    p95 = detectors.p95_from_histogram(histogram)
    assert p95 == 0.0


def test_code_mix_drift_no_baseline(advanced_detectors: AdvancedDetectors) -> None:
    """Test code mix drift with no baseline (should not alert)."""
    alert = advanced_detectors.check_code_mix_drift(0, 100, 100000)
    assert alert is None


def test_latency_shift_no_baseline(advanced_detectors: AdvancedDetectors) -> None:
    """Test latency shift with no baseline (should not alert)."""
    alert = advanced_detectors.check_latency_shift(0, 100, 100000)
    assert alert is None


def test_silence_no_baseline(advanced_detectors: AdvancedDetectors) -> None:
    """Test silence detection with no baseline (should not alert)."""
    alert = advanced_detectors.check_silence(0, 100, 100000)
    assert alert is None


def test_advanced_detectors_evaluate_no_alerts(
    advanced_detectors: AdvancedDetectors,
) -> None:
    """Test that evaluate returns no alerts when conditions are not met."""
    alerts = advanced_detectors.evaluate(100, 100000)
    assert len(alerts) == 0


def test_advanced_detectors_to_json(advanced_detectors: AdvancedDetectors) -> None:
    """Test JSON serialization of advanced detectors."""
    json_data = advanced_detectors.to_json()
    assert "seconds_evaluated" in json_data
    assert "denial_codes" in json_data
    assert json_data["denial_codes"] == ["W4101", "W4102", "W4103", "W4104", "W4105"]


def test_code_mix_drift_warmup(
    advanced_detectors: AdvancedDetectors, baselines: Baselines
) -> None:
    """Test that code mix drift does not alert during warm-up."""
    # Reset baseline to force warm-up
    baselines.reset()
    alert = advanced_detectors.check_code_mix_drift(0, 100, 100000)
    assert alert is None


def test_latency_shift_warmup(
    advanced_detectors: AdvancedDetectors, baselines: Baselines
) -> None:
    """Test that latency shift does not alert during warm-up."""
    # Reset baseline to force warm-up
    baselines.reset()
    alert = advanced_detectors.check_latency_shift(0, 100, 100000)
    assert alert is None


def test_silence_warmup(
    advanced_detectors: AdvancedDetectors, baselines: Baselines
) -> None:
    """Test that silence detection does not alert during warm-up."""
    # Reset baseline to force warm-up
    baselines.reset()
    alert = advanced_detectors.check_silence(0, 100, 100000)
    assert alert is None


def test_advanced_detectors_inactive_component(
    advanced_detectors: AdvancedDetectors, rings: Rings
) -> None:
    """Test that inactive components are skipped."""
    # Deactivate component
    rings.slots[0].active = False
    alerts = advanced_detectors.evaluate(100, 100000)
    assert len(alerts) == 0
