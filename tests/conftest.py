"""Shared test fixtures and helpers.

The generator is driven through its own API rather than through a subprocess wherever
possible, so tests can inspect stats and reach into the fault store. Where a real log file
is needed (determinism, format checks) the dump path writes one to tmp_path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from detector import parse_ref
from detector.parse_ref import Vocabulary, build_vocabulary
from sim.config import load_all
from sim.generator import Generator, GeneratorOptions

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def catalog_and_platform() -> tuple[object, object]:
    return load_all()


@pytest.fixture(scope="session")
def catalog():
    cat, _ = load_all()
    return cat


@pytest.fixture(scope="session")
def platform():
    _, plat = load_all()
    return plat


@pytest.fixture(scope="session")
def vocab(catalog) -> Vocabulary:
    """The detector-side vocabulary, built from the same catalog the generator uses."""
    return build_vocabulary(
        {code: {"service": spec.service} for code, spec in catalog.components.items()},
        catalog.codes,
    )


@pytest.fixture(scope="session")
def catalog_vocab() -> Vocabulary:
    """The vocabulary loaded straight from sim/catalog.yaml, the way the detector loads it.

    Distinct from the ``vocab`` fixture on purpose: this one goes through
    ``parse_ref.load_vocabulary``, so the catalog file itself is covered.
    """
    return parse_ref.load_vocabulary()


def make_generator(tmp_path: Path, **overrides: object) -> Generator:
    """A Generator with test defaults: no truth file, no daily curve, seed 1.

    Explicit keyword arguments win over the defaults, so a test can say daily=True.
    """
    settings: dict[str, object] = {
        "log_path": tmp_path / "platform.log",
        "truth_path": None,
        "seed": 1,
        "daily": False,
        "force_manual_clock": True,
    }
    settings.update(overrides)
    generator = Generator(GeneratorOptions(**settings))  # type: ignore[arg-type]
    install_collector(generator)
    return generator


def install_collector(generator) -> list[bytes]:
    """Give a generator an in-memory sink and return the list it appends to.

    Tests step ticks with no writer task running, so emitted lines need somewhere to go
    that is neither the bounded queue (which nobody drains, so it fills and raises
    QueueFull) nor the disk.
    """
    collected: list[bytes] = []
    # The sink receives a str; the log is ASCII, so encoding here keeps the collected lines
    # byte-identical to what the writer would have written.
    generator.sink = lambda line: collected.append(line.encode("ascii"))  # type: ignore[attr-defined]
    return collected


def run_dump(tmp_path: Path, lines: int, name: str = "dump.log", **overrides: object) -> Path:
    """Write a deterministic dump of ``lines`` and return the path."""
    path = tmp_path / name
    generator = make_generator(tmp_path, dump_lines=lines, dump_path=path, **overrides)
    asyncio.run(generator.run_dump())
    return path


def read_lines(path: Path) -> list[bytes]:
    """Every line of a log file, without the newlines."""
    return path.read_bytes().split(b"\n")[:-1]


def run_ticks(generator: Generator, ticks: int = 1, codes: list[str] | None = None) -> None:
    """Advance the manual clock by ``ticks`` for the given components (default: all)."""

    async def go() -> None:
        assert generator.manual is not None
        for _ in range(ticks):
            for code in codes or list(generator.platform.components):
                await generator.component_tick(code)
            generator.manual.advance_to(generator.clock.now_ms() + generator.tick_ms)

    asyncio.run(go())


def collected_lines(generator) -> list[bytes]:
    """Every line this generator has emitted, in order."""
    sink = generator.sink
    assert sink is not None and getattr(sink, "__closure__", None), "no collecting sink"
    for cell in sink.__closure__ or ():
        contents = cell.cell_contents
        if isinstance(contents, list):
            return contents
    raise AssertionError("could not reach the collected lines")


def tick_all(generator, ticks: int = 1) -> None:
    """Run ``ticks`` rounds of every component's tick inside one event loop.

    One tick is ``tick_ms`` of event time. Kept in one loop so the clock advances exactly
    once per round and the rate history lines up with whole seconds.
    """
    assert generator.manual is not None

    async def go() -> None:
        for _ in range(ticks):
            for code in generator.platform.components:
                await generator.component_tick(code)
            generator.manual.advance_to(generator.clock.now_ms() + generator.tick_ms)

    asyncio.run(go())


def settle(generator, seconds: float) -> None:
    """Advance ``seconds`` of event time at the real tick rate."""
    tick_all(generator, max(int(round(seconds * 1000 / generator.tick_ms)), 1))


@pytest.fixture
def tiny_vocab() -> Vocabulary:
    """A two-component vocabulary for parser tests that should not need the real catalog."""
    return Vocabulary(
        keys=("CLM|STR", "CLM|EDT"),
        codes=("00000", "E4410", "E4411", "W4103"),
        services=("CLM",),
    )
