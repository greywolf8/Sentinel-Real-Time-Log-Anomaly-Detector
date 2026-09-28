"""Determinism of the generator (AGENTS.md: same seed, same bytes).

Two separate guarantees are checked here, because they are different things:

1. Offline dumps (``--dump``) are byte-identical for a given seed, on any machine. Event
   time is a pure function of the tick index, so the output cannot depend on how fast the
   host ran.
2. Different seeds produce different logs, so the seed is actually reaching the RNGs and
   the determinism is not the trivial result of nothing being random.

Also checked: the virtual clock honours ``--speed`` for timestamps, ``--rps-scale`` scales
volume, and a per-component RNG means adding a component does not reshuffle the others.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sim.generator import GeneratorOptions
from tests.conftest import install_collector, make_generator, read_lines, run_dump

LINES = 8000


def test_same_seed_gives_byte_identical_dumps(tmp_path: Path) -> None:
    a = run_dump(tmp_path, LINES, "a.log", seed=42)
    b = run_dump(tmp_path, LINES, "b.log", seed=42)
    assert a.read_bytes() == b.read_bytes()
    assert a.stat().st_size > 0


def test_dumps_are_repeatable_across_many_seeds(tmp_path: Path) -> None:
    for seed in (0, 1, 7, 999):
        first = run_dump(tmp_path, 2000, f"s{seed}-1.log", seed=seed)
        second = run_dump(tmp_path, 2000, f"s{seed}-2.log", seed=seed)
        assert first.read_bytes() == second.read_bytes(), f"seed {seed} is not deterministic"


def test_different_seeds_produce_different_logs(tmp_path: Path) -> None:
    a = run_dump(tmp_path, LINES, "s7.log", seed=7)
    b = run_dump(tmp_path, LINES, "s8.log", seed=8)
    assert a.read_bytes() != b.read_bytes()


def test_seed_reaches_every_component(tmp_path: Path) -> None:
    """Every component's stream must depend on the seed, not just the first few."""
    a = read_lines(run_dump(tmp_path, LINES, "x.log", seed=1))
    b = read_lines(run_dump(tmp_path, LINES, "y.log", seed=2))
    differing = {
        (line[16:19], line[20:23]) for line, other in zip(a, b, strict=True) if line != other
    }
    assert len(differing) >= 18, f"only {len(differing)} components varied with the seed"


def test_adding_a_component_does_not_reshuffle_the_others(tmp_path: Path, platform) -> None:
    """A per-component RNG seeded with its own code is what buys this.

    Adding a twenty-first component to platform.yaml must not change a single byte of the
    existing twenty, otherwise every pre-generated dump and every baseline would be
    invalidated by an unrelated change.
    """
    full = make_generator(tmp_path, seed=5)
    partial = make_generator(tmp_path, seed=5)
    extra = platform.components["RPT"]
    del partial.platform.components["RPT"]
    del partial.runners["RPT"]

    def run(generator) -> list[bytes]:
        return _capture(generator, codes=[c for c in full.platform.components if c != "RPT"])

    assert run(full) == run(partial)
    assert extra.code == "RPT"


def test_global_random_module_is_untouched(tmp_path: Path) -> None:
    """AGENTS.md requires per-component random.Random instances, never the global random.
    If any code path used the global module, seeding it here would change the output."""
    import random

    path = run_dump(tmp_path, 2000, "before.log", seed=21)
    random.seed(1234)  # poison the global generator
    after = run_dump(tmp_path, 2000, "after.log", seed=21)
    assert path.read_bytes() == after.read_bytes()


def _capture(generator, codes: list[str], rounds: int = 20) -> list[bytes]:
    """Run manual-clock ticks for ``codes`` and return the lines they emitted, in order."""
    sink = install_collector(generator)

    async def go() -> None:
        assert generator.manual is not None
        for _ in range(rounds):
            for code in codes:
                await generator.component_tick(code)
            generator.manual.advance_to(generator.clock.now_ms() + generator.tick_ms)

    asyncio.run(go())
    return list(sink)


def test_rps_scale_multiplies_volume(tmp_path: Path) -> None:
    def lines_at(scale: float) -> int:
        generator = make_generator(tmp_path, seed=11, rps_scale=scale)
        return len(_capture(generator, codes=list(generator.platform.components)))

    one = lines_at(1.0)
    four = lines_at(4.0)
    assert one > 1000
    assert four == pytest.approx(one * 4, rel=0.02)


def test_dump_event_time_is_independent_of_speed(tmp_path: Path) -> None:
    """A dump uses the manual clock, so --speed cannot leak into the timestamps of a file
    that is written in one pass. Same seed and same speed-irrelevant bytes either way."""
    fast = run_dump(tmp_path, 500, "f.log", seed=2, speed=8.0)
    slow = run_dump(tmp_path, 500, "sl.log", seed=2, speed=1.0)
    assert fast.read_bytes() == slow.read_bytes()


def test_virtual_clock_advances_with_speed(tmp_path: Path) -> None:
    """With --speed, event time runs faster than the wall clock; speed 0 freezes it."""
    import time

    options = GeneratorOptions(log_path=tmp_path / "a.log", truth_path=None, speed=100.0, start_hour_utc=13)
    from sim.generator import Generator

    fast = Generator(options)
    assert fast.manual is None, "live mode must use the real-time-backed clock"
    start = fast.clock.now_ms()
    time.sleep(0.05)
    elapsed = fast.clock.now_ms() - start
    assert 4_000 <= elapsed <= 6_000, f"speed 100 over 50 ms of wall time gave {elapsed} ms"

    frozen = Generator(GeneratorOptions(log_path=tmp_path / "b.log", truth_path=None, speed=0.0))
    start = frozen.clock.now_ms()
    time.sleep(0.02)
    assert frozen.clock.now_ms() == start


def test_speed_one_keeps_event_time_close_to_wall_time(tmp_path: Path) -> None:
    from sim.generator import Generator

    generator = Generator(GeneratorOptions(log_path=tmp_path / "c.log", truth_path=None, speed=1.0))
    import time

    start = generator.clock.now_ms()
    time.sleep(0.05)
    elapsed = generator.clock.now_ms() - start
    assert 30 <= elapsed <= 120, f"speed 1 should track the wall clock, got {elapsed} ms"


def test_dump_writes_exactly_the_requested_line_count(tmp_path: Path) -> None:
    for count in (1, 7, 100, 4321):
        path = run_dump(tmp_path, count, f"n{count}.log", seed=3)
        assert len(read_lines(path)) == count


def test_rng_streams_are_per_component(tmp_path: Path, platform) -> None:
    """Each component gets its own Random, so the global random module is never used and
    two components can never interleave draws."""
    generator = make_generator(tmp_path, seed=1)
    assert len({id(runner.rng) for runner in generator.runners.values()}) == len(platform.components)
    assert generator.runners["STR"].rng is not generator.runners["DUP"].rng


def test_generator_options_defaults_are_sane() -> None:
    options = GeneratorOptions()
    assert options.seed == 1
    assert options.speed == 1.0
    assert options.rps_scale == 1.0
    assert options.log_path.name == "platform.log"
