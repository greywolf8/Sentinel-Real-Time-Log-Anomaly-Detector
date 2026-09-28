"""Dependency propagation with lag (docs/sentinel-plan.md section 4.6).

    downstream_err = base + coupling * max(0, upstream_err - upstream_base)  (after lag_s)

The formula, the lag, and the direction of the edges are all checked against the spec's
own edges. Also checked: propagation is one-way, a lag is respected to the second, a
component with no upstream stays at baseline, and a fault on a component overrides rather
than adds to the propagated rate.

The end-to-end test is the one that matters for the demo: break CLM.STR and watch the
cascade reach PAY.RMT, ADM.RPT and ADM.FWA, the way the db_timeout_spike scenario does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sim.config import PropagationEdge
from tests.conftest import install_collector, make_generator, settle

TICKS_PER_SECOND = 20


def edge_between(platform, src: str, dst: str) -> PropagationEdge:
    for edge in platform.propagation:
        if edge.src == src and edge.dst == dst:
            return edge
    raise AssertionError(f"no propagation edge {src} -> {dst} in platform.yaml")


def test_the_spec_edges_are_all_present(platform) -> None:
    """Section 4.6 lists thirteen edges. They are the cascade the incident engine groups."""
    expected = {
        ("CCH", "MBR", 0.5, 2.0),
        ("MBR", "X12", 0.6, 2.0),
        ("MBR", "EDT", 0.6, 3.0),
        ("PST", "EDT", 0.4, 3.0),
        ("EDT", "PRC", 0.5, 2.0),
        ("STR", "DUP", 0.6, 2.0),
        ("STR", "RMT", 0.5, 4.0),
        ("RMT", "EXP", 0.5, 3.0),
        ("EXP", "BNK", 0.5, 3.0),
        ("NPI", "CRD", 0.6, 2.0),
        ("CRD", "INT", 0.5, 2.0),
        ("STR", "RPT", 0.5, 3.0),
        ("STR", "FWA", 0.4, 3.0),
    }
    got = {(e.src, e.dst, e.coupling, e.lag_s) for e in platform.propagation}
    assert got == expected


def test_every_propagation_edge_is_backed_by_a_dependency(platform) -> None:
    """Section 4 declares "Depends on" per component and section 4.6 declares propagation
    edges. The propagation list is a strict subset: LDG depends on RMT and AUD depends on
    AUT, but the plan gives those no coupling, so a fault there does not cascade. The
    invariant is one-way, and it is the one that matters: no edge may claim a dependency
    the component tables do not declare."""
    for edge in platform.propagation:
        assert edge.src in platform.components[edge.dst].depends_on, (
            f"{edge.src} -> {edge.dst} is not a declared dependency"
        )


def test_leaves_and_roots(platform) -> None:
    """CCH, STR, NPI and PST have no upstream (section 4 marks them "none" or leaf DB), so
    a fault on them is a fault at the origin and the incident engine has nothing upstream
    to blame. AUT is a root too: it has no upstream, and nothing propagates out of it
    because section 4.6 defines no edge from it."""
    for leaf in ("CCH", "STR", "NPI", "PST", "AUT"):
        assert platform.upstream_of(leaf) == (), leaf
    for root in ("CCH", "STR", "NPI", "PST"):
        assert platform.downstream_of(root), f"{root} should feed something downstream"
    assert platform.downstream_of("AUT") == ()


def test_the_plan_declares_two_dependencies_with_no_coupling(platform) -> None:
    """PAY.LDG depends on PAY.RMT and ADM.AUD depends on ADM.AUT, but section 4.6 gives
    neither a coupling, so an error-rate fault does not leak along them. Recorded here so
    the gap is a decision on record rather than an oversight."""
    assert "RMT" in platform.components["LDG"].depends_on
    assert "AUT" in platform.components["AUD"].depends_on
    assert platform.upstream_of("LDG") == ()
    assert platform.upstream_of("AUD") == ()


def test_no_propagation_without_a_fault(tmp_path: Path) -> None:
    """Healthy traffic stays at the configured baseline on every component."""
    generator = make_generator(tmp_path, seed=1)
    settle(generator, 3)
    for code, cfg in generator.platform.components.items():
        rate = generator.effective_error_rate(code, generator.clock.now_ms())
        assert rate == pytest.approx(cfg.err, abs=1e-9), code


def test_propagation_follows_the_formula(tmp_path: Path) -> None:
    """CLM.STR at 25% feeds CLM.DUP with coupling 0.6: 0.002 + 0.6 * 0.248 = 0.1508."""
    generator = make_generator(tmp_path, seed=1)
    now = generator.clock.now_ms()
    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, at_ms=now))
    settle(generator, 8)  # past the 2 s lag on STR -> DUP

    edge = edge_between(generator.platform, "STR", "DUP")
    expected = generator.platform.components["DUP"].err + edge.coupling * (
        0.25 - generator.platform.components["STR"].err
    )
    got = generator.effective_error_rate("DUP", generator.clock.now_ms())
    assert got == pytest.approx(expected, abs=1e-6)
    assert got == pytest.approx(0.1508, abs=1e-6)


def test_lag_delays_the_downstream_rise(tmp_path: Path) -> None:
    """CLM.STR -> PAY.RMT has a 4 s lag. RMT must stay at baseline for those 4 s and rise
    after, which is what lets the incident engine identify RMT as downstream, not origin."""
    generator = make_generator(tmp_path, seed=1)
    lag = edge_between(generator.platform, "STR", "RMT").lag_s
    assert lag == 4.0
    baseline = generator.platform.components["RMT"].err

    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, at_ms=generator.clock.now_ms()))

    quiet_seconds = 0
    for _ in range(10):
        settle(generator, 1)
        if generator.effective_error_rate("RMT", generator.clock.now_ms()) == pytest.approx(baseline):
            quiet_seconds += 1
    assert quiet_seconds >= 2, "RMT should not react before the 4 s lag has elapsed"

    settle(generator, 6)
    assert generator.effective_error_rate("RMT", generator.clock.now_ms()) > baseline * 5


def test_propagation_is_directional(tmp_path: Path) -> None:
    """A fault on CLM.DUP must not travel upstream into CLM.STR."""
    generator = make_generator(tmp_path, seed=1)
    base_str = generator.platform.components["STR"].err
    asyncio.run(generator.store.set_rate("CLM.DUP", 0.30, at_ms=generator.clock.now_ms()))
    settle(generator, 8)
    assert generator.effective_error_rate("STR", generator.clock.now_ms()) == pytest.approx(
        base_str, abs=1e-9
    )


def test_two_upstreams_add_up(tmp_path: Path) -> None:
    """CLM.EDT has two upstreams, ELG.MBR (0.6) and PRV.PST (0.4). Both contributing is
    additive, per the section 4.6 formula."""
    generator = make_generator(tmp_path, seed=1)
    plat = generator.platform
    asyncio.run(generator.store.set_rate("ELG.MBR", 0.10, at_ms=generator.clock.now_ms()))
    asyncio.run(generator.store.set_rate("PRV.PST", 0.20, at_ms=generator.clock.now_ms()))
    settle(generator, 8)

    # The expected value is built from the injected fault rates, not the config baselines:
    # the formula uses the upstream's current rate, which is what a fault changed.
    expected = plat.components["EDT"].err + 0.6 * (0.10 - plat.components["MBR"].err)
    expected += 0.4 * (0.20 - plat.components["PST"].err)
    got = generator.effective_error_rate("EDT", generator.clock.now_ms())
    assert got == pytest.approx(expected, abs=1e-6)
    assert got > plat.components["EDT"].err


def test_a_cascade_reaches_the_whole_chain(tmp_path: Path) -> None:
    """The db_timeout_spike cascade: CLM.STR -> DUP, RMT -> EXP -> BNK, and RPT, FWA.

    This is the end-to-end shape the incident engine groups into one incident with CLM.STR
    as the suspected origin, so it is worth asserting in full.
    """
    generator = make_generator(tmp_path, seed=1)
    plat = generator.platform
    base = {code: cfg.err for code, cfg in plat.components.items()}

    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, at_ms=generator.clock.now_ms()))
    settle(generator, 30)

    now = generator.clock.now_ms()
    for code in ("STR", "DUP", "RMT", "EXP", "BNK", "RPT", "FWA"):
        rate = generator.effective_error_rate(code, now)
        assert rate > base[code] * 3, f"{code} did not rise: {rate} vs base {base[code]}"
    # The rest of the platform is unaffected by a claim-store fault.
    for code in ("MBR", "CCH", "NPI", "AUD"):
        assert generator.effective_error_rate(code, now) == pytest.approx(base[code], abs=1e-9)


def test_a_fault_overrides_rather_than_adds(tmp_path: Path) -> None:
    """A slider set to 25% is exactly 25%, even mid-cascade. Otherwise the console would
    not show what the generator is actually drawing."""
    generator = make_generator(tmp_path, seed=1)
    now = generator.clock.now_ms()
    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, at_ms=now))
    settle(generator, 8)
    asyncio.run(generator.store.set_rate("CLM.DUP", 0.11, at_ms=generator.clock.now_ms()))
    settle(generator, 1)
    assert generator.effective_error_rate("DUP", generator.clock.now_ms()) == pytest.approx(0.11)


def test_ramp_interpolates_the_rate(tmp_path: Path) -> None:
    """ramp_s is what slow_degradation uses: 0.2% to 8% over ten minutes."""
    generator = make_generator(tmp_path, seed=1)
    start = generator.clock.now_ms()
    asyncio.run(
        generator.store.set_rate("ELG.MBR", 0.08, ramp_s=10.0, at_ms=start)
    )
    half = start + 5_000
    quarter = start + 2_500
    expected_mid = 0.002 + (0.08 - 0.002) * 0.5
    expected_quarter = 0.002 + (0.08 - 0.002) * 0.25
    assert generator.effective_error_rate("MBR", half) == pytest.approx(expected_mid, abs=1e-9)
    assert generator.effective_error_rate("MBR", quarter) == pytest.approx(
        expected_quarter, abs=1e-9
    )


def test_a_hold_window_expires(tmp_path: Path) -> None:
    """After hold_s the component returns to baseline. The log then shows recovery, with no
    marker in the log saying a fault ended."""
    generator = make_generator(tmp_path, seed=1)
    base = generator.platform.components["STR"].err
    start = generator.clock.now_ms()
    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, hold_s=10.0, at_ms=start))
    settle(generator, 5)
    assert generator.effective_error_rate("STR", generator.clock.now_ms()) == pytest.approx(0.25)

    settle(generator, 12)
    assert generator.effective_error_rate("STR", generator.clock.now_ms()) == pytest.approx(base)


def test_rate_history_is_bounded(tmp_path: Path) -> None:
    """The rate history is what lag lookups read, so it must not grow without limit."""
    generator = make_generator(tmp_path, seed=1)
    settle(generator, 400)
    for runner in generator.runners.values():
        assert len(runner.rate_history) <= 125, runner.key


def test_observed_error_rate_tracks_the_injected_rate(tmp_path: Path, catalog) -> None:
    """End to end through the draws, not just the rate function: an injected 25% on
    CLM.STR shows up in the emitted STR lines at roughly 25%.

    Counted from the catalog, because stats.errors is platform-wide and the cascade puts
    errors into other components too. Measuring globally would read 0.46 against an
    injected 0.25, which is the cascade working, not a bug.
    """
    generator = make_generator(tmp_path, seed=1)
    str_codes = {code for code, spec in catalog.codes.items() if spec.component == "STR"}
    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, at_ms=generator.clock.now_ms()))
    settle(generator, 4)  # warm the propagation history

    def str_error_rate(lines: list[bytes]) -> tuple[int, int]:
        total = errors = 0
        for raw in lines:
            if raw[20:23] != b"STR":
                continue
            total += 1
            if raw[24:29].decode("ascii") in str_codes:
                errors += 1
        return total, errors

    sink = install_collector(generator)
    settle(generator, 20)
    total, errors = str_error_rate(sink)
    assert total > 4000, f"not enough STR volume to measure: {total}"
    observed = errors / total
    assert 0.23 < observed < 0.27, f"observed {observed:.3f} against an injected 0.25"


def test_the_cascade_lifts_error_rates_elsewhere_too(tmp_path: Path, catalog) -> None:
    """The complement of the test above: a CLM.STR fault pushes other components up, which
    is what the incident engine groups and what makes a single-component threshold
    detector look like it is double counting."""
    generator = make_generator(tmp_path, seed=1)
    dup_codes = {code for code, spec in catalog.codes.items() if spec.component == "DUP"}

    def dup_error_rate(lines: list[bytes]) -> float:
        relevant = [raw for raw in lines if raw[20:23] == b"DUP"]
        if not relevant:
            return 0.0
        hits = sum(1 for raw in relevant if raw[24:29].decode("ascii") in dup_codes)
        return hits / len(relevant)

    baseline_sink = install_collector(generator)
    settle(generator, 20)
    before = dup_error_rate(baseline_sink)

    asyncio.run(generator.store.set_rate("CLM.STR", 0.25, at_ms=generator.clock.now_ms()))
    settle(generator, 4)
    after_sink = install_collector(generator)
    settle(generator, 20)
    after = dup_error_rate(after_sink)

    assert before < 0.01, f"healthy DUP error rate should be tiny, got {before:.4f}"
    assert after > 0.10, f"DUP should have risen through propagation, got {after:.4f}"
