"""Traffic generator for the simulated Medicaid claims platform.

Implements docs/sentinel-plan.md sections 4 and 6.1:

- one asyncio task per component (20 of them), one writer task, one event loop
- a single writer appends whole lines to platform.log, so lines never interleave
- error draws are binomial from the current rate, with jitter and a daily multiplier
- CLM.EDT emits a denial-code mix (W4101-W4105) that the drift detector watches
- dependency propagation with lag, per section 4.6
- a virtual clock, so --speed N runs faster than real time
- --rps-scale K multiplies volume, --dump N writes N lines to a file for offline replay

The fault layer is supplied by sim.faults and read here through ``FaultStore``. No fault
state is ever written to the log: the generator only draws a different distribution.

Usage:
    python3.12 -m sim.generator --seed 7 --seconds 60
    python3.12 -m sim.generator --seed 7 --dump 200000 --out dump.log
    python3.12 -m sim.generator --seed 7 --api            # also serve the control API
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import random
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from sim.clock import ManualClock, VirtualClock
from sim.config import (
    SUCCESS_CODE,
    Catalog,
    ComponentConfig,
    Platform,
    load_all,
)
from sim.faults import FaultStore
from sim.line_format import LogFields, format_line

DEFAULT_LOG = Path("platform.log")
DEFAULT_TRUTH = Path("ground_truth.jsonl")
# The writer drains this many lines per pass. Whole lines are joined into one write, so a
# crash can truncate the file but can never interleave two lines.
WRITE_BATCH = 4096
# Real seconds to sleep when the queue is full. Backpressure, not loss: the generator
# must not silently drop traffic.
QUEUE_MAX = 65536
# Recent trace ids per component, offered to downstream components so a request chain
# shares one id. Bounded, because an unbounded pool would make a single trace live forever.
TRACE_POOL = 64
CPT_CODES = ("97110", "99213", "99214", "85025", "93000", "80053", "36415", "90834", "23340")
STATES = ("AL", "AZ", "CA", "FL", "GA", "IL", "MI", "NC", "OH", "PA", "TX", "WA")


@dataclass(slots=True)
class GeneratorStats:
    """Counters the tests and the console read. Not written to the log."""

    lines: int = 0
    errors: int = 0
    warnings: int = 0
    info: int = 0
    ticks: int = 0
    by_component: dict[str, int] = field(default_factory=dict)
    by_code: dict[str, int] = field(default_factory=dict)

    def record(self, component: str, code: str, level: str) -> None:
        self.lines += 1
        self.by_component[component] = self.by_component.get(component, 0) + 1
        self.by_code[code] = self.by_code.get(code, 0) + 1
        if level == "E":
            self.errors += 1
        elif level == "W":
            self.warnings += 1
        else:
            self.info += 1


@dataclass(slots=True)
class ComponentRunner:
    """Per-component state: its RNG, its id counters and its rate history.

    One of these drives one asyncio task. The RNG is a private ``random.Random`` seeded
    from the run seed and the component code, so two components never share a stream and
    ``--seed`` reproduces output exactly (AGENTS.md, determinism).
    """

    key: str
    cfg: ComponentConfig
    rng: random.Random
    catalog: Catalog
    platform: Platform
    latency_mult: float = 1.0
    id_counter: int = 0
    # Ring of the last 60 s of effective error rate, one sample per virtual second, used
    # to apply section 4.6 propagation lag.
    rate_history: list[tuple[int, float]] = field(default_factory=list)
    _credit: float = 0.0
    _warn_credit: float = 0.0

    def history_at(self, second: int, lag_s: float) -> float:
        """Effective error rate of this component ``lag_s`` seconds ago.

        Returns the configured baseline when history does not reach back that far, so a
        downstream component never sees a phantom spike at the start of a run.
        """
        wanted = second - int(math.ceil(lag_s))
        for sec, rate in reversed(self.rate_history):
            if sec == wanted:
                return rate
        return self.cfg.err


@dataclass(slots=True)
class GeneratorOptions:
    """Everything the CLI can set, in one place so tests can build it directly."""

    log_path: Path = DEFAULT_LOG
    truth_path: Path | None = DEFAULT_TRUTH
    seed: int = 1
    speed: float = 1.0
    rps_scale: float = 1.0
    duration_s: float | None = None
    dump_lines: int | None = None
    dump_path: Path | None = None
    # Use the manual clock in live mode too. Only tests set this; it makes the event-time
    # base a pure function of the tick index so a single tick can be inspected in isolation.
    force_manual_clock: bool = False
    # None means "use platform.yaml's demo_start_hour_utc"; 0 pins the raw start epoch.
    start_hour_utc: int | None = None
    daily: bool = True
    tick_ms: int | None = None
    serve_api: bool = False
    api_port: int = 8077
    api_token: str | None = None
    trace_reuse: float = 0.7


class Generator:
    """The platform simulator: 20 component tasks feeding one writer task."""

    def __init__(
        self,
        options: GeneratorOptions,
        catalog: Catalog | None = None,
        platform: Platform | None = None,
    ) -> None:
        self.options = options
        self.catalog, self.platform = (catalog, platform) if catalog and platform else load_all()
        self.tick_ms = options.tick_ms or self.platform.tick_ms
        self.stats = GeneratorStats()

        start_epoch_ms = self._start_epoch_ms()
        self.manual: ManualClock | None = None
        self.clock: VirtualClock
        if options.dump_lines is not None or options.force_manual_clock:
            # Offline mode: event time is a pure function of the tick index, so --seed
            # gives byte-identical output regardless of how fast the machine runs. Tests
            # step the same manual clock to inspect individual ticks.
            self.manual = ManualClock(start_epoch_ms)
            self.clock = self.manual
        else:
            self.clock = VirtualClock(start_epoch_ms, speed=options.speed)

        self.store = FaultStore(
            self.catalog,
            self.platform,
            truth_path=options.truth_path,
            token=options.api_token,
            time_source=self.clock.now_ms,
        )
        self.runners: dict[str, ComponentRunner] = {}
        for code, cfg in self.platform.components.items():
            self.runners[code] = ComponentRunner(
                key=f"{cfg.service}.{code}",
                cfg=cfg,
                rng=random.Random(f"{options.seed}:{code}"),
                catalog=self.catalog,
                platform=self.platform,
            )
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._recent_traces: dict[str, list[str]] = {}
        self._stop = asyncio.Event()
        self._written = 0
        self._file = None
        self._start_wall = time.monotonic()
        # When set, emitted lines go to this callable instead of the queue. Set for offline
        # dumps and by tests; None in live mode, where the writer task is the only consumer.
        self.sink: Callable[[str], None] | None = None

    def _start_epoch_ms(self) -> int:
        """Start the virtual clock, by default at the business-hours demo hour.

        platform.yaml's start_epoch_ms is the plan's example epoch (02:30 UTC), which sits
        in the overnight band of the daily curve and would make the demo open on a trickle
        of traffic. --start-hour-utc 0 pins it back to the raw epoch when that is wanted.
        """
        base = self.platform.start_epoch_ms
        if self.options.start_hour_utc is not None:
            hour = self.options.start_hour_utc
        else:
            hour = self.platform.demo_start_hour_utc
        if hour is None:
            return base
        if not 0 <= hour <= 23:
            raise ValueError(f"start hour must be 0-23, got {hour}")
        day_ms = 86_400_000
        day = base // day_ms
        return day * day_ms + hour * 3_600_000

    def runner(self, code: str) -> ComponentRunner:
        return self.runners[code]

    # -- line construction ------------------------------------------------

    def _trace_id(self, runner: ComponentRunner) -> str:
        """A trace id for one event, shared along a request chain (section 5, byte 37-44).

        A component that depends on an upstream usually reuses a trace the upstream used
        moments ago, so the log looks like one request fanning out through several
        components. That is what the incident engine groups on when it sees several
        components alerting on the same trace. With trace_reuse at 0 every event gets a
        fresh id, which is the degenerate case the incident engine must also handle.
        """
        trace: str | None = None
        if runner.cfg.depends_on and self.options.trace_reuse > 0.0:
            if runner.rng.random() < self.options.trace_reuse:
                parent = runner.cfg.depends_on[runner.rng.randrange(len(runner.cfg.depends_on))]
                pool = self._recent_traces.get(parent)
                if pool:
                    trace = pool[runner.rng.randrange(len(pool))]
        if trace is None:
            trace = f"{runner.rng.getrandbits(32):08x}"
        # Every emitted trace is offered downstream, including a reused one. Without this a
        # component that mostly reuses would never populate its own pool, and the chain
        # would break after one hop: CCH -> MBR -> (nothing to reuse) -> X12.
        pool = self._recent_traces.setdefault(runner.cfg.code, [])
        pool.append(trace)
        if len(pool) > TRACE_POOL:
            del pool[: len(pool) - TRACE_POOL]
        return trace

    def _latency(self, runner: ComponentRunner, level: str, code_spec_latency: float | None) -> int:
        """Log-normal latency (platform.yaml) with a floor per code."""
        base = runner.cfg.p50_ms * runner.latency_mult
        mu = math.log(max(base, 0.5))
        value = runner.rng.lognormvariate(mu, self.platform.latency_sigma)
        if code_spec_latency is not None:
            value = max(value, code_spec_latency)
        latency = int(min(max(value, self.platform.latency_min_ms), self.platform.latency_max_ms))
        if level in ("E", "F") and latency < 1:
            latency = 1
        return latency

    def _render(self, template: str, runner: ComponentRunner, latency_ms: int) -> str:
        runner.id_counter += 1
        n = runner.id_counter
        values = {
            "n": str(n),
            "m": str(max(n - runner.rng.randrange(1, 50), 1)),
            "k": str(runner.rng.randrange(1, 40)),
            "ms": str(latency_ms),
            "cpt": runner.rng.choice(CPT_CODES),
            "npi": str(runner.rng.randrange(10_000_000_000, 19_999_999_999)),
            "state": runner.rng.choice(STATES),
            "date": time.strftime("%y%m%d", time.gmtime(self.clock.now_ms() // 1000)),
            "yy": time.strftime("%y", time.gmtime(self.clock.now_ms() // 1000)),
            "nn": f"{runner.rng.randrange(10, 100)}",
            "a": str(runner.rng.randrange(1, 254)),
            "b": str(runner.rng.randrange(1, 254)),
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError) as exc:
            raise ValueError(f"template {template!r} has an unknown placeholder: {exc}") from exc

    def _line_for(
        self,
        runner: ComponentRunner,
        level: str,
        code: str,
        message: str,
        latency_ms: int,
    ) -> str:
        fields = LogFields(
            ts_ms=self.clock.now_ms(),
            level=level,
            service=runner.cfg.service,
            component=runner.cfg.code,
            code=code,
            latency_ms=latency_ms,
            trace_id=self._trace_id(runner),
            message=message,
        )
        line = format_line(fields)
        self.stats.record(runner.cfg.code, code, level)
        return line

    def _emit(
        self,
        runner: ComponentRunner,
        level: str,
        code: str,
        template: str,
        code_latency: float | None = None,
    ) -> None:
        latency = self._latency(runner, level, code_latency)
        message = self._render(template, runner, latency)
        line = self._line_for(runner, level, code, message, latency)
        if self.sink is not None:
            # A caller-supplied sink owns the output: offline dumps write straight to the
            # file so a multi-million-line dump need not fit in the bounded queue, and
            # tests collect in memory rather than blocking on a queue nobody is draining.
            self.sink(line)
        else:
            self.queue.put_nowait(line)

    # -- rate computation -------------------------------------------------

    def daily_multipliers(self, at_ms: int) -> tuple[float, float]:
        """Volume and error-rate multipliers for the hour of day (platform.yaml curve)."""
        if not self.options.daily or not self.platform.daily_enabled:
            return 1.0, 1.0
        return self.platform.daily_multipliers(self.clock.daily_curve_slot(at_ms))

    def effective_error_rate(self, code: str, at_ms: int) -> float:
        """Error rate for a component: baseline x daily x faults x propagation.

        Section 4.6: ``downstream = base + coupling * max(0, upstream - upstream_base)``
        applied after the edge's lag. A fault override replaces the whole rate rather than
        adding to it, so a slider at 25% is exactly 25%.
        """
        runner = self.runners[code]
        vol_mult, err_mult = self.daily_multipliers(at_ms)
        rate = runner.cfg.err * err_mult
        for edge in self.platform.propagation:
            if edge.dst != code:
                continue
            upstream = self.runners.get(edge.src.split(".")[-1])
            if upstream is None:
                continue
            lagged = upstream.history_at(at_ms // 1000, edge.lag_s)
            excess = lagged - upstream.cfg.err
            if excess > 0.0:
                rate += edge.coupling * excess
        rate = min(rate, 1.0)
        # An override is applied last, so a slider at 11% is exactly 11% even in the middle
        # of a cascade. Reading it before propagation would leave the console showing a
        # number the generator is not actually drawing.
        if self.store.known_key(runner.key):
            override = self.store.effective_rate(runner.key, at_ms)
            if override is not None:
                rate = override
        return rate

    def propagation_latency_mult(self, code: str, at_ms: int) -> float:
        """Latency degradation follows the same edges with a milder coupling (platform.yaml)."""
        cfg = self.platform.latency_propagation
        if not cfg:
            return 1.0
        cap = float(cfg.get("cap_multiplier", 6.0))
        lag = float(cfg.get("lag_s", 2.0))
        coupling = float(cfg.get("coupling", 0.35))
        mult = 1.0
        for edge in self.platform.propagation:
            if edge.dst != code:
                continue
            upstream = self.runners.get(edge.src.split(".")[-1])
            if upstream is None:
                continue
            lagged = upstream.history_at(at_ms // 1000, lag)
            if lagged > upstream.cfg.err:
                mult += coupling * min(lagged - upstream.cfg.err, 0.5)
        return min(mult, cap)

    def _record_history(self, runner: ComponentRunner, at_ms: int, rate: float) -> None:
        second = at_ms // 1000
        history = runner.rate_history
        if history and history[-1][0] == second:
            history[-1] = (second, max(history[-1][1], rate))
        else:
            history.append((second, rate))
        cutoff = second - 120
        while history and history[0][0] < cutoff:
            history.pop(0)

    def _code_weights(
        self, runner: ComponentRunner, level: str, at_ms: int
    ) -> list[tuple[str, float, float | None, str]]:
        """Pick (code, weight, latency, template) for the next error or warning line.

        Catalog weights are the default. A mix fault overrides them by code name, which
        is how bad_rule_deploy shifts W4103 from 30% to 55% while the error rate stays
        flat (section 6.3).
        """
        component = runner.cfg.code
        if level == "W":
            if runner.cfg.denials:
                spec_by_code = {c: self.catalog.codes[c] for c in runner.cfg.denials}
                spec_by_code = {k: v for k, v in spec_by_code.items() if k in self.catalog.codes}
            else:
                spec_by_code = {c.code: c for c in self.catalog.warn_codes.get(component, [])}
        else:
            spec_by_code = {c.code: c for c in self.catalog.error_codes.get(component, [])}
        if not spec_by_code:
            return []

        override = self.store.code_weights(runner.key, at_ms) if self.store.known_key(runner.key) else None
        if override:
            spec_by_code = {k: v for k, v in spec_by_code.items() if k in override} or spec_by_code
        weights = [
            (code, float(override.get(code, spec.weight)) if override else spec.weight,
             spec.latency_ms, spec.template)
            for code, spec in sorted(spec_by_code.items())
        ]
        return [w for w in weights if w[1] > 0.0]

    def _weighted_pick(
        self, runner: ComponentRunner, options: Sequence[tuple[str, float, float, str]]
    ) -> tuple[str, float, str]:
        total = sum(opt[1] for opt in options)
        target = runner.rng.random() * total
        acc = 0.0
        for code, weight, latency, template in options:
            acc += weight
            if target < acc:
                return code, latency, template
        code, weight, latency, template = options[-1]
        return code, latency, template

    # -- the tick ---------------------------------------------------------

    async def component_tick(self, code: str, budget: int | None = None) -> None:
        """One scheduling tick for one component.

        Volume is carried as a fractional credit so a 4 rps component emits on the tick
        boundaries it should. Errors are a single binomial draw from the current rate,
        then the drawn positions are shuffled, so the number of error lines is exactly
        Binomial(volume, rate) (section 6.1). ``budget`` caps the lines emitted this tick,
        which is how ``--dump N`` lands on exactly N lines.
        """
        runner = self.runners[code]
        at_ms = self.clock.now_ms()

        if self.store.is_silent(runner.key, at_ms):
            self._record_history(runner, at_ms, self.effective_error_rate(code, at_ms))
            return

        vol_mult, _ = self.daily_multipliers(at_ms)
        if self.store.known_key(runner.key):
            vol_mult *= self.store.effective_volume(runner.key, at_ms)
        base_rps = runner.cfg.rps * self.options.rps_scale * vol_mult
        # Jitter is a small multiplicative wobble, applied per tick, so a component with
        # a tiny rps does not get a bang-bang volume.
        jitter = 1.0 + runner.rng.uniform(-self.platform.jitter, self.platform.jitter)
        rate_per_tick = base_rps * (self.tick_ms / 1000.0) * max(jitter, 0.1)
        runner._credit += rate_per_tick
        volume = int(runner._credit)
        runner._credit -= volume
        if budget is not None:
            if budget <= 0:
                volume = 0
            elif volume > budget:
                # Give back the credit we will not use, so truncating the last tick of a
                # dump does not distort the next component's draw.
                runner._credit += volume - budget
                volume = budget

        if volume > 0:
            rate = self.effective_error_rate(code, at_ms)
            n_errors = runner.rng.binomialvariate(volume, rate)
            n_warns = (
                runner.rng.binomialvariate(volume, runner.cfg.warn) if runner.cfg.warn > 0.0 else 0
            )
            # Draw the error and warning positions first, then shuffle the whole tick, so
            # an error is as likely to land in any slot and the mix stays unbiased.
            kinds = ["E"] * n_errors + ["W"] * n_warns
            kinds += ["I"] * (volume - n_errors - n_warns)
            runner.rng.shuffle(kinds)

            runner.latency_mult = self.propagation_latency_mult(code, at_ms)
            error_options = self._code_weights(runner, "E", at_ms)
            warn_options = self._code_weights(runner, "W", at_ms)
            success = self.catalog.components[code].success_template

            for kind in kinds:
                if kind == "E" and error_options:
                    err_code, err_lat, err_tpl = self._weighted_pick(runner, error_options)
                    self._emit(runner, "E", err_code, err_tpl, err_lat)
                elif kind == "W" and warn_options:
                    warn_code, warn_lat, warn_tpl = self._weighted_pick(runner, warn_options)
                    self._emit(runner, "W", warn_code, warn_tpl, warn_lat)
                else:
                    self._emit(runner, "I", SUCCESS_CODE, success)

        self._record_history(runner, at_ms, self.effective_error_rate(code, at_ms))
        self.stats.ticks += 1

    async def component_loop(self, code: str) -> None:
        """Run one component until the stop event fires.

        Ticks are scheduled against a wall-clock deadline, not by sleeping a fixed
        interval. asyncio.sleep always overshoots by the loop's own scheduling latency, so
        a naive ``sleep(tick)`` loop quietly runs at well under the intended rate; chasing
        the deadline keeps real-time volume at the configured rps. The generator's volume
        credit absorbs whatever residual jitter there is.
        """
        tick_s = self.tick_ms / 1000.0 / max(self.clock.speed, 1e-9)
        deadline = time.monotonic()
        while not self._stop.is_set():
            deadline += tick_s
            if self.manual is not None:
                self.manual.advance_to(self.clock.now_ms() + self.tick_ms)
            await self.component_tick(code)
            if self.manual is None:
                await asyncio.sleep(max(deadline - time.monotonic(), 0.0))

    async def writer_loop(self) -> None:
        """The single writer. Whole lines, joined per batch, so nothing interleaves."""
        assert self._file is not None
        while True:
            if self._stop.is_set() and self.queue.empty():
                return
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            batch = [first]
            while len(batch) < WRITE_BATCH:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            payload = "".join(line + "\n" for line in batch).encode("ascii")
            self._file.write(payload)
            self._written += len(batch)

    def stop(self) -> None:
        self._stop.set()

    # -- offline dump -----------------------------------------------------

    def _write_dump_line(self, line: str) -> None:
        assert self._file is not None
        self._file.write((line + "\n").encode("ascii"))

    async def run_dump(self) -> int:
        """Pre-generate ``--dump N`` lines with a manual clock. Byte-identical per seed.

        The component tasks are not used here: event time is a pure function of the tick
        index, so ticks are stepped in catalog order rather than by the scheduler. Same
        seed, same bytes, on any machine. The queue is bypassed and lines are written
        whole, because a dump is written in one pass.
        """
        target = int(self.options.dump_lines or 0)
        path = self.options.dump_path or Path("dump.log")
        codes = list(self.platform.components)
        assert self.manual is not None
        with path.open("wb") as handle:
            self._file = handle
            self.sink = self._write_dump_line
            while self.stats.lines < target:
                before = self.stats.lines
                for code in codes:
                    remaining = target - self.stats.lines
                    if remaining <= 0:
                        break
                    await self.component_tick(code, budget=remaining)
                self.manual.advance_to(self.clock.now_ms() + self.tick_ms)
                if self.stats.lines == before:
                    # Every component rounded its volume down to zero. Advance the clock
                    # again rather than spinning; the next tick will emit.
                    self.manual.advance_to(self.clock.now_ms() + self.tick_ms)
        return self.stats.lines

    # -- realtime ---------------------------------------------------------

    async def run_live(self) -> None:
        """Run the platform until duration_s of event time elapses, or until stopped."""
        self._file = self.options.log_path.open("ab", buffering=0)
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self.component_loop(code), name=f"component:{code}")
            for code in self.platform.components
        ]
        writer = asyncio.create_task(self.writer_loop(), name="writer")
        api_task: asyncio.Task[None] | None = None
        if self.options.serve_api:
            api_task = asyncio.create_task(self._serve_api(), name="control-api")

        try:
            if self.options.duration_s is not None:
                # duration_s is event time, so --speed shortens the wall-clock run.
                wall = (
                    self.options.duration_s / self.clock.speed if self.clock.speed else None
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wall)
                except TimeoutError:
                    self.stop()
            else:
                await self._stop.wait()
        except asyncio.CancelledError:
            self.stop()
            raise
        finally:
            self.stop()
            if api_task is not None:
                api_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await api_task
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await writer
            self._file.close()
            self._file = None

    async def _serve_api(self) -> None:
        import uvicorn

        from sim.control_api import create_app

        config = uvicorn.Config(
            create_app(self.store, self.catalog, self.platform),
            host="127.0.0.1",
            port=self.options.api_port,
            log_level="warning",
        )
        await uvicorn.Server(config).serve()


async def _amain(args: argparse.Namespace) -> int:
    options = GeneratorOptions(
        log_path=Path(args.log),
        truth_path=Path(args.truth) if args.truth else None,
        seed=args.seed,
        speed=args.speed,
        rps_scale=args.rps_scale,
        duration_s=args.seconds,
        dump_lines=args.dump,
        dump_path=Path(args.out) if args.out else None,
        start_hour_utc=args.start_hour_utc,
        daily=not args.no_daily,
        tick_ms=args.tick_ms,
        serve_api=args.api,
        api_port=args.api_port,
        api_token=args.api_token,
    )
    if options.dump_lines is not None and options.rps_scale <= 0:
        raise SystemExit("--rps-scale must be positive")

    generator = Generator(options)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, generator.stop)

    if options.dump_lines is not None:
        emitted = await generator.run_dump()
        print(f"wrote {emitted} lines to {options.dump_path or 'dump.log'}")
        return 0

    print(
        f"writing {options.log_path} "
        f"(seed={options.seed} speed={options.speed} rps-scale={options.rps_scale})"
    )
    await generator.run_live()
    elapsed = time.monotonic() - generator._start_wall
    print(
        f"stopped: {generator.stats.lines} lines, {generator.stats.errors} errors, "
        f"{generator.stats.warnings} warnings in {elapsed:.1f}s wall "
        f"({generator.stats.lines / max(elapsed, 1e-9):.0f} lines/s)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="log file to append to")
    parser.add_argument("--truth", default=str(DEFAULT_TRUTH), help="ground truth file (fault records)")
    parser.add_argument("--seed", type=int, default=1, help="RNG seed; same seed, same output")
    parser.add_argument("--speed", type=float, default=1.0, help="event-time speed factor (0 = manual)")
    parser.add_argument("--rps-scale", type=float, default=1.0, help="multiply all component volume")
    parser.add_argument("--seconds", type=float, default=None, help="run for N seconds of event time")
    parser.add_argument("--dump", type=int, default=None, help="pre-generate N lines and exit")
    parser.add_argument("--out", default=None, help="output file for --dump")
    parser.add_argument(
        "--start-hour-utc",
        type=int,
        default=None,
        help="hour of day to start the virtual clock at (default: platform.yaml demo hour)",
    )
    parser.add_argument("--no-daily", action="store_true", help="disable the daily traffic curve")
    parser.add_argument("--tick-ms", type=int, default=None, help="override the component tick")
    parser.add_argument("--api", action="store_true", help="also serve the control API")
    parser.add_argument("--api-port", type=int, default=8077)
    parser.add_argument("--api-token", default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
