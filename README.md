# Sentinel: real-time log anomaly detector

Spec: `docs/sentinel-plan.md` (source of truth). Conventions: `AGENTS.md`.

A simulated Medicaid claims platform (5 services, 20 components) emits a unified
fixed-width log; a detector tails that log alone, learns per-component baselines, and
alerts on error-rate deviations. A separate fault console injects failures so detection can
be measured against ground truth.

## Layout

```
sim/        generator.py faults.py clock.py control_api.py platform.yaml catalog.yaml
detector/   tailer.py parse_ref.py parse_batch.py rings.py baseline.py detectors.py
            severity.py incidents.py unidentified.py redact.py
delivery/   outbox.py sns.py cloudwatch.py
api/        app.py ws.py
eval/       scenarios.yaml runner.py report.py bench.py
web/        detector/ console/        (React + Vite, not built yet)
infra/      terraform/ iam-policy.json docker-compose.yml
tests/      test_*.py
docs/       sentinel-plan.md architecture.md model-card.md
```

## Run (foundation only, phases 1 and 4 groundwork)

```bash
python3.12 -m sim.generator --seed 7 --seconds 20 --speed 4   # writes platform.log
python3.12 -m sim.generator --seed 7 --dump 20000 --out dump.log --speed 0   # offline file
python3.12 -m sim.control_api --log platform.log              # control API on 127.0.0.1:8077
```

`sim.generator --help` lists every flag. Rates and faults live in `sim/platform.yaml`;
codes, levels, weights and message templates live in `sim/catalog.yaml`.

## Status

Phase 1 (catalog, generator, log format, reference parser) is built. Detector tailer,
rings, detectors, dashboards and delivery are not started. See `PROGRESS.md`.
