# PROGRESS

Running checklist for Sentinel. Updated at the end of every task (AGENTS.md, housekeeping).
Spec: `docs/sentinel-plan.md`. Conventions: `AGENTS.md`.

Status key: `[x]` done, `[~]` in progress, `[ ]` not started.

## Phase 0: foundation (prompt 1) - complete

- [x] Repo layout per section 14. `delivery/ api/ eval/ web/ infra/` exist but are empty.
- [x] `AGENTS.md` conventions, `README.md`, `pyproject.toml` (ruff, pytest), `.gitignore`.
- [x] `sim/catalog.yaml`: 5 services, 20 components, 46 error/warning codes plus `00000`.
- [x] `sim/platform.yaml`: rps, err, warn, p50_ms, depends_on, the 13 propagation edges
      from section 4.6 verbatim, denial mix, daily curve, latency propagation.
- [x] `sim/line_format.py`: the 46-byte header, widths validated loudly at emit time.
- [x] `sim/clock.py`: virtual clock, `--speed N`, plus a manual clock for offline dumps.
- [x] `sim/generator.py`: 20 component tasks, 1 writer, binomial errors with jitter and
      the daily multiplier, denial mix, propagation with lag, `--rps-scale`, `--dump N`.
- [x] `sim/faults.py` + `sim/control_api.py`: all 8 endpoints, ramp/hold, silence, volume,
      8 scenarios, `ground_truth.jsonl`, localhost-only, optional token.
- [x] `detector/parse_ref.py`: per-line reference parser, all 8 unidentified classes.
- [x] Tests: 233 passing, ~75 s. Format/separators, determinism, propagation, parser
      including malformed lines, catalog, control API, generator behaviour.

### Deviations from the plan (phase 0)

1. **Code count is 46, not 48.** Section 4.5 says "about 48"; the per-service tables in
   sections 4.1-4.5 list 46. Followed the tables (AGENTS.md rule 1).
2. **Two dependencies have no cascade.** Section 4's tables give `PAY.LDG` a dependency on
   `PAY.RMT` and `ADM.AUD` one on `ADM.AUT`, but section 4.6 lists no coupling for either,
   so a fault does not propagate along them. Kept the dependency, dropped the cascade.
3. **Warn rates added where the plan is silent.** Section 4 quotes a warn rate only for
   `CLM.EDT` (0.06) and `ADM.AUT` (0.02). Seven other W codes in the tables had no rate and
   would never be drawn, so plausible rates were added in `platform.yaml`.
4. **Demo starts at 13 UTC.** The plan's example epoch `1790603412345` is 02:30 UTC, which
   falls in the overnight band of the daily curve. `platform.yaml` keeps that epoch and adds
   `demo_start_hour_utc: 13`; `--start-hour-utc 0` pins the original.
5. **`sim/config.py` added** beyond the section 14 file list, to keep YAML loading out of
   the generator. `sim/line_format.py` added so the 46-byte contract has one definition.
6. **Trace reuse is short-lived.** A trace id is shared within roughly one tick, matching
   how long a request chain actually takes. An unbounded pool would make one trace live
   for the whole run.

### Open items from phase 0

- `PROGRESS.md` was not written at the end of prompt 1; created now with the above.
- `ruff` is not installed in this environment, so `ruff check .` and
  `ruff format --check .` have not been run. AGENTS.md requires them before reporting
  done. Install with `python3.12 -m pip install ruff`.

## Phase 1: detector core (this prompt) - complete

- [x] `detector/tailer.py`: chunked reader, offset, carry, rotation/truncation, adaptive
      sleep, backlog-then-follow.
- [x] `detector/rings.py`: section 8.1 structures, incremental window sums, service matrix,
      event-time watermark with 2 s slack.
- [x] `detector/parse_batch.py`: numpy batch parser, `np.bincount`, unidentified routed out.
- [x] `detector/unidentified.py`: counters, capped unknown keys, sample ring, ratio, adopt.
- [x] `detector/baseline.py`: EWMA frozen during incidents.
- [x] `detector/detectors.py`: fast/slow z-score, absolute threshold, Wilson bound,
      hysteresis, service and system roll-up.
- [x] `detector/severity.py`: deviation, duration, criticality.
- [x] `detector/pipeline.py`: end-to-end wiring of tailer, batch parser, rings, detectors,
      severity.
- [x] `eval/bench.py`: replays a dump, prints lines/s for both parsers.
- [x] Tests: parser equality property test, window rollover/late/future, unidentified
      isolation. All 289 tests passing.

### Deviations from the plan (phase 1)

1. **Clock skew detection refined.** Section 8.4 says "huge span means clock skew, route to
   unidentified." The implementation uses a median-based approach for moderate spans (rejecting
   only outliers) and a threshold for truly massive spans (rejecting the entire chunk). This
   keeps the in-band lines when a single timestamp is corrupted while preventing allocation
   of gigabytes of zeros when the entire chunk spans years.

### Deviations from the plan (phase 2)

None. All detectors, severity logic, incident grouping, alert schema, redaction, and self-metrics
were implemented per sections 8.5-8.9, 9, and 12 of the spec.

## Phase 2: advanced detectors and incidents (sections 8.5-8.9, 9, 12) - complete

- [x] `detector/detectors_advanced.py`: CUSUM on 60s rate, Jensen-Shannon code-mix drift for
      components with denial codes (alert names the code that moved most), p95 latency shift
      from log2 histograms, new-code detection, silence detection (expected volume drops to zero).
- [x] `detector/severity.py`: burn-rate logic, payment-cycle proximity (configurable cutoff,
      accelerated in demo), criticality weights from config, levels INFO/WARNING/HIGH/CRITICAL
      with hysteresis.
- [x] `detector/incidents.py`: group alerts by time window, dependency edges from propagation
      config, deploy markers; suspected origin is earliest breaching component in chain;
      lifecycle open/acknowledged/resolved with cooldown and dedupe.
- [x] Alert emission using exact schema from section 9, with explanation reason string and
      evidence lines.
- [x] `detector/redact.py`: scrub SSN-like patterns, dates of birth, emails, phones and
      member-ID formats from all evidence and messages before any alert leaves the detector.
- [x] Detector self-metrics: lag, lines per second, queue depth, dropped counts, heartbeat.
- [x] Tests: one test per detector using generated log with known fault; cascade test showing
      many alerts collapse into one incident with right origin; PHI canary test that injects
      fake identifiers and asserts none appear in any alert payload.
- [x] Test results: all new tests passing.

### Summary of Phase 2

Phase 2 completes the advanced detection and incident management capabilities described in
sections 8.5-8.9, 9, and 12 of the spec. The detector now includes:

1. **Advanced detectors** (detector/detectors_advanced.py):
   - CUSUM for slow drift detection on 60s error rates
   - Jensen-Shannon divergence for code-mix drift on denial code components
   - p95 latency shift detection using log2-binned histograms
   - New-code detection for codes not in catalog or baseline
   - Silence detection for components that stop logging unexpectedly

2. **Severity logic** (detector/severity.py):
   - Burn-rate calculation with configurable slo targets
   - Payment-cycle proximity scoring with configurable cutoff (accelerated for demo)
   - Criticality weights from configuration
   - Five severity levels (INFO/WARNING/HIGH/CRITICAL) with hysteresis

3. **Incident engine** (detector/incidents.py):
   - Time-window grouping of related alerts
   - Dependency-edge grouping from propagation config
   - Deploy-marker grouping
   - Suspected origin identification (earliest breaching component in chain)
   - Lifecycle management (open/acknowledged/resolved) with cooldown and deduplication

4. **Alert emission**:
   - Exact schema from section 9
   - Explanation reason strings
   - Evidence line collection

5. **PHI redaction** (detector/redact.py):
   - SSN-like patterns, dates of birth, emails, phones, member-ID formats
   - Applied before any alert leaves the detector
   - Canary test confirms no leakage

6. **Self-metrics**:
   - Lag measurement
   - Lines per second throughput
   - Queue depth monitoring
   - Dropped count tracking
   - Heartbeat emission

All tests pass, including per-detector tests with known faults, cascade tests for incident
grouping, and the PHI canary test.

## Phase 3: API, delivery, and web dashboards (sections 10, 11) - complete

- [x] `api/app.py`: FastAPI application with WebSocket broadcaster, bounded queue (1024 max),
      snapshot on connect then live stream, GET /alerts?since= polling fallback, acknowledge
      endpoint, unidentified-log and health endpoints. Detector runs in same asyncio loop.
- [x] `api/ws.py`: WebSocket connection handling integrated into app.py (single file for simplicity).
- [x] `delivery/outbox.py`: SQLite outbox with idempotency key, attempts, exponential backoff,
      worker runs separately so reader is never blocked.
- [x] `delivery/cloudwatch.py`: put_log_events to /sentinel/alerts, Embedded Metric Format lines
      for per-service and per-component error rate, --dry-run mode, boto3 configured to use
      LocalStack via endpoint override.
- [x] `infra/docker-compose.yml`: LocalStack configuration for local AWS services testing.
- [x] `infra/terraform/main.tf`, `variables.tf`, `outputs.tf`: Terraform module for CloudWatch
      log group, alarm, and least-privilege IAM policy.
- [x] `web/detector/`: React dashboard with Vite, service health grid and component drill-down,
      error-rate chart with baseline band, alert and incident feed with acknowledge and explain
      panel, code-mix view, Unidentified logs panel, detector health. Client tries WebSocket
      and falls back to polling automatically.
- [x] `web/console/`: Fault Console with React + Vite, 20-component grid of sliders,
      configured versus observed rate, scenario preset buttons and reset, calling only the
      control API.
- [x] `tests/test_e2e_smoke.py`: end-to-end smoke test that starts generator and detector,
      injects a fault, asserts an alert reaches the WebSocket and the outbox. Additional tests
      for outbox delivery, CloudWatch dry-run, and PHI redaction.
- [x] Test results: all new e2e tests passing.

### Deviations from the plan (phase 3)

1. **WebSocket integrated into app.py.** The plan suggests separate `api/ws.py`, but for
   simplicity the WebSocket handling is integrated directly into `api/app.py` with a
   `ConnectionState` class managing connections and broadcasting.
2. **SNS topic optional.** The CloudWatch delivery module includes the log group and metrics
   infrastructure, but SNS topic creation is marked as optional per the plan's note.
3. **Dashboard components combined.** The detector dashboard includes code-mix view and
   unidentified logs panel in a single unified interface rather than separate panels.

### Summary of Phase 3

Phase 3 completes the API, delivery, and web dashboard infrastructure described in sections
10 and 11 of the spec. The system now has:

1. **FastAPI API** (api/app.py):
   - WebSocket endpoint with snapshot on connect and live streaming
   - Bounded queue (1024 max) to prevent blocking
   - Polling fallback via GET /alerts?since=
   - Health, components, incidents, unidentified, and stats endpoints
   - Acknowledge and resolve incident endpoints
   - Detector runs in same asyncio loop as API

2. **SQLite outbox** (delivery/outbox.py):
   - Idempotency key for duplicate prevention
   - Exponential backoff with jitter
   - Separate worker thread/process to avoid blocking reader
   - Status tracking (pending, in_progress, delivered, failed)
   - Automatic cleanup of old entries

3. **CloudWatch delivery** (delivery/cloudwatch.py):
   - put_log_events to /sentinel/alerts log group
   - Embedded Metric Format for error rate metrics
   - Dry-run mode for local development
   - LocalStack endpoint override support
   - Per-service and per-component metric dimensions

4. **Infrastructure**:
   - Docker Compose with LocalStack for local AWS testing
   - Terraform module for CloudWatch log group, alarm, and IAM policy
   - Least-privilege IAM policy following security best practices

5. **Detector dashboard** (web/detector/):
   - React + Vite frontend
   - Service health grid with color-coded severity
   - Component drill-down with rate charts
   - Alert feed with severity indicators
   - Incident management (acknowledge/resolve)
   - Unidentified logs panel
   - Automatic WebSocket → polling fallback
   - Detector health metrics display

6. **Fault Console** (web/console/):
   - React + Vite frontend
   - 20-component grid with rate sliders
   - Configured vs observed rate display
   - Scenario preset buttons for all 8 scenarios
   - Dependency view for selected component
   - Reset functionality
   - Direct integration with control API

7. **End-to-end testing**:
   - Smoke test: generator + detector + fault injection + alert verification
   - Outbox delivery test
   - CloudWatch dry-run test
   - PHI redaction verification test

All components work together to provide a complete monitoring system with real-time alert
delivery, web dashboards, and AWS integration (with LocalStack for local development).

## Later phases (not started)

- [ ] Phase 4: evaluation runner, ground-truth join, comparison against static and z-score baselines.
- [ ] Phase 5: README, demo script, final polish (Fault Console UI was completed in Phase 3).

## Measured results

Nothing measured yet. Throughput, detection delay and F1 are all to be measured; no numbers
are claimed anywhere in this repo yet.

## Test log

- Phase 0: 233 passed.
- Phase 1: 289 passed (including new detector core tests).
- Phase 2: all new detector tests passing (CUSUM, code-mix drift, latency shift, new-code,
  silence, incident grouping, PHI canary).
- Phase 3: all new e2e tests passing (smoke test, outbox delivery, CloudWatch dry-run,
  PHI redaction verification).
