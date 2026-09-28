"""Control API and fault layer (docs/sentinel-plan.md section 6.2).

Endpoints under test, exactly as the plan lists them:

    GET  /rates
    PUT  /rates/{svc}/{cmp}        {rate, ramp_s?, hold_s?}
    PUT  /mix/{svc}/{cmp}          {code_weights, hold_s?}
    POST /silence/{svc}/{cmp}      {hold_s}
    POST /scenarios/{name}/run
    POST /reset
    GET  /truth

The two rules that matter most are checked directly: a control action appends to
ground_truth.jsonl, and no control action produces a log line.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sim.config import load_all
from sim.control_api import SCENARIOS, LOCALHOST, create_app
from sim.faults import FaultStore


@pytest.fixture
def store(tmp_path: Path):
    catalog, platform = load_all()
    return FaultStore(
        catalog, platform, truth_path=tmp_path / "ground_truth.jsonl", token=None
    )


@pytest.fixture
def client(store):
    app = create_app(store)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def truth_path(tmp_path: Path) -> Path:
    return tmp_path / "ground_truth.jsonl"


# --- GET /rates -------------------------------------------------------------


def test_rates_lists_every_component(client) -> None:
    body = client.get("/rates").json()
    assert set(body["components"]) == {
        "PAY.RMT", "PAY.EXP", "PAY.LDG", "PAY.BNK",
        "CLM.EDT", "CLM.PRC", "CLM.DUP", "CLM.STR",
        "ELG.X12", "ELG.MBR", "ELG.CCH", "ELG.CVR",
        "PRV.INT", "PRV.CRD", "PRV.NPI", "PRV.PST",
        "ADM.AUT", "ADM.AUD", "ADM.RPT", "ADM.FWA",
    }
    assert body["components"]["CLM.STR"]["base_err"] == 0.002
    assert body["components"]["CLM.STR"]["rps"] == 300
    assert body["components"]["CLM.STR"]["silent"] is False
    assert body["components"]["CLM.STR"]["volume_multiplier"] == 1.0


def test_rates_shows_the_effective_rate_after_a_change(client) -> None:
    client.put("/rates/CLM/STR", json={"rate": 0.25, "hold_s": 60})
    entry = client.get("/rates").json()["components"]["CLM.STR"]
    assert entry["effective_err"] == 0.25
    assert entry["active_faults"], "the console needs to know a fault is live"


# --- PUT /rates/{svc}/{cmp} -------------------------------------------------


def test_put_rate_sets_the_rate(client) -> None:
    response = client.put("/rates/CLM/STR", json={"rate": 0.25, "hold_s": 60})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["key"] == "CLM.STR"
    assert body["window"]["target"]["rate"] == 0.25
    assert body["window"]["hold_s"] == 60


def test_put_rate_accepts_a_ramp(client) -> None:
    body = client.put("/rates/ELG/MBR", json={"rate": 0.08, "ramp_s": 600, "hold_s": 300}).json()
    assert body["window"]["ramp_s"] == 600


@pytest.mark.parametrize(
    "payload",
    [
        {"rate": -0.1},
        {"rate": 1.5},
        {"rate": 2.0},
        {"ramp_s": -5, "rate": 0.1},
        {"hold_s": 0, "rate": 0.1},
        {"hold_s": -10, "rate": 0.1},
        {},
    ],
)
def test_put_rate_rejects_impossible_values(client, payload) -> None:
    assert client.put("/rates/CLM/STR", json=payload).status_code == 422


def test_put_rate_on_an_unknown_component_is_404(client) -> None:
    assert client.put("/rates/CLM/ZZZ", json={"rate": 0.1}).status_code == 404
    assert client.put("/rates/ZZZ/STR", json={"rate": 0.1}).status_code == 404


def test_a_rate_change_on_a_leaf_component_works(client) -> None:
    """CLM.STR has no upstream, so this is a fault at the origin."""
    assert client.put("/rates/CLM/STR", json={"rate": 0.2}).status_code == 200


# --- PUT /mix/{svc}/{cmp} ---------------------------------------------------


def test_put_mix_shifts_the_code_weights(client) -> None:
    weights = {"W4101": 0.15, "W4102": 0.10, "W4103": 0.55, "W4104": 0.15, "W4105": 0.05}
    body = client.put("/mix/CLM/EDT", json={"code_weights": weights, "hold_s": 300}).json()
    assert body["window"]["target"]["code_weights"] == weights
    assert body["window"]["hold_s"] == 300


def test_put_mix_accepts_a_code_outside_the_catalog(client, catalog) -> None:
    """new_error_after_deploy injects E3299, which the catalog does not have. Rejecting it
    here would make the new-code detector untestable."""
    assert "E3299" not in catalog.codes
    body = client.put("/mix/PRV/CRD", json={"code_weights": {"E3299": 1.0}}).json()
    assert body["ok"] is True


def test_put_mix_rejects_an_empty_map(client) -> None:
    assert client.put("/mix/CLM/EDT", json={"code_weights": {}}).status_code == 422


def test_put_mix_rejects_negative_weights(client) -> None:
    response = client.put("/mix/CLM/EDT", json={"code_weights": {"W4103": -1.0}})
    assert response.status_code in (400, 422)


# --- POST /silence/{svc}/{cmp} ----------------------------------------------


def test_silence_stops_a_component(client, store) -> None:
    body = client.post("/silence/PAY/BNK", json={"hold_s": 60}).json()
    assert body["ok"] is True
    assert store.is_silent("PAY.BNK", store._now_ms())
    entry = client.get("/rates").json()["components"]["PAY.BNK"]
    assert entry["silent"] is True


def test_silence_requires_a_positive_hold(client) -> None:
    assert client.post("/silence/PAY/BNK", json={"hold_s": 0}).status_code == 422
    assert client.post("/silence/PAY/BNK", json={}).status_code == 422


def test_silence_on_an_unknown_component_is_404(client) -> None:
    assert client.post("/silence/PAY/ZZZ", json={"hold_s": 60}).status_code == 404


# --- POST /reset ------------------------------------------------------------


def test_reset_clears_every_fault(client) -> None:
    client.put("/rates/CLM/STR", json={"rate": 0.25, "hold_s": 600})
    client.post("/silence/PAY/BNK", json={"hold_s": 600})
    body = client.post("/reset").json()
    assert body["ok"] is True

    rates = client.get("/rates").json()["components"]
    assert rates["CLM.STR"]["effective_err"] is None
    assert rates["CLM.STR"]["active_faults"] == []
    assert rates["PAY.BNK"]["silent"] is False


# --- GET /truth -------------------------------------------------------------


def test_truth_returns_the_control_history(client) -> None:
    client.put("/rates/CLM/STR", json={"rate": 0.25, "hold_s": 60})
    client.post("/reset")
    body = client.get("/truth").json()
    assert body["count"] == 2
    events = [record["event"] for record in body["events"]]
    assert events == ["rate_change", "reset"]
    assert body["events"][0]["target"] == "CLM.STR"
    assert body["events"][0]["detail"]["target"]["rate"] == 0.25
    assert body["events"][0]["at_ms"] > 0


def test_every_control_action_is_recorded(client, truth_path: Path) -> None:
    client.put("/rates/CLM/STR", json={"rate": 0.25, "ramp_s": 5, "hold_s": 90})
    client.put("/mix/CLM/EDT", json={"code_weights": {"W4103": 0.55}, "hold_s": 60})
    client.post("/silence/PAY/BNK", json={"hold_s": 60})
    client.put("/volume/PAY/EXP", json={"multiplier": 20, "hold_s": 60})
    client.post("/reset")

    records = [json.loads(line) for line in truth_path.read_text().splitlines()]
    assert [r["event"] for r in records] == [
        "rate_change",
        "mix_change",
        "silence",
        "volume_change",
        "reset",
    ]
    assert [r["target"] for r in records] == ["CLM.STR", "CLM.EDT", "PAY.BNK", "PAY.EXP", "*"]
    assert [r["seq"] for r in records] == [1, 2, 3, 4, 5]
    for record in records:
        assert set(record) == {"seq", "event", "target", "at_ms", "detail"}
        assert isinstance(record["at_ms"], int)


def test_a_scenario_run_is_recorded_with_every_step(client, truth_path: Path) -> None:
    client.post("/scenarios/db_timeout_spike/run")
    records = [json.loads(line) for line in truth_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["event"] == "rate_change"
    assert records[0]["target"] == "CLM.STR"
    assert records[0]["detail"]["code"] == "db_timeout_spike"
    assert records[0]["detail"]["target"]["rate"] == 0.25
    assert records[0]["detail"]["ramp_s"] == 5.0
    assert records[0]["detail"]["hold_s"] == 90.0


# --- POST /scenarios/{name}/run --------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_runs(client, name: str) -> None:
    body = client.post(f"/scenarios/{name}/run").json()
    assert body["ok"] is True
    assert body["scenario"] == name
    assert body["steps"] >= 1


def test_db_timeout_spike_is_what_the_plan_describes(client) -> None:
    """CLM.STR 0.2% to 25% for 90 s, testing the cascade to PAY.RMT, ADM.RPT and ADM.FWA."""
    scenario = SCENARIOS["db_timeout_spike"]
    assert scenario.steps[0].key == "CLM.STR"
    assert scenario.steps[0].rate == 0.25
    assert scenario.steps[0].hold_s == 90.0


def test_slow_degradation_ramps_over_ten_minutes(client) -> None:
    step = SCENARIOS["slow_degradation"].steps[0]
    assert step.key == "ELG.MBR"
    assert step.rate == 0.08
    assert step.ramp_s == 600.0


def test_bad_rule_deploy_shifts_w4103_to_55_percent(client) -> None:
    step = SCENARIOS["bad_rule_deploy"].steps[0]
    weights = step.code_weights or {}
    assert step.key == "CLM.EDT"
    assert weights["W4103"] == 0.55
    assert sum(weights.values()) == pytest.approx(1.0)


def test_nightly_batch_surge_scales_volume_not_rate(client) -> None:
    step = SCENARIOS["nightly_batch_surge"].steps[0]
    assert step.kind == "volume"
    assert step.key == "PAY.EXP"
    assert step.multiplier == 20.0
    assert step.rate is None, "a volume surge must not touch the error rate"


def test_service_silent_silences_pay_bnk(client) -> None:
    step = SCENARIOS["service_silent"].steps[0]
    assert step.kind == "silence"
    assert step.key == "PAY.BNK"


def test_brute_force_login_targets_the_bad_password_code(client) -> None:
    step = SCENARIOS["brute_force_login"].steps[0]
    assert step.key == "ADM.AUT"
    assert step.code_weights == {"W1102": 1.0}


def test_an_unknown_scenario_is_404(client) -> None:
    assert client.post("/scenarios/not_a_scenario/run").status_code == 404


def test_scenarios_are_listed(client) -> None:
    body = client.get("/scenarios").json()
    names = {s["name"] for s in body["scenarios"]}
    assert names == set(SCENARIOS)
    for scenario in body["scenarios"]:
        assert scenario["description"]
        assert scenario["duration_s"] > 0


# --- localhost only ---------------------------------------------------------


def test_the_control_api_refuses_a_non_localhost_bind() -> None:
    """Section 6.2: localhost only. A token is not a substitute for the bind address."""
    from sim.control_api import main

    for host in ("0.0.0.0", "192.168.1.10", "::"):
        with pytest.raises(SystemExit, match="localhost only"):
            main(["--host", host])


def test_a_token_is_enforced_when_set(tmp_path: Path) -> None:
    catalog, platform = load_all()
    store = FaultStore(
        catalog, platform, truth_path=tmp_path / "gt.jsonl", token="s3cret"
    )
    with TestClient(create_app(store)) as client:
        assert client.get("/rates").status_code == 401
        assert client.get("/rates", headers={"X-Token": "wrong"}).status_code == 401
        assert client.get("/rates", headers={"X-Token": "s3cret"}).status_code == 200
        assert client.post("/reset", headers={"X-Token": "s3cret"}).status_code == 200


def test_no_token_means_open(tmp_path: Path) -> None:
    catalog, platform = load_all()
    store = FaultStore(catalog, platform, truth_path=tmp_path / "gt.jsonl", token=None)
    assert store.authorize(None)
    assert store.authorize("anything")


def test_the_default_bind_is_localhost() -> None:
    assert LOCALHOST == "127.0.0.1"


# --- no control action touches the log --------------------------------------


def test_no_control_action_produces_a_log_line(tmp_path: Path, client) -> None:
    """Section 6.1: rate changes are never written to the log. Every control endpoint is
    called, and the generator's output is compared before and after."""
    from tests.conftest import install_collector, make_generator, settle

    generator = make_generator(tmp_path, seed=1)
    sink = install_collector(generator)
    settle(generator, 2)
    before = list(sink)

    store = generator.store
    now = generator.clock.now_ms()
    asyncio.run(store.set_rate("CLM.STR", 0.25, ramp_s=5.0, hold_s=90.0, at_ms=now))
    asyncio.run(store.set_code_weights("CLM.EDT", {"W4103": 0.55}, hold_s=60, at_ms=now))
    asyncio.run(store.silence("PAY.BNK", hold_s=60, at_ms=now))
    asyncio.run(store.set_volume("PAY.EXP", 20.0, hold_s=60, at_ms=now))
    asyncio.run(store.reset())

    sink.clear()
    settle(generator, 2)
    after = list(sink)

    assert before and after
    # A fault changes what is drawn. What it must never do is add a line about itself.
    for raw in after:
        message = raw[46:].decode("ascii")
        for word in ("FAULT", "SCENARIO", "INJECTED", "RATE_CHANGED", "hold_s", "ramp_s"):
            assert word not in message, (word, raw)
    # Every line is still a catalogued message at the right width.
    assert all(len(raw) >= 46 for raw in after)
    assert all(raw[13] == raw[15] == raw[19] == raw[23] == raw[29] == raw[36] == raw[45]
               for raw in after)


def test_the_detector_never_imports_simulator_code() -> None:
    """Section 3 key rule: the detector reads platform.log and nothing else.

    Checked over the AST rather than the raw text, so a module docstring that explains the
    rule does not trip it while a real ``from sim.x import y`` does. catalog.yaml is loaded
    as a data file, which is allowed.
    """
    import ast

    detector_dir = Path(__file__).resolve().parent.parent / "detector"
    sources = sorted(detector_dir.glob("*.py"))
    assert sources, "no detector sources found"
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("sim"), f"{source.name}: imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("sim"), f"{source.name}: imports from {module}"


def test_the_detector_never_opens_the_oracle_file() -> None:
    """No detector module may read ground_truth.jsonl.

    Docstrings are excluded: a module that documents why it must not read the oracle file
    is doing the right thing, and a string that is actually used to open a file is not.
    """
    import ast

    detector_dir = Path(__file__).resolve().parent.parent / "detector"
    for source in sorted(detector_dir.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.value in docstrings:
                continue
            assert "ground_truth" not in node.value, (
                f"{source.name}:{node.lineno} refers to the oracle file"
            )
