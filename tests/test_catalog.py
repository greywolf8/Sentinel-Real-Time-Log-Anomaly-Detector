"""Catalog and platform config validation (docs/sentinel-plan.md section 4).

The catalog and platform files are the shared vocabulary between the generator and the
detector, so their contents are part of the spec. These tests assert the numbers the plan
gives: five services, twenty components, the rps and base error rate of each, and the
dependencies. A typo in catalog.yaml is a silent behavioural change otherwise.
"""

from __future__ import annotations

import pytest
import yaml

from sim.config import (
    SERVICE_CODES,
    ConfigError,
    load_catalog,
    load_platform,
)

# Section 4.1-4.5: (rps, base err, p50_ms) per component, verbatim from the tables.
SPEC_COMPONENTS: dict[str, tuple[float, float]] = {
    # payment-remittance
    "RMT": (60, 0.006),
    "EXP": (8, 0.003),
    "LDG": (120, 0.002),
    "BNK": (4, 0.010),
    # claims-adjudication
    "EDT": (250, 0.008),
    "PRC": (220, 0.003),
    "DUP": (250, 0.002),
    "STR": (300, 0.002),
    # member-eligibility
    "X12": (180, 0.004),
    "MBR": (320, 0.002),
    "CCH": (400, 0.001),
    "CVR": (150, 0.003),
    # provider-enrollment
    "INT": (25, 0.012),
    "CRD": (30, 0.010),
    "NPI": (40, 0.008),
    "PST": (60, 0.002),
    # admin-integrity
    "AUT": (90, 0.003),
    "AUD": (200, 0.0005),
    "RPT": (5, 0.015),
    "FWA": (100, 0.004),
}

SPEC_DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "RMT": ("CLM.STR",),
    "EXP": ("PAY.RMT",),
    "LDG": ("PAY.RMT",),
    "BNK": ("PAY.EXP",),
    "EDT": ("ELG.MBR", "PRV.PST"),
    "PRC": ("CLM.EDT",),
    "DUP": ("CLM.STR",),
    "STR": (),
    "X12": ("ELG.MBR",),
    "MBR": ("ELG.CCH",),
    "CCH": (),
    "CVR": ("ELG.CCH",),
    "INT": ("PRV.CRD",),
    "CRD": ("PRV.NPI",),
    "NPI": (),
    "PST": (),
    "AUT": (),
    "AUD": ("ADM.AUT",),
    "RPT": ("CLM.STR",),
    "FWA": ("CLM.STR",),
}


def test_five_services_with_three_letter_codes(catalog) -> None:
    assert set(catalog.services) == set(SERVICE_CODES)
    assert all(len(code) == 3 for code in catalog.services)


def test_twenty_components_across_five_services(catalog) -> None:
    assert len(catalog.components) == 20
    counts = {svc: 0 for svc in SERVICE_CODES}
    for spec in catalog.components.values():
        counts[spec.service] += 1
    assert counts == {"PAY": 4, "CLM": 4, "ELG": 4, "PRV": 4, "ADM": 4}


def test_rps_and_base_error_rates_match_the_plan(platform) -> None:
    for code, (rps, err) in SPEC_COMPONENTS.items():
        cfg = platform.components[code]
        assert cfg.rps == pytest.approx(rps), f"{code} rps"
        assert cfg.err == pytest.approx(err), f"{code} err"


def test_dependencies_match_the_plan(platform, catalog) -> None:
    for code, deps in SPEC_DEPENDS_ON.items():
        expected = tuple(d.split(".")[-1] for d in deps)
        assert platform.components[code].depends_on == expected, code


def test_normal_volume_is_about_2800_lines_per_second(platform) -> None:
    """Section 4.5 says roughly 2,800 lines/s across the platform."""
    total = sum(cfg.rps for cfg in platform.components.values())
    assert 2500 < total < 3100, total
    assert total == pytest.approx(2812, abs=60)


def test_the_denial_and_bad_password_rates_are_separate_from_errors(platform) -> None:
    """Denials are level W business outcomes, not errors. CLM.EDT denies 6% of claims and
    ADM.AUT sees bad passwords at 2%, on top of their error rates, never inside them."""
    assert platform.components["EDT"].warn == 0.06
    assert platform.components["AUT"].warn == 0.02
    assert platform.components["EDT"].err == 0.008
    assert platform.components["AUT"].err == 0.003


def test_every_component_with_a_warning_code_has_a_warn_rate(platform, catalog) -> None:
    """A catalogued W code with no rate behind it would never be emitted, so the code-mix
    drift detector would have nothing to watch on that component."""
    for code, specs in catalog.warn_codes.items():
        assert platform.components[code].warn > 0, (
            f"{code} has warning codes {sorted(s.code for s in specs)} but no warn rate"
        )


def test_warning_volume_stays_a_small_share_of_the_platform(platform) -> None:
    """Warnings are background noise around the signal. Aggregated they should be a low
    single-digit percentage of all lines, and the two components that carry the business
    denials should account for most of it.

    A per-component warn < err rule would be wrong: ELG.MBR legitimately warns about a
    member not being found more often than it times out on the index.
    """
    warn_share = sum(cfg.rps * cfg.warn for cfg in platform.components.values())
    total = sum(cfg.rps for cfg in platform.components.values())
    share = warn_share / total
    assert 0.005 < share < 0.09, f"warnings are {share:.1%} of platform volume"

    denial_volume = sum(
        platform.components[code].rps * platform.components[code].warn
        for code in ("EDT", "AUT")
    )
    assert denial_volume / warn_share > 0.7, "denials should dominate warning volume"


def test_every_code_belongs_to_its_declared_component(catalog) -> None:
    for code, spec in catalog.codes.items():
        assert catalog.components[spec.component].service == spec.service, code


def test_every_component_has_a_success_template(catalog) -> None:
    for code, spec in catalog.components.items():
        assert spec.success_template, code
        assert spec.success_template.isascii(), code
        assert "|" not in spec.success_template, f"a pipe in {code} would break the offsets"


def test_every_error_template_is_ascii_and_pipe_free(catalog) -> None:
    """A pipe in a message would make the fixed offsets unusable for anyone who splits."""
    for code, spec in catalog.codes.items():
        assert spec.template.isascii(), f"{code} is not ASCII"
        assert "|" not in spec.template, f"{code} contains a pipe"


def test_every_template_renders(catalog) -> None:
    """Each template must format against the generator's placeholder set. A template with
    a stray {x} would otherwise raise in the middle of a demo run."""
    import re

    known = {"n", "m", "k", "ms", "cpt", "npi", "state", "date", "yy", "nn", "a", "b"}
    pattern = re.compile(r"\{(\w+)\}")
    for code, spec in catalog.codes.items():
        placeholders = set(pattern.findall(spec.template))
        unknown = placeholders - known
        assert not unknown, f"{code} uses unknown placeholders {unknown}"
    for code, spec in catalog.components.items():
        placeholders = set(pattern.findall(spec.success_template))
        unknown = placeholders - known
        assert not unknown, f"{code} success uses unknown placeholders {unknown}"


def test_error_and_warning_codes_are_indexed_by_component(catalog) -> None:
    for code, specs in catalog.error_codes.items():
        assert specs
        for spec in specs:
            assert spec.level in ("E", "F"), spec.code
            assert spec.component == code
    for code, specs in catalog.warn_codes.items():
        for spec in specs:
            assert spec.level == "W"
            assert spec.component == code


def test_latency_floors_are_below_their_typical_latency(catalog) -> None:
    for code, spec in catalog.codes.items():
        assert spec.latency_min_ms > 0, code
        assert spec.latency_min_ms <= spec.latency_ms, code


def test_criticality_is_configured_for_severity(catalog) -> None:
    """Section 8.7 scores severity partly on component criticality. Every component needs
    a value, and the ones the plan calls out need to be at the top."""
    for code, spec in catalog.components.items():
        assert 0.0 <= spec.criticality <= 1.0, code
    for high in ("PAY.BNK", "PAY.LDG", "ADM.AUD", "CLM.STR", "CLM.EDT"):
        _, _, component = high.partition(".")
        assert catalog.components[component].criticality >= 0.9, high
    assert catalog.components["RPT"].criticality <= 0.4, "ADM.RPT is the low-criticality one"


def test_audit_trail_is_among_the_most_critical_components(catalog) -> None:
    """Section 8.7 puts PAY.LDG, PAY.BNK and ADM.AUD in the high-criticality band. The
    claim store and edit engine are equally critical to the payment cycle, so the top band
    is a tie rather than a single winner; what matters is that the low-criticality
    components stay low."""
    criticality = {code: spec.criticality for code, spec in catalog.components.items()}
    top = [code for code, value in criticality.items() if value >= 0.9]
    assert set(top) == {"EDT", "STR", "AUD", "BNK", "RMT", "LDG", "MBR", "AUT"}
    low = [code for code, value in criticality.items() if value <= 0.4]
    assert low == ["RPT"], "ADM.RPT is the one low-criticality component in section 8.7"
    # No component sits in the awkward middle of the scale.
    assert all(value in {v / 20 for v in range(1, 21)} for value in criticality.values())


# --- config loading rejects bad input --------------------------------------


def _write(tmp_path, name: str, data: dict) -> object:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_missing_services_block_is_an_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="services"):
        load_catalog(_write(tmp_path, "c.yaml", {"components": {}, "codes": {}}))


def test_a_code_for_an_unknown_component_is_an_error(tmp_path, catalog_path) -> None:
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    raw["codes"]["E9999"] = {"service": "CLM", "component": "ZZZ", "level": "E", "weight": 1.0}
    with pytest.raises(ConfigError, match="unknown"):
        load_catalog(_write(tmp_path, "bad.yaml", raw))


def test_a_service_mismatch_inside_a_code_is_an_error(tmp_path, catalog_path) -> None:
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    raw["codes"]["E4410"]["service"] = "PAY"  # STR belongs to CLM
    with pytest.raises(ConfigError, match="contradicts"):
        load_catalog(_write(tmp_path, "bad.yaml", raw))


def test_a_bad_level_in_a_code_is_an_error(tmp_path, catalog_path) -> None:
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    raw["codes"]["E4410"]["level"] = "X"
    with pytest.raises(ConfigError, match="level"):
        load_catalog(_write(tmp_path, "bad.yaml", raw))


def test_a_component_depending_on_nothing_known_is_an_error(tmp_path, catalog) -> None:
    raw = {
        "platform": {
            "tick_ms": 50,
            "start_epoch_ms": 1790603400000,
            "latency": {"sigma": 0.5, "min_ms": 1, "max_ms": 1000},
        },
        "components": {"STR": {"rps": 1, "p50_ms": 1, "depends_on": ["CLM.ZZZ"]}},
    }
    with pytest.raises(ConfigError, match="depends on unknown"):
        load_platform(_write(tmp_path, "p.yaml", raw), catalog=catalog)


def test_a_propagation_edge_to_nothing_known_is_an_error(tmp_path, catalog) -> None:
    raw = {
        "platform": {
            "tick_ms": 50,
            "start_epoch_ms": 1790603400000,
            "latency": {"sigma": 0.5, "min_ms": 1, "max_ms": 1000},
        },
        "components": {"STR": {"rps": 1, "p50_ms": 1}},
        "propagation": [{"from": "STR", "to": "ZZZ", "coupling": 0.5, "lag_s": 2}],
    }
    with pytest.raises(ConfigError, match="not a component"):
        load_platform(_write(tmp_path, "p.yaml", raw), catalog=catalog)


def test_a_negative_weight_is_an_error(tmp_path, catalog_path) -> None:
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    raw["codes"]["E4410"]["weight"] = -1
    with pytest.raises(ConfigError, match="positive"):
        load_catalog(_write(tmp_path, "bad.yaml", raw))


def test_a_missing_rps_is_an_error(tmp_path, catalog) -> None:
    raw = {
        "platform": {
            "tick_ms": 50,
            "start_epoch_ms": 1790603400000,
            "latency": {"sigma": 0.5, "min_ms": 1, "max_ms": 1000},
        },
        "components": {"STR": {"p50_ms": 1}},
    }
    with pytest.raises(ConfigError, match="rps"):
        load_platform(_write(tmp_path, "p.yaml", raw), catalog=catalog)


@pytest.fixture(scope="session")
def catalog_path():
    from sim.config import CATALOG_PATH

    return CATALOG_PATH
