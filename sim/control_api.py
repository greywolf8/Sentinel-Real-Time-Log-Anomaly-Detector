"""Control API for the Fault Console (docs/sentinel-plan.md section 6.2).

Endpoints, exactly as the plan lists them:

    GET  /rates
    PUT  /rates/{svc}/{cmp}        {rate, ramp_s?, hold_s?}
    PUT  /mix/{svc}/{cmp}          {code_weights, hold_s?}
    POST /silence/{svc}/{cmp}      {hold_s}
    POST /scenarios/{name}/run
    POST /reset
    GET  /truth

Rules enforced here:

- Bound to localhost. A token is required only when the API is deployed somewhere other
  than the developer's machine, and it is checked on every call.
- Every control action appends a record to ground_truth.jsonl. That file is the
  evaluation oracle; the detector must never read it.
- No endpoint produces a log line. The generator reads the fault store and draws a
  different distribution; there is no marker, hint or annotation in platform.log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from sim.config import Catalog, Platform, load_all
from sim.faults import FaultStore
from sim.generator import DEFAULT_TRUTH, Generator, GeneratorOptions

LOCALHOST = "127.0.0.1"
DEFAULT_PORT = 8077


class RateChange(BaseModel):
    """Body of PUT /rates/{svc}/{cmp}. ``rate`` is the new error rate, 0.0 to 1.0."""

    rate: float = Field(ge=0.0, le=1.0)
    ramp_s: float | None = Field(default=None, ge=0.0)
    hold_s: float | None = Field(default=None, gt=0.0)
    scenario: str | None = None


class MixChange(BaseModel):
    """Body of PUT /mix/{svc}/{cmp}. Weights are re-normalised by the generator."""

    code_weights: dict[str, float]
    hold_s: float | None = Field(default=None, gt=0.0)
    scenario: str | None = None


class SilenceRequest(BaseModel):
    """Body of POST /silence/{svc}/{cmp}."""

    hold_s: float = Field(gt=0.0)
    scenario: str | None = None


class VolumeChange(BaseModel):
    """Body of PUT /volume/{svc}/{cmp}. Volume only; the error rate is untouched."""

    multiplier: float = Field(gt=0.0)
    hold_s: float | None = Field(default=None, gt=0.0)
    scenario: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """One control call inside a scenario, so a scenario is data, not code."""

    kind: str
    key: str
    rate: float | None = None
    ramp_s: float | None = None
    hold_s: float | None = None
    multiplier: float | None = None
    code_weights: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named sequence of control calls (docs/sentinel-plan.md section 6.3)."""

    name: str
    description: str
    steps: tuple[ScenarioStep, ...]
    warmup_s: float = 0.0
    duration_s: float = 120.0


def _k(key: str) -> str:
    return key


SCENARIOS: dict[str, Scenario] = {
    "db_timeout_spike": Scenario(
        name="db_timeout_spike",
        description="CLM.STR error rate to 25% for 90 s; cascade to PAY.RMT, ADM.RPT, ADM.FWA",
        steps=(ScenarioStep(kind="rate", key="CLM.STR", rate=0.25, ramp_s=5.0, hold_s=90.0),),
        duration_s=180.0,
    ),
    "slow_degradation": Scenario(
        name="slow_degradation",
        description="ELG.MBR error rate ramps to 8% over 10 minutes; tests CUSUM and slow windows",
        steps=(ScenarioStep(kind="rate", key="ELG.MBR", rate=0.08, ramp_s=600.0, hold_s=300.0),),
        duration_s=900.0,
    ),
    "bad_rule_deploy": Scenario(
        name="bad_rule_deploy",
        description="EDT denial mix shifts W4103 to 55%; error rate stays flat",
        steps=(
            ScenarioStep(
                kind="mix",
                key="CLM.EDT",
                code_weights={"W4101": 0.15, "W4102": 0.10, "W4103": 0.55, "W4104": 0.15, "W4105": 0.05},
                hold_s=300.0,
            ),
        ),
        duration_s=420.0,
    ),
    "new_error_after_deploy": Scenario(
        name="new_error_after_deploy",
        description="PRV.CRD emits an unseen code E3299; exercises the new-code detector",
        steps=(ScenarioStep(kind="mix", key="PRV.CRD", code_weights={"E3299": 1.0}, hold_s=300.0),),
        duration_s=420.0,
    ),
    "upstream_cascade": Scenario(
        name="upstream_cascade",
        description="ELG.MBR fails and downstream components follow; tests incident grouping",
        steps=(ScenarioStep(kind="rate", key="ELG.MBR", rate=0.20, ramp_s=2.0, hold_s=120.0),),
        duration_s=200.0,
    ),
    "service_silent": Scenario(
        name="service_silent",
        description="PAY.BNK stops logging; only a silence detector can see this",
        steps=(ScenarioStep(kind="silence", key="PAY.BNK", hold_s=120.0),),
        duration_s=200.0,
    ),
    "nightly_batch_surge": Scenario(
        name="nightly_batch_surge",
        description="PAY.EXP volume x20 for 60 s at a normal error rate; false-positive control",
        steps=(ScenarioStep(kind="volume", key="PAY.EXP", multiplier=20.0, hold_s=60.0),),
        duration_s=150.0,
    ),
    "brute_force_login": Scenario(
        name="brute_force_login",
        description="ADM.AUT W1102 bad-password burst; tests warning-level rate detection",
        steps=(ScenarioStep(kind="mix", key="ADM.AUT", code_weights={"W1102": 1.0}, hold_s=180.0),),
        warmup_s=0.0,
        duration_s=240.0,
    ),
    # The remaining two scenarios (log_format_drift, phi_leak_canary) inject raw lines
    # rather than rate changes, so they are not expressible as control calls. They belong
    # to the generator's --inject path and the eval runner.
}


def create_app(
    store: FaultStore,
    catalog: Catalog | None = None,
    platform: Platform | None = None,
) -> FastAPI:
    """Build the FastAPI app around an existing fault store.

    The store is injected, so the API can serve a live generator in-process (generator
    --api) or a standalone store in tests.
    """
    if catalog is None or platform is None:
        catalog, platform = load_all()
    app = FastAPI(title="Sentinel control API", version="1.0")
    app.state.store = store
    app.state.catalog = catalog
    app.state.platform = platform

    def require_key(key: str) -> None:
        if not store.known_key(key):
            raise HTTPException(status_code=404, detail=f"unknown component {key}")

    def require_token(x_token: str | None) -> None:
        if not store.authorize(x_token):
            raise HTTPException(status_code=401, detail="bad or missing token")

    @app.get("/rates")
    async def get_rates(x_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Configured rate, the live effective rate, and any active override, per component."""
        require_token(x_token)
        # The store's own time base, which is the virtual clock when the store is the
        # generator's, so the console reads the same "now" the log is being written at.
        now = store.now_ms()
        out: dict[str, Any] = {}
        for code, cfg in platform.components.items():
            key = f"{cfg.service}.{code}"
            out[key] = {
                "service": cfg.service,
                "component": code,
                "name": catalog.components[code].name,
                "rps": cfg.rps,
                "base_err": cfg.err,
                # None when no override is live, i.e. the generator is drawing the
                # configured rate modified only by the daily curve and any propagation.
                "effective_err": store.effective_rate(key, now),
                "volume_multiplier": store.effective_volume(key, now),
                "silent": store.is_silent(key, now),
                "active_faults": [
                    w for w in store.active_summary(now) if w["key"] == key
                ],
            }
        return {"at_ms": now, "components": out}

    @app.put("/rates/{svc}/{cmp}")
    async def put_rate(
        svc: str,
        cmp: str,
        body: RateChange,
        x_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_token)
        key = f"{svc}.{cmp}"
        require_key(key)
        window = await store.set_rate(
            key,
            body.rate,
            ramp_s=body.ramp_s or 0.0,
            hold_s=body.hold_s,
            code=body.scenario or "manual",
        )
        return {"ok": True, "key": key, "window": window.to_json()}

    @app.put("/mix/{svc}/{cmp}")
    async def put_mix(
        svc: str,
        cmp: str,
        body: MixChange,
        x_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_token)
        key = f"{svc}.{cmp}"
        require_key(key)
        if not body.code_weights:
            raise HTTPException(status_code=422, detail="code_weights must not be empty")
        if any(weight < 0.0 for weight in body.code_weights.values()):
            raise HTTPException(status_code=422, detail="code weights must be non-negative")
        # Codes outside the catalog are allowed on purpose: new_error_after_deploy injects
        # E3299, which has to reach the detector as an unmapped code for the new-code
        # detector to see it (section 6.3).
        window = await store.set_code_weights(
            key,
            body.code_weights,
            hold_s=body.hold_s,
            code=body.scenario or "manual",
        )
        return {"ok": True, "key": key, "window": window.to_json()}

    @app.post("/silence/{svc}/{cmp}")
    async def post_silence(
        svc: str,
        cmp: str,
        body: SilenceRequest,
        x_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_token)
        key = f"{svc}.{cmp}"
        require_key(key)
        window = await store.silence(key, hold_s=body.hold_s, code=body.scenario or "manual")
        return {"ok": True, "key": key, "window": window.to_json()}

    @app.put("/volume/{svc}/{cmp}")
    async def put_volume(
        svc: str,
        cmp: str,
        body: VolumeChange,
        x_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_token)
        key = f"{svc}.{cmp}"
        require_key(key)
        window = await store.set_volume(
            key,
            body.multiplier,
            hold_s=body.hold_s,
            code=body.scenario or "manual",
        )
        return {"ok": True, "key": key, "window": window.to_json()}

    @app.get("/scenarios")
    async def get_scenarios(x_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_token)
        return {
            "scenarios": [
                {
                    "name": s.name,
                    "description": s.description,
                    "duration_s": s.duration_s,
                    "steps": [_step_json(step) for step in s.steps],
                }
                for s in SCENARIOS.values()
            ]
        }

    @app.post("/scenarios/{name}/run")
    async def post_scenario(
        name: str,
        x_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_token)
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise HTTPException(status_code=404, detail=f"unknown scenario {name}")
        for step in scenario.steps:
            if step.kind == "rate":
                await store.set_rate(
                    step.key,
                    step.rate or 0.0,
                    ramp_s=step.ramp_s or 0.0,
                    hold_s=step.hold_s,
                    code=name,
                )
            elif step.kind == "mix":
                await store.set_code_weights(
                    step.key, step.code_weights or {}, hold_s=step.hold_s, code=name
                )
            elif step.kind == "silence":
                await store.silence(step.key, hold_s=step.hold_s or 60.0, code=name)
            elif step.kind == "volume":
                await store.set_volume(
                    step.key, step.multiplier or 1.0, hold_s=step.hold_s, code=name
                )
            else:
                raise HTTPException(
                    status_code=400, detail=f"scenario step kind {step.kind} is not implemented"
                )
        return {
            "ok": True,
            "scenario": scenario.name,
            "duration_s": scenario.duration_s,
            "steps": len(scenario.steps),
        }

    @app.post("/reset")
    async def post_reset(x_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_token)
        await store.reset()
        return {"ok": True, "reset": True}

    @app.get("/truth")
    async def get_truth(x_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_token)
        records = store.truth_records()
        return {"count": len(records), "events": records[-500:]}

    return app


def _step_json(step: ScenarioStep) -> dict[str, Any]:
    return {
        "kind": step.kind,
        "key": step.key,
        "rate": step.rate,
        "ramp_s": step.ramp_s,
        "hold_s": step.hold_s,
        "multiplier": step.multiplier,
        "code_weights": step.code_weights,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=LOCALHOST, help="bind address; keep this on localhost")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None, help="require this X-Token header")
    parser.add_argument("--log", default="platform.log", help="log file the generator writes to")
    parser.add_argument("--truth", default=str(DEFAULT_TRUTH))
    parser.add_argument("--seconds", type=float, default=None, help="stop the platform after N event seconds")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--rps-scale", type=float, default=1.0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    """Start the platform and the control API in one process, sharing one fault store.

    The store has to live in the same process as the generator: rate changes reach the log
    only by changing what the generator draws, and nothing about them is ever written to
    platform.log.
    """
    options = GeneratorOptions(
        log_path=Path(args.log),
        truth_path=Path(args.truth) if args.truth else None,
        seed=args.seed,
        speed=args.speed,
        rps_scale=args.rps_scale,
        duration_s=args.seconds,
        serve_api=True,
        api_port=args.port,
        api_token=args.token,
    )
    generator = Generator(options)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, generator.stop)
    print(f"platform -> {args.log}, control API on http://{LOCALHOST}:{args.port}")
    await generator.run_live()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"refusing to bind {args.host}: the control API is localhost only (section 6.2). "
            "Pass --token as well if you really mean it."
        )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally stops us first
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
