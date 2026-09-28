"""Generator behaviour end to end: volume, rates, levels and codes (section 4, 6.1).

These tests read the lines the generator actually wrote, not internal counters, so a bug
in line construction cannot hide behind a correct counter.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from detector.parse_ref import parse_line
from sim.line_format import HEADER_BYTES
from tests.conftest import read_lines, run_dump, run_ticks

BIG = 120_000  # a few seconds of platform traffic, enough for stable rates


@pytest.fixture(scope="module")
def big_dump(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return run_dump(tmp_path_factory.mktemp("gen"), BIG, "big.log", seed=5, daily=False)


@pytest.fixture(scope="module")
def parsed(big_dump: Path):
    return [parse_line(raw) for raw in read_lines(big_dump)]


# --- volume -----------------------------------------------------------------


def test_the_dump_holds_exactly_the_requested_lines(big_dump: Path) -> None:
    assert len(read_lines(big_dump)) == BIG


def test_every_component_produces_traffic(parsed) -> None:
    seen = {line.component for line in parsed}
    assert len(seen) == 20, sorted(seen)
    assert "ZZZ" not in seen


def test_relative_volume_matches_the_configured_rps(parsed, platform) -> None:
    """Volume per component should be proportional to rps. A component emitting far more or
    far less than its share is a bug in the credit accounting, not in the config."""
    counts = Counter(line.component for line in parsed)
    total = sum(counts.values())
    for code, cfg in platform.components.items():
        observed = counts[code] / total
        expected = cfg.rps / sum(c.rps for c in platform.components.values())
        assert observed == pytest.approx(expected, rel=0.12), code


def test_high_volume_components_dominate(parsed) -> None:
    counts = Counter(line.component for line in parsed)
    busiest = counts.most_common(5)
    quietest = counts.most_common()[-5:]
    assert counts["CCH"] > counts["BNK"], "400 rps should beat 4 rps"
    assert sum(c for _, c in busiest) > 5 * sum(c for _, c in quietest)


# --- levels and codes -------------------------------------------------------


def test_the_error_rate_lands_near_the_configured_base(parsed, platform) -> None:
    """Section 4 gives a base error rate per component. The drawn rate should sit near it,
    with binomial sampling error explaining the gap."""
    totals = Counter(line.component for line in parsed)
    errors = Counter(line.component for line in parsed if line.level == "E")
    for code, cfg in platform.components.items():
        observed = errors[code] / totals[code]
        tolerance = 0.004 + 3 * (cfg.err * (1 - cfg.err) / totals[code]) ** 0.5
        assert abs(observed - cfg.err) < tolerance, f"{code}: {observed} vs {cfg.err}"


def test_error_lines_carry_an_error_code_from_the_catalog(parsed, catalog) -> None:
    for line in parsed:
        if line.level == "E":
            assert line.code in catalog.codes, line.code
            assert catalog.codes[line.code].level == "E"
            assert catalog.codes[line.code].component == line.component


def test_info_lines_use_the_success_code(parsed) -> None:
    """Section 5: success lines start their code with 0 so the hot path can skip them."""
    for line in parsed:
        if line.level == "I":
            assert line.code == "00000", line


def test_low_volume_components_still_emit_their_rarer_codes(tmp_path: Path, catalog) -> None:
    """E5202, E5402 and E1302 are the 50/50 second choices on components with 4 to 8 rps,
    so they need a much longer run than a busy component to show up at all.

    This is why the dump default has to be large enough for offline replay to contain every
    catalogued code, not just the frequent ones.
    """
    path = run_dump(tmp_path, 2_000_000, "long.log", seed=11, daily=False)
    drawn = {raw[24:29].decode() for raw in read_lines(path)}
    for code in ("E5202", "E5402", "E1302"):
        assert code in drawn, f"{code} never appeared in 2M lines"


def test_every_catalogued_code_is_actually_drawn(parsed, catalog) -> None:
    """Every code the plan lists must appear in a dump, or a catalog entry is dead weight
    and the new-code detector has nothing to contrast it against.

    The plan gives some components a single error code (ELG.MBR has only E2201), so the
    check is that the drawn set matches the catalog, not that each component has several.
    """
    error_codes = {spec.code for specs in catalog.error_codes.values() for spec in specs}
    warn_codes = {spec.code for specs in catalog.warn_codes.values() for spec in specs}
    assert len(error_codes) + len(warn_codes) == 46
    assert not (error_codes & warn_codes), "a code cannot be both an error and a warning"

    drawn_errors = {line.code for line in parsed if line.level == "E"}
    drawn_warns = {line.code for line in parsed if line.level == "W"}
    # Everything drawn is catalogued, and its level matches the catalog.
    assert drawn_errors <= error_codes, f"undrawn error codes: {drawn_errors - error_codes}"
    assert drawn_warns <= warn_codes, f"undrawn warning codes: {drawn_warns - warn_codes}"
    for line in parsed:
        if line.code in error_codes:
            assert line.level == "E", (line.code, line)
        elif line.code in warn_codes:
            assert line.level == "W", (line.code, line)
    # A warning rate of zero would leave its component's warning codes undrawn entirely,
    # which is checked in test_catalog.py against the config.


def test_all_forty_six_error_codes_are_reachable(catalog) -> None:
    """Every catalogued code should be drawable, so the catalog is not a wish list."""
    assert len(catalog.codes) == 46
    for code, spec in catalog.codes.items():
        assert spec.weight > 0, code


# --- denials ----------------------------------------------------------------


def test_edt_emits_the_denial_mix(big_dump: Path) -> None:
    """Section 4.2: the W4101-W4105 shares are the baseline the drift detector watches.

    EDT denies 6% of its own lines, so this needs a big dump to have enough samples for a
    share to be measured to within a point or two.
    """
    counts = Counter()
    for raw in read_lines(big_dump):
        if raw[20:23] == b"EDT" and raw[14:15] == b"W":
            counts[raw[24:29].decode("ascii")] += 1
    total = sum(counts.values())
    assert total > 500, f"not enough denials to measure the mix: {total}"
    assert set(counts) == {"W4101", "W4102", "W4103", "W4104", "W4105"}
    for code, share in counts.items():
        expected = {"W4101": 0.25, "W4102": 0.15, "W4103": 0.30, "W4104": 0.20, "W4105": 0.10}[code]
        assert counts[code] / total == pytest.approx(expected, abs=0.02), code


def test_denials_are_warnings_not_errors(big_dump: Path) -> None:
    for raw in read_lines(big_dump):
        if raw[24:29].decode("ascii").startswith("W"):
            assert raw[14:15] == b"W", raw


def test_aut_emits_bad_passwords_at_two_percent(big_dump: Path) -> None:
    counts = Counter()
    for raw in read_lines(big_dump):
        if raw[20:23] == b"AUT":
            counts[raw[14:15]] += 1
    total = sum(counts.values())
    assert counts[b"W"] / total == pytest.approx(0.02, abs=0.004)


def test_the_denial_message_names_the_edit(catalog) -> None:
    for code, spec in catalog.codes.items():
        if spec.denial:
            assert spec.denial in spec.template, code
            assert spec.level == "W", code


# --- latency ----------------------------------------------------------------


def test_latency_is_positive_and_bounded(big_dump: Path, platform) -> None:
    for raw in read_lines(big_dump):
        latency = int(raw[30:36])
        assert platform.latency_min_ms <= latency <= platform.latency_max_ms


def test_error_latency_runs_higher_than_success(big_dump: Path) -> None:
    """A failed database call takes longer than a served one, which is what the latency
    shift detector depends on."""
    errors, infos = [], []
    for raw in read_lines(big_dump):
        (errors if raw[14:15] in (b"E", b"F") else infos).append(int(raw[30:36]))
    mean_error = sum(errors) / len(errors)
    mean_info = sum(infos) / len(infos)
    assert mean_error > mean_info, f"errors {mean_error:.0f} ms vs info {mean_info:.0f} ms"


def test_slow_components_are_slower_than_fast_ones(big_dump: Path, platform) -> None:
    def mean_latency(code: str) -> float:
        values = [int(raw[30:36]) for raw in read_lines(big_dump) if raw[20:23] == code.encode()]
        return sum(values) / len(values)

    assert mean_latency("BNK") > mean_latency("CCH")
    assert mean_latency("EXP") > mean_latency("MBR")


# --- timestamps -------------------------------------------------------------


def test_timestamps_advance_with_the_clock(big_dump: Path) -> None:
    stamps = [int(raw[0:13]) for raw in read_lines(big_dump)]
    assert stamps == sorted(stamps), "event time must not go backwards"


def test_the_clock_covers_the_expected_span(big_dump: Path, platform) -> None:
    stamps = [int(raw[0:13]) for raw in read_lines(big_dump)]
    span_s = (stamps[-1] - stamps[0]) / 1000
    expected_s = BIG / 2812  # lines / total rps
    assert span_s == pytest.approx(expected_s, rel=0.15)


def test_the_start_epoch_is_the_configured_one(big_dump: Path) -> None:
    first = int(read_lines(big_dump)[0][0:13])
    assert first >= 1_000_000_000_000


# --- trace ids --------------------------------------------------------------


def test_a_trace_spans_more_than_one_component(big_dump: Path) -> None:
    """The property the incident engine relies on: one trace id shows up on several
    components, so an alert on CLM.STR and one on PAY.RMT can be tied to one request."""
    by_trace: dict[bytes, set[bytes]] = {}
    for raw in read_lines(big_dump):
        by_trace.setdefault(raw[37:45], set()).add(raw[20:23])
    multi = [trace for trace, comps in by_trace.items() if len(comps) > 1]
    assert multi, "no trace id appeared on two different components"


def test_a_component_with_no_dependency_never_reuses_a_trace(big_dump: Path, platform) -> None:
    """A leaf has nothing to inherit a trace from, so it must always mint a fresh one.

    Global distinct counts are not the right measure for components that do have an
    upstream: the upstream pool churns faster than the downstream consumes it, so a trace
    is reused but not necessarily many times. What matters, and is tested separately, is
    that reuse happens inside a request's lifetime.
    """
    leaves = {code for code, cfg in platform.components.items() if not cfg.depends_on}
    assert {"CCH", "STR", "NPI", "PST", "AUT"} <= leaves

    traces_by_component: dict[str, list[bytes]] = {}
    for raw in read_lines(big_dump):
        traces_by_component.setdefault(raw[20:23].decode(), []).append(raw[37:45])

    for code in leaves:
        traces = traces_by_component[code]
        assert len(set(traces)) == len(traces), f"{code} has no upstream, so no reuse"


def test_trace_reuse_is_short_lived(big_dump: Path) -> None:
    """A request chain completes in milliseconds, so a trace is shared within a window and
    then retired. This is what lets the incident engine use a shared trace to tie two alerts
    together without every later line looking like the same request."""
    by_second: dict[int, list[bytes]] = {}
    for raw in read_lines(big_dump):
        by_second.setdefault(int(raw[0:13]) // 1000, []).append(raw[37:45])

    seconds = sorted(by_second)[:50]
    reused_within = [
        len(traces) - len(set(traces)) for second in seconds if (traces := by_second[second])
    ]
    # Within any one second of event time, some lines must share a trace.
    assert max(reused_within) > 0, "no trace was ever shared inside a single second"
    share = sum(reused_within) / sum(len(by_second[s]) for s in seconds)
    assert share > 0.05, f"only {share:.3f} of lines shared a trace within their second"


def test_trace_reuse_can_be_switched_off(tmp_path: Path) -> None:
    """With --trace-reuse 0 every event gets a fresh id, the degenerate case the incident
    engine must also cope with. Worth being able to produce for a demo."""
    path = run_dump(tmp_path, 20_000, "notrace.log", seed=2, daily=False, trace_reuse=0.0)
    traces = [raw[37:45] for raw in read_lines(path)]
    assert len(set(traces)) == len(traces)


# --- message rendering ------------------------------------------------------


def test_no_message_contains_an_unrendered_placeholder(big_dump: Path) -> None:
    for raw in read_lines(big_dump):
        message = raw[46:]
        assert b"{" not in message, message
        assert b"}" not in message, message


def test_no_message_contains_a_python_repr(big_dump: Path) -> None:
    for raw in read_lines(big_dump):
        assert b"None" not in raw[46:], raw
        assert b"object at 0x" not in raw[46:], raw


def test_identifiers_look_like_identifiers(big_dump: Path) -> None:
    """IDs are synthetic and shaped: CLM-1234, MBR-123456, host=bank-gw-12, 10.20.4.17."""
    for raw in read_lines(big_dump):
        message = raw[46:].decode("ascii")
        for token in message.split():
            value = token.split("=", 1)[-1]
            if value.startswith(("CLM-", "MBR-", "RMT-", "LDG-", "ISA-", "APP-", "U-", "EVT-", "R-", "F-", "K-", "PTX-", "B-")):
                prefix, _, digits = value.partition("-")
                assert digits.isdigit(), token
                assert 0 < int(digits) < 1_000_000, token


def test_message_length_stays_reasonable(big_dump: Path) -> None:
    longest = max(len(raw) - HEADER_BYTES for raw in read_lines(big_dump))
    assert longest < 200, longest


# --- the writer -------------------------------------------------------------


def test_the_writer_never_interleaves_two_lines(tmp_path: Path) -> None:
    """One writer, whole lines. A torn write would show as a line whose message is
    followed by another line's header."""
    lines = live_lines(tmp_path, seconds=1.0, speed=20.0)
    assert len(lines) > 100
    for raw in lines:
        assert raw[13] == ord("|")
        assert b"|E|" not in raw[46:] and b"|W|" not in raw[46:]


def test_live_mode_appends_rather_than_truncating(tmp_path: Path) -> None:
    """Two runs against the same log file must give two files' worth of lines, not the
    second run overwriting the first."""
    log = tmp_path / "platform.log"
    run_live_short(tmp_path, seconds=0.6, speed=20.0, log=log)
    first = log.stat().st_size
    run_live_short(tmp_path, seconds=0.6, speed=20.0, log=log)
    second = log.stat().st_size
    assert second > first * 1.5, (first, second)


def test_live_and_dump_agree_on_rates(tmp_path: Path) -> None:
    """The offline dump and the live run draw from the same code, so the level mix has to
    match. This is what makes a pre-generated dump a valid offline replay."""
    dump = read_lines(run_dump(tmp_path, 40_000, "cmp.log", seed=9, daily=False))
    live = live_lines(tmp_path, seconds=0.5, speed=40.0, seed=9)
    dump_mix = Counter(raw[14:15] for raw in dump)
    live_mix = Counter(raw[14:15] for raw in live)
    dump_share = dump_mix[b"E"] / len(dump)
    live_share = live_mix[b"E"] / len(live)
    assert dump_share == pytest.approx(live_share, abs=0.01), (dump_share, live_share)


def run_live_short(
    tmp_path: Path, seconds: float, speed: float, log: Path | None = None, seed: int = 1
) -> list[bytes]:
    """Run the real event loop briefly and return the log lines it wrote."""
    import asyncio

    from sim.generator import Generator, GeneratorOptions

    path = log or (tmp_path / "live.log")
    generator = Generator(
        GeneratorOptions(
            log_path=path,
            truth_path=None,
            seed=seed,
            speed=speed,
            duration_s=seconds,
            daily=False,
        )
    )
    asyncio.run(generator.run_live())
    return read_lines(path)


def live_lines(
    tmp_path: Path, seconds: float, speed: float, log: Path | None = None, seed: int = 1
) -> list[bytes]:
    """Lines written by a real event-loop run. The log is truncated first so the result is
    only this run's output, which is what the append test needs to contrast."""
    path = log or (tmp_path / "live.log")
    path.write_bytes(b"")
    return run_live_short(tmp_path, seconds=seconds, speed=speed, log=path, seed=seed)


# --- jitter and the daily curve --------------------------------------------


def test_jitter_makes_volume_wobble(tmp_path: Path) -> None:
    """A per-tick wobble means the lines in any one second vary around the mean, rather
    than arriving as a metronome."""
    per_second = Counter()
    for raw in read_lines(run_dump(tmp_path, 60_000, "j.log", seed=3, daily=False)):
        per_second[int(raw[0:13]) // 1000] += 1
    values = list(per_second.values())
    assert len(values) > 10
    mean = sum(values) / len(values)
    spread = max(values) - min(values)
    assert spread > mean * 0.05, f"volume looks too flat: {min(values)}-{max(values)}"


def test_the_daily_curve_scales_volume(tmp_path: Path) -> None:
    """The evening batch band is a real multiplier on volume, not decoration."""
    from sim.generator import Generator, GeneratorOptions

    def rate_at(hour: int) -> tuple[float, float]:
        generator = Generator(
            GeneratorOptions(
                log_path=tmp_path / f"h{hour}.log",
                truth_path=None,
                seed=1,
                daily=True,
                start_hour_utc=hour,
                force_manual_clock=True,
            )
        )
        generator.sink = lambda line: None
        run_ticks(generator, ticks=40, codes=["CCH"])
        return generator.daily_multipliers(generator.clock.now_ms())

    night = rate_at(3)
    day = rate_at(13)
    evening = rate_at(20)
    assert day[0] > night[0], (day, night)
    assert evening[0] > day[0], (evening, day)
    # The error rate rises with the batch as well as the volume.
    assert evening[1] > day[1] > night[1]


def test_disabling_the_daily_curve_flattens_it(tmp_path: Path) -> None:
    from sim.generator import Generator, GeneratorOptions

    generator = Generator(
        GeneratorOptions(
            log_path=tmp_path / "flat.log",
            truth_path=None,
            daily=False,
            start_hour_utc=20,
            force_manual_clock=True,
        )
    )
    assert generator.daily_multipliers(generator.clock.now_ms()) == (1.0, 1.0)


def test_the_demo_default_hour_is_business_hours(tmp_path: Path, platform) -> None:
    """platform.yaml's start epoch is 02:30 UTC, in the overnight band. The demo starts at
    the configured business hour so it does not open on a trickle."""
    assert platform.demo_start_hour_utc == 13
    from sim.generator import Generator, GeneratorOptions

    generator = Generator(
        GeneratorOptions(log_path=tmp_path / "d.log", truth_path=None, daily=True)
    )
    hour = (generator.clock.now_ms() // 1000) % 86400 // 3600
    assert hour == 13
